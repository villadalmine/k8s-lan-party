"""The live page (docs/LIVE-PAGE.md, #113): the streamer pushes the house match's frames with its own token, anyone reads the last one.
LV01 only the stream token pushes, checked before the body is read; LV02 only a JPEG of bounded size and picture; LV03 nothing on disk;
LV04 the per-origin and global read limits; LV06 offline after a quiet while, on the streamer's word, and when the operator closes the
page; LV07 no visitor input. Through a real server, as poc/test_page.py does.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "page"))
import live as livemod  # noqa: E402
import page as pagemod  # noqa: E402

STREAM = "stream-token-for-tests-0123456789abcdef"
PULLER = "puller-token-for-tests-0123456789abcdef"


def jpeg(w: int = 320, h: int = 200, fill: bytes = b"x" * 100) -> bytes:
    """A JPEG as the page reads it: SOI, a baseline frame header with the picture's size, some bytes, EOI."""
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xda\x00\x02" + fill + b"\xff\xd9"


class JpegSize(unittest.TestCase):
    def test_reads_the_size_and_refuses_what_is_not_a_jpeg(self):
        self.assertEqual(livemod.jpeg_size(jpeg(320, 200)), (320, 200))
        for bad in (b"", b"GIF89a....", b"\xff\xd8no end", jpeg()[:-2], b"\xff\xd8\xff\xda\x00\x02xx\xff\xd9", b"\xff\xd8\xff\xc0\x00\xff\xff\xd9"):
            self.assertIsNone(livemod.jpeg_size(bad), bad[:12])


class LivePage(unittest.TestCase):
    cfg: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="live-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = [1000.0]
        cfg = {"socket_timeout_seconds": 2, "access_log": 0, "puller_stale_seconds": 10 ** 8, **self.cfg}
        self.page = pagemod.Page(self.tmp / "page", cfg)
        self.page.live.clock = lambda: self.t[0]
        (self.tmp / "page" / "stream.sha256").write_text(hashlib.sha256(STREAM.encode()).hexdigest() + "\n")
        (self.tmp / "page" / "puller.sha256").write_text(hashlib.sha256(PULLER.encode()).hexdigest() + "\n")
        self.srv = pagemod.make_server(self.page, "127.0.0.1:0")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def http(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def push(self, data=None, token=STREAM, players="3", ctype="image/jpeg"):
        h = {"Content-Type": ctype, "X-Live-Players": players}
        if token is not None:
            h["Authorization"] = "Bearer " + token
        return self.http("POST", "/api/v1/live/frame", jpeg() if data is None else data, h)

    def status(self):
        import json
        return json.loads(self.http("GET", "/api/v1/live")[2])

    def test_lv01_only_the_stream_token_pushes(self):
        self.assertEqual(self.push(token=None)[0], 401)
        self.assertEqual(self.push(token=PULLER)[0], 401)      # the puller's token is not the streamer's: one power each
        self.assertEqual(self.push(token=STREAM)[0], 204)
        self.assertFalse(self.status()["live"] is False)
        os.remove(self.tmp / "page" / "stream.sha256")
        self.assertEqual(self.push()[0], 503)                  # no streamer set up: nobody pushes

    def test_lv01_the_token_is_checked_before_the_body_is_read(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/v1/live/frame")
        c.putheader("Content-Type", "image/jpeg")
        c.putheader("Content-Length", "200000")   # announced, never sent: an unauthenticated client must get its answer anyway
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 401)
        c.close()

    def test_lv01_wrong_tokens_are_limited(self):
        for _ in range(20):
            self.push(token="wrong-token-0123456789")
        self.assertEqual(self.push(token=STREAM)[0], 429)       # this origin is locked out for a while, even with the right token

    def test_lv02_only_a_bounded_jpeg(self):
        self.assertEqual(self.push(b"not a picture")[0], 400)
        self.assertEqual(self.push(jpeg(1920, 1080))[0], 400)
        self.assertEqual(self.push(ctype="application/json")[0], 415)
        self.assertEqual(self.push(jpeg(fill=b"x" * 300000))[0], 413)
        self.t[0] += 1
        self.assertEqual(self.push(jpeg(640, 400))[0], 204)

    def test_lv03_nothing_on_disk(self):
        before = sorted(p.name for p in (self.tmp / "page").rglob("*"))
        self.assertEqual(self.push(jpeg(fill=b"SECRET-FRAME-BYTES"))[0], 204)
        self.assertEqual(self.http("GET", "/api/v1/live/frame")[0], 200)
        self.assertEqual(sorted(p.name for p in (self.tmp / "page").rglob("*")), before)
        for f in (self.tmp / "page").rglob("*"):
            if f.is_file():
                self.assertNotIn(b"SECRET-FRAME-BYTES", f.read_bytes(), f)

    def test_viewers_read_the_last_frame_and_the_page_says_so(self):
        self.assertEqual(self.http("GET", "/api/v1/live/frame")[0], 404)        # offline
        self.assertEqual(self.status(), {"api": pagemod.API, "live": False, "since": None, "players": 0, "viewers": 0})
        self.push(jpeg(fill=b"frame-one"))
        status, headers, body = self.http("GET", "/api/v1/live/frame")
        self.assertEqual((status, headers["Content-Type"], headers["Cache-Control"]), (200, "image/jpeg", "no-store"))
        self.assertIn(b"frame-one", body)
        s = self.status()
        self.assertEqual((s["live"], s["players"], s["viewers"]), (True, 3, 1))
        status, headers, body = self.http("GET", "/live")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"<script>", body)                                      # no inline script
        self.assertIn(b"This match is broadcast", body)                          # LV05: said on the page
        self.assertIn(b"/#ask", body)                                            # join and play: the ask flow
        self.assertIn(b"href=/live", self.http("GET", "/")[2])                   # linked from the public page

    def test_lv04_reads_are_limited_per_origin_and_in_total(self):
        self.push()
        codes = [self.http("GET", "/api/v1/live/frame")[0] for _ in range(6)]
        self.assertEqual(codes[:4], [200] * 4)
        self.assertEqual(codes[4], 429)
        self.t[0] += 1
        self.page.live.cfg["live_reads_total"] = 2
        live = self.page.live
        live.read("a")
        live.read("b")
        with self.assertRaises(livemod.LiveRefusal) as e:
            live.read("c")
        self.assertEqual((e.exception.status, e.exception.error), (429, "busy"))

    def test_lv06_offline_when_quiet_on_the_streamers_word_or_closed(self):
        self.push()
        self.t[0] += 16
        self.assertFalse(self.status()["live"])                                   # 15 s without a frame
        self.assertEqual(self.http("GET", "/api/v1/live/frame")[0], 404)
        self.push()
        self.assertTrue(self.status()["live"])
        h = {"Authorization": "Bearer " + STREAM, "Content-Type": "image/jpeg", "Content-Length": "0"}
        self.assertEqual(self.http("POST", "/api/v1/live/offline", b"", {"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.http("POST", "/api/v1/live/offline", b"", h)[0], 204)
        self.assertFalse(self.status()["live"])
        self.t[0] += 1
        self.push()
        (self.tmp / "page" / "closed").touch()                                    # the operator closed the page
        self.assertFalse(self.status()["live"])

    def test_lv07_no_visitor_input(self):
        for path in ("/live", "/api/v1/live"):
            self.assertEqual(self.http("POST", path, b"{}", {"Content-Type": "application/json"})[0], 405)
        for method in ("PUT", "DELETE"):
            self.assertEqual(self.http(method, "/api/v1/live/frame")[0], 405)


if __name__ == "__main__":
    unittest.main()
