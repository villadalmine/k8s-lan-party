"""What's new (issue #63): the recordings are made by script from real runs, the filter keeps secrets out of them, and the page serves
them from itself only. No network, no machines.

- every committed recording is rebuilt from its committed source and must be byte-identical (nobody edited a .cast by hand);
- every file of poc/page/whatsnew is scanned with the filter's own patterns: one leak (a public IP, a k3s token, a long base64, a claim
  code, a JWT, a key, a fingerprint) fails the test, and a planted leak proves the scan sees it;
- a game screen (.gif) is a real GIF of at most 5 MiB with no text block that holds a leak, and is served as an image of this page only;
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
    "home path": "bundle /home/alice/regress/reg/state/poc-doom/bundles/carmack written",
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
        files = [str(f) for f in sorted(NEWS.rglob("*")) if f.is_file() and f.suffix in (".cast", ".tsv", ".txt", ".json", ".gif")]
        self.assertTrue(any(f.endswith(".cast") for f in files))
        self.assertEqual(showcase.scan(files), 0, "a recording holds a secret: run the filter (showcase.py redact) again")

    def test_casts_are_rebuilt_from_their_sources_byte_for_byte(self):
        for rel in sorted(p for p in NEWS.glob("v*") if p.is_dir()):
            d = Path(tempfile.mkdtemp(prefix="wn-"))
            try:
                shutil.copytree(rel, d / rel.name)
                shutil.copy(rel.parent / f"{rel.name}.json", d / f"{rel.name}.json")
                shutil.copy(NEWS / "annotations.json", d / "annotations.json")
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


def screen(cast: Path) -> list[str]:
    """The lines a viewer reads in a .cast, colours removed."""
    text = "".join(json.loads(ln)[2] for ln in cast.read_text().splitlines()[1:])
    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in text.split("\r\n")]


class ExpectedErrors(unittest.TestCase):
    ANN = json.loads((NEWS / "annotations.json").read_text())

    def test_every_expected_error_is_explained_just_before(self):
        n = 0
        for f in sorted(NEWS.glob("v*/*.cast")):
            lines = screen(f)
            for i, ln in enumerate(lines):
                if "Forbidden" in ln or "Unable to connect" in ln or "no server answered" in ln:
                    n += 1
                    self.assertTrue(any(x.startswith("# expected") for x in lines[max(0, i - 2):i]),
                                    f"{f.name} line {i}: {ln[:60]!r} has no '# expected' note in the 2 lines before it")
        self.assertGreaterEqual(n, 7)   # 1 + 3 Forbidden, 1 + 2 Unable to connect in v0.2.0-poc (and the denials of v0.3.0-poc)

    def test_notes_only_add_lines(self):
        rows = showcase.read_tsv(str(NEWS / "v0.2.0-poc" / "rec" / "aws-player.tsv"))
        out = showcase.annotate(rows, {**self.ANN, "collapse": []})
        self.assertEqual([r for r in out if r[1] != "note" or r in rows], rows)
        self.assertGreater(len(out), len(rows))

    def test_a_long_run_of_k3s_retries_is_shortened_and_counted(self):
        e = [(float(i), "out", f'E1103 17:52:{i:02d}.000001    2162 controller.go:208] "Failed to ensure lease exists"') for i in range(6)]
        rows = [(0.0, "out", "before")] + e + [(9.0, "out", "after")]
        out = showcase.annotate(rows, self.ANN)
        self.assertEqual([x[2] for x in out], ["before", e[0][2], "... (5 lines of the expelled node's k3s retrying)", "after"])
        self.assertEqual(showcase.annotate(rows[:5], self.ANN), rows[:5])   # 4 lines: kept as they are


def gif_with(comment: str) -> bytes:
    """A minimal 1x1 GIF89a with a comment extension (what a tool or a viewer can read besides the pixels)."""
    c = comment.encode()
    return (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00"
            b"!\xfe" + bytes([len(c)]) + c + b"\x00;")


class Media(unittest.TestCase):
    def test_every_game_screen_is_a_small_real_gif_with_no_leak(self):
        gifs = sorted(NEWS.glob("v*/*.gif"))
        for g in gifs:
            b = g.read_bytes()
            self.assertIn(b[:6], (b"GIF87a", b"GIF89a"), g.name)
            self.assertLessEqual(len(b), news.GIF_MAX, g.name)
            self.assertEqual(showcase.scan([str(g)]), 0, g.name)
        listed = {(r["version"], m["file"]) for r in news.load(NEWS) for m in r.get("media", [])}
        self.assertEqual(listed, {(g.parent.name, g.name) for g in gifs}, "a GIF no release lists, or a listed one that is missing")

    def test_a_leak_in_a_gif_is_seen_and_a_clean_one_is_not(self):
        d = Path(tempfile.mkdtemp(prefix="wn-"))
        try:
            for what, line in LEAKS.items():
                (d / "bad.gif").write_bytes(gif_with(line))
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(showcase.scan([str(d / "bad.gif")]), 1, what)
                self.assertIn("LEAK", out.getvalue())
            (d / "ok.gif").write_bytes(gif_with("Created with ffmpeg, 10.77.0.2 is the mesh"))
            self.assertEqual(showcase.scan([str(d / "ok.gif")]), 0)
        finally:
            shutil.rmtree(d)

    def test_a_bad_media_entry_stops_the_page(self):
        d = Path(tempfile.mkdtemp(prefix="wn-"))
        try:
            good = {"version": "v9.9.9", "date": "2026-01-01", "title": "t", "summary": "s", "issues": [1],
                    "release": f"{news.REPO}/releases/tag/v9.9.9"}
            m = {"file": "m.gif", "title": "t", "alt": "a"}
            (d / "v9.9.9").mkdir()
            (d / "v9.9.9" / "m.gif").write_bytes(gif_with("x"))
            (d / "v9.9.9" / "big.gif").write_bytes(b"GIF89a" + b"\0" * news.GIF_MAX)
            (d / "v9.9.9" / "fake.gif").write_bytes(b"<html>")
            for bad, key in (({**m, "file": "../m.gif"}, "is not a name"), ({**m, "file": "m.mp4"}, "is not a name"), ({**m, "file": "big.gif"}, "big.gif"),
                             ({**m, "file": "fake.gif"}, "fake.gif"), ({**m, "file": "missing.gif"}, "missing.gif"),
                             ({k: v for k, v in m.items() if k != "alt"}, "alt"), ({**m, "caption": 3}, "caption")):
                (d / "v9.9.9.json").write_text(json.dumps({**good, "media": [bad]}))
                with self.assertRaises(news.NewsError, msg=str(bad)) as e:
                    news.load(d)
                self.assertIn(key, str(e.exception))
            (d / "v9.9.9.json").write_text(json.dumps({**good, "media": [m]}))
            self.assertEqual(len(news.load(d)[0]["media"]), 1)   # the positive control
        finally:
            shutil.rmtree(d)


class Releases(unittest.TestCase):
    def test_every_release_loads_and_v0_2_0_tells_both_sides(self):
        rs = {r["version"]: r for r in news.load(NEWS)}
        self.assertIn("v0.1.0-poc", rs)
        self.assertEqual(rs["v0.2.1-poc"]["recordings_from"], "v0.2.0-poc")
        r = rs["v0.2.0-poc"]
        self.assertEqual({c["side"] for c in r["casts"]}, {"player", "host"})
        self.assertTrue(r["story"]["player"]["steps"] and r["story"]["host"]["steps"])
        self.assertGreaterEqual(len(r["features"]), 6)

    def test_a_bad_learn_file_stops_the_page(self):
        d = Path(tempfile.mkdtemp(prefix="wn-learn-"))
        try:
            (d / "v9.9.9.json").write_text(json.dumps({"version": "v9.9.9", "date": "2026-01-01", "title": "t", "summary": "s",
                                                       "issues": [1], "release": f"{news.REPO}/releases/tag/v9.9.9"}))
            card = {"id": "x", "concept": "c", "analogy": "a", "real_world": "r", "seen_in": "s"}
            (d / "learn.json").write_text(json.dumps({"releases": {"v9.9.9": [card]}}))
            self.assertEqual(news.load(d)[0]["learn"], [card])
            for bad in ({"concept": ""}, {"analogy": 3}):
                (d / "learn.json").write_text(json.dumps({"releases": {"v9.9.9": [{**card, **bad}]}}))
                with self.assertRaises(news.NewsError):
                    news.load(d)
            (d / "learn.json").write_text(json.dumps({"releases": {"v1.1.1": [card]}}))
            with self.assertRaises(news.NewsError):
                news.load(d)
            self.assertEqual(len(news.load(d, strict=False)), 1)
        finally:
            shutil.rmtree(d)

    def test_a_bad_release_stops_the_page(self):
        d = Path(tempfile.mkdtemp(prefix="wn-"))
        try:
            good = {"version": "v9.9.9", "date": "2026-01-01", "title": "t", "summary": "s", "issues": [1],
                    "release": f"{news.REPO}/releases/tag/v9.9.9"}
            (d / "v9.9.9").mkdir()
            (d / "v9.9.9" / "p.cast").write_text("{}\n")
            cast = {"file": "p.cast", "title": "t", "side": "player"}
            feat = {"name": "n", "what": "w", "why": "y", "command": "c"}
            for bad in ({"release": "https://evil.example/releases/tag/v9.9.9"}, {"version": "v9"}, {"issues": ["1"]},
                        {"casts": [{**cast, "file": "../x.cast"}]}, {"casts": [{**cast, "file": "missing.cast"}]},
                        {"recordings_from": "v1.0.0"}, {"recordings_from": "v9.9.9"}):
                (d / "v9.9.9.json").write_text(json.dumps({**good, **bad}))
                with self.assertRaises(news.NewsError, msg=str(bad)):
                    news.load(d)
            # every key the HTML prints: missing or not a string stops the page, and the error names the file and the key
            cases = [({}, "title", None), ({}, "summary", None), ({"aws_title": 3}, "aws_title", None)]
            cases += [({"casts": [{k: v for k, v in cast.items() if k != key}]}, key, None) for key in ("title", "side")]
            cases += [({"casts": [{**cast, "side": "both"}]}, "side", None), ({"casts": [{**cast, "run": ""}]}, "run", None),
                      ({"casts": [{**cast, "caption": ["x"]}]}, "caption", None)]
            cases += [({"features": [{k: v for k, v in feat.items() if k != key}]}, key, None) for key in feat]
            cases += [({"story": {"player": {"steps": ["a"]}}}, "title", None), ({"story": {"host": {"title": "t", "steps": [1]}}}, "steps", None),
                      ({"story": {"judge": {"title": "t", "steps": []}}}, "judge", None)]
            for extra, key, _ in cases:
                rel = {**good, **extra}
                if key in ("title", "summary") and not extra:
                    rel.pop(key)
                (d / "v9.9.9.json").write_text(json.dumps(rel))
                with self.assertRaises(news.NewsError, msg=f"{key}: {extra}") as e:
                    news.load(d)
                self.assertIn("v9.9.9.json", str(e.exception))
                self.assertIn(key, str(e.exception))
            (d / "v9.9.9.json").write_text(json.dumps({**good, "casts": [cast], "features": [feat],
                                                       "story": {"player": {"title": "t", "steps": ["a"]}}}))
            self.assertEqual(len(news.load(d)), 1)   # the positive control: the same file with every key is accepted
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

    def test_each_release_has_its_own_page_and_the_index_links_them(self):
        rs = news.load(NEWS)
        index = self.get("/whats-new")[2].decode()
        for i, r in enumerate(rs):
            v = r["version"]
            self.assertIn(f'href="/whats-new/{v}"', index)
            s, h, body = self.get(f"/whats-new/{v}")
            self.assertEqual(s, 200, v)
            self.assertIn("'wasm-unsafe-eval'", h["Content-Security-Policy"])
            text = body.decode()
            self.assertIn(f'href="{r["release"]}"', text)                    # its release on GitHub
            self.assertIn('href="/whats-new">all releases', text)            # back to the index
            if i:
                self.assertIn(f'href="/whats-new/{rs[i - 1]["version"]}"', text)   # the newer one
            if i + 1 < len(rs):
                self.assertIn(f'href="/whats-new/{rs[i + 1]["version"]}"', text)   # the older one
        self.assertIn(f'<section id="{rs[0]["version"]}">', index)            # the newest one in full on the index

    def test_the_three_releases_show_what_you_just_saw(self):
        # issue #77: the section is built from docs/FEATURES.yml (learn.json), nothing else on the page changes: same CSP as before
        shown = {"v0.2.0-poc": {"join", "admission-auto", "pool", "public-page", "cilium-guard", "spire-scope"},
                 "v0.2.1-poc": {"public-page"}, "v0.3.0-poc": {"party", "doom"}}
        for v, ids in shown.items():
            s, h, body = self.get(f"/whats-new/{v}")
            text = body.decode()
            self.assertIn("What you just saw, and where it", text, v)
            for i in ids:
                self.assertIn(f"<h4>{i}</h4>", text, f"{v} does not show {i}")
            self.assertEqual(h["Content-Security-Policy"], pagemod.SECURITY_HEADERS["Content-Security-Policy"].replace(
                "script-src 'self'", "script-src 'self' 'wasm-unsafe-eval'"))
            self.assertNotIn("<script>", text.replace("<script src=", ""))   # no inline script was added
        self.assertNotIn("What you just saw", self.get("/whats-new/v0.1.0-poc")[2].decode())   # a release that introduced none shows none

    def test_game_screens_are_served_as_images_of_this_server_only(self):
        for r in news.load(NEWS):
            page = self.get(f"/whats-new/{r['version']}")
            for m in r.get("media", []):
                s, h, body = self.get(f"/whats-new/{r['version']}/{m['file']}")
                self.assertEqual((s, h["Content-Type"], body), (200, "image/gif", (NEWS / r["version"] / m["file"]).read_bytes()))
                self.assertIn(f'<img class=shot src="/whats-new/{r["version"]}/{m["file"]}"', page[2].decode())
            self.assertNotIn("media-src", page[1]["Content-Security-Policy"])   # an image needs img-src 'self', which the page has already
            self.assertIn("img-src 'self';", page[1]["Content-Security-Policy"])
            for src in re.findall(r'<img[^>]*src="?([^" >]+)', page[2].decode()):
                self.assertTrue(src.startswith("/"), f"loads {src} from another site")
        self.assertEqual(self.get("/whats-new/v0.3.0-poc/../../page.py")[0], 404)

    def test_only_known_versions_route(self):
        for p in ("/whats-new/v9.9.9-poc", "/whats-new/v0.2.0-poc/", "/whats-new/v0.2.0-poc.json", "/whats-new/../page.py",
                  "/whats-new/v0.2.0-poc/../../page.py", "/whats-new/%2e%2e/page.py", "/whats-new/v0.2.0-poc%2f..%2fpage.py",
                  "/whats-new/annotations.json", "/whats-new/v0.2.0-poc/rec/player.tsv", "/whats-new//v0.2.0-poc", "/whats-new/V0.2.0-POC"):
            self.assertEqual(self.get(p)[0], 404, p)
        self.assertEqual(self.get("/whats-new/v0.2.0-poc?x=../../etc/passwd")[0], 200)   # the query is never read

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
