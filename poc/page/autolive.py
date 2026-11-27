#!/usr/bin/env python3
"""The live page on its own (#119, v0.8): whenever the host's cluster is up, the house match is on the air; when it is down, the page
says offline. Nobody starts anything by hand.

It runs on the HOST, in the foreground (a systemd unit, `emesh-live-auto.service`, or `mesh host live auto`), and every --check seconds:

- asks the cluster's API server whether it is ready (`kc get --raw /readyz`);
- when it is, and the operator has made the house once (`mesh host house init`: the namespace `house` exists), makes sure the house's
  match runs: if one of its four pods is missing it runs `mesh host house start` (which only creates what is missing). It never makes
  the house and never opens it: broadcasting players stays the operator's choice (`house open`) and each player's (their accept);
- keeps the streamer (`streamer.py run`) running as its child, and starts it again if it stops (with a growing pause: 5 s to 5 min),
  unless the live switch is off (`mesh host live off`, or the console's `live-off`): then it stops the streamer and keeps it stopped,
  while the house match and the demo keep running, until `live on`. Its pid is in the same file `mesh host live status` reads.
  The streamer alone already says offline on panic, and the page goes offline by itself after 15 s without a frame, so a cluster that
  goes down takes the match off the air, and one that comes back puts it back.

A streamer that the page refused (exit 1: a wrong stream token) is not started again: that needs the operator (page-server.sh
stream-token). SIGTERM stops the streamer (which tells the page offline) and then this.

    autolive.py --url https://page.example.org [--cacert FILE] [--mesh poc/mesh] [--state poc/.state] [--check 30] [--once] run

Exit codes: 0 stopped (SIGTERM, or --once done); 1 the page refused the stream token; 2 bad arguments or no stream token.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOUSE = "house"
HOUSE_PODS = ("doom-server", "doom-bot-a", "doom-bot-b", "doom-watch-house")
BACKOFF_MIN, BACKOFF_MAX = 5.0, 300.0
HEALTHY_SECONDS = 60.0   # a streamer that lived this long earns the short pause again


class Auto:
    def __init__(self, a, run=None, spawn=None, sleep=time.sleep, clock=time.monotonic, say=None):
        self.a = a
        self.run_cmd = run or self._run
        self.spawn = spawn or self._spawn
        self.sleep, self.clock = sleep, clock
        self.say = say or (lambda m: print(f"autolive: {m}", file=sys.stderr, flush=True))
        self.child = None
        self.backoff = BACKOFF_MIN
        self.next_start = 0.0
        self.refused = False
        self.started_at = 0.0
        self.last = None   # the last thing said about the cluster, so the log says each change once

    # --- the host -------------------------------------------------------------------------------------------------------
    def _env(self) -> dict:
        keep = {k: v for k, v in os.environ.items() if k.startswith("EMESH_") or k in ("POOL_CONFIG", "RUNTIME", "INCUS_PROJECT", "HOME")}
        return {**keep, "EMESH_STATE": self.a.state, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

    def _run(self, argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self._env())

    def _spawn(self) -> subprocess.Popen:
        argv = [sys.executable, str(HERE / "streamer.py"), "run", "--url", self.a.url, "--token-file", self.token_file(),
                "--mesh", self.a.mesh, "--state", self.a.state] + (["--cacert", self.a.cacert] if self.a.cacert else [])
        return subprocess.Popen(argv, env=self._env())   # its stderr is this unit's: one journal

    def kc(self) -> str:
        return str(Path(self.a.mesh).parent / "kc")

    def house_dir(self) -> Path:
        return Path(self.a.state) / "house"

    def switched_off(self) -> bool:
        return (self.house_dir() / "live.off").exists()

    def token_file(self) -> str:
        return str(Path(self.a.state) / "page" / "stream.token")

    def cluster_up(self) -> bool:
        try:
            r = self.run_cmd([self.kc(), "get", "--raw", "/readyz"], timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and r.stdout.strip() == "ok"

    def house_pods(self) -> list[str] | None:
        """The house's pods by name, or None when there is no house (the operator never made it)."""
        try:
            r = self.run_cmd([self.kc(), "get", "pods", "-n", HOUSE, "-o", "json"], timeout=30)
            ns = self.run_cmd([self.kc(), "get", "namespace", HOUSE, "--ignore-not-found", "-o", "name"], timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if ns.returncode == 0 and not ns.stdout.strip():
            return None
        try:
            items = json.loads(r.stdout).get("items", []) if r.returncode == 0 else []
        except ValueError:
            items = []
        return [i.get("metadata", {}).get("name", "") for i in items if isinstance(i, dict)]

    # --- one pass -------------------------------------------------------------------------------------------------------
    def tell(self, what: str) -> None:
        if what != self.last:
            self.say(what)
            self.last = what

    def ensure_house(self) -> None:
        pods = self.house_pods()
        if pods is None:
            self.tell("the cluster is up, but there is no house (the operator runs mesh host house init once); nothing to show")
            return
        missing = [p for p in HOUSE_PODS if p not in pods]
        if not missing:
            self.tell("the cluster is up and the house match runs")
            return
        self.say(f"the house misses {', '.join(missing)}: mesh host house start")
        try:
            r = self.run_cmd([self.a.mesh, "host", "house", "start"], timeout=600)
            self.say((r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else f"house start: exit {r.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            self.say(f"house start failed: {e}")

    def ensure_streamer(self) -> None:
        if self.refused:
            return
        if self.switched_off():
            if self.child is not None:
                self.stop()
            self.tell("the live switch is off: no streamer (mesh host live on, or the console's live-on)")
            self.next_start, self.backoff = 0.0, BACKOFF_MIN
            return
        if self.child is not None:
            code = self.child.poll()
            if code is None:
                return
            self.child = None
            if code == 1:
                self.refused = True
                self.say("the page refused the stream token: not starting the streamer again (page-server.sh stream-token)")
                return
            self.say(f"the streamer stopped (exit {code}); again in {int(self.backoff)} s")
            self.next_start = self.clock() + self.backoff
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)
            return
        if self.clock() < self.next_start:
            return
        self.child = self.spawn()
        self.started_at = self.clock()
        self.write_pid(self.child.pid)
        self.say("the streamer started")

    def step(self) -> None:
        if self.cluster_up():
            self.ensure_house()
        else:
            self.tell("the cluster does not answer: the page goes offline by itself (no frames); waiting")
        self.ensure_streamer()
        if self.child is not None and self.child.poll() is None and self.clock() - self.started_at >= HEALTHY_SECONDS:
            self.backoff = BACKOFF_MIN

    def write_pid(self, pid: int | None) -> None:
        f = self.house_dir() / "streamer.pid"
        try:
            if pid is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                f.write_text(f"{pid}\n")
        except OSError:
            pass

    def stop(self) -> None:
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()   # the streamer tells the page offline on SIGTERM
            try:
                self.child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.child.kill()
        if self.child is not None:
            self.write_pid(None)
        self.child = None

    def loop(self, once: bool = False) -> int:
        try:
            while True:
                self.step()
                if once:
                    return 0
                if self.refused:
                    return 1
                self.sleep(self.a.check)
        finally:
            self.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--url", required=True)
    ap.add_argument("--cacert")
    ap.add_argument("--mesh", default=str(HERE.parent / "mesh"))
    ap.add_argument("--state", default=str(HERE.parent / ".state"))
    ap.add_argument("--check", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?", a.url):
        print("--url: http(s)://host[:port][/path]", file=sys.stderr)
        return 2
    if a.url.startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:|/|$)", a.url):
        print("--url: https, except on this machine's loopback (the stream token never crosses a network in clear)", file=sys.stderr)
        return 2
    if a.check < 5:
        print("--check: at least 5 seconds", file=sys.stderr)
        return 2
    auto = Auto(a)
    if not Path(auto.token_file()).is_file():
        print(f"no stream token at {auto.token_file()}: poc/page/page-server.sh stream-token --ssh ... makes it", file=sys.stderr)
        return 2

    def bye(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    return auto.loop(once=a.once)


if __name__ == "__main__":
    sys.exit(main())
