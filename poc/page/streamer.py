#!/usr/bin/env python3
"""The streamer: the host's side of the live page (docs/LIVE-PAGE.md L3, #113). It runs on the host, next to `mesh`, and only ever
connects **out** to the page, like the puller.

It reads the house match's spectator (`doom-watch-house` in the namespace `house`, on the host's own machine) the way the host console
reads its views (`watcher.Tap`: one `kubectl exec -i`, the pod's secret on stdin) and pushes each frame to the page with the **stream
token** (`POST /api/v1/live/frame`, `Authorization: Bearer`), at most --fps a second. It never pulls anything from the page and the page
never connects to it. Every --check seconds it asks `mesh host admission-status --json`: with panic on (or that read failing) it tells
the page `offline` (`POST /api/v1/live/offline`) and sends nothing until that changes (LV06). The live switch does the same: while
`<state>/live.off` exists (`mesh host live off`, the console's `live-off`) it is offline, whoever started it (#119). The stream token can only push pictures
(LV10): it is not the puller's, and the page keeps only its SHA-256 (`page.py set-stream-hash`).

    streamer.py --url https://page.example.org --token-file FILE [--cacert FILE] [--mesh poc/mesh] [--state poc/.state]
                [--fps 2] [--check 10] [--once] run

Exit codes: 0 stopped (SIGTERM, or --once done); 2 bad arguments; 4 the page refused the token (fix it, then start again). Not 1: that
is what Python exits with on any crash, and autolive.py starts a crashed streamer again but never a refused one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_OFF = "live.off"   # the live switch, in the state directory (not under house/: it outlives `house down`)
REFUSED = 4
sys.path.insert(0, str(HERE.parent))
import watcher  # noqa: E402

HOUSE, POD = "house", "doom-watch-house"


class Stop(Exception):
    pass


class Streamer:
    def __init__(self, a, fetch=None, gate=None, players=None, post=None, sleep=time.sleep, clock=time.monotonic):
        self.a = a
        token = Path(a.token_file).read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9._~-]{32,128}", token):
            raise SystemExit("the stream token file must hold one token (32 to 128 characters: letters, digits, . _ ~ -)")
        self.token = token
        self.fetch = fetch or self._tap_fetch
        self.gate = gate or self._gate
        self.players = players or self._players
        self.post = post or self._post
        self.sleep, self.clock = sleep, clock
        self.tap = None
        self.on_air = None     # None: not said yet; True: frames going; False: the page was told offline
        self.n_players = 0
        self.sent = self.failed = 0

    # --- the host -------------------------------------------------------------------------------------------------------
    def _mesh(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        keep = {k: v for k, v in os.environ.items() if k.startswith("EMESH_") or k in ("POOL_CONFIG", "RUNTIME", "INCUS_PROJECT", "HOME")}
        return subprocess.run([self.a.mesh, "host", *args], capture_output=True, text=True, timeout=timeout,
                              env={**keep, "EMESH_STATE": self.a.state, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})

    def _gate(self) -> str | None:
        """None when the house may be on the air; else the reason it may not (the live switch, panic, or the host's tools fail)."""
        if (Path(self.a.state) / LIVE_OFF).exists():   # read first and locally: off holds even when the engine is busy or down
            return "the live switch is off"
        try:
            r = self._mesh("admission-status", "--json")
            st = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            st = None
        if not isinstance(st, dict):
            return "the host's status could not be read"
        if st.get("panic"):
            return "panic"
        return None

    def _players(self) -> int:
        """How many players are in the house match: its active parties (no names leave the host)."""
        try:
            r = self._mesh("party", "list", "--read-only", "--json")
            rows = json.loads(r.stdout) if r.returncode == 0 else []
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0
        return sum(1 for p in rows if isinstance(p, dict) and p.get("player") == HOUSE and str(p.get("status", "")).startswith("active"))

    def _tap_fetch(self) -> bytes:
        if self.tap is None:
            self.tap = watcher.Tap(str(Path(self.a.mesh).parent / "kc"), POD, Path(self.a.state) / "house" / "spectator.token", HOUSE)
        return self.tap.fetch()

    # --- the page -------------------------------------------------------------------------------------------------------
    def _post(self, path: str, body: bytes, players: int) -> int:
        req = urllib.request.Request(self.a.url.rstrip("/") + path, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "image/jpeg",
                                              "X-Live-Players": str(min(players, 99))})
        ctx = ssl.create_default_context(cafile=self.a.cacert) if self.a.url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError):
            return 0

    def offline(self, why: str) -> None:
        if self.on_air is not False:
            code = self.post("/api/v1/live/offline", b"", 0)
            if code == 401:
                raise Stop("the page refused the stream token")
            print(f"streamer: offline ({why})", file=sys.stderr)
            self.on_air = False

    # --- the loop -------------------------------------------------------------------------------------------------------
    def step(self) -> None:
        """One frame: read it from the house's spectator and push it. A frame the spectator cannot give is skipped, never invented."""
        try:
            frame = self.fetch()
        except (watcher.NoFrame, RuntimeError, OSError, ValueError):
            self.failed += 1
            return
        code = self.post("/api/v1/live/frame", frame, self.n_players)
        if code == 401:
            raise Stop("the page refused the stream token")
        if code == 204:
            self.sent += 1
            if self.on_air is not True:
                print("streamer: on the air", file=sys.stderr)
            self.on_air = True
        else:
            self.failed += 1

    def run(self, once: bool = False) -> int:
        period = 1.0 / max(0.2, min(2.0, self.a.fps))   # L5: never more than 2 frames a second
        next_check = 0.0
        blocked = None
        try:
            while True:
                now = self.clock()
                if now >= next_check:
                    blocked = self.gate()
                    self.n_players = self.players() if blocked is None else 0
                    next_check = now + self.a.check
                    if blocked:
                        self.offline(blocked)
                if not blocked:
                    self.step()
                if once:
                    return 0
                self.sleep(period if not blocked else min(self.a.check, 5))
        except Stop as e:
            print(f"streamer: {e}", file=sys.stderr)
            return REFUSED
        finally:
            if self.tap is not None:
                self.tap.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--cacert")
    ap.add_argument("--mesh", default=str(HERE.parent / "mesh"))
    ap.add_argument("--state", default=str(HERE.parent / ".state"))
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--check", type=float, default=10.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?", a.url):
        print("--url: http(s)://host[:port][/path]", file=sys.stderr)
        return 2
    if a.url.startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:|/|$)", a.url):
        print("--url: https, except on this machine's loopback (the stream token never crosses a network in clear)", file=sys.stderr)
        return 2
    s = Streamer(a)

    def bye(*_):
        try:
            s.offline("the streamer stopped")
        except Stop:
            pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, bye)
    return s.run(once=a.once)


if __name__ == "__main__":
    sys.exit(main())
