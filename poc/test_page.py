"""The public page and its puller (docs/ADMISSION.md B2, docs/PUBLIC-PAGE.md): one test class per threat of the threat model in
docs/ADMISSION.md ("B2: threats and their tests"). A real HTTP server on 127.0.0.1 with a simulated clock; the puller against a fake
`mesh` and a fake `nebula-cert` that record their argument lists. No network, no Docker. Needs `openssl` for the sealed envelope
(skipped without it) and a POSIX system for the fake commands (skipped on Windows)."""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
import random
import shutil
import socket
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "page"))
import page as pagemod  # noqa: E402
import puller as pullermod  # noqa: E402
import seal as keys  # noqa: E402
import visit  # noqa: E402

HAS_OPENSSL = shutil.which("openssl") is not None
POSIX = os.name == "posix"
T0 = 1_800_000_000
TOKEN = "puller-token-for-tests-0123456789abcdef"


def raw_key() -> bytes:
    b = bytearray(os.urandom(32))
    b[31] &= 0x7F
    b[0] |= 8   # never one of the small-order values
    return bytes(b)


def x25519_pair(d: Path, stem: str) -> tuple[Path, str]:
    """A real X25519 seal key pair made by openssl (what visit.py keygen does)."""
    import subprocess
    subprocess.run(["openssl", "genpkey", "-algorithm", "X25519", "-out", str(d / f"{stem}.key")], check=True, capture_output=True)
    pub = subprocess.run(["openssl", "pkey", "-in", str(d / f"{stem}.key"), "-pubout"], check=True, capture_output=True, text=True).stdout
    return d / f"{stem}.key", pub


class Base(unittest.TestCase):
    cfg: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="page-test-"))
        self.now = [T0]
        cfg = {"difficulty": 8, "socket_timeout_seconds": 2, "puller_stale_seconds": 10 ** 8, "access_log": 0, **self.cfg}
        self.page = pagemod.Page(self.tmp / "page", cfg, clock=lambda: self.now[0])
        (self.tmp / "page" / "puller.sha256").write_text(__import__("hashlib").sha256(TOKEN.encode()).hexdigest() + "\n")
        os.chmod(self.tmp / "page" / "puller.sha256", 0o600)
        self.srv = pagemod.make_server(self.page, "127.0.0.1:0")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- HTTP helpers ---------------------------------------------------------------------------------------------------
    def http(self, method: str, path: str, body=None, headers: dict | None = None, raw: bytes | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None or raw is not None else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read()
        c.close()
        try:
            j = json.loads(text)
        except ValueError:
            j = text.decode(errors="replace")
        return r.status, dict(r.getheaders()), j

    def report(self, closed=False, status=None, updates=None, token=TOKEN):
        s = status or {"slots_total": 2, "slots_used": 0, "waiting": 0, "eligible": 0, "lease_seconds": 3600, "next_ends": []}
        return self.http("POST", "/api/v1/puller/report", {"closed": closed, "status": s, "updates": updates or []},
                         {"Authorization": f"Bearer {token}"})

    def solved(self):
        _, _, c = self.http("GET", "/api/v1/challenge")
        return c["challenge"], visit.pow_solve(c["challenge"], c["difficulty"])

    def ask(self, name="alice", xff=None, neb=None, seal=None, challenge=None, nonce=None, extra=None):
        if challenge is None:
            challenge, nonce = self.solved()
        body = {"name": name, "nebula_public_key": neb or keys.nebula_pem(raw_key()),
                "seal_public_key": seal or keys.seal_pem(raw_key()), "challenge": challenge, "nonce": nonce, **(extra or {})}
        return self.http("POST", "/api/v1/requests", body, {"X-Forwarded-For": xff} if xff else None)

    def open_page(self):
        self.assertEqual(self.report()[0], 200)

    def intakes(self):
        return self.http("GET", "/api/v1/puller/intake", headers={"Authorization": f"Bearer {TOKEN}"})[2]["intakes"]


class T01ProofOfWork(Base):
    def test_solved_challenge_is_taken_once(self):
        self.open_page()
        ch, n = self.solved()
        self.assertEqual(self.ask("alice", xff="198.51.100.1", challenge=ch, nonce=n)[0], 201)
        s, _, j = self.ask("bob", xff="198.51.100.2", challenge=ch, nonce=n)
        self.assertEqual((s, j["error"]), (409, "pow-used"))

    def test_spent_even_when_the_rest_is_refused(self):
        self.open_page()
        ch, n = self.solved()
        self.assertEqual(self.ask("Bad Name", xff="198.51.100.1", challenge=ch, nonce=n)[0], 400)
        self.assertEqual(self.ask("alice", xff="198.51.100.1", challenge=ch, nonce=n)[2]["error"], "pow-used")

    def test_expired_forged_unsolved(self):
        self.open_page()
        ch, n = self.solved()
        forged = ch[:-1] + ("0" if ch[-1] != "0" else "1")
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=forged, nonce=n)[0], 403)
        bad = str(int(n) + 1)
        while visit.leading_zero_bits(__import__("hashlib").sha256(f"{ch}:{bad}".encode()).digest()) >= 8:
            bad = str(int(bad) + 1)
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=ch, nonce=bad)[2]["error"], "bad-pow")
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=ch, nonce="12a")[0], 403)
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=["x"], nonce=n)[0], 400)
        self.now[0] += 301
        self.report()
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=ch, nonce=n)[2]["error"], "pow-expired")

    def test_an_easier_challenge_is_refused_after_the_difficulty_rises(self):
        self.open_page()
        ch, n = self.solved()
        self.page.cfg["difficulty"] = 9
        self.assertEqual(self.ask(xff="198.51.100.1", challenge=ch, nonce=n)[0], 403)

    def test_another_page_key_does_not_validate(self):
        self.open_page()
        ch, n = self.solved()
        other = pagemod.Page(self.tmp / "other", {"difficulty": 8})
        st = {"pow_used": {}}
        with self.assertRaises(pagemod.Refusal):
            other._pow(st, ch, n, T0)


class T02ClaimCode(Base):
    def test_code_entropy_hash_only_single_use(self):
        self.open_page()
        s, _, j = self.ask("alice", xff="198.51.100.1")
        self.assertEqual(s, 201)
        code = j["claim_code"]
        raw = code.replace("-", "")
        self.assertRegex(raw, r"^[A-Z2-7]{26}$")   # 130 bits of base32, 128 random
        disk = b"".join(p.read_bytes() for p in (self.tmp / "page").rglob("*") if p.is_file())
        self.assertNotIn(raw.encode(), disk)
        self.assertNotIn(code.encode(), disk)
        iid = self.intakes()[0]["id"]
        env = {"v": 1, "alg": keys.ALG, "epk": base64.b64encode(raw_key()).decode(), "ct": base64.b64encode(b"x" * 64).decode(),
               "mac": base64.b64encode(b"m" * 32).decode()}
        self.report(updates=[{"id": iid, "state": "ready", "sealed": env, "lease_end": T0 + 3600}])
        s, _, j = self.http("POST", "/api/v1/requests/claim", {"claim_code": code.lower()})
        self.assertEqual((s, j["sealed"]), (200, env))
        s, _, j = self.http("POST", "/api/v1/requests/claim", {"claim_code": code})
        self.assertEqual((s, j["error"]), (410, "gone"))
        self.assertNotIn(b'"ct"', (self.tmp / "page" / "state.json").read_bytes())   # the sealed blob is gone after the claim

    def test_unknown_and_malformed_codes_are_the_same_404_and_limited(self):
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        good = j["claim_code"]
        for i in range(20):
            wrong = "A" * 26 if i % 2 else "not a code <script>"
            s, _, r = self.http("POST", "/api/v1/requests/status", {"claim_code": wrong})
            self.assertEqual((s, r["error"]), (404, "unknown"))
            self.assertNotIn("script", r["message"])
        s, h, r = self.http("POST", "/api/v1/requests/status", {"claim_code": good})
        self.assertEqual(s, 429)                       # even the right code, from that address, until the window passes
        self.assertIn("Retry-After", h)
        self.now[0] += 601
        self.report()
        self.assertEqual(self.http("POST", "/api/v1/requests/status", {"claim_code": good})[0], 200)

    def test_ready_bundle_expires_unclaimed(self):
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        iid = self.intakes()[0]["id"]
        env = {"v": 1, "alg": keys.ALG, "epk": base64.b64encode(raw_key()).decode(), "ct": base64.b64encode(b"x" * 64).decode(),
               "mac": base64.b64encode(b"m" * 32).decode()}
        self.report(updates=[{"id": iid, "state": "ready", "sealed": env, "lease_end": T0 + 3600}])
        self.now[0] += 481
        self.report()
        s, _, r = self.http("POST", "/api/v1/requests/claim", {"claim_code": j["claim_code"]})
        self.assertEqual(s, 410)
        self.assertIn("expired", r["message"])


class T03Queue(Base):
    def test_no_position_from_the_visitor_one_request_per_identity_names_once(self):
        self.open_page()
        self.assertEqual(self.ask("alice", xff="198.51.100.1", extra={"position": 1})[0], 400)
        self.assertEqual(self.ask("alice", xff="198.51.100.1")[0], 201)
        self.now[0] += 200
        self.assertEqual(self.ask("alice2", xff="198.51.100.1")[2]["error"], "already-asked")
        self.assertEqual(self.ask("alice", xff="198.51.100.9")[2]["error"], "name-taken")

    def test_positions_only_from_the_puller(self):
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        iid = self.intakes()[0]["id"]
        self.report(updates=[{"id": iid, "state": "waiting", "position": 3, "of": 4, "eta": T0 + 7200}])
        r = self.http("POST", "/api/v1/requests/status", {"claim_code": j["claim_code"]})[2]
        self.assertEqual((r["state"], r["position"], r["of"], r["eta_in_seconds"]), ("waiting", 3, 4, 7200))

    def test_puller_endpoints_need_the_token(self):
        self.open_page()
        self.assertEqual(self.http("GET", "/api/v1/puller/intake")[0], 401)
        self.assertEqual(self.report(token="wrong")[0], 401)
        for _ in range(19):
            self.report(token="wrong")
        self.assertEqual(self.report()[0], 429)   # the failures of that address lock it out, the right token too


class T04ForwardedFor(Base):
    def test_trusted_proxy_gives_the_client(self):
        self.open_page()
        self.assertEqual(self.ask("alice", xff="203.0.113.5, 198.51.100.1")[0], 201)
        self.assertEqual(self.ask("bob", xff="198.51.100.2")[0], 201)
        self.assertEqual({i["name"]: i["ip"] for i in self.intakes()}["alice"], "198.51.100.1")   # the right-most hop the proxy appended, not what the client wrote

    def test_untrusted_peer_header_ignored(self):
        self.page.cfg["trusted_proxies"] = []
        self.open_page()
        self.assertEqual(self.ask("alice", xff="198.51.100.1")[0], 201)
        self.now[0] += 200
        self.assertEqual(self.ask("bob", xff="198.51.100.2")[2]["error"], "already-asked")
        self.assertEqual(self.intakes()[0]["ip"], "127.0.0.1")


class T05Keys(Base):
    def test_malformed_and_malicious_keys(self):
        self.open_page()
        k = raw_key()
        good = keys.nebula_pem(k)
        b64 = base64.b64encode
        bad_neb = [
            good.replace("X25519 PUBLIC", "ED25519 PUBLIC"),                                     # a CA's key type
            "-----BEGIN NEBULA CERTIFICATE-----\n" + b64(os.urandom(120)).decode() + "\n-----END NEBULA CERTIFICATE-----\n",
            good.replace("X25519", "P256"),
            "-----BEGIN NEBULA X25519 PUBLIC KEY-----\n" + b64(k[:31]).decode() + "\n-----END NEBULA X25519 PUBLIC KEY-----\n",
            "-----BEGIN NEBULA X25519 PUBLIC KEY-----\n" + b64(k + b"\0").decode() + "\n-----END NEBULA X25519 PUBLIC KEY-----\n",
            good + good,                                                                          # two blocks
            good + "trailing",
            keys.nebula_pem(k).replace(b64(k).decode(), b64(bytes(32)).decode()),                # all zero: small order
            keys.nebula_pem(k).replace(b64(k).decode(), b64(b"\xff" * 32).decode()),             # not canonical
            "-----BEGIN NEBULA X25519 PUBLIC KEY-----\n!!!!\n-----END NEBULA X25519 PUBLIC KEY-----\n",
            "x" * 450, 42, ["k"],
        ]
        for i, neb in enumerate(bad_neb):
            self.now[0] += 1000
            s, _, j = self.ask(f"n{i}x", xff=f"198.51.100.{i + 1}", neb=neb)
            self.assertEqual(s, 400, f"nebula key {i} was taken: {j}")
        rsa_spki = "-----BEGIN PUBLIC KEY-----\n" + b64(bytes.fromhex("30819f300d06092a864886f70d010101050003818d00") + os.urandom(141)).decode() + "\n-----END PUBLIC KEY-----\n"
        for i, sk in enumerate([rsa_spki, keys.seal_pem(k).replace("PUBLIC KEY", "PRIVATE KEY"), " ", keys.nebula_pem(raw_key())]):
            self.now[0] += 1000
            self.assertEqual(self.ask(f"s{i}x", xff=f"198.51.101.{i + 1}", seal=sk)[0], 400, f"seal key {i}")
        self.now[0] += 1000
        self.assertEqual(self.ask("same", xff="198.51.102.1", neb=keys.nebula_pem(k), seal=keys.seal_pem(k))[0], 400)

    def test_a_key_of_another_request_is_refused(self):
        self.open_page()
        k = keys.nebula_pem(raw_key())
        self.assertEqual(self.ask("alice", xff="198.51.100.1", neb=k)[0], 201)
        self.assertEqual(self.ask("bob", xff="198.51.100.2", neb=k)[2]["error"], "key-taken")


class T06Names(Base):
    def test_bad_names(self):
        self.open_page()
        bad = ["../x", "a/b", "cp", "lh", "n1", "n2", "A", "Alice", "a", "a;id", "$(id)", "`id`", "alice\n", "-alice", "a" * 21,
               "alicé", "al ice", "", "a‮", "a\x00b", ["alice"], 7]
        for i, name in enumerate(bad):
            self.now[0] += 1000
            s, _, j = self.ask(name, xff=f"198.51.100.{i + 1}")
            self.assertEqual((s, j["error"]), (400, "bad-name"), f"{name!r} was taken")


class T09Limits(Base):
    cfg = {"max_open": 2, "rate_max_requests": 2, "request_cooldown_seconds": 60, "global_max_requests_per_hour": 3,
           "max_threads": 2}

    def test_body_and_type_limits(self):
        self.open_page()
        s = self.http("POST", "/api/v1/requests", raw=b"{" + b" " * 5000 + b"}")[0]
        self.assertEqual(s, 413)
        self.assertEqual(self.http("POST", "/api/v1/requests", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.http("POST", "/api/v1/requests", raw=b'{"a":1,"a":2}')[0], 400)
        self.assertEqual(self.http("POST", "/api/v1/requests", raw=b'{"a":NaN}')[0], 400)
        self.assertEqual(self.http("POST", "/api/v1/requests", raw=b"[" * 2000 + b"]" * 2000)[0], 400)
        c = socket.create_connection(("127.0.0.1", self.port))
        c.sendall(b"POST /api/v1/requests HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
        self.assertIn(b" 411 ", c.recv(200))
        c.close()

    def test_rate_cooldown_global_and_full_list(self):
        self.open_page()
        self.assertEqual(self.ask("a1", xff="198.51.100.1")[0], 201)
        s, h, j = self.ask("a2", xff="198.51.100.1")
        self.assertEqual((s, j["error"]), (429, "cooldown"))
        self.assertIn("Retry-After", h)
        self.assertEqual(self.ask("b1", xff="198.51.100.2")[0], 201)
        self.assertEqual(self.ask("c1", xff="198.51.100.3")[2]["error"], "full")   # max_open 2
        self.report(updates=[{"id": i["id"], "state": "refused", "reason": "test"} for i in self.intakes()])
        self.now[0] += 61
        self.report()
        self.assertEqual(self.ask("c2", xff="198.51.100.3")[0], 201)            # third accepted this hour
        self.now[0] += 61
        self.report()
        self.assertEqual(self.ask("d1", xff="198.51.100.4")[2]["error"], "busy")  # global ceiling: 3 an hour

    def test_slow_clients_are_dropped_and_connections_bounded(self):
        a = socket.create_connection(("127.0.0.1", self.port))
        b = socket.create_connection(("127.0.0.1", self.port))
        time.sleep(0.3)
        c = socket.create_connection(("127.0.0.1", self.port))
        c.settimeout(5)
        self.assertIn(b"503", c.recv(100))   # two idle connections hold the two slots: the third gets a 503 at once
        a.settimeout(6)
        t = time.time()
        self.assertEqual(a.recv(100), b"")   # an idle client is dropped after socket_timeout_seconds (2)
        self.assertLess(time.time() - t, 5)
        for s in (a, b, c):
            s.close()


class T10Headers(Base):
    def test_every_answer_has_the_security_headers_and_no_cors(self):
        self.open_page()
        for method, path, body in [("GET", "/", None), ("GET", "/static/page.js", None), ("GET", "/api/v1/status", None),
                                   ("GET", "/nope", None), ("OPTIONS", "/api/v1/requests", None), ("POST", "/api/v1/requests", {}),
                                   ("GET", "/static/../page.py", None), ("DELETE", "/", None)]:
            s, h, _ = self.http(method, path, body, {"Origin": "https://evil.example"})
            for k in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
                self.assertIn(k, h, f"{method} {path}")
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", h["Content-Security-Policy"])
            self.assertFalse(any(k.lower().startswith("access-control-") for k in h), f"CORS header on {method} {path}")
            self.assertNotIn("Python", h.get("Server", ""))
        self.assertEqual(self.http("OPTIONS", "/api/v1/requests")[0], 405)
        self.assertEqual(self.http("GET", "/static/../page.py")[0], 404)

    def test_html_has_no_inline_script_and_escapes(self):
        s, h, body = self.http("GET", "/")
        self.assertEqual(s, 200)
        import re
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", body), [])
        self.assertNotRegex(body, r"\son[a-z]+=")   # no inline event handlers
        self.assertIn("Closed:", body)              # no report yet: closed
        self.open_page()
        self.assertIn("2</b> of 2 places free", self.http("GET", "/")[2])

    def test_puller_reason_is_cleaned(self):
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        iid = self.intakes()[0]["id"]
        self.report(updates=[{"id": iid, "state": "waiting", "position": 1, "of": 1, "reason": "x\x1b[31m\n<b>"}])
        r = self.http("POST", "/api/v1/requests/status", {"claim_code": j["claim_code"]})[2]
        self.assertNotIn("\x1b", r.get("reason", ""))
        self.assertNotIn("\n", r.get("reason", ""))


class T11NoSecretsOnTheServer(Base):
    def test_state_has_no_private_key_token_or_code(self):
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        intake = self.intakes()[0]
        self.assertNotIn("PRIVATE", json.dumps(intake))
        disk = b"".join(p.read_bytes() for p in (self.tmp / "page").rglob("*") if p.is_file())
        for secret in (b"PRIVATE KEY", b"K10", TOKEN.encode(), j["claim_code"].replace("-", "").encode()):
            self.assertNotIn(secret, disk)
        for p in (self.tmp / "page").rglob("*"):
            if p.is_file() and POSIX and p.stat().st_size:   # the lock file is empty
                self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0, p)


class T12Closed(Base):
    cfg = {"puller_stale_seconds": 300}

    def test_closed_until_the_first_report_then_by_panic_staleness_and_operator(self):
        self.assertEqual(self.ask("alice", xff="198.51.100.1")[2]["error"], "closed")
        self.assertFalse(self.http("GET", "/api/v1/status")[2]["open"])
        self.open_page()
        _, _, j = self.ask("alice", xff="198.51.100.1")
        iid = self.intakes()[0]["id"]
        env = {"v": 1, "alg": keys.ALG, "epk": base64.b64encode(raw_key()).decode(), "ct": base64.b64encode(b"x" * 64).decode(),
               "mac": base64.b64encode(b"m" * 32).decode()}
        self.report(updates=[{"id": iid, "state": "ready", "sealed": env, "lease_end": T0 + 3600}])
        self.report(closed=True)
        s = self.http("GET", "/api/v1/status")[2]
        self.assertEqual((s["open"], s["closed_reason"]), (False, "closed by the host"))
        self.assertEqual(self.ask("bob", xff="198.51.100.2")[2]["error"], "closed")
        self.assertEqual(self.http("POST", "/api/v1/requests/claim", {"claim_code": j["claim_code"]})[0], 410)
        self.assertNotIn(b'"ct"', (self.tmp / "page" / "state.json").read_bytes())
        self.report()
        self.now[0] += 301
        self.assertIn("has not reported", self.http("GET", "/api/v1/status")[2]["closed_reason"])
        self.report()
        (self.tmp / "page" / "closed").touch()
        self.assertEqual(self.http("GET", "/api/v1/status")[2]["closed_reason"], "closed by the operator of this page")


class T13Fuzz(Base):
    def test_odd_inputs_never_break_the_page(self):
        self.open_page()
        rnd = random.Random(26)
        values = [None, True, 0, -1, 10 ** 30, 1.5, "", "x" * 3000, "\u0000", "<script>", "../..", [], {}, [1, [2, [3]]], {"a": {"b": 1}}]
        fields = ["name", "nebula_public_key", "seal_public_key", "challenge", "nonce", "claim_code", "closed", "status", "updates", "zz"]
        paths = ["/api/v1/requests", "/api/v1/requests/status", "/api/v1/requests/claim", "/api/v1/puller/report", "/api/v1/challenge",
                 "/api/v1/status", "/", "/api/v1/../v1/status", "/%00", "/api/v1/requests?x=1", "//", "/static/"]
        for i in range(300):
            path = rnd.choice(paths)
            kind = rnd.randrange(3)
            if kind == 0:
                raw = bytes(rnd.randrange(256) for _ in range(rnd.randrange(64)))
            elif kind == 1:
                raw = json.dumps({rnd.choice(fields): rnd.choice(values) for _ in range(rnd.randrange(6))}).encode()
            else:
                raw = json.dumps(rnd.choice(values)).encode()
            hdr = {"Authorization": f"Bearer {TOKEN}"} if rnd.random() < 0.3 else {}
            s, _, j = self.http(rnd.choice(["GET", "POST", "POST", "PUT"]), path, raw=raw, headers=hdr)
            self.assertLess(s, 500, f"{path} {raw[:80]!r} -> {s} {j}")
        self.assertEqual(self.http("GET", "/api/v1/status")[0], 200)


class T14Report(Base):
    def test_bad_reports_and_transitions(self):
        self.open_page()
        for body in [{}, {"closed": "no", "status": {}, "updates": []}, {"closed": False, "status": {"slots_total": -1}, "updates": []},
                     {"closed": False, "status": {"slots_total": 2, "slots_used": 0, "waiting": 0, "eligible": 0, "lease_seconds": 1,
                                                  "next_ends": ["x"]}, "updates": []}]:
            self.assertEqual(self.http("POST", "/api/v1/puller/report", body, {"Authorization": f"Bearer {TOKEN}"})[0], 400)
        _, _, j = self.ask("alice", xff="198.51.100.1")
        iid = self.intakes()[0]["id"]
        s, _, r = self.report(updates=[{"id": iid, "state": "ready", "sealed": {"v": 1}, "lease_end": 1},   # not an envelope
                                       {"id": iid, "state": "claimed"},                                      # not the puller's
                                       {"id": "i-0000000000000000", "state": "ended"},                       # unknown
                                       {"id": iid, "state": "waiting", "position": True}])
        self.assertEqual(r, {"applied": 0, "ignored": 4})
        self.report(updates=[{"id": iid, "state": "refused", "reason": "test"}])
        self.assertEqual(self.report(updates=[{"id": iid, "state": "waiting", "position": 1, "of": 1}])[2]["applied"], 0)  # final stays


# --- the puller against a page that may be compromised -----------------------------------------------------------------------

FAKE_MESH = r'''#!/usr/bin/env python3
import json, sys, pathlib
d = pathlib.Path(sys.argv[0]).resolve().parent
log = d / "argv.log"
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
st = json.loads((d / "status.json").read_text())
args = sys.argv[1:]
if args[:2] == ["host", "admission-status"]:
    print(json.dumps(st)); sys.exit(0)
if args[:2] == ["host", "request"]:
    rc = json.loads((d / "rc.json").read_text()).get(args[2], 0) if (d / "rc.json").exists() else 0
    if rc == 0:
        st["requests"].append({"name": args[2], "state": "waiting", "position": st["waiting"] + 1, "eligible_at": 0, "why": ""})
        st["waiting"] += 1
        (d / "status.json").write_text(json.dumps(st))
    sys.exit(rc)
sys.exit(9)
'''
FAKE_NEBULA_CERT = r'''#!/usr/bin/env python3
import json, sys
path = sys.argv[sys.argv.index("-path") + 1]
print(json.dumps([{"details": {"publicKey": open(path).read().strip()}}]))
'''


@unittest.skipUnless(POSIX and HAS_OPENSSL, "needs a POSIX system and openssl")
class T07T08Puller(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="puller-test-"))
        self.state = self.tmp / "state"
        (self.state / "admission").mkdir(parents=True)
        (self.state / "bundles" / "lh").mkdir(parents=True)
        self.lh_key = raw_key()
        (self.state / "bundles" / "lh" / "host.crt").write_text(self.lh_key.hex())
        for name, body in (("mesh", FAKE_MESH), ("nebula-cert", FAKE_NEBULA_CERT)):
            (self.tmp / name).write_text(body)
            os.chmod(self.tmp / name, 0o755)
        self.set_status()
        (self.tmp / "token").write_text(TOKEN)
        os.chmod(self.tmp / "token", 0o600)
        self.reports = []
        self.offered = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_status(self, **kw):
        st = {"now": T0, "auto": True, "panic": False, "slots_total": 2, "slots_used": 0, "lease_seconds": 3600, "waiting": 0,
              "eligible": 0, "leases": [], "requests": [], **kw}
        (self.tmp / "status.json").write_text(json.dumps(st))

    def argv(self):
        p = self.tmp / "argv.log"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def run_puller(self, **over):
        a = Namespace(**{"url": "https://page.example", "token_file": str(self.tmp / "token"), "cacert": None, "mesh": str(self.tmp / "mesh"),
                         "state": str(self.state), "nebula_cert": str(self.tmp / "nebula-cert"), "max_per_run": 10, "max_per_hour": 30,
                         "max_waiting": 20, "now": T0, **over})
        p = pullermod.Puller(a)

        def call(path, body=None):
            if path == "/api/v1/puller/intake":
                return {"intakes": self.offered}
            self.reports.append(body)
            return {"applied": len(body["updates"])}
        p.call = call
        with __import__("contextlib").redirect_stdout(io.StringIO()):
            return p.run()

    def intake(self, i, name=None, ip=None, neb=None, seal=None):
        return {"id": f"i-{i:016x}", "name": name or f"v{i}", "ip": ip or f"198.51.100.{i % 250 + 1}",
                "nebula_public_key": neb or keys.nebula_pem(raw_key()), "seal_public_key": seal or keys.seal_pem(raw_key()),
                "received_at": T0}

    def test_compromised_page_hostile_fields_never_reach_mesh(self):
        hostile = ["a;id", "$(touch /tmp/pwn)", "../../etc", "cp", "x\ny", "A" * 30]
        self.offered = [self.intake(i, name=n) for i, n in enumerate(hostile)]
        self.offered += [self.intake(10, ip="not-an-ip"), self.intake(11, ip="1.2.3.4; id"),
                         self.intake(12, neb="-----BEGIN NEBULA ED25519 PUBLIC KEY-----\nAAAA\n-----END NEBULA ED25519 PUBLIC KEY-----"),
                         self.intake(13, neb=keys.nebula_pem(self.lh_key))]   # the lighthouse's own key
        self.assertEqual(self.run_puller(), 0)
        self.assertFalse([a for a in self.argv() if a[:2] == ["host", "request"]])
        ups = self.reports[-1]["updates"]
        self.assertEqual(len(ups), 10)
        self.assertTrue(all(u["state"] == "refused" for u in ups))
        self.assertFalse((self.state / "admission" / "pubkeys").exists() and any((self.state / "admission" / "pubkeys").iterdir()))

    def test_argument_list_and_canonical_key_file(self):
        k = raw_key()
        self.offered = [self.intake(1, name="alice", ip="198.51.100.7", neb=keys.nebula_pem(k).replace("\n", "\r\n"))]
        self.run_puller()
        req = [a for a in self.argv() if a[:2] == ["host", "request"]]
        self.assertEqual(req, [["host", "request", "alice", "--ip", "198.51.100.7", "--note", "public page"]])
        self.assertEqual((self.state / "admission" / "pubkeys" / "alice.pub").read_text(), keys.nebula_pem(k))
        self.assertEqual(self.reports[-1]["updates"][0]["state"], "waiting")
        self.assertNotIn("alice", json.dumps(self.reports[-1]["status"]))   # numbers only

    def test_caps_per_run_per_hour_and_list(self):
        self.offered = [self.intake(i) for i in range(1, 25)]
        self.run_puller(max_per_run=10, max_per_hour=6, max_waiting=20)
        self.assertEqual(len([a for a in self.argv() if a[:2] == ["host", "request"]]), 6)
        self.offered = [self.intake(i) for i in range(30, 40)]
        self.run_puller(max_per_run=10, max_per_hour=100, max_waiting=8)
        self.assertEqual(len([a for a in self.argv() if a[:2] == ["host", "request"]]), 8)
        self.assertIn("the list is full: try later", [u.get("reason") for u in self.reports[-1]["updates"]])

    def test_existing_names_and_keys_are_never_overwritten(self):
        (self.state / "admission" / "pubkeys").mkdir(parents=True)
        (self.state / "admission" / "pubkeys" / "bob.pub").write_text("original")
        (self.state / "bundles" / "carol").mkdir()
        self.set_status(requests=[{"name": "dave", "state": "waiting", "position": 1, "eligible_at": 0, "why": ""}], waiting=1)
        self.offered = [self.intake(1, name="bob"), self.intake(2, name="carol"), self.intake(3, name="dave")]
        self.run_puller()
        self.assertEqual((self.state / "admission" / "pubkeys" / "bob.pub").read_text(), "original")
        self.assertFalse([a for a in self.argv() if a[:2] == ["host", "request"]])
        self.assertEqual({u["state"] for u in self.reports[-1]["updates"]}, {"refused"})

    def test_panic_reports_closed_and_pulls_nothing(self):
        self.set_status(panic=True)
        self.offered = [self.intake(1)]
        self.assertEqual(self.run_puller(), 0)
        self.assertEqual(self.reports, [{"closed": True, "status": {"slots_total": 2, "slots_used": 0, "waiting": 0, "eligible": 0,
                                                                    "lease_seconds": 3600, "next_ends": []}, "updates": []}])
        self.assertFalse([a for a in self.argv() if a[:2] == ["host", "request"]])

    def test_ready_bundle_is_checked_and_sealed_to_the_visitor(self):
        seal_key, seal_pub = x25519_pair(self.tmp, "visitor")
        k = raw_key()
        self.offered = [self.intake(1, name="alice", neb=keys.nebula_pem(k), seal=seal_pub)]
        self.run_puller()
        b = self.state / "bundles" / "alice"
        b.mkdir()
        (b / "host.crt").write_text(k.hex())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for n, body in (("ca.crt", b"ca"), ("host.crt", k.hex().encode()), ("config.yml", b"c"), ("join.env", b"K3S_TOKEN=K10x")):
                info = tarfile.TarInfo(f"alice/{n}")
                info.size = len(body)
                t.addfile(info, io.BytesIO(body))
        (self.state / "admission" / "outbox").mkdir()
        (self.state / "admission" / "outbox" / "alice.tar.gz").write_bytes(buf.getvalue())
        self.set_status(slots_used=1, leases=[{"name": "alice", "end": T0 + 3600}],
                        requests=[{"name": "alice", "state": "approved", "eligible_at": 0, "why": "", "lease_end": T0 + 3600}])
        self.offered = []
        self.run_puller()
        up = self.reports[-1]["updates"][0]
        self.assertEqual((up["state"], up["lease_end"]), ("ready", T0 + 3600))
        self.assertNotIn("K10", json.dumps(up))
        self.assertEqual(keys.unseal(seal_key, up["sealed"]), buf.getvalue())
        other, _ = x25519_pair(self.tmp, "other")
        with self.assertRaises(ValueError):
            keys.unseal(other, up["sealed"])
        tampered = dict(up["sealed"], ct=base64.b64encode(b"Z" + base64.b64decode(up["sealed"]["ct"])[1:]).decode())
        with self.assertRaises(ValueError):
            keys.unseal(seal_key, tampered)
        self.run_puller()   # delivered once
        self.assertEqual(self.reports[-1]["updates"], [])
        # a bundle with a private key in it is never sent
        self.set_status(requests=[{"name": "alice", "state": "expired", "eligible_at": 0, "why": ""}])
        self.run_puller()
        self.assertEqual(self.reports[-1]["updates"][0]["state"], "ended")
        self.assertFalse((self.state / "admission" / "pubkeys" / "alice.pub").exists())

    def test_a_bundle_with_a_private_key_or_another_key_is_not_sent(self):
        _, seal_pub = x25519_pair(self.tmp, "visitor")
        k = raw_key()
        self.offered = [self.intake(1, name="alice", neb=keys.nebula_pem(k), seal=seal_pub)]
        self.run_puller()
        b = self.state / "bundles" / "alice"
        b.mkdir()
        (self.state / "admission" / "outbox").mkdir()
        self.set_status(requests=[{"name": "alice", "state": "approved", "eligible_at": 0, "why": "", "lease_end": T0 + 3600}])
        self.offered = []
        for crt, extra in ((raw_key().hex(), b""), (k.hex(), b"-----BEGIN NEBULA X25519 PRIVATE KEY-----")):
            (b / "host.crt").write_text(crt)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                for n, body in (("ca.crt", b"ca"), ("host.crt", crt.encode()), ("config.yml", b"c"), ("join.env", b"x" + extra)):
                    info = tarfile.TarInfo(f"alice/{n}")
                    info.size = len(body)
                    t.addfile(info, io.BytesIO(body))
            (self.state / "admission" / "outbox" / "alice.tar.gz").write_bytes(buf.getvalue())
            self.run_puller()
            self.assertEqual(self.reports[-1]["updates"], [])


@unittest.skipUnless(HAS_OPENSSL, "needs openssl")
class SealTest(unittest.TestCase):
    def test_round_trip_and_strict_shape(self):
        d = Path(tempfile.mkdtemp(prefix="seal-test-"))
        try:
            key, pub = x25519_pair(d, "v")
            env = keys.seal(keys.seal_pub(pub), b"bundle K10secret")
            self.assertNotIn("K10", json.dumps(env))
            self.assertEqual(keys.unseal(key, env), b"bundle K10secret")
            for bad in (dict(env, v=2), dict(env, extra=1), dict(env, mac="AAAA"), {k: v for k, v in env.items() if k != "mac"}):
                with self.assertRaises(ValueError):
                    keys.unseal(key, bad)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
