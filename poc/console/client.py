#!/usr/bin/env python3
"""The CLI as a client of the host console (docs/CONSOLE.md, issue #88).

    client.py run --socket PATH --mesh PATH -- <mesh arguments>
        `poc/mesh` hands a registry verb here when the daemon's socket exists and EMESH_DIRECT is unset. The call goes through the API with
        the `local` role: the plan and the confirmation are made here, without asking (the person who typed the command already confirmed
        it), the job is followed until it ends, its stdout and stderr are printed as the engine printed them, and its exit code is ours.
        While the job waits in the daemon's queue: `waiting for <verb> (job <id>)` on stderr, once.
        Exit 75 (EX_TEMPFAIL) when the daemon is busy (4 jobs waiting), over its rate limit, or the plan changed 3 times in a row.
        When nothing was sent (not exactly a registry call, or no daemon answers the socket) the engine runs directly instead, the same
        as with EMESH_DIRECT=1: the CLI never needs the daemon.

    client.py audit --state DIR --verb VERB --started EPOCH --exit N -- <mesh arguments>
        The audit line of a registry verb that `poc/mesh` ran directly (source `direct`, role `local`), the same line a job of the daemon
        writes, redacted the same way, appended under <state>/console/audit.lock.

Python standard library only.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import actions  # noqa: E402

EX_TEMPFAIL = 75
CONNECT_TIMEOUT = 5     # seconds to reach the socket
READ_TIMEOUT = 30       # seconds a GET of a job may take (the daemon answers those from memory)
POST_TIMEOUT = 300      # a POST waits far longer than the daemon can take to answer one (its plan reads wait for the ledger's lock, 60 s
                        # each, two of them; a job's run time is never part of the answer): a client that gave up early would report a
                        # failure for a command that runs anyway, one that never gave up would hang on a stopped daemon
POLL = 0.2
WAIT_NOTE_AFTER = 1.0   # a job still queued after this long: say so (a direct command waiting for the lock would just wait)
PLAN_TRIES = 3


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None = READ_TIMEOUT):
        super().__init__("localhost")
        self.path, self.read_timeout = path, timeout

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(self.path)
        s.settimeout(self.read_timeout)
        self.sock = s


class NotSent(Exception):
    """Nothing reached the daemon: running the engine directly is safe."""


class MaybeRan(Exception):
    """A call that starts a job was sent, and no answer came back: the job may run. Never run it again behind its back."""


class LostJob(Exception):
    """The job started, and following it failed: it goes on in the daemon."""


def call(sock: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    c = UnixHTTP(sock, POST_TIMEOUT if method == "POST" else READ_TIMEOUT)
    try:
        try:
            c.connect()
        except OSError as e:   # no daemon behind the socket (stopped, crashed and left the file): nothing was sent
            raise NotSent(str(e)) from None
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, f"/api/v1/{path}", body=data, headers={"Content-Type": "application/json"} if data else {})
        r = c.getresponse()
        text = r.read()
    finally:
        c.close()
    try:
        out = json.loads(text) if text else {}
    except ValueError:
        out = {"error": "internal", "message": text.decode("utf-8", "replace")[:200]}
    return r.status, out


def direct(mesh: str, words: list[str]) -> None:
    os.execve(mesh, [mesh, *words], dict(os.environ, EMESH_DIRECT="1"))


def refused(status: int, out: dict) -> int:
    msg = out.get("message") or out.get("error") or f"HTTP {status}"
    print(f"mesh: {msg}", file=sys.stderr)
    if status in (409, 429):
        return EX_TEMPFAIL
    if status in (400, 403, 404, 410):
        return 2
    return 1


def start(sock: str, verb: str, args: dict) -> tuple[int, dict]:
    """POST the call, and when it wants a confirmation, confirm it: (status, job) on 202, else (status, error). A lost answer to a
    call that starts a job is MaybeRan; a lost plan is a plain error (nothing ran)."""
    if actions.REGISTRY[verb].confirm == "none":
        return started(sock, verb, args)
    status, out = call(sock, "POST", f"actions/{verb}", {"args": args})
    if status != 200:
        return status, out
    body = {"args": args, "confirm": out["confirm"]}
    if out.get("type_to_confirm"):
        body["typed"] = out["type_to_confirm"]
    try:
        return call(sock, "POST", f"actions/{verb}", body)
    except (OSError, http.client.HTTPException) as e:   # NotSent included: the plan was answered, so the daemon was there
        raise MaybeRan(str(e)) from None


def started(sock: str, verb: str, args: dict) -> tuple[int, dict]:
    """start() for a verb with no confirmation: its one POST starts the job."""
    try:
        return call(sock, "POST", f"actions/{verb}", {"args": args})
    except NotSent:
        raise
    except (OSError, http.client.HTTPException) as e:
        raise MaybeRan(str(e)) from None


def follow(sock: str, job: dict, verb: str) -> dict:
    t0, noted = time.monotonic(), False
    while job.get("state") in ("queued", "running"):
        if not noted and job["state"] == "queued" and time.monotonic() - t0 >= WAIT_NOTE_AFTER:
            print(f"waiting for {verb} (job {job['id']})", file=sys.stderr, flush=True)
            noted = True
        time.sleep(POLL)
        try:
            status, seen = call(sock, "GET", f"jobs/{job['id']}")
        except (OSError, http.client.HTTPException) as e:   # NotSent included: the daemon went away after it took the job
            raise LostJob(str(e) or "connection lost") from None
        if status != 200:
            raise LostJob(seen.get("message") or f"HTTP {status}")
        job = seen
    return job


def run(a) -> int:
    words = a.words
    parsed = actions.parse(words)
    if parsed is None or parsed[0] in ("tick", "panic"):   # the timer's tick and panic never go through the API (docs/CONSOLE.md)
        direct(a.mesh, words)
    verb, args = parsed
    job: dict = {}
    sending = False
    try:
        for _ in range(PLAN_TRIES):
            try:
                sending = True   # from here a Ctrl-C may land after the daemon took the job; once it answers, the job has an id
                status, job = start(a.socket, verb, args)
            except NotSent:
                if job:   # a call already reached the daemon: never run it a second time behind its back
                    raise
                direct(a.mesh, words)
            if status != 202:
                if status == 409 and job.get("error") == "plan-changed":
                    continue
                return refused(status, job)
            job = follow(a.socket, job, verb)
            if job.get("error") != "plan-changed":
                break
        else:
            print(f"mesh: the state kept changing under {verb}: try again", file=sys.stderr)
            return EX_TEMPFAIL
    except KeyboardInterrupt:
        if job.get("id"):
            print(f"\nmesh: {verb} goes on in the console daemon (job {job['id']})", file=sys.stderr)
        elif sending:
            print(f"\nmesh: {verb} may already be in the console daemon's queue: look at <state>/console/audit.jsonl", file=sys.stderr)
        sys.stderr.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)   # die by SIGINT, as the direct command would: a shell loop around it stops too
        os.kill(os.getpid(), signal.SIGINT)
        return 130
    except MaybeRan as e:
        print(f"mesh: the console daemon did not answer once {verb} was sent ({e or 'connection lost'}): it may have run. "
              "Look at <state>/console/audit.jsonl before you try again.", file=sys.stderr)
        return 1
    except LostJob as e:
        print(f"mesh: {verb} goes on in the console daemon (job {job.get('id')}), but following it failed ({e}); its end will be in "
              "<state>/console/audit.jsonl", file=sys.stderr)
        return 1
    except (OSError, NotSent, http.client.HTTPException) as e:
        print(f"mesh: the console daemon did not answer the plan of {verb} ({e or 'connection lost'}); nothing ran", file=sys.stderr)
        return 1
    if job.get("error") == "plan-changed":
        print(f"mesh: the state kept changing under {verb}: try again", file=sys.stderr)
        return EX_TEMPFAIL
    sys.stdout.write(job.get("stdout", ""))
    sys.stdout.flush()
    sys.stderr.write(job.get("stderr", ""))
    sys.stderr.flush()
    return job["exit"] if isinstance(job.get("exit"), int) else 1


def audit(a) -> int:
    import mutate
    import server
    parsed = actions.parse(a.words)
    args = parsed[1] if parsed else {"argv": " ".join(a.words[2:])[:200]}
    ended = time.time()
    line = {"ts": int(ended), "source": "direct", "role": "local", "uid": os.getuid(), "verb": a.verb,
            "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()}, "confirmed": False, "exit": a.exit,
            "duration_ms": max(0, int((ended - a.started) * 1000)), "job": None}
    given = Path(os.path.abspath(a.state))
    state = given.resolve()
    redact = server.Redactor(state, given)
    mutate.write_audit(state, json.dumps(redact(line), sort_keys=True) + "\n", threading.Lock())
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="client.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--socket", required=True)
    r.add_argument("--mesh", required=True)
    r.add_argument("words", nargs=argparse.REMAINDER)
    au = sub.add_parser("audit")
    au.add_argument("--state", required=True)
    au.add_argument("--verb", required=True)
    au.add_argument("--started", type=float, required=True)
    au.add_argument("--exit", type=int, required=True)
    au.add_argument("words", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    if a.words[:1] == ["--"]:
        a.words = a.words[1:]
    return run(a) if a.cmd == "run" else audit(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
