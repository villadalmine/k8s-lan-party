"""The streamer (docs/LIVE-PAGE.md L3, #113): LV06 panic makes it say offline and send nothing; LV10 its token only pushes pictures, and
a refused token stops it; the rate never passes 2 frames a second; a frame the spectator cannot give is skipped, never invented. End to
end against a real page server: what it pushes is what viewers read."""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "page"))
import page as pagemod  # noqa: E402
import streamer as st  # noqa: E402
import watcher  # noqa: E402
from test_live_page import jpeg  # noqa: E402

TOKEN = "stream-token-for-tests-0123456789abcdef"


class Fake:
    def __init__(self):
        self.posts, self.gate_says, self.frames = [], None, [jpeg(fill=b"f1"), jpeg(fill=b"f2")]
        self.code = 204

    def fetch(self):
        if not self.frames:
            raise watcher.NoFrame("none")
        return self.frames.pop(0)

    def post(self, path, body, players):
        self.posts.append((path, body, players))
        return self.code


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="streamer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "tok").write_text(TOKEN + "\n")
        self.t = [0.0]
        self.sleeps = []

    def make(self, fake, url="http://127.0.0.1:1", fps=2.0, players=2, **kw):
        a = Namespace(url=url, token_file=str(self.tmp / "tok"), cacert=None, mesh="/nonexistent/mesh", state=str(self.tmp),
                      fps=fps, check=10.0, once=False)

        def sleep(s):
            self.sleeps.append(s)
            self.t[0] += s
        return st.Streamer(a, fetch=fake.fetch if fake else None, gate=lambda: fake.gate_says if fake else None,
                           players=lambda: players, post=fake.post if fake else None, sleep=sleep, clock=lambda: self.t[0], **kw)


class Streamer(Base):
    def test_pushes_frames_with_the_player_count(self):
        f = Fake()
        s = self.make(f)
        s.run(once=True)
        s.run(once=True)
        self.assertEqual(f.posts, [("/api/v1/live/frame", jpeg(fill=b"f1"), 2), ("/api/v1/live/frame", jpeg(fill=b"f2"), 2)])

    def test_a_missing_frame_is_skipped(self):
        f = Fake()
        f.frames = []
        s = self.make(f)
        s.run(once=True)
        self.assertEqual((f.posts, s.failed), ([], 1))

    def test_lv06_panic_says_offline_once_and_sends_nothing(self):
        f = Fake()
        f.gate_says = "panic"
        s = self.make(f)
        s.run(once=True)
        self.t[0] += 11
        s.run(once=True)
        self.assertEqual([p for p, _, _ in f.posts], ["/api/v1/live/offline"])   # told once, no frame
        f.gate_says = None
        self.t[0] += 11
        s.run(once=True)
        self.assertEqual([p for p, _, _ in f.posts], ["/api/v1/live/offline", "/api/v1/live/frame"])

    def test_the_live_switch_holds_whoever_started_the_streamer(self):
        s = self.make(None)
        (self.tmp / "live.off").write_text("")
        self.assertEqual(s._gate(), "the live switch is off", "read before the engine: it holds while the engine is busy or down")
        (self.tmp / "live.off").unlink()
        self.assertEqual(s._gate(), "the host's status could not be read")

    def test_lv10_a_refused_token_stops_it(self):
        f = Fake()
        f.code = 401
        self.assertEqual(self.make(f).run(), st.REFUSED)

    def test_never_more_than_two_frames_a_second(self):
        f = Fake()
        f.frames = [jpeg()] * 5
        s = self.make(f, fps=30)
        f.code = 204
        calls = []
        s.step = lambda: calls.append(1) or (len(calls) < 5 or (_ for _ in ()).throw(st.Stop("enough")))
        s.run()
        self.assertTrue(self.sleeps and min(self.sleeps) >= 0.5, self.sleeps)

    def test_no_clear_text_url_but_the_loopback(self):
        args = ["run", "--token-file", str(self.tmp / "tok"), "--once"]
        self.assertEqual(st.main([*args, "--url", "http://page.example.org"]), 2)

    def test_the_token_file_must_hold_a_token(self):
        (self.tmp / "tok").write_text("short\n")
        with self.assertRaises(SystemExit):
            self.make(Fake())


class EndToEnd(Base):
    def test_what_it_pushes_is_what_viewers_read_and_offline_reaches_them(self):
        page = pagemod.Page(self.tmp / "page", {"access_log": 0, "socket_timeout_seconds": 2})
        (self.tmp / "page" / "stream.sha256").write_text(hashlib.sha256(TOKEN.encode()).hexdigest() + "\n")
        srv = pagemod.make_server(page, "127.0.0.1:0")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        f = Fake()
        s = self.make(None, url=url, players=1)
        s.fetch, s.gate = f.fetch, (lambda: f.gate_says)
        s.run(once=True)
        with urllib.request.urlopen(url + "/api/v1/live/frame") as r:
            self.assertEqual(r.read(), jpeg(fill=b"f1"))
        f.gate_says = "panic"
        self.t[0] += 11
        s.run(once=True)
        with urllib.request.urlopen(url + "/api/v1/live") as r:
            self.assertIn(b'"live": false', r.read())
        (self.tmp / "tok").write_text("another-token-that-the-page-does-not-know-0123\n")
        f.gate_says = None
        self.t[0] += 11
        self.assertEqual(self.make(None, url=url).run(once=True), 0)   # its fetch fails (no cluster): nothing pushed, no crash


if __name__ == "__main__":
    unittest.main()
