"""The host console's mutations (docs/CONSOLE.md; issue #87): plans and single-use confirm tokens, jobs, timeouts, the audit log.

A registry verb whose confirmation is not `none` runs in two calls: the first answers a plan (what will happen, in sentences, from pure
reads) and a token valid 60 seconds, once, bound to the verb, its arguments, the caller's credential and a hash of the plan; the second,
with that token (and the typed name or `all` for type:*), starts a job. The plan is computed again when the job starts: if the state
changed meanwhile the job does not run (`plan-changed`, exit 75).

Jobs run one at a time, in order, with at most 4 waiting (a fifth caller: 409 busy). `panic` skips the queue and has no timeout. A job
is the engine started as an argument list (never a shell) in its own process group, with EMESH_DIRECT=1 and EMESH_AUDITED=1; at its
timeout the group gets SIGTERM, then SIGKILL 30 s later, and the job ends with exit 124. Its output (64 KiB at most) is redacted.

Every job writes one line to <state>/console/audit.jsonl (0600), appended with one write under <state>/mesh.lock, where the file is
also rotated (1 MiB, one old copy). A job of the `console` role also calls the admission notify hook with `console_action`.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import actions

try:
    import fcntl
except ImportError:  # pragma: no cover - the daemon is Linux only
    fcntl = None

CONFIRM_SECONDS = 60
QUEUE_MAX = 4
OUTPUT_MAX = 64 * 1024
KILL_GRACE = 30
JOB_KEEP_SECONDS = 3600
AUDIT_MAX = 1024 * 1024
RATE_PER_MINUTE = 30
EX_TEMPFAIL = 75


class Refused(actions.Refused):
    pass


class Plans:
    """Confirm tokens: issued with a plan, redeemed once."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens: dict[str, dict] = {}

    def issue(self, verb: str, args: dict, cred: str, plan_hash: str) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self.tokens = {t: v for t, v in self.tokens.items() if v["expires"] > now}
            self.tokens[tok] = {"verb": verb, "args": args, "cred": cred, "hash": plan_hash, "expires": now + CONFIRM_SECONDS}
        return tok

    def redeem(self, tok: str, verb: str, args: dict, cred: str) -> str:
        """The plan hash the token was issued for; the token is gone after this call, whatever the answer."""
        with self.lock:
            entry = self.tokens.pop(tok, None) if isinstance(tok, str) else None
        if entry is None or entry["expires"] <= self.clock():
            raise Refused(410, "confirm", "the confirm token is wrong, expired (60 s) or used: ask for a new plan")
        if not (entry["verb"] == verb and entry["args"] == args and hmac.compare_digest(entry["cred"], cred)):
            raise Refused(410, "confirm", "that token was issued for another call")
        return entry["hash"]


def plan_hash(will: list[str]) -> str:
    return hashlib.sha256(json.dumps(will, sort_keys=True).encode()).hexdigest()


def plan(verb: str, args: dict, reads) -> list[str]:
    """What the verb will do, in sentences, from the console's pure reads (`reads(route)`: status, requests, leases, players)."""
    def leases():
        return reads("leases")

    def lease_line(ls):
        return ", ".join(f"{l['name']} (ends {time.strftime('%H:%M', time.gmtime(l['end']))} UTC)" for l in ls) or "none"
    if verb == "panic":
        ls = leases()
        return ["auto off; slots 0; admission closed", f"expel {len(ls)} lease(s): {lease_line(ls)}", "empty the outbox"]
    if verb == "panic-clear":
        return ["reopen admission: the slots of pool.yaml apply again", "auto stays off until auto on"]
    if verb == "auto-on":
        st, rq = reads("status"), reads("requests")
        free = max(0, int(st.get("slots_total", 0)) - int(st.get("slots_used", 0)))
        return ["automatic approvals, without a human, every tick", f"{free} place(s) free now; {len(rq)} in the list"]
    if verb == "approve":
        rq = reads("requests")
        who = args.get("name") or next((r["name"] for r in rq if r.get("state") == "waiting"), None)
        return [f"approve {who or 'nobody (no one is eligible now)'} with trust {args.get('trust', 'public')}",
                "a certificate and a namespace for one lease; the bundle into the outbox"]
    if verb in ("reap-all", "reap-one", "kick", "revoke"):
        ls = leases()
        if verb == "reap-all":
            return [f"end every lease now: {lease_line(ls)}"]
        name = args["name"]
        mine = [l for l in ls if l["name"] == name]
        what = {"reap-one": "end the lease of", "kick": "kick", "revoke": "revoke the certificate of"}[verb]
        return [f"{what} {name}", f"lease: {lease_line(mine)}"]
    if verb == "party-end":
        return [f"end the party of {args['a']} and {args['b']}: both consents and the NetworkPolicies removed"]
    return [f"{verb} {json.dumps(args, sort_keys=True)}"]


class Job:
    def __init__(self, verb: str, argv: list[str], role: str, source: str, uid: int, args: dict, confirmed: bool,
                 timeout: int | None, recheck=None):
        self.id = secrets.token_hex(8)
        self.verb, self.argv, self.role, self.source, self.uid, self.args = verb, argv, role, source, uid, args
        self.confirmed, self.timeout, self.recheck = confirmed, timeout, recheck
        self.state, self.exit, self.output, self.error = "queued", None, "", None
        self.created, self.started, self.ended = time.time(), None, None
        self.done = threading.Event()

    def view(self, redact) -> dict:
        return {"id": self.id, "verb": self.verb, "state": self.state, "exit": self.exit, "output": redact(self.output),
                "started": int(self.started) if self.started else None, "ended": int(self.ended) if self.ended else None,
                "error": self.error}


class Runner:
    """One job at a time, in order; panic in a lane of its own."""

    def __init__(self, state: Path, mesh: Path, redact, notify=None, kill_grace: int = KILL_GRACE):
        self.state, self.mesh, self.redact, self.kill_grace = state, mesh, redact, kill_grace
        self.notify = notify
        self.lock = threading.Lock()
        self.queue: collections.deque[Job] = collections.deque()
        self.jobs: dict[str, Job] = {}
        self.busy = False
        self.recent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.audit_lock = threading.Lock()

    def rate(self, role: str) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.recent[role]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= RATE_PER_MINUTE:
                raise Refused(429, "rate", f"at most {RATE_PER_MINUTE} mutations a minute")
            q.append(now)

    def submit(self, job: Job) -> Job:
        with self.lock:
            cutoff = time.time() - JOB_KEEP_SECONDS
            self.jobs = {k: j for k, j in self.jobs.items() if j.ended is None or j.ended > cutoff}
            if job.verb == "panic":   # its own lane, one at a time: a second press while one runs gets that same job
                running = next((j for j in self.jobs.values() if j.verb == "panic" and j.ended is None), None)
                if running is not None:
                    return running
                self.jobs[job.id] = job
                threading.Thread(target=self._safe_run, args=(job,), daemon=True).start()
                return job
            if len(self.queue) >= QUEUE_MAX:
                raise Refused(409, "busy", f"busy: {QUEUE_MAX} jobs waiting, try again")
            self.jobs[job.id] = job
            self.queue.append(job)
            if not self.busy:
                self.busy = True
                threading.Thread(target=self._drain, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _drain(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    self.busy = False
                    return
                job = self.queue.popleft()
            self._safe_run(job)

    def _safe_run(self, job: Job) -> None:
        """_run, and whatever goes wrong in it the job ends (failed, `internal`) and the queue keeps moving."""
        try:
            self._run(job)
        except Exception as e:
            print(f"console: job {job.id} ({job.verb}) failed inside the daemon: {e.__class__.__name__}", file=sys.stderr)
            if not job.done.is_set():
                job.exit = job.exit if job.exit is not None else 1
                job.error = job.error or "internal"
                job.ended = job.ended or time.time()
                job.state = "failed"
                job.done.set()

    def _run(self, job: Job) -> None:
        job.started = time.time()
        if job.recheck is not None:
            try:
                ok = job.recheck()
            except Exception:
                ok = False
            if not ok:
                job.exit, job.error = EX_TEMPFAIL, "plan-changed"
                job.output = "the state changed while the job waited: ask for a new plan\n"
                return self._finish(job, "failed")
        job.state = "running"
        env = dict(os.environ, EMESH_DIRECT="1", EMESH_AUDITED="1")
        try:
            p = subprocess.Popen([str(self.mesh), *job.argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True)
        except OSError:
            job.exit, job.error = 127, "engine"
            return self._finish(job, "failed")
        chunks, size = [], 0

        def read():
            nonlocal size
            for chunk in iter(lambda: p.stdout.read(4096), b""):
                if size < OUTPUT_MAX:
                    chunks.append(chunk[:OUTPUT_MAX - size])
                    size += len(chunks[-1])
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            job.exit = p.wait(timeout=job.timeout)
            final = "done" if job.exit == 0 else "failed"
        except subprocess.TimeoutExpired:
            self._kill(p)
            job.exit, job.error, final = 124, "timeout", "timeout"
        reader.join(5)
        job.output = b"".join(chunks).decode("utf-8", "replace")
        self._finish(job, final)

    def _kill(self, p: subprocess.Popen) -> None:
        for sig, wait in ((signal.SIGTERM, self.kill_grace), (signal.SIGKILL, 5)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                return
            try:
                p.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def _finish(self, job: Job, final: str) -> None:
        """ended, the audit line and the notify first; the state last, so a client that sees a finished job sees all of it."""
        job.ended = time.time()
        try:
            self.audit(job)
        except OSError as e:   # the job ran: say it was not audited, never hide its result or stop the queue
            print(f"console: the audit line of job {job.id} was not written ({e.__class__.__name__})", file=sys.stderr)
            job.error = job.error or "audit"
        if job.role == "console" and self.notify:
            try:
                self.notify(self.redact(notify_summary(job)))
            except Exception:
                pass
        job.state = final
        job.done.set()

    def audit(self, job: Job) -> None:
        line = {"ts": int(job.ended), "source": job.source, "role": job.role, "uid": job.uid, "verb": job.verb,
                "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in job.args.items()}, "confirmed": job.confirmed,
                "exit": job.exit, "duration_ms": int((job.ended - (job.started or job.ended)) * 1000), "job": job.id}
        write_audit(self.state, json.dumps(self.redact(line), sort_keys=True) + "\n", self.audit_lock)


def notify_summary(job: Job) -> str:
    """`kick alice: exit 0`: the verb and only its name and choice arguments, never free text (a note or a reason)."""
    a = actions.REGISTRY[job.verb]
    words = [str(v) for k, v in sorted(job.args.items()) if "enum" in a.params.get(k, {}) or a.params.get(k) is actions.NAME]
    return " ".join([job.verb, *words]) + f": exit {job.exit}"


def write_audit(state: Path, line: str, tlock: threading.Lock) -> None:
    d = state / "console"
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / "audit.jsonl"
    with tlock, open(state / "mesh.lock", "a+") as lock:
        if fcntl:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists() and path.stat().st_size + len(line) > AUDIT_MAX:
                os.replace(path, d / "audit.jsonl.1")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
        finally:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def console_notify(summary: str) -> None:
    """The admission notify hook with `console_action` (admission.notify: an argument list, never a shell; a failure is only logged)."""
    try:
        import admission
        admission.notify([{"type": "console_action", "summary": summary[:160], "identity": ""}])
    except Exception as e:   # the job is done and audited; a hook never undoes it
        print(f"console: notify failed ({e.__class__.__name__})", file=sys.stderr)
