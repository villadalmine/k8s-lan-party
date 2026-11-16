#!/usr/bin/env python3
"""Authenticated, low-rate JPEG stream for one local Odamex spectator pod.

The caller is the player's daemon on the same Kubernetes node. This endpoint
is reached by Pod IP, never a Service, hostPort or Kubernetes port-forward.
"""
from __future__ import annotations

import hmac
import http.server
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

TOKEN = os.environ.get("WATCH_TOKEN", "")
PORT = int(os.environ.get("WATCH_PORT", "17777"))
FPS = max(1, min(int(os.environ.get("WATCH_FPS", "3")), 5))
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
