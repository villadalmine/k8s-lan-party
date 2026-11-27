"""The live page's state (docs/LIVE-PAGE.md, #113): the last frame of the house match, in memory only, and the limits around it.

The streamer on the host pushes a JPEG with its own token (the stream token: the page keeps its SHA-256 in the control directory as
`stream.sha256`, like the puller's); anyone reads the last one. Nothing is written to disk (LV03), a visitor has no input path (LV07),
and the limits of L5 keep the viewers from exhausting the server (LV04). Standard library only, thread-safe: the page's server runs a
thread per request.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import deque
from pathlib import Path

LIVE_DEFAULTS = {
    "live_max_frame_bytes": 262144,     # one pushed JPEG (a 320x200 frame is about 10 KB)
    "live_max_width": 640,
    "live_max_height": 400,
    "live_stale_seconds": 15,           # no frame for this long: offline
    "live_push_per_second": 4,          # the streamer's POSTs
    "live_reads_per_origin": 4,         # a viewer's frame requests a second
    "live_reads_total": 120,            # every frame answer a second, all viewers together (about 60 viewers at 2 a second)
    "live_viewer_window_seconds": 10,   # a viewer counts while it asked within this
    "live_max_failures": 20,            # wrong stream tokens per origin in live_fail_window_seconds
    "live_fail_window_seconds": 600,
}


class LiveRefusal(Exception):
    def __init__(self, status: int, error: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.error, self.message, self.retry_after = status, error, message, retry_after


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a baseline or progressive JPEG, or None when it is not one: SOI first, EOI last, a SOF before the scan."""
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 7:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        if marker == 0xDA:   # the scan began with no frame header
            return None
        i += 2 + length
    return None


class Live:
    def __init__(self, control: Path, cfg: dict | None = None, clock=None):
        self.control = Path(control)
        self.cfg = {**LIVE_DEFAULTS, **(cfg or {})}
        self.clock = clock or time.monotonic
        self.wall = time.time
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.frame_at = 0.0       # monotonic
        self.since: int | None = None   # wall clock of the first frame of this broadcast
        self.players = 0
        self.offline_said = False
        self.pushes: deque = deque()
        self.reads: deque = deque()
        self.per_origin: dict[str, deque] = {}
        self.viewers: dict[str, float] = {}
        self.fails: dict[str, deque] = {}

    # --- the streamer ---------------------------------------------------------------------------------------------------
    def stream_hash(self) -> str | None:
        try:
            h = (self.control / "stream.sha256").read_text().strip()
        except OSError:
            return None
        return h if re.fullmatch(r"[0-9a-f]{64}", h) else None

    @staticmethod
    def _window(q: deque, now: float, seconds: float) -> deque:
        while q and now - q[0] >= seconds:
            q.popleft()
        return q

    def auth(self, ident: str, header: str | None) -> None:
        """The stream token, before any body is read (LV01); wrong tries are counted per origin, in memory."""
        now = self.clock()
        with self.lock:
            want = self.stream_hash()
            if want is None:
                raise LiveRefusal(503, "no-stream", "no streamer is set up on this page")
            token = header[7:] if isinstance(header, str) and header.startswith("Bearer ") and len(header) <= 200 else ""
            good = bool(token) and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), want)
            if not good:   # the right token is never locked out by others' wrong tries from the same address (CGNAT; PR #117 review)
                window = self.cfg["live_fail_window_seconds"]
                fails = self._window(self.fails.get(ident, deque()), now, window)
                if len(fails) >= self.cfg["live_max_failures"]:
                    raise LiveRefusal(429, "too-many-tries", "too many failed tries from your address", int(window))
                if len(self.fails) >= 10000:   # bounded memory whatever the number of origins: forget the expired ones, then the oldest
                    self.fails = {k: q for k, q in self.fails.items() if q and now - q[-1] < window}
                    while len(self.fails) >= 10000:
                        self.fails.pop(next(iter(self.fails)))
                fails.append(now)
                self.fails[ident] = fails
                raise LiveRefusal(401, "unauthorized", "not the streamer")
            if len(self._window(self.pushes, now, 1)) >= self.cfg["live_push_per_second"]:
                raise LiveRefusal(429, "rate", "at most %d frames a second" % self.cfg["live_push_per_second"], 1)
            self.pushes.append(now)

    def push(self, data: bytes, players: str | None) -> None:
        size = jpeg_size(data)
        if size is None:
            raise LiveRefusal(400, "not-a-jpeg", "the body is not one JPEG picture")
        if size[0] > self.cfg["live_max_width"] or size[1] > self.cfg["live_max_height"]:
            raise LiveRefusal(400, "too-big-picture", "at most %dx%d" % (self.cfg["live_max_width"], self.cfg["live_max_height"]))
        n = int(players) if isinstance(players, str) and re.fullmatch(r"[0-9]{1,2}", players) else 0
        now = self.clock()
        with self.lock:
            if self.frame is None or now - self.frame_at > self.cfg["live_stale_seconds"] or self.offline_said:
                self.since = int(self.wall())
            self.frame, self.frame_at, self.players, self.offline_said = data, now, n, False

    def offline(self) -> None:
        with self.lock:
            self.frame, self.offline_said, self.since, self.players = None, True, None, 0

    # --- the viewers ----------------------------------------------------------------------------------------------------
    def _live(self, now: float, closed: bool) -> bool:
        return (not closed and self.frame is not None and not self.offline_said
                and now - self.frame_at <= self.cfg["live_stale_seconds"])

    def status(self, closed: bool = False) -> dict:
        now = self.clock()
        with self.lock:
            live = self._live(now, closed)
            w = self.cfg["live_viewer_window_seconds"]
            self.viewers = {k: t for k, t in self.viewers.items() if now - t < w}
            return {"live": live, "since": self.since if live else None, "players": self.players if live else 0,
                    "viewers": len(self.viewers) if live else 0}

    def read(self, ident: str, closed: bool = False) -> bytes:
        now = self.clock()
        with self.lock:
            mine = self._window(self.per_origin.setdefault(ident, deque()), now, 1)
            if len(mine) >= self.cfg["live_reads_per_origin"]:
                raise LiveRefusal(429, "rate", "at most %d frames a second" % self.cfg["live_reads_per_origin"], 1)
            if len(self._window(self.reads, now, 1)) >= self.cfg["live_reads_total"]:
                raise LiveRefusal(429, "busy", "too many viewers right now; try again in a moment", 2)
            mine.append(now)
            self.reads.append(now)
            if len(self.per_origin) > 10000:   # a flood of origins must not grow the memory: forget the idle ones
                self.per_origin = {k: q for k, q in self.per_origin.items() if q and now - q[-1] < 1}
            if not self._live(now, closed):
                raise LiveRefusal(404, "offline", "the house match is not on the air right now")
            self.viewers[ident] = now
            if len(self.viewers) > 10000:   # bounded even if nobody asks the status
                w = self.cfg["live_viewer_window_seconds"]
                self.viewers = {k: t for k, t in self.viewers.items() if now - t < w}
            return self.frame
