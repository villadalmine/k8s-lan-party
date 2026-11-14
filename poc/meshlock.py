#!/usr/bin/env python3
"""The engine's lock (docs/CONSOLE.md, "Jobs, locks and timeouts", issue #88): one `flock` on `$STATE/mesh.lock` around every mutating verb,
whoever calls it (the timer, the CLI direct, a job of the console daemon), taken by the outermost `mesh` only.

The lock is held by a small holder process, not by a file descriptor the whole process tree inherits: a process that outlives the command
(an ssh ControlPersist master started by `LH_RSH`, a daemonized helper) would otherwise keep the lock for hours. The holder takes the lock,
writes the pid it serves and its own pid to `mesh.lock.owner`, prints `locked`, and keeps the lock until that pid is gone (it polls every
0.1 s) or until it is killed itself: it runs in the caller's process group, so the console daemon's timeout (SIGKILL to the group) frees it too.

A nested `mesh` (the engine calls `mesh` from inside `mesh`: a tick's kicks, approve's invite) sees `EMESH_LOCK_HELD=<pid>` and runs without
locking, but only when the marker holds: the lock is held now (a non-blocking `flock` fails), the owner file names that pid and a holder
that still runs, and that pid is one of the nested call's ancestors. A marker set by hand, or left in an environment, fails the check and
the lock is taken as usual. The holder's pid is what makes the owner file trustworthy: a holder removes the file before it lets the lock
go, and one that was killed leaves a file naming a dead holder; so a file that is not the current holder's, read in the moment between
the next holder taking the lock and rewriting the file, is never trusted. The holder must also belong to the pid it serves (be that
process, as in admission.py, or its child, as in lib.sh), so a dead holder's pid taken by an unrelated process does not pass either.

    meshlock.py hold <lockfile> <pid>     hold the lock for <pid> (prints "locked" once it is held)
    meshlock.py held <lockfile> <pid>     exit 0 when <pid> holds the lock and is an ancestor of this process, else 1

Python standard library only; `fcntl.flock` exists on Linux and macOS (the `flock(1)` command does not on macOS).
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

POLL = 0.1


def alive(pid: int) -> bool:
    """The pid runs (a zombie does not: a `mesh` that ended but whose parent has not reaped it yet must not keep the lock)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return not r.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def parent_of(pid: int) -> int:
    try:   # Linux: field 4 of /proc/<pid>/stat, after the command name in parentheses (which may hold spaces)
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        pass
    try:   # macOS and the rest
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def ancestors(pid: int) -> set[int]:
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        pid = parent_of(pid)
    return seen


def owner_path(lockfile: Path) -> Path:
    return lockfile.with_name(lockfile.name + ".owner")


def take(lockfile: Path, pid: int) -> int:
    """Block until the lock is ours; record whom it serves. Returns the open descriptor (the lock lives as long as it is open)."""
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    tmp = owner_path(lockfile).with_suffix(f".{os.getpid()}")
    tmp.write_text(f"{pid} {os.getpid()}\n")
    os.replace(tmp, owner_path(lockfile))
    return fd


def held_by(lockfile: Path, pid: int, me: int | None = None) -> bool:
    """True when the lock is held now, the owner file names `pid` and a holder that runs, and `pid` is an ancestor of `me` (this process
    by default)."""
    try:
        served, holder = (int(w) for w in owner_path(lockfile).read_text().split())
    except (OSError, ValueError):
        return False
    if served != pid or not alive(holder) or (holder != pid and parent_of(holder) != pid):
        return False   # the holder is the served process itself (admission.py) or its child (lib.sh): a reused pid is neither
    if pid not in ancestors(me if me is not None else os.getpid()):
        return False
    try:
        fd = os.open(lockfile, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True   # somebody holds it: the owner file, written under the lock, says it is pid
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False   # nobody holds it: the owner file is stale
    finally:
        os.close(fd)


def hold(lockfile: Path, pid: int) -> int:
    fd = take(lockfile, pid)
    print("locked", flush=True)
    while alive(pid):
        time.sleep(POLL)
    owner_path(lockfile).unlink(missing_ok=True)   # still under the lock: the next holder writes its own
    os.close(fd)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("hold", "held") or not argv[2].isdigit():
        print("usage: meshlock.py hold|held <lockfile> <pid>", file=sys.stderr)
        return 2
    lockfile, pid = Path(argv[1]), int(argv[2])
    if argv[0] == "hold":
        return hold(lockfile, pid)
    return 0 if held_by(lockfile, pid) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
