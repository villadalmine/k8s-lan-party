"""The web console (issue #89, docs/CONSOLE.md "Roles and transport" and the threat model): the listener binds a loopback address and
nothing else (C01); Host and Origin are checked, against DNS rebinding (C03); no CORS, no cookie, JSON bodies only (C04); a bearer token
whose SHA-256 is all that is kept, 10 wrong ones lock the web listener and never the socket (C05); the page's CSP, no inline script,
`textContent` only and no third-party file (C06); the console role cannot reach the local-only verbs. The daemon runs with the fake engine
of poc/test_console_actions.py, on a real unix socket and a real loopback port.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import stat
import sys
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
sys.path.insert(0, str(HERE))
import server  # noqa: E402
from test_console import UnixHTTP  # noqa: E402
from test_console_actions import LINUX, DaemonCase  # noqa: E402

STATIC = HERE / "console" / "static"


class AddressTest(unittest.TestCase):
    def test_c01_only_a_loopback_address_is_accepted(self):
        for ok, want in (("127.0.0.1:8090", ("127.0.0.1", 8090)), ("127.0.0.2:1", ("127.0.0.2", 1)), ("[::1]:8090", ("::1", 8090))):
            self.assertEqual(server.web_address(ok), want)
        for bad in ("0.0.0.0:8090", "[::]:8090", "192.168.178.5:8090", "10.83.0.1:8090", "100.64.0.1:8090", "[::ffff:127.0.0.1]:8090",
                    "localhost:8090", "example.com:80", "127.0.0.1", "127.0.0.1:0", "127.0.0.1:70000", "", "[fe80::1]:8090"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as e:
                    server.web_address(bad)
                self.assertEqual(e.exception.code, 2)

    def test_c01_serve_refuses_before_it_creates_anything(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([sys.executable, str(HERE / "console" / "server.py"), "serve", "--state", t, "--web", "0.0.0.0:8090"],
                               capture_output=True, text=True, timeout=20)   # a daemon that started would be killed here, and fail
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("loopback only", r.stderr)
            self.assertEqual(os.listdir(t), [])   # no console directory, no socket


class BusyPortTest(unittest.TestCase):
    @unittest.skipUnless(LINUX, "the daemon needs Linux")
    def test_a_busy_web_port_leaves_no_socket_behind(self):
        import socket as sk
        import subprocess
        import tempfile
        taken = sk.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.addCleanup(taken.close)
        with tempfile.TemporaryDirectory(dir="/tmp") as t:
            r = subprocess.run([sys.executable, str(HERE / "console" / "server.py"), "serve", "--state", t, "--mesh", "/bin/false",
                                "--web", f"127.0.0.1:{taken.getsockname()[1]}"], capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertFalse((Path(t) / "console" / "api.sock").exists())


class GateTest(unittest.TestCase):
    def test_ten_wrong_tokens_in_ten_minutes_lock_for_ten_minutes(self):
        now = [1000.0]
        g = server.Gate(lambda: now[0])
        for _ in range(9):
            g.count()
        self.assertFalse(g.locked())
        now[0] += 601   # the first nine are older than the window now
        g.count()
        self.assertFalse(g.locked())
        for _ in range(9):
            g.count()
        self.assertTrue(g.locked())
        now[0] += 599
        self.assertTrue(g.locked())
        now[0] += 2
        self.assertFalse(g.locked())
        g.count()
        self.assertFalse(g.locked())   # the lockout started a new count

    def test_a_new_token_lifts_the_lockout(self):
        now = [0.0]
        g = server.Gate(lambda: now[0])
        self.assertFalse(g.locked("h1"))
        for _ in range(10):
            g.count()
        self.assertTrue(g.locked("h1"))
        self.assertFalse(g.locked("h2"))
        for _ in range(9):
            g.count()
        self.assertFalse(g.locked("h2"))


@unittest.skipUnless(LINUX, "the daemon needs Linux (SO_PEERCRED)")
class WebTest(DaemonCase):
    def setUp(self):
        super().setUp()
        self.web = server.make_web(self.srv.RequestHandlerClass, ("127.0.0.1", 0))
        self.port = self.web.server_address[1]
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)
        self.token = server.new_token(self.state)

    def http(self, method: str, path: str, body=None, token: str | None = "", host: str | None = None, headers: dict | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if token is not None:
            h["Authorization"] = f"Bearer {token or self.token}"
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if raw is not None:
            c.putheader("Content-Length", str(len(raw)))
        c.endheaders(raw)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def j(self, *a, **k):
        status, headers, data = self.http(*a, **k)
        return status, json.loads(data) if data else None

    # C03: Host and Origin
    def test_c03_another_host_gets_nothing(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}", f"10.83.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.http("GET", "/", token=None, host=host)[0], 403)
                self.assertEqual(self.http("GET", "/api/v1/status", host=host)[0], 403)
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.http("GET", "/api/v1/health", host=host)[0], 200)

    def test_c03_an_origin_must_be_the_same(self):
        for origin in ("http://evil.example", f"http://evil.example:{self.port}", "null", f"https://127.0.0.1:{self.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.http("POST", "/api/v1/actions/burn", {"args": {}}, headers={"Origin": origin})[0], 403)
        self.assertEqual(self.mutations(), [])
        status, _, _ = self.http("POST", "/api/v1/actions/burn", {"args": {}}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 202)

    # C04: no CORS, no cookie, JSON only
    def test_c04_no_cors_no_cookie_json_only(self):
        status, headers, _ = self.http("OPTIONS", "/api/v1/actions/burn", headers={"Origin": f"http://127.0.0.1:{self.port}",
                                                                                "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 405)
        for path in ("/", "/console.js", "/api/v1/status"):
            _, headers, _ = self.http("GET", path)
            self.assertFalse([k for k in headers if k.lower().startswith("access-control-") or k.lower() == "set-cookie"], headers)
        status, _ = self.j("POST", "/api/v1/actions/burn", {"args": {}}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)   # a form from another page cannot send a body the API reads
        self.assertEqual(self.mutations(), [])

    # C05: the token
    def test_c05_only_the_hash_is_kept_0600(self):
        f = server.token_file(self.state)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(f.read_text().strip(), server.digest(self.token))
        self.assertNotIn(self.token, f.read_text())
        self.assertGreaterEqual(len(self.token), 43)   # 32 bytes in base64url

    def test_c05_no_token_is_401_and_counts_nothing(self):
        for _ in range(25):
            status, headers, _ = self.http("GET", "/api/v1/status", token=None)
            self.assertEqual(status, 401)
            self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(self.http("GET", "/api/v1/health")[0], 200)

    def test_c05_ten_wrong_tokens_lock_the_web_not_the_socket(self):
        for i in range(10):
            self.assertEqual(self.http("GET", "/api/v1/health", token=f"wrong-{i}")[0], 401)
        status, headers, _ = self.http("GET", "/api/v1/health")
        self.assertEqual(status, 429)   # the right token too
        self.assertEqual(headers.get("Retry-After"), "600")
        self.assertEqual(self.http("GET", "/", token=None)[0], 200)   # the page itself still loads, and says what happened
        c = UnixHTTP(str(self.sock))
        c.request("GET", "/api/v1/health")
        self.assertEqual(c.getresponse().status, 200)   # the socket is not affected
        c.close()

    def test_c05_a_new_link_kills_the_old_token_and_rotate_kills_it_without_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server.main(["url", "--state", str(self.state), "--web", f"127.0.0.1:{self.port}"]), 0)
        link = out.getvalue().strip()
        m = re.fullmatch(rf"http://127\.0\.0\.1:{self.port}/#t=([A-Za-z0-9_-]{{43}})", link)
        self.assertTrue(m, link)
        self.assertEqual(self.http("GET", "/api/v1/health")[0], 401)   # the old one died at once
        self.assertEqual(self.http("GET", "/api/v1/health", token=m.group(1))[0], 200)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server.main(["rotate-token", "--state", str(self.state)]), 0)
        self.assertNotIn("#t=", out.getvalue())
        self.assertEqual(self.http("GET", "/api/v1/health", token=m.group(1))[0], 401)

    def test_c05_a_confirm_token_is_bound_to_the_web_token(self):
        status, plan = self.j("POST", "/api/v1/actions/kick", {"args": {"name": "ana"}})
        self.assertEqual(status, 200)
        fresh = server.new_token(self.state)   # rotated between the plan and the click
        status, out = self.j("POST", "/api/v1/actions/kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"},
                             token=fresh)
        self.assertEqual(status, 410, out)
        self.assertEqual(self.mutations(), [])

    # the role
    def test_the_console_role_never_reaches_the_local_only_verbs(self):
        status, acts = self.j("GET", "/api/v1/actions")
        self.assertEqual(status, 200)
        verbs = {a["verb"] for a in acts}
        self.assertFalse(verbs & {"invite", "refresh", "bundle"}, verbs)
        self.assertIn("kick", verbs)
        self.assertEqual(self.j("POST", "/api/v1/actions/invite", {"args": {"name": "ana"}})[0], 403)
        self.assertEqual(self.j("POST", "/api/v1/actions/approve", {"args": {"trust": "friends"}})[0], 403)
        self.assertEqual(self.mutations(), [])

    def test_a_web_job_is_audited_as_web_and_its_output_redacted(self):
        os.environ["FAKE_PLANT"] = str(self.state) + "/bundles/ana"
        self.addCleanup(os.environ.pop, "FAKE_PLANT", None)
        status, job = self.j("POST", "/api/v1/actions/burn", {"args": {}})
        self.assertEqual(status, 202)
        done = self.job(job["id"])
        status, seen = self.j("GET", f"/api/v1/jobs/{job['id']}")
        self.assertEqual(seen["state"], "done")
        self.assertNotIn(str(self.state), json.dumps(seen))   # the console role sees redacted output only
        line = json.loads((self.state / "console" / "audit.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["source"], line["role"], line["uid"], line["verb"]), ("web", "console", None, "burn"))
        self.assertTrue(done)

    def test_c05_a_new_link_lifts_the_lockout(self):
        """Anyone on the host can reach the loopback port and send 10 wrong tokens: the operator's way out is a new link, at once."""
        for i in range(10):
            self.http("GET", "/api/v1/health", token=f"wrong-{i}")
        self.assertEqual(self.http("GET", "/api/v1/health")[0], 429)
        fresh = server.new_token(self.state)
        self.assertEqual(self.http("GET", "/api/v1/health", token=fresh)[0], 200)
        for i in range(9):   # a new count, not the old one carried over
            self.http("GET", "/api/v1/health", token=f"again-{i}")
        self.assertEqual(self.http("GET", "/api/v1/health", token=fresh)[0], 200)

    def test_a_slow_client_is_hung_up_and_a_slow_plan_is_not(self):
        """A request that is not read within REQUEST_READ_SECONDS is cut (16 drippers cannot hold the listener); the work after the
        request is read (a plan's reads, up to 2 minutes) is not under that deadline."""
        import socket as sk
        import time
        import unittest.mock
        log = io.StringIO()
        with unittest.mock.patch.object(server, "REQUEST_READ_SECONDS", 1), contextlib.redirect_stderr(log):
            drips = []
            for _ in range(server.MAX_CONNECTIONS):
                c = sk.create_connection(("127.0.0.1", self.port), timeout=5)
                c.sendall(b"GET /api/v1/health HTTP/1.0\r\nHost: x")   # never finished
                drips.append(c)
            time.sleep(1.8)
            self.assertEqual(self.http("GET", "/api/v1/health")[0], 200)
            for c in drips:
                c.close()
            real = server.mutate.plan

            def slow(*a):
                time.sleep(2)
                return real(*a)
            with unittest.mock.patch.object(server.mutate, "plan", slow):
                self.assertEqual(self.j("POST", "/api/v1/actions/kick", {"args": {"name": "ana"}})[0], 200)
        self.assertNotIn("Traceback", log.getvalue())   # a hung-up client is one line in the journal, not a traceback each

    def test_a_request_hung_up_after_its_body_starts_no_job(self):
        """The deadline fires in the instant between the body's last byte and the handler's next step: no job behind the client's back."""
        import time
        import unittest.mock
        real = server.PageHandler._body

        def late(handler, limit):
            out = real(handler, limit)
            time.sleep(1.5)   # the deadline (1 s) fires here
            return out
        with unittest.mock.patch.object(server, "REQUEST_READ_SECONDS", 1), unittest.mock.patch.object(server.PageHandler, "_body", late):
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                self.http("POST", "/api/v1/actions/burn", {"args": {}})
        end = time.monotonic() + 5   # long enough for a job that was submitted to run and leave its audit line
        while time.monotonic() < end and not (self.state / "console" / "audit.jsonl").exists():
            time.sleep(0.1)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.state / "console" / "audit.jsonl").exists())

    def test_every_method_checks_the_host(self):
        for method in ("OPTIONS", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.http(method, "/api/v1/status", host="evil.example")[0], 403, method)
            self.assertEqual(self.http(method, "/api/v1/status")[0], 405, method)

    def test_health_says_the_role(self):
        self.assertEqual(self.j("GET", "/api/v1/health")[1]["role"], "console")

    def test_another_loopback_address_is_its_own_host(self):
        try:
            web = server.make_web(self.srv.RequestHandlerClass, ("127.0.0.2", 0))
        except SystemExit:
            self.skipTest("no 127.0.0.2 here")
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        port = web.server_address[1]
        c = http.client.HTTPConnection("127.0.0.2", port, timeout=10)
        c.request("GET", "/api/v1/health", headers={"Authorization": f"Bearer {self.token}"})   # Host: 127.0.0.2:<port>
        self.assertEqual(c.getresponse().status, 200)
        c.close()

    def test_a_leftover_temporary_file_is_never_in_the_way(self):
        d = self.state / "console"
        (d / f".web.token.{os.getpid()}").write_text("left by a killed run")
        server.new_token(self.state)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name.startswith(".web.token.")), [f".web.token.{os.getpid()}"])

    # C06: the page
    def test_c06_the_page_is_served_with_page_py_headers(self):
        for path, ctype in (("/", "text/html"), ("/console.js", "application/javascript"), ("/console.css", "text/css")):
            status, headers, body = self.http("GET", path, token=None)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(ctype))
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(body, (STATIC / {"/": "index.html"}.get(path, path[1:])).read_bytes())
        self.assertEqual(self.http("GET", "/index.html", token=None)[0], 401)   # nothing else is static: an API route needs the token

    def test_c06_no_inline_script_no_html_from_data_no_third_party(self):
        html = (STATIC / "index.html").read_text()
        js = (STATIC / "console.js").read_text()
        css = (STATIC / "console.css").read_text()
        self.assertEqual(re.findall(r"<script\b[^>]*>", html), ['<script src="/console.js" defer>'])
        self.assertNotRegex(html, r"<script\b[^>]*>(?!</script>)")       # nothing between the tags
        self.assertNotRegex(html, r"\son[a-z]+\s*=")                    # no inline handler
        self.assertNotRegex(html, r"<style\b|\sstyle\s*=")              # no inline style
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"",
                    "localStorage"):
            self.assertNotIn(bad, js)
        for text in (html, js, css):
            self.assertIsNone(re.search(r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", text), "a third-party file or link")
        self.assertEqual(sorted(p.name for p in STATIC.iterdir()), ["console.css", "console.js", "index.html"])

    def test_the_link_token_leaves_the_address_bar(self):
        js = (STATIC / "console.js").read_text()
        self.assertIn('history.replaceState(null, "", location.pathname)', js)
        self.assertIn('sessionStorage.setItem("t"', js)
        self.assertIn('credentials: "omit"', js)


if __name__ == "__main__":
    unittest.main()
