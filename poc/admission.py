#!/usr/bin/env python3
"""Admission with manual approval (option A, docs/ADMISSION.md): the ledger behind `mesh host request|waitlist|approve|
reject|leases|reap|admission-status`.

People ask for a place; the host approves them by hand, one at a time, inside the limits of poc/pool.yaml (slots, lease,
cooldown, daily cap per identity), which pool.py enforces. Approving issues the bundle with the existing `mesh host invite`
(certificate lifetime = the lease); `reap` first burns the join token of every player whose node registered (`mesh host burn`),
then expels every lease that ended with the existing `mesh host kick` and frees its place.

Retention: before every write the ledger forgets what no rule needs (pool.prune) and blanks the identity and the note of every
lease and request that closed more than `identity_retention_seconds` ago (the names and times stay: a name is used once).

State: one JSON file, <state>/state.json, read and written under an exclusive lock (<state>/lock: fcntl.flock on Linux and
macOS, msvcrt.locking on Windows) and replaced atomically (temporary file + fsync + rename), so two commands at the same time
never lose a request. Time is injectable (`--now <epoch seconds>`): the unit tests simulate a day in no time.

    admission.py --state DIR --config pool.yaml [--mesh poc/mesh] [--now N] <command> ...
      request <name> [--ip IP] [--peer IP --xff CHAIN] [--note TEXT]
      waitlist | leases | status
      approve [<name>] [--trust public|friends]
      reject <name> [--reason TEXT]
      reap [--all | --name NAME]

Exit codes: 0 done; 1 the invite failed (nothing recorded), a kick failed (`reap` records the kicks that succeeded and keeps
the failed lease for the next run) or the ledger file is unreadable (nothing changed); 2 bad input (name, IP); 3 unknown or
already used name, or a player with that name already exists; 4 nobody eligible; 5 no free place; 6 that identity is not
eligible yet (cooldown, daily cap, active lease).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
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


def write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, sort_keys=True, indent=1)
            f.write("\n")
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
def locked_state(state_dir: str | Path):
    """Yield the state dict under the lock; written back (atomically) only when the block ends without an exception."""
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
        finally:
            _unlock(lock)


# --- the ledger -------------------------------------------------------------------------------------------------------

class Ledger:
    """Requests (FIFO), active leases (kept by pool.py, with the name and trust) and the history that feeds the caps."""

    def __init__(self, cfg: dict, state: dict, now: int, bundles: str | Path | None = None):
        self.cfg, self.now = cfg, now
        self.bundles = Path(bundles) if bundles else None   # <state>/bundles: a name with a bundle is taken
        self.pool = Pool(cfg, state.get("pool"))
        self.requests: list[dict] = state.get("requests", [])
        self.history: list[dict] = state.get("history", [])
        self.next_request: int = state.get("next_request", 1)
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
                "next_request": self.next_request}

    def _refresh(self) -> None:
        """waiting <-> deferred, from the identity's eligibility right now."""
        for r in self.open():
            when, why = self.pool.eligible_at(r["identity"], self.now)
            if when > self.now:
                r.update(state="deferred", eligible_at=when, why=why)
            else:
                r.update(state="waiting", eligible_at=self.now, why="")

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

    def finish(self, lease: dict) -> None:
        """The lease is over (its node already kicked): cooldown, place freed, request expired, history kept."""
        self.pool.reap(lease["id"], self.now)
        how = "expired" if lease["end"] <= self.now else "ended early"
        for r in self.requests:
            if r.get("lease") == lease["id"]:
                r.update(state="expired", ended_at=self.now)
        self.history.append({"name": lease.get("name"), "identity": lease["ip"], "trust": lease.get("trust"),
                             "start": lease["start"], "end": lease["end"], "ended_at": self.now, "how": how})

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


def mesh_burn(mesh: str):
    def burn() -> None:
        """Delete the join token of every player whose node registered (`mesh host burn`); its output goes to stderr so
        that `reap` keeps printing one fixed line."""
        r = subprocess.run([mesh, "host", "burn"], capture_output=True, text=True)
        sys.stderr.write(r.stdout + r.stderr)
    return burn


def run(a, cfg: dict) -> int:
    with locked_state(a.state) as state:
        led = Ledger(cfg, state, a.now, a.bundles or Path(a.state).parent / "bundles")
        try:
            code = command(a, cfg, led)
        finally:
            state.clear()
            state.update(led.dump())
    return code


def command(a, cfg: dict, led: Ledger) -> int:
    if a.cmd == "request":
        ip = a.ip
        if a.peer:
            ip = client_ip(a.peer, a.xff, cfg.get("trusted_proxies", []))
        try:
            ident = identity(ip, a.name, cfg.get("identity_ipv4_prefix", 32), cfg.get("identity_ipv6_prefix", 64))
        except ValueError:
            raise AdmissionError(2, f"not an IP address: {ip!r}")
        req, what = led.request(a.name, ident, a.note or "")
        where = f"position {led.position(req)} of {len(led.open())}"
        if req["state"] == "deferred":
            print(f"{req['name']}: deferred until {iso(req['eligible_at'])} ({req['why']}), {where} [{what}]")
        else:
            print(f"{req['name']}: waiting, {where} [{what}]")
    elif a.cmd == "waitlist":
        print(f"{'POS':<4} {'NAME':<20} {'STATE':<9} {'ELIGIBLE':<21} {'IDENTITY':<24} NOTE")
        for i, r in enumerate(led.open(), 1):
            when = "now" if r["state"] == "waiting" else f"{iso(r['eligible_at'])}"
            print(f"{i:<4} {r['name']:<20} {r['state']:<9} {when:<21} {r['identity']:<24} {r['note']}".rstrip())
    elif a.cmd == "approve":
        ttl = token_ttl_seconds(os.environ.get("TOKEN_TTL", "8m"))
        req = led.pick(a.name)
        lease = led.approve(req, a.trust, mesh_invite(a.mesh))
        print(f"approved {req['name']} ({a.trust}) until {iso(lease['end'])}; bundle: {lease['bundle']}")
        print(f"send the bundle privately; its join token lives {ttl // 60}m{ttl % 60:02d}s, or until the node registers "
              "and the next reap burns it")
    elif a.cmd == "reject":
        print(led.reject(check_name(a.name), a.reason or ""))
    elif a.cmd == "leases":
        print(f"{'NAME':<20} {'TRUST':<8} {'ENDS':<21} {'LEFT':<9} IDENTITY")
        for l in led.due(everyone=True):
            print(f"{l.get('name', '?'):<20} {l.get('trust', '?'):<8} {iso(l['end']):<21} {left(l['end'] - led.now):<9} {l['ip']}")
    elif a.cmd == "reap":
        mesh_burn(a.mesh)()   # a registered node needs its join token no more: delete it before anything else
        kick, n, failed = mesh_kick(a.mesh), 0, []
        for lease in led.due(a.all, a.name):
            if kick(lease.get("name", "")):
                led.finish(lease)
                n += 1
                print(f"reaped {lease.get('name')}: kicked, place freed")
            else:
                failed.append(lease.get("name"))
        print(f"reaped {n}")
        if failed:
            print(f"could not kick: {' '.join(failed)} (their leases are kept; the others are recorded; run reap again)")
            return 1
    else:
        print(led.status_line())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="manual admission ledger (docs/ADMISSION.md)")
    ap.add_argument("--state", required=True, help="directory of state.json")
    ap.add_argument("--config", required=True, help="pool.yaml")
    ap.add_argument("--mesh", default=str(Path(__file__).resolve().parent / "mesh"))
    ap.add_argument("--bundles", help="the players' bundle directory (default: <state>/../bundles)")
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
    g.add_argument("--all", action="store_true", help="end every lease now (the abuse switch, with slots: 0)")
    g.add_argument("--name", help="end this one lease now")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    a.now = int(time.time()) if a.now is None else a.now
    try:
        return run(a, load_config(a.config))
    except AdmissionError as e:
        print(str(e), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
