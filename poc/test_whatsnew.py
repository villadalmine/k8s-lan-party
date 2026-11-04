"""What's new (issue #63): the recordings are made by script from real runs, the filter keeps secrets out of them, and the page serves
them from itself only. No network, no machines.

- every committed recording is rebuilt from its committed source and must be byte-identical (nobody edited a .cast by hand);
- every file of poc/page/whatsnew is scanned with the filter's own patterns: one leak (a public IP, a k3s token, a long base64, a claim
  code, a JWT, a key, a fingerprint) fails the test, and a planted leak proves the scan sees it;
- the vendored player matches the SHA-256 in poc/page/static/VENDOR.md;
- /whats-new is served with the page's CSP plus only 'wasm-unsafe-eval', loads nothing from another site, and each release is there.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import http.client
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEWS = HERE / "page" / "whatsnew"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "page"))
sys.path.insert(0, str(NEWS))
import news  # noqa: E402
import page as pagemod  # noqa: E402
import showcase  # noqa: E402

LEAKS = {   # one sample per thing the filter must remove (made up: none of them was ever real)
    "public ip": "joined from 81.2.69.160 via the lighthouse",
    "k3s token": "K10a1b2c3d4e5f60718293a4b5c6d7e8f9::server:0123456789abcdef",
    "jwt": "token eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJzdWIiOiJzeXN0ZW06c2EifQ.c2lnbmF0dXJl",
    "base64": "sealed: q8Jx4mN0pL2vR7tY9wZ3kB5cD1fG6hH8jK0lM2nP4rS6uV8x",
    "claim code": "Claim code: IRBH5-FJDA5-TJ3TA-VHMS7-AY2ND-E (saved)",
    "pem": "-----BEGIN PRIVATE KEY-----",
    "fingerprint": "fingerprint 3f:9a:11:0c:7d:e2:55:a0:9b:42",
    "hex": "sha256 4f6c1b2a9d8e7f60514233a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8",
    "uuid": "UID 2f1e4c7a-9b3d-4e5f-8a6b-7c8d9e0f1a2b",
    "bearer": "Authorization: Bearer Zm9vYmFyYmF6cXV4",
    "ipv6": "from 2a01:4f8:c0c:1234::1 and 2001:db8:1:2:3:4:5:6",
}
KEEP = ["10.77.0.2:6443 is the API on the mesh", "pod 10.42.9.2 on node aws-e2e", "Claim code: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-X",
        "io/v1/namespaces/kube-node-lease/leases/aws-e2e", "v1.37.1+k3s1", "2026-11-03T16:49:34Z", "<visitor-ip>"]


class Filter(unittest.TestCase):
    def test_each_leak_is_seen_and_removed(self):
        for what, line in LEAKS.items():
            with self.subTest(what):
                self.assertTrue(showcase.leaks(line), f"the scan does not see a {what}")
                self.assertEqual(showcase.leaks(showcase.redact_line(line)), [], f"the filter leaves a {what}")

    def test_what_is_not_a_secret_stays(self):
        for line in KEEP:
            self.assertEqual(showcase.redact_line(line), line)

    def test_filter_is_idempotent_and_strips_paths(self):
        once = showcase.redact_line("/home/someone/clone/poc/.state/bundles/decoy " + LEAKS["k3s token"], ["/home/someone/clone/poc/"])
        self.assertEqual(once, ".state/bundles/decoy <k3s-token>")
        self.assertEqual(showcase.redact_line(once), once)

    def test_a_planted_leak_fails_the_scan(self):
        d = Path(tempfile.mkdtemp(prefix="wn-"))
        try:
            cast = sorted(NEWS.glob("v*/*.cast"))[0].read_text().splitlines()
            for what, line in LEAKS.items():
                bad = d / "bad.cast"
                bad.write_text("\n".join(cast[:3] + [json.dumps([9.9, "o", line + "\r\n"])] + cast[3:]) + "\n")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(showcase.scan([str(bad)]), 1, what)
                self.assertIn("LEAK", out.getvalue())
        finally:
            shutil.rmtree(d)


class Recordings(unittest.TestCase):
    def test_no_leak_in_any_committed_file(self):
        files = [str(f) for f in sorted(NEWS.rglob("*")) if f.is_file() and f.suffix in (".cast", ".tsv", ".txt", ".json")]
        self.assertTrue(any(f.endswith(".cast") for f in files))
        self.assertEqual(showcase.scan(files), 0, "a recording holds a secret: run the filter (showcase.py redact) again")

    def test_casts_are_rebuilt_from_their_sources_byte_for_byte(self):
        for rel in sorted(p for p in NEWS.glob("v*") if p.is_dir()):
            d = Path(tempfile.mkdtemp(prefix="wn-"))
            try:
                shutil.copytree(rel, d / rel.name)
                shutil.copy(rel.parent / f"{rel.name}.json", d / f"{rel.name}.json")
                for c in (d / rel.name).glob("*.cast"):
                    c.unlink()
                with contextlib.redirect_stdout(io.StringIO()):
                    showcase.build(str(d / rel.name))
                for f in sorted(rel.rglob("*")):
                    if f.suffix in (".cast", ".tsv"):
                        self.assertEqual((d / rel.name / f.relative_to(rel)).read_bytes(), f.read_bytes(), f"{f} is not what make showcase builds")
            finally:
                shutil.rmtree(d)

    def test_casts_are_asciicast_v2_in_time_order(self):
        for f in sorted(NEWS.glob("v*/*.cast")):
            lines = f.read_text().splitlines()
            head = json.loads(lines[0])
            self.assertEqual((head["version"], head["width"], head["height"]), (2, showcase.COLS, showcase.ROWS))
            times = [json.loads(ln)[0] for ln in lines[1:]]
            self.assertEqual(times, sorted(times), f.name)
            self.assertTrue(all(json.loads(ln)[1] == "o" for ln in lines[1:]))

    def test_vendored_player_matches_its_pinned_hash(self):
        table = (HERE / "page" / "static" / "VENDOR.md").read_text()
        pins = dict(re.findall(r"^\| `([\w.-]+)` \|.*\| `([0-9a-f]{64})` \|$", table, re.M))
        self.assertEqual(set(pins), {"asciinema-player.min.js", "asciinema-player.css"})
        for name, want in pins.items():
            self.assertEqual(hashlib.sha256((HERE / "page" / "static" / name).read_bytes()).hexdigest(), want, name)
        self.assertIn("asciinema-player 3.17.0", (HERE / "page" / "page.py").read_text())


class Releases(unittest.TestCase):
    def test_every_release_loads_and_v0_2_0_tells_both_sides(self):
        rs = {r["version"]: r for r in news.load(NEWS)}
        self.assertIn("v0.1.0-poc", rs)
        r = rs["v0.2.0-poc"]
        self.assertEqual({c["side"] for c in r["casts"]}, {"player", "host"})
        self.assertTrue(r["story"]["player"]["steps"] and r["story"]["host"]["steps"])
        self.assertGreaterEqual(len(r["features"]), 6)

    def test_a_bad_release_stops_the_page(self):
        d = Path(tempfile.mkdtemp(prefix="wn-"))
        try:
            good = {"version": "v9.9.9", "date": "2026-01-01", "title": "t", "summary": "s", "issues": [1],
                    "release": f"{news.REPO}/releases/tag/v9.9.9"}
            for bad in ({"release": "https://evil.example/releases/tag/v9.9.9"}, {"version": "v9"}, {"issues": ["1"]},
                        {"casts": [{"file": "../x.cast", "title": "x"}]}, {"casts": [{"file": "missing.cast", "title": "x"}]}):
                (d / "v9.9.9.json").write_text(json.dumps({**good, **bad}))
                with self.assertRaises(news.NewsError, msg=str(bad)):
                    news.load(d)
        finally:
            shutil.rmtree(d)


class Served(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wn-page-"))
        cls.srv = pagemod.make_server(pagemod.Page(cls.tmp / "page", {"access_log": 0}), "127.0.0.1:0")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_whats_new_page_and_its_csp(self):
        s, h, body = self.get("/whats-new")
        self.assertEqual(s, 200)
        self.assertEqual(h["Content-Security-Policy"], pagemod.SECURITY_HEADERS["Content-Security-Policy"].replace(
            "script-src 'self'", "script-src 'self' 'wasm-unsafe-eval'"))
        text = body.decode()
        for r in news.load(NEWS):
            self.assertIn(r["version"], text)
        for src in re.findall(r'(?:src|data-cast)="?([^" >]+)', text):
            self.assertTrue(src.startswith("/"), f"loads {src} from another site")
        hrefs = re.findall(r'href="?([^" >]+)', text)
        self.assertTrue(all(u.startswith(("/", "#", news.REPO + "/")) for u in hrefs), hrefs)
        self.assertNotIn("wasm", self.get("/")[1]["Content-Security-Policy"])   # only /whats-new needs it
        self.assertIn(b'href=/whats-new', self.get("/")[2])

    def test_casts_and_player_are_served_from_here(self):
        for f in sorted(NEWS.glob("v*/*.cast")):
            s, h, body = self.get(f"/whats-new/{f.parent.name}/{f.name}")
            self.assertEqual((s, body), (200, f.read_bytes()))
            self.assertTrue(h["Content-Type"].startswith("application/x-asciicast"))
        for p in ("/static/asciinema-player.min.js", "/static/asciinema-player.css", "/static/whatsnew.js"):
            self.assertEqual(self.get(p)[0], 200, p)
        self.assertEqual(self.get("/whats-new/v0.2.0-poc/../../page.py")[0], 404)
        self.assertEqual(self.get("/whats-new/v0.2.0-poc/rec/aws.txt")[0], 404)   # only what a release lists
        self.assertEqual(self.get("/whats-new", "POST")[0], 405)


if __name__ == "__main__":
    unittest.main()
