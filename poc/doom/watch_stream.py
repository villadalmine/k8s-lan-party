#!/usr/bin/env python3
"""Authenticated, low-rate JPEG stream for one local Odamex spectator pod, and with PLAY_INPUT=1 the keys of a player's own game.

The caller is the player's daemon on the same Kubernetes node. This endpoint
is reached by Pod IP, never a Service, hostPort or Kubernetes port-forward.

POST /input (PLAY_INPUT=1 only, issue #102): {"down": [...], "up": [...]} with browser key names from KEYS. The keys go to this pod's
own virtual display (:99, fixed) with xdotool and nowhere else (WB07). No console key, no Enter, no text: a browser tab cannot type a
command or a chat line into the game. Same per-pod secret as the frames (WB08).
"""
from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

TOKEN = os.environ.get("WATCH_TOKEN", "")
PORT = int(os.environ.get("WATCH_PORT", "17777"))
PLAY = os.environ.get("PLAY_INPUT", "") == "1"
FPS = max(1, min(int(os.environ.get("WATCH_FPS", "3")), 12 if PLAY else 5))
# the browser's KeyboardEvent.key -> the X keysym; Doom's default controls only (Odamex 12.3.0): move, turn, fire, use, strafe, run, weapons
KEYS = {"ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right", "Control": "Control_L", " ": "space",
        "Alt": "Alt_L", "Shift": "Shift_L", ",": "comma", ".": "period", **{str(n): str(n) for n in range(1, 8)}}
MAX_INPUT = 512
INPUTS_PER_SECOND = 60
TOKEN_RE = re.compile(r"[a-f0-9]{64}")
MAX_FRAME = 2 * 1024 * 1024


class Frames:
    def __init__(self):
        self.condition = threading.Condition()
        self.value: bytes | None = None
        self.sequence = 0

    def publish(self, frame: bytes) -> None:
        if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9") or len(frame) > MAX_FRAME:
            return
        with self.condition:
            self.value = frame
            self.sequence += 1
            self.condition.notify_all()

    def after(self, sequence: int, timeout: float) -> tuple[int, bytes | None]:
        with self.condition:
            self.condition.wait_for(lambda: self.sequence > sequence, timeout)
            return self.sequence, self.value


frames = Frames()


def capture() -> None:
    """Keep one bounded-rate MJPEG capture alive; discard malformed/oversized output."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab", "-framerate", str(FPS),
               "-video_size", "320x200", "-i", ":99.0", "-an", "-vf", f"fps={FPS}", "-q:v", "8",
               "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    while True:
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=8192)
            assert proc.stdout is not None
            buffer = bytearray()
            while True:
                chunk = proc.stdout.read1(8192)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        buffer[:] = buffer[-1:]
                        break
                    if start:
                        del buffer[:start]
                    end = buffer.find(b"\xff\xd9", 2)
                    if end < 0:
                        if len(buffer) > MAX_FRAME:
                            buffer.clear()
                        break
                    frames.publish(bytes(buffer[:end + 2]))
                    del buffer[:end + 2]
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except (NameError, OSError, AttributeError):
                pass
        time.sleep(1)


class Keyboard:
    """The pod's own virtual display only: DISPLAY is fixed here, never taken from the request. Counts requests per second."""

    def __init__(self, runner=subprocess.run):
        self.runner, self.lock, self.window = runner, threading.Lock(), []

    def allow(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.window = [t for t in self.window if now - t < 1] + [now]
            return len(self.window) <= INPUTS_PER_SECOND

    def press(self, down: list, up: list) -> None:
        args = []
        for k in up:
            args += ["keyup", KEYS[k]]
        for k in down:
            args += ["keydown", KEYS[k]]
        if args:
            self.runner(["xdotool", *args], env={"DISPLAY": ":99", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                        timeout=2, check=False, capture_output=True)


keyboard = Keyboard()


def parse_input(raw: bytes) -> tuple[list, list] | None:
    """The keys of one POST /input, or None when anything in it is not allowed."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) - {"down", "up"}:
        return None
    lists = [body.get("down", []), body.get("up", [])]
    if not all(isinstance(l, list) and len(l) <= 8 and all(isinstance(k, str) and k in KEYS for k in l) for l in lists):
        return None
    return lists[0], lists[1]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = ""
    sys_version = ""

    def log_message(self, _fmt: str, *_args) -> None:
        pass  # URLs and bearer secrets must never reach stdout/stderr.

    def authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return value.startswith(prefix) and hmac.compare_digest(value[len(prefix):], TOKEN)

    def send_headers(self, status: int, ctype: str, *, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_POST(self) -> None:
        path = urlsplit(self.path)
        if not PLAY or path.path != "/input" or path.query or path.fragment:
            self.send_headers(404, "text/plain; charset=utf-8", length=0)
            return
        if not self.authorized():
            self.send_headers(401, "text/plain; charset=utf-8", length=0)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        keys = parse_input(self.rfile.read(n)) if 0 < n <= MAX_INPUT else None
        if keys is None:
            self.send_headers(400, "text/plain; charset=utf-8", length=0)
            return
        if not keyboard.allow():
            self.send_headers(429, "text/plain; charset=utf-8", length=0)
            return
        keyboard.press(*keys)
        self.send_headers(204, "text/plain; charset=utf-8", length=0)

    def do_GET(self) -> None:
        path = urlsplit(self.path)
        if path.path not in ("/frame", "/stream") or path.query or path.fragment:
            self.send_headers(404, "text/plain; charset=utf-8", length=0)
            return
        if not self.authorized():
            self.send_headers(401, "text/plain; charset=utf-8", length=0)
            return
        sequence, frame = frames.after(0, 3)
        if not frame:
            self.send_headers(503, "text/plain; charset=utf-8", length=0)
            return
        if path.path == "/frame":
            self.send_headers(200, "image/jpeg", length=len(frame))
            self.wfile.write(frame)
            return
        self.send_headers(200, "multipart/x-mixed-replace; boundary=frame")
        try:
            while True:
                new_sequence, frame = frames.after(sequence, 10)
                if frame is None or new_sequence <= sequence:
                    continue
                sequence = new_sequence
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                 str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


def main() -> int:
    if not TOKEN_RE.fullmatch(TOKEN) or not 1024 < PORT < 65536:
        print("watch stream requires a 64-character lowercase hex WATCH_TOKEN and valid WATCH_PORT", flush=True)
        return 2
    threading.Thread(target=capture, name="watch-frame-capture", daemon=True).start()
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        server.daemon_threads = True
        server.serve_forever(poll_interval=0.5)
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
