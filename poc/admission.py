#!/usr/bin/env python3
"""Admission (docs/ADMISSION.md): the ledger behind `mesh host request|waitlist|approve|reject|leases|reap|admission-status`
(option A, manual approval) and `mesh host auto|panic|bundle|admission-metrics` (phase B1, the automatic pool).

People ask for a place; the host approves them by hand, or the automatic tick (`auto --once`, run by the host's timer) approves the first
eligible one while a place is free, always inside the limits of poc/pool.yaml (slots, lease, cooldown, daily cap per identity), which
pool.py enforces. Approving issues the bundle with the existing `mesh host invite` (certificate lifetime = the lease); `reap` and the tick
first burn the join token of every player whose node registered (`mesh host burn`), then expel every lease that ended with the existing
`mesh host kick` and free its place.

The tick, in order: burn, expel what ended (always, also with auto off), the idle kick (a lease whose node has been missing, NotReady or
without a workload for `idle_kick_seconds`, as `mesh host activity` sees it, is expelled early; 0 switches it off), warn the leases that
end within `lease_warning_seconds`, and only with auto on and no panic: approve while a place is free and the first eligible request
passes the limits. A request is refused (exit 6, not listed, no event: the event log is not the spammer's) once its identity sent
`rate_max_requests` within `rate_window_seconds`. `panic` switches auto off, makes
the effective slots 0 and expels every active lease. Every fact is a JSON line in <state>/events.jsonl and, when ADMISSION_NOTIFY_CMD is set,
a call of that command with two arguments, the event type and a short summary (an argument list: no shell ever sees a name or a note).

Retention: before every write the ledger forgets what no rule needs (pool.prune) and blanks the identity and the note of every
lease and request that closed more than `identity_retention_seconds` ago (the names and times stay: a name is used once); the event log
blanks the identity of every event older than the same retention and keeps at most `events_max_lines` lines.

State: <state>/state.json (the ledger), <state>/auto.json (the switch: auto on or off, panic), <state>/events.jsonl, <state>/last-tick and
<state>/outbox/<name>.tar.gz, read and written under one exclusive lock (<state>/lock: fcntl.flock on Linux and macOS, msvcrt.locking on
Windows) and replaced atomically (temporary file + fsync + rename), so two commands at the same time never lose a request and two ticks never
grant more than the slots. Time is injectable (`--now <epoch seconds>`): the unit tests simulate a day in no time.

    admission.py --state DIR --config pool.yaml [--mesh poc/mesh] [--now N] <command> ...
      request <name> [--ip IP] [--peer IP --xff CHAIN] [--note TEXT]
      waitlist | leases | status [--json] | metrics
      approve [<name>] [--trust public|friends]
      reject <name> [--reason TEXT]
      reap [--all | --name NAME]
      auto --once | auto on | auto off | auto status
      panic [--clear]
      bundle <name>

Exit codes: 0 done; 1 the invite failed (nothing recorded), a kick failed (`reap`, the tick and `panic` record the kicks that succeeded and
keep the failed lease for the next run), the ledger file is unreadable (nothing changed) or the outbox is unsafe; 2 bad input (name, IP,
arguments); 3 unknown or already used name, a player with that name already exists, or no active lease for `bundle`; 4 nobody eligible;
5 no free place; 6 that identity is not eligible yet (cooldown, daily cap, active lease), or, with auto on, a request over the daily cap
(refused, not listed), or a request over the rate limit (refused, not listed); 7 admission is closed by `panic` (approve, request and `auto on` are refused until `panic --clear`).
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pool import Pool, client_ip, identity, load_config  # noqa: E402

# A name becomes a node name, a certificate name and a directory: nothing else gets through. fullmatch, not match:
# with match, `$` also matches before a trailing newline (a name ending in a line break).
NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")
RESERVED = {"lh", "cp", "n1", "n2"}   # the lighthouse, the control plane and the PoC's own nodes
OPEN = ("waiting", "deferred")
# Every fact the admission records, in the fixed order of the metrics (emesh_admission_events_total{type=...}).
EVENT_TYPES = ("request", "refused", "approved", "bundle_ready", "expiring", "expelled", "rejected", "kick_failed",
               "invite_failed", "auto_on", "auto_off", "panic", "panic_cleared")
AUTO_DEFAULT = {"enabled": False, "panic": False, "changed_at": 0}
NOTIFY_TIMEOUT = 10   # seconds a notify hook may take; a slow or failing hook never stops the tick
CLOSED = "admission is closed by panic (mesh host panic --clear reopens it; auto stays off until mesh host auto on)"


class AdmissionError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def check_name(name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise AdmissionError(2, f"invalid name {name!r}: 2 to 20 characters, a-z, 0-9 and '-', starting with a letter")
    if name in RESERVED:
        raise AdmissionError(2, f"invalid name {name!r}: reserved for the host's own machines")
    return name


# A join token must die before k3s may drop the node password of a name nobody took (10 minutes): at most 8 minutes, as in lib.sh.
TOKEN_TTL_MAX = 480


def token_ttl_seconds(value: str) -> int:
    """TOKEN_TTL as issue.sh reads it (90s, 5m, 8m): 1 to 8 minutes, or AdmissionError 2 before anything is changed."""
    m = re.fullmatch(r"(\d+)([smh]?)", value or "")
    seconds = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 0
    if not 60 <= seconds <= TOKEN_TTL_MAX:
        raise AdmissionError(2, f"TOKEN_TTL={value}: a join token lives 1 to 8 minutes (90s, 5m, 8m); nothing was changed")
    return seconds


def clean_text(text: str | None, limit: int = 200) -> str:
    """A note or a reason as it may reach the host's terminal: no control or format characters (ANSI escapes, newlines,
    bidi overrides), at most `limit` characters."""
    return "".join(ch for ch in (text or "") if not unicodedata.category(ch).startswith("C"))[:limit]


def iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def left(seconds: int) -> str:
    seconds = max(0, seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s"


# --- the file: lock, read, atomic write -------------------------------------------------------------------------------

try:
    import fcntl

    def _lock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock(f):
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.02)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def write_atomic(path: Path, data) -> None:
    """A JSON object (dict) or raw bytes, replaced atomically; the file is created 0600 (mkstemp): it may hold identities."""
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            if isinstance(data, (bytes, bytearray)):
                f.write(data)
            else:
                f.write((json.dumps(data, sort_keys=True, indent=1) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):   # the rename itself survives a crash only once the directory is synced (POSIX)
            dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked_state(state_dir: str | Path, after: list | None = None):
    """Yield the state dict under the lock; written back (atomically) only when the block ends without an exception. Then, still
    under the lock, every callable in `after` runs (auto.json, the event log): the order of the log is the order of the commits."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "state.json"
    with open(d / "lock", "a+") as lock:
        _lock(lock)
        try:
            try:
                state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(state, dict):
                    raise ValueError("not a JSON object")
            except (ValueError, UnicodeDecodeError) as e:
                raise AdmissionError(1, f"the ledger {path} is unreadable ({e.__class__.__name__}): restore it from your "
                                        "backup; nothing was changed") from None
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                write_atomic(path, state)
            for fn in after or ():
                fn()
        finally:
            _unlock(lock)


# --- the switch: auto on or off, panic ----------------------------------------------------------------------------------

def read_auto(state_dir: Path) -> dict:
    """<state>/auto.json; missing = auto off, no panic (the demo is switched on on purpose)."""
    path = state_dir / "auto.json"
    if not path.exists():
        return dict(AUTO_DEFAULT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(data.get(k), bool) for k in ("enabled", "panic")):
            raise ValueError("not the switch")
    except (ValueError, UnicodeDecodeError) as e:
        raise AdmissionError(1, f"the switch {path} is unreadable ({e.__class__.__name__}): fix or delete it (missing = auto off); "
                                "nothing was changed") from None
    return {**AUTO_DEFAULT, **data}


# --- events: the log and the notify hook --------------------------------------------------------------------------------

class Events:
    """Collects the facts of one command; they reach events.jsonl only when the ledger is committed (flush_events, under the
    lock) and the notify hook after the lock is released (notify). The counters live in the ledger, so they survive the
    log's rotation (Prometheus counters never go down)."""

    def __init__(self, counters: dict, now: int):
        self.counters, self.now, self.pending = counters, now, []

    def emit(self, kind: str, summary: str, name: str = "", identity: str = "", **extra) -> dict:
        if kind not in EVENT_TYPES:
            raise ValueError(f"unknown event type {kind!r}")
        self.counters["seq"] = self.counters.get("seq", 0) + 1
        count = self.counters.setdefault("count", {})
        count[kind] = count.get(kind, 0) + 1
        ev = {"seq": self.counters["seq"], "ts": self.now, "time": iso(self.now), "type": kind, "name": name,
              "identity": identity, "summary": clean_text(summary, 160), **extra}
        self.pending.append(ev)
        return ev


def flush_events(state_dir: Path, pending: list[dict], now: int, retention: int, max_lines: int) -> None:
    """Append the new events to <state>/events.jsonl, blank the identity of every event older than the retention and keep the
    last `max_lines` lines. Rewritten atomically, only when something changed."""
    path = state_dir / "events.jsonl"
    old = path.read_bytes() if path.exists() else b""
    events = []
    for line in old.decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue   # a torn line (it cannot happen with the atomic rewrite; a hand edit could): dropped
        if isinstance(ev, dict):
            events.append(ev)
    cutoff = now - retention
    for ev in events:
        if ev.get("identity") and ev.get("ts", now) <= cutoff:
            ev["identity"] = ""
    events = (events + pending)[-max(1, max_lines):]
    new = "".join(json.dumps(ev, sort_keys=True, ensure_ascii=True) + "\n" for ev in events).encode("ascii")
    if new != old:
        write_atomic(path, new)


def notify(events: list[dict], env=None) -> int:
    """Call ADMISSION_NOTIFY_CMD once per event with two arguments: the type and the short summary (plus the identity when
    ADMISSION_NOTIFY_IP=1). An argument list, never a shell: the command line is the host's own setting, split once with shlex;
    nothing a visitor typed is ever parsed. The summary holds no note, key, token or bundle content. A hook that fails, hangs or
    does not exist is reported on stderr and counted; the event is already in the log. Returns the number of failed calls."""
    env = os.environ if env is None else env
    cmd = env.get("ADMISSION_NOTIFY_CMD", "").strip()
    if not cmd or not events:
        return 0
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        print(f"notify: ADMISSION_NOTIFY_CMD cannot be split ({e}); the events are in events.jsonl", file=sys.stderr)
        return len(events)
    failed = 0
    for ev in events:
        summary = ev["summary"]
        if env.get("ADMISSION_NOTIFY_IP") == "1" and ev.get("identity"):
            summary += f" [{ev['identity']}]"
        try:
            r = subprocess.run([*argv, ev["type"], summary], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=NOTIFY_TIMEOUT)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            failed += 1
    if failed:
        print(f"notify: the hook failed for {failed} of {len(events)} event(s); they are in events.jsonl", file=sys.stderr)
    return failed


# --- the outbox: the approved bundle as one file ------------------------------------------------------------------------

def _outbox_dir(outbox: Path) -> Path:
    """The outbox, created 0700; a symbolic link (to anywhere) is refused, so nothing is ever written outside it."""
    if outbox.is_symlink():
        raise AdmissionError(1, f"the outbox {outbox} is a symbolic link: refusing to write through it")
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    if outbox.is_symlink() or not outbox.is_dir():
        raise AdmissionError(1, f"the outbox {outbox} is not a directory: refusing")
    os.chmod(outbox, 0o700)   # also when it existed already with looser modes: it holds private keys
    return outbox


def outbox_path(outbox: Path, name: str) -> Path:
    check_name(name)   # the name is the file name: [a-z][a-z0-9-], so no '/', no '..'
    return outbox / f"{name}.tar.gz"


def pack_bundle(bundles: Path, outbox: Path, name: str, mtime: int) -> tuple[Path, bool]:
    """<outbox>/<name>.tar.gz from <bundles>/<name>: regular files only (a symbolic link in the bundle is not followed), under
    the directory <name>/, mode 0600, fixed owner and times, so the same bundle always gives the same bytes. Returns (path,
    changed)."""
    src = bundles / check_name(name)
    if src.is_symlink() or not src.is_dir():
        raise AdmissionError(3, f"{name}: no bundle at {src}")
    target = outbox_path(_outbox_dir(outbox), name)
    if target.is_symlink():
        raise AdmissionError(1, f"{target} is a symbolic link: refusing")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=mtime) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            d = tarfile.TarInfo(name)
            d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o700, mtime
            tar.addfile(d)
            for f in sorted(src.iterdir(), key=lambda x: x.name):
                if f.is_symlink() or not f.is_file():
                    continue
                data = f.read_bytes()
                info = tarfile.TarInfo(f"{name}/{f.name}")
                info.size, info.mode, info.mtime = len(data), 0o600, mtime
                tar.addfile(info, io.BytesIO(data))
    data = buf.getvalue()
    if target.is_file() and target.read_bytes() == data:
        return target, False
    write_atomic(target, data)
    return target, True


def remove_outbox(outbox: Path, name: str) -> bool:
    path = outbox_path(outbox, name)
    if outbox.is_dir() and not outbox.is_symlink() and (path.exists() or path.is_symlink()):
        path.unlink()
        return True
    return False


def join_line(name: str) -> str:
    return f"tar xzf {name}.tar.gz && sudo edge-mesh join ./{name} --accept-golden-rule"


# --- the ledger -------------------------------------------------------------------------------------------------------

class Ledger:
    """Requests (FIFO), active leases (kept by pool.py, with the name and trust) and the history that feeds the caps."""

    def __init__(self, cfg: dict, state: dict, now: int, bundles: str | Path | None = None, closed: bool = False):
        self.cfg, self.now, self.closed = cfg, now, closed   # closed: panic is on, nothing new is approved
        self.bundles = Path(bundles) if bundles else None   # <state>/bundles: a name with a bundle is taken
        self.pool = Pool(cfg, state.get("pool"))
        self.requests: list[dict] = state.get("requests", [])
        self.history: list[dict] = state.get("history", [])
        self.next_request: int = state.get("next_request", 1)
        self.counters: dict = state.get("events", {"seq": 0, "count": {}})
        self.events = Events(self.counters, now)
        self._refresh()

    def prune(self) -> None:
        """Retention: pool.prune drops what no rule needs; a lease or a request that closed more than the retention ago
        keeps its name and times (a name is used once) but loses the identity, the note and the reason."""
        self.pool.prune(self.now)
        cutoff = self.now - self.pool.retention()
        for h in self.history:
            if h.get("ended_at", self.now) <= cutoff:
                h["identity"] = ""
        for r in self.requests:
            closed = r.get("ended_at") or r.get("rejected_at")
            if r["state"] in ("expired", "rejected") and closed is not None and closed <= cutoff:
                r.update(identity="", note="", reason="")

    def dump(self) -> dict:
        self.prune()
        return {"version": 1, "pool": self.pool.dump(), "requests": self.requests, "history": self.history,
                "next_request": self.next_request, "events": self.counters}

    def _refresh(self) -> None:
        """waiting <-> deferred, from the identity's eligibility right now. A request that stays waiting keeps the time it
        became eligible, so a run that changes nothing writes nothing (a tick every minute is a no-op until something happens)."""
        for r in self.open():
            when, why = self.pool.eligible_at(r["identity"], self.now)
            if when > self.now:
                r.update(state="deferred", eligible_at=when, why=why)
            elif r["state"] != "waiting" or r.get("why"):
                r.update(state="waiting", eligible_at=min(r.get("eligible_at", self.now), self.now), why="")

    def open(self) -> list[dict]:
        return [r for r in self.requests if r["state"] in OPEN]

    def position(self, req: dict) -> int:
        return self.open().index(req) + 1

    def find(self, name: str) -> dict | None:
        return next((r for r in self.requests if r["name"] == name), None)

    def taken(self, name: str) -> bool:
        """A player with this name exists already (invited by hand, or kicked: the bundle stays and a name is used once)."""
        return bool(self.bundles) and (self.bundles / name).exists()

    def request(self, name: str, ident: str, note: str = "") -> tuple[dict, str]:
        """Join the list. Idempotent for the same name and identity; one place in the list per identity."""
        check_name(name)
        mine = self.find(name)
        if not mine and self.taken(name):
            raise AdmissionError(3, f"{name}: a player with that name already exists (a name is used once), pick another")
        if mine:
            if mine["state"] in OPEN and mine["identity"] == ident:
                return mine, "already in the list"
            if mine["state"] in OPEN:
                raise AdmissionError(3, f"{name}: that name is already in the list for another identity")
            raise AdmissionError(3, f"{name}: that name was already used ({mine['state']}); a name is used once, pick another")
        same = next((r for r in self.open() if r["identity"] == ident), None)
        if same:
            return same, f"this identity is already in the list as {same['name']}; not added"
        req = {"id": f"req-{self.next_request}", "name": name, "identity": ident, "note": clean_text(note), "ts": self.now,
               "state": "waiting", "eligible_at": self.now, "why": ""}
        self.next_request += 1
        self.requests.append(req)
        self._refresh()
        return req, "added"

    def _next_end(self) -> str:
        if not self.pool.leases:
            return "no lease is active"
        first = min(self.pool.leases, key=lambda l: (l["end"], l["id"]))
        return f"the next lease ends at {iso(first['end'])} ({first.get('name', first['id'])}, in {left(first['end'] - self.now)})"

    def pick(self, name: str | None = None) -> dict:
        """The request to approve: the named one, or the first eligible in FIFO order. Raises when it cannot be approved."""
        if name:
            req = self.find(name)
            if not req or req["state"] not in OPEN:
                raise AdmissionError(3, f"{name}: not in the list (see: mesh host waitlist)")
            if self.taken(name):
                raise AdmissionError(3, f"{name}: a player with that name already exists; reject the request")
        if self.closed:
            raise AdmissionError(7, CLOSED)
        if self.pool.slots_free() <= 0:
            raise AdmissionError(5, f"no free place ({len(self.pool.leases)}/{self.cfg['slots']} used); {self._next_end()}")
        if name:
            if req["state"] == "deferred":
                raise AdmissionError(6, f"{name}: not eligible until {iso(req['eligible_at'])} ({req['why']})")
            return req
        ready = [r for r in self.open() if r["state"] == "waiting" and not self.taken(r["name"])]
        if not ready:
            later = sorted(self.open(), key=lambda r: r["eligible_at"])
            hint = f"; {later[0]['name']} is eligible at {iso(later[0]['eligible_at'])}" if later else ""
            raise AdmissionError(4, f"nobody eligible in the list ({len(self.open())} waiting){hint}")
        return ready[0]

    def approve(self, req: dict, trust: str, invite) -> dict:
        """invite(name, ttl_seconds, trust) -> bundle path, or raises. Nothing is recorded if it fails."""
        if trust not in ("public", "friends"):
            raise AdmissionError(2, "trust must be public or friends")
        if self.closed:
            raise AdmissionError(7, CLOSED)
        when, why = self.pool.eligible_at(req["identity"], self.now)   # the limits, checked again where the lease is made
        if when > self.now:
            raise AdmissionError(6, f"{req['name']}: not eligible until {iso(when)} ({why})")
        if self.pool.slots_free() <= 0:
            raise AdmissionError(5, f"no free place ({len(self.pool.leases)}/{self.cfg['slots']} used); {self._next_end()}")
        bundle = invite(req["name"], self.cfg["lease_seconds"], trust)
        lease = self.pool.grant(req["identity"], self.now, {"name": req["name"], "trust": trust, "request": req["id"],
                                                             "bundle": bundle})
        req.update(state="approved", lease=lease["id"], approved_at=self.now)
        return lease

    def reject(self, name: str, reason: str = "") -> str:
        req = self.find(name)
        if not req:
            raise AdmissionError(3, f"{name}: not in the list")
        if req["state"] == "rejected":
            return f"{name}: already rejected"
        if req["state"] not in OPEN:
            raise AdmissionError(3, f"{name}: is {req['state']}, not waiting; to end a lease early: mesh host reap --name {name}")
        req.update(state="rejected", reason=clean_text(reason), rejected_at=self.now)
        return f"{name}: rejected"

    def due(self, everyone: bool = False, name: str | None = None) -> list[dict]:
        leases = sorted(self.pool.leases, key=lambda l: (l["end"], l["id"]))
        if name:
            mine = [l for l in leases if l.get("name") == name]
            if not mine:
                raise AdmissionError(3, f"{name}: has no active lease (see: mesh host leases)")
            return mine
        return leases if everyone else [l for l in leases if l["end"] <= self.now]

    def finish(self, lease: dict, how: str | None = None) -> str:
        """The lease is over (its node already kicked): cooldown, place freed, request expired, history kept. Returns how it
        ended: expired, ended early, or what the caller says (panic)."""
        self.pool.reap(lease["id"], self.now)
        how = how or ("expired" if lease["end"] <= self.now else "ended early")
        for r in self.requests:
            if r.get("lease") == lease["id"]:
                r.update(state="expired", ended_at=self.now)
        self.history.append({"name": lease.get("name"), "identity": lease["ip"], "trust": lease.get("trust"),
                             "start": lease["start"], "end": lease["end"], "ended_at": self.now, "how": how})
        return how

    def status_line(self) -> str:
        used, total = len(self.pool.leases), self.cfg["slots"]
        waiting = [r for r in self.open() if r["state"] == "waiting"]
        soon = ", ".join(f"{l.get('name', l['id'])} in {left(l['end'] - self.now)}" for l in self.due(everyone=True)[:2])
        return (f"admission: slots {used}/{total} used | {len(self.open())} in the list ({len(waiting)} eligible now) | "
                f"next to end: {soon or 'none'}")


# --- the commands that call mesh --------------------------------------------------------------------------------------

def mesh_invite(mesh: str):
    def invite(name: str, ttl_seconds: int, trust: str) -> str:
        r = subprocess.run([mesh, "host", "invite", name, "--ttl", f"{ttl_seconds}s", "--trust", trust],
                           capture_output=True, text=True)
        sys.stderr.write(r.stdout + r.stderr)
        if r.returncode != 0:
            raise AdmissionError(1, f"{name}: the invite failed (exit {r.returncode}); nothing was recorded")
        m = re.search(r"^bundle: (\S+)", r.stdout, re.M)
        return m.group(1) if m else ""
    return invite


def mesh_kick(mesh: str):
    def kick(name: str) -> bool:
        r = subprocess.run([mesh, "host", "kick", name], capture_output=True, text=True)
        sys.stderr.write(r.stdout + r.stderr)
        return r.returncode in (0, 3)   # 3: no such player any more (already gone): nothing left to kick
    return kick


def mesh_activity(mesh: str):
    def activity(name: str) -> str | None:
        """`mesh host activity <name>`: "active", "idle: <why>", or None when the API did not answer (an unknown never expels)."""
        r = subprocess.run([mesh, "host", "activity", name], capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode == 0:
            return "active"
        if r.returncode == 1 and last.startswith("idle: "):
            return last
        sys.stderr.write(r.stdout + r.stderr)
        return None
    return activity


def mesh_burn(mesh: str):
    def burn() -> None:
        """Delete the join token of every player whose node registered (`mesh host burn`); its output goes to stderr so
        that `reap` keeps printing one fixed line."""
        r = subprocess.run([mesh, "host", "burn"], capture_output=True, text=True)
        sys.stderr.write(r.stdout + r.stderr)
    return burn


def expel(led: Ledger, lease: dict, kick, outbox: Path, how: str | None = None) -> bool:
    """Kick one lease's node; on success the lease is finished, its outbox file deleted and `expelled` recorded. A kick that
    fails keeps the lease and its place (the next run tries again) and records `kick_failed`."""
    name = lease.get("name", "")
    if not kick(name):
        led.events.emit("kick_failed", f"{name}: the kick failed; the lease and its place are kept and the next run tries again",
                        name, lease["ip"])
        return False
    how = led.finish(lease, how)
    with contextlib.suppress(AdmissionError):   # a name from before the name rule has no outbox file
        remove_outbox(outbox, name)
    led.events.emit("expelled", f"{name}: lease {how}, node expelled, place freed", name, lease["ip"], how=how)
    return True


def deliver(led: Ledger, lease: dict, outbox: Path) -> int:
    """The approved bundle into the outbox, and the `bundle_ready` event with its path. 0, or 1 when it could not be packed
    (the lease stands; `mesh host bundle <name>` packs it again)."""
    name = lease["name"]
    try:
        path, changed = pack_bundle(led.bundles, outbox, name, lease["start"])
    except (AdmissionError, OSError) as e:
        print(f"{name}: the bundle could not be put in the outbox ({e}); run: mesh host bundle {name}", file=sys.stderr)
        return 1
    if changed:
        led.events.emit("bundle_ready", f"{name}: bundle ready in the outbox ({name}.tar.gz), lease until {iso(lease['end'])}",
                        name, lease["ip"], path=str(path), end=lease["end"])
    return 0


def tick(a, cfg: dict, led: Ledger, auto: dict, outbox: Path) -> int:
    """One pass of the automatic pool, idempotent: burn, expel what ended (always; in panic: everything), warn what ends soon,
    then, only with auto on, approve the first eligible request while a place is free. 0, or 1 when a kick, an invite or a
    bundle failed (everything else of the pass is recorded)."""
    code, expelled, approved = 0, 0, 0
    mesh_burn(a.mesh)()
    kick = mesh_kick(a.mesh)
    for lease in led.due(everyone=auto["panic"]):
        if expel(led, lease, kick, outbox, "panic" if auto["panic"] else None):
            expelled += 1
        else:
            code = 1
    idle = int(cfg.get("idle_kick_seconds", 0) or 0)
    if idle > 0 and not auto["panic"]:
        expelled, failed = idle_kick(led, idle, mesh_activity(a.mesh), kick, outbox, expelled)
        code = max(code, failed)
    led._refresh()
    warn = cfg.get("lease_warning_seconds", 300)
    for lease in led.due(everyone=True):
        if not lease.get("warned") and lease["end"] - led.now <= warn:
            lease["warned"] = True
            led.events.emit("expiring", f"{lease.get('name')}: the lease ends at {iso(lease['end'])} "
                                        f"(in {left(lease['end'] - led.now)})", lease.get("name", ""), lease["ip"], end=lease["end"])
    ttl_ok = True
    if auto["enabled"] and not auto["panic"]:
        try:   # a TOKEN_TTL longer than 8 minutes stops approvals, never the expulsions above
            token_ttl_seconds(os.environ.get("TOKEN_TTL", "8m"))
        except AdmissionError as e:
            print(f"{e}; this tick approves nobody", file=sys.stderr)
            code, ttl_ok = 2, False
    if auto["enabled"] and not auto["panic"] and ttl_ok:
        invite = mesh_invite(a.mesh)
        while led.pool.slots_free() > 0:
            try:
                req = led.pick()
            except AdmissionError as e:
                if e.code == 4:      # nobody eligible now: the rest of the list waits for its time
                    break
                raise
            try:
                lease = led.approve(req, "public", invite)
            except AdmissionError as e:
                if e.code == 1:
                    led.events.emit("invite_failed", f"{req['name']}: the automatic invite failed; the next tick tries again",
                                    req["name"], req["identity"])
                    code = 1
                break
            approved += 1
            led.events.emit("approved", f"{req['name']}: approved automatically (public) until {iso(lease['end'])}",
                            req["name"], req["identity"], by="auto", end=lease["end"])
            code = max(code, deliver(led, lease, outbox))
        led._refresh()
    mode = "on" if auto["enabled"] else ("off (panic)" if auto["panic"] else "off")
    waiting = [r for r in led.open() if r["state"] == "waiting"]
    print(f"tick: auto {mode} | expelled {expelled} | approved {approved} | slots {len(led.pool.leases)}/{cfg['slots']} used | "
          f"{len(led.open())} in the list ({len(waiting)} eligible now)")
    return code


def idle_kick(led: Ledger, idle: int, activity, kick, outbox: Path, expelled: int) -> tuple[int, int]:
    """Expel every lease whose node has been idle (missing, NotReady or without a workload) for `idle` seconds. A lease is idle from its
    start until its node is first seen active, and afterwards from the first tick that sees it idle again (`idle_since`). Only a change
    is written, so a tick that sees the same thing changes nothing. Returns (expelled so far, 1 if a kick failed else 0)."""
    failed = 0
    for lease in led.due(everyone=True):
        seen = activity(lease.get("name", ""))
        if seen is None:
            continue
        if seen == "active":
            if not lease.get("active_seen") or "idle_since" in lease:
                lease.pop("idle_since", None)
                lease["active_seen"] = True
            continue
        since = lease.setdefault("idle_since", led.now if lease.get("active_seen") else lease["start"])
        if led.now - since >= idle:
            if expel(led, lease, kick, outbox, "idle"):
                expelled += 1
            else:
                failed = 1
    return expelled, failed


def last_tick(state_dir: Path) -> int:
    try:
        return int((state_dir / "last-tick").read_text().strip())
    except (OSError, ValueError):
        return 0


def metrics(cfg: dict, led: Ledger, auto: dict, state_dir: Path) -> str:
    """Prometheus textfile format: fixed order, no identity and no name. The host publishes it (node_exporter's textfile
    collector, or a file a scraper reads); this only prints it."""
    out: list[str] = []

    def metric(name: str, kind: str, help_: str, *samples) -> None:
        out.extend([f"# HELP emesh_admission_{name} {help_}", f"# TYPE emesh_admission_{name} {kind}"])
        out.extend(f"emesh_admission_{name}{labels} {value}" for labels, value in samples)

    leases = led.pool.leases
    remaining = min((max(0, l["end"] - led.now) for l in leases), default=0)
    count = led.counters.get("count", {})
    metric("slots_total", "gauge", "Places at the same time (0 while panic is on).", ("", cfg["slots"]))
    metric("slots_used", "gauge", "Places taken by an active lease.", ("", len(leases)))
    metric("waiting", "gauge", "Requests in the list (waiting or deferred).", ("", len(led.open())))
    metric("leases_active", "gauge", "Active leases.", ("", len(leases)))
    metric("lease_seconds_remaining_min", "gauge", "Seconds left of the lease that ends first (0 with no lease).", ("", remaining))
    metric("auto_enabled", "gauge", "1 when the automatic tick approves requests.", ("", int(auto["enabled"])))
    metric("panic", "gauge", "1 while panic is on (admission closed).", ("", int(auto["panic"])))
    metric("events_total", "counter", "Admission events recorded, by type.",
           *((f'{{type="{t}"}}', count.get(t, 0)) for t in EVENT_TYPES))
    metric("last_tick_timestamp_seconds", "gauge", "When the last tick ran (epoch seconds; 0 = never).", ("", last_tick(state_dir)))
    return "\n".join(out)


def status_json(cfg: dict, led: Ledger, auto: dict) -> dict:
    """`status --json`: the numbers of the status line and every request by name, for the public page's puller (poc/page/puller.py),
    which runs on the host and publishes only numbers and each visitor's own position. Not for publishing as it is: it has names."""
    opened = led.open()
    ends = {l["id"]: l["end"] for l in led.pool.leases}
    reqs = []
    for r in led.requests:
        item = {"name": r["name"], "state": r["state"], "eligible_at": r.get("eligible_at", 0), "why": r.get("why", "")}
        if r["state"] in OPEN:
            item["position"] = opened.index(r) + 1
        if r.get("lease") in ends:
            item["lease_end"] = ends[r["lease"]]
        reqs.append(item)
    return {"now": led.now, "auto": auto["enabled"], "panic": auto["panic"], "slots_total": cfg["slots"],
            "slots_used": len(led.pool.leases), "lease_seconds": cfg["lease_seconds"], "waiting": len(opened),
            "eligible": len([r for r in opened if r["state"] == "waiting"]),
            "leases": [{"name": l.get("name", ""), "end": l["end"]} for l in led.due(everyone=True)], "requests": reqs}


def run(a, cfg: dict) -> int:
    d = Path(a.state)
    after: list = []
    with locked_state(d, after) as state:
        auto = read_auto(d)
        saved = dict(auto)
        eff = dict(cfg, slots=0) if auto["panic"] else cfg   # panic: no place at all, whatever pool.yaml says
        led = Ledger(eff, state, a.now, a.bundles or d.parent / "bundles", closed=auto["panic"])
        try:
            code = command(a, eff, led, auto, d)
        finally:
            state.clear()
            state.update(led.dump())

        def commit() -> None:   # under the lock, right after state.json: the switch, the log, the heartbeat
            if auto != saved:
                write_atomic(d / "auto.json", auto)
            flush_events(d, led.events.pending, led.now, led.pool.retention(), cfg.get("events_max_lines", 5000))
            if a.cmd == "auto" and a.once and last_tick(d) != led.now:
                write_atomic(d / "last-tick", f"{led.now}\n".encode())
        after.append(commit)
    notify(led.events.pending)   # after the lock: a slow hook never holds up another command
    return code


def command(a, cfg: dict, led: Ledger, auto: dict, state_dir: Path) -> int:
    outbox = Path(a.outbox) if a.outbox else state_dir / "outbox"
    if a.cmd == "request":
        ip = a.ip
        if a.peer:
            ip = client_ip(a.peer, a.xff, cfg.get("trusted_proxies", []))
        try:
            ident = identity(ip, a.name, cfg.get("identity_ipv4_prefix", 32), cfg.get("identity_ipv6_prefix", 64))
        except ValueError:
            raise AdmissionError(2, f"not an IP address: {ip!r}")
        check_name(a.name)
        if led.closed:
            raise AdmissionError(7, CLOSED)
        rate, window = int(cfg.get("rate_max_requests", 0) or 0), int(cfg.get("rate_window_seconds", 600) or 600)
        if rate > 0:   # per identity, every request counts (a repeat too); a refused one does not extend the wait
            recent = [t for t in led.pool.requests.get(ident, []) if led.now - t < window]
            if len(recent) >= rate:
                print(f"{a.name}: refused: too many requests from this identity ({rate} in {window} s); try again at "
                      f"{iso(min(recent) + window)} [not added]")
                return 6
            led.pool.requests[ident] = recent + [led.now]
        mine = led.find(a.name)
        if auto["enabled"] and not (mine and mine["state"] in OPEN):
            when, why = led.pool.eligible_at(ident, led.now)
            if why == "daily-cap":   # automatic mode: over the cap is a no, not a place in the list for tomorrow
                led.events.emit("refused", f"{a.name}: refused, daily cap reached; eligible again at {iso(when)}", a.name, ident,
                                why=why, retry_at=when)
                print(f"{a.name}: refused: the daily cap ({cfg.get('daily_leases_per_identity', 2)} leases in 24 h) is reached; "
                      f"eligible again at {iso(when)} [not added]")
                return 6
        req, what = led.request(a.name, ident, a.note or "")
        where = f"position {led.position(req)} of {len(led.open())}"
        if req["state"] == "deferred":
            line = f"{req['name']}: deferred until {iso(req['eligible_at'])} ({req['why']}), {where}"
        else:
            line = f"{req['name']}: waiting, {where}"
        if what == "added":
            led.events.emit("request", line, req["name"], req["identity"])
        print(f"{line} [{what}]")
    elif a.cmd == "waitlist":
        print(f"{'POS':<4} {'NAME':<20} {'STATE':<9} {'ELIGIBLE':<21} {'IDENTITY':<24} NOTE")
        for i, r in enumerate(led.open(), 1):
            when = "now" if r["state"] == "waiting" else f"{iso(r['eligible_at'])}"
            print(f"{i:<4} {r['name']:<20} {r['state']:<9} {when:<21} {r['identity']:<24} {r['note']}".rstrip())
    elif a.cmd == "approve":
        ttl = token_ttl_seconds(os.environ.get("TOKEN_TTL", "8m"))
        req = led.pick(a.name)
        lease = led.approve(req, a.trust, mesh_invite(a.mesh))
        led.events.emit("approved", f"{req['name']}: approved by the host ({a.trust}) until {iso(lease['end'])}", req["name"],
                        req["identity"], by="host", end=lease["end"])
        print(f"approved {req['name']} ({a.trust}) until {iso(lease['end'])}; bundle: {lease['bundle']}")
        print(f"send the bundle privately; its join token lives {ttl // 60}m{ttl % 60:02d}s, or until the node registers "
              "and the next reap burns it")
    elif a.cmd == "reject":
        msg = led.reject(check_name(a.name), a.reason or "")
        if msg.endswith(": rejected"):
            led.events.emit("rejected", f"{a.name}: request rejected by the host", a.name, led.find(a.name)["identity"])
        print(msg)
    elif a.cmd == "leases":
        print(f"{'NAME':<20} {'TRUST':<8} {'ENDS':<21} {'LEFT':<9} IDENTITY")
        for l in led.due(everyone=True):
            print(f"{l.get('name', '?'):<20} {l.get('trust', '?'):<8} {iso(l['end']):<21} {left(l['end'] - led.now):<9} {l['ip']}")
    elif a.cmd == "reap":
        mesh_burn(a.mesh)()   # a registered node needs its join token no more: delete it before anything else
        kick, n, failed = mesh_kick(a.mesh), 0, []
        for lease in led.due(a.all, a.name):
            if expel(led, lease, kick, outbox):
                n += 1
                print(f"reaped {lease.get('name')}: kicked, place freed")
            else:
                failed.append(lease.get("name"))
        print(f"reaped {n}")
        if failed:
            print(f"could not kick: {' '.join(failed)} (their leases are kept; the others are recorded; run reap again)")
            return 1
    elif a.cmd == "auto":
        if bool(a.once) == bool(a.action):
            raise AdmissionError(2, "auto takes --once (one tick, for the host's timer) or one of on, off, status; "
                                    "there is no loop: the host's timer runs `mesh host auto --once`")
        if a.once:
            return tick(a, cfg, led, auto, outbox)
        if a.action == "on":
            if auto["panic"]:
                raise AdmissionError(7, CLOSED)
            if not auto["enabled"]:
                auto.update(enabled=True, changed_at=led.now)
                led.events.emit("auto_on", "automatic approval switched on")
            print("auto: on")
        elif a.action == "off":
            if auto["enabled"]:
                auto.update(enabled=False, changed_at=led.now)
                led.events.emit("auto_off", "automatic approval switched off (ended leases are still expelled)")
            print("auto: off")
        else:
            t = last_tick(state_dir)
            waiting = [r for r in led.open() if r["state"] == "waiting"]
            print(f"auto: {'on' if auto['enabled'] else 'off'} | panic: {'on' if auto['panic'] else 'off'} | slots "
                  f"{len(led.pool.leases)}/{cfg['slots']} used | {len(led.open())} in the list ({len(waiting)} eligible now) | "
                  f"last tick: {iso(t) if t else 'never'}")
    elif a.cmd == "panic":
        if a.clear:
            if auto["panic"]:
                auto.update(panic=False, changed_at=led.now)
                led.events.emit("panic_cleared", "panic cleared: the slots of pool.yaml apply again; auto stays off")
                print("panic: cleared; the slots of pool.yaml apply again; auto stays off (mesh host auto on)")
            else:
                print("panic: was not on")
            return 0
        first = not auto["panic"]
        if first:
            auto.update(enabled=False, panic=True, changed_at=led.now)
            led.closed = True
            led.events.emit("panic", "panic: admission closed, auto off, every active lease expelled")
        mesh_burn(a.mesh)()
        kick, n, code = mesh_kick(a.mesh), 0, 0
        for lease in led.due(everyone=True):
            if expel(led, lease, kick, outbox, "panic"):
                n += 1
            else:
                code = 1
        if outbox.is_dir() and not outbox.is_symlink():   # no bundle waits for delivery while admission is closed
            for f in outbox.iterdir():
                if f.name.endswith(".tar.gz") and (f.is_file() or f.is_symlink()):
                    f.unlink()
        print(f"panic: on{'' if first else ' (already)'} | expelled {n} | auto off | slots 0"
              + (" | some kicks failed: run mesh host panic again" if code else ""))
        return code
    elif a.cmd == "bundle":
        name = check_name(a.name)
        lease = next((l for l in led.pool.leases if l.get("name") == name), None)
        if not lease:
            raise AdmissionError(3, f"{name}: has no active lease; only an approved bundle goes to the outbox")
        path, changed = pack_bundle(led.bundles, outbox, name, lease["start"])
        if changed:
            led.events.emit("bundle_ready", f"{name}: bundle ready in the outbox ({name}.tar.gz), lease until {iso(lease['end'])}",
                            name, lease["ip"], path=str(path), end=lease["end"])
        print(f"bundle: {path} ({'written' if changed else 'unchanged'}); deliver it privately, it holds the node's key")
        print(f"join: {join_line(name)}")
    elif a.cmd == "metrics":
        print(metrics(cfg, led, auto, state_dir))
    elif getattr(a, "json", False):
        print(json.dumps(status_json(cfg, led, auto), sort_keys=True))
    else:
        print(led.status_line())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="admission ledger and automatic pool (docs/ADMISSION.md)")
    ap.add_argument("--state", required=True, help="directory of state.json")
    ap.add_argument("--config", required=True, help="pool.yaml")
    ap.add_argument("--mesh", default=str(Path(__file__).resolve().parent / "mesh"))
    ap.add_argument("--bundles", help="the players' bundle directory (default: <state>/../bundles)")
    ap.add_argument("--outbox", help="where approved bundles are packed (default: <state>/outbox)")
    ap.add_argument("--now", type=int, default=None, help="epoch seconds (tests); default: the clock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("request")
    p.add_argument("name")
    p.add_argument("--ip")
    p.add_argument("--peer", help="socket peer: X-Forwarded-For is believed only from a trusted proxy")
    p.add_argument("--xff")
    p.add_argument("--note")
    sub.add_parser("waitlist")
    p = sub.add_parser("approve")
    p.add_argument("name", nargs="?")
    p.add_argument("--trust", default="public", choices=["public", "friends"])
    p = sub.add_parser("reject")
    p.add_argument("name")
    p.add_argument("--reason")
    sub.add_parser("leases")
    p = sub.add_parser("reap")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="end every lease now")
    g.add_argument("--name", help="end this one lease now")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true", help="the numbers and every request by name, as JSON (for the page's puller)")
    p = sub.add_parser("auto")
    p.add_argument("action", nargs="?", choices=["on", "off", "status"])
    p.add_argument("--once", action="store_true", help="one tick (the host's timer runs it every minute)")
    p = sub.add_parser("panic")
    p.add_argument("--clear", action="store_true", help="reopen admission (auto stays off)")
    p = sub.add_parser("bundle")
    p.add_argument("name")
    sub.add_parser("metrics")
    a = ap.parse_args(argv)
    a.now = int(time.time()) if a.now is None else a.now
    try:
        return run(a, load_config(a.config))
    except AdmissionError as e:
        print(str(e), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
