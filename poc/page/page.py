#!/usr/bin/env python3
"""The public page of the automatic pool (docs/ADMISSION.md, phase B2; how to run it: docs/PUBLIC-PAGE.md).

It runs on the public server, behind a reverse proxy with TLS (Caddy), and holds **no credential of the mesh**: no CA, no join token in
clear, no private key, no access to the Kubernetes API. It only keeps a list of requests and the numbers the control plane reports. The
control plane comes to it (pull, outbound only: poc/page/puller.py) to read new requests, and leaves back, per request, the position, an
estimate, a refusal, or the bundle **sealed to the visitor's own key** (poc/page/seal.py), which only the visitor can open.

The visitor (a person with a browser, a script or an agent: the same JSON API and the same limits for all of them):
  GET  /api/v1/status                 the free places, the length of the list, an estimate; open or closed
  GET  /api/v1/challenge              a proof-of-work challenge (HMAC-signed, expires, single use)
  POST /api/v1/requests               name, the two public keys, the solved challenge -> a claim code, shown once (the page keeps its hash)
  POST /api/v1/requests/status        claim code -> state, position, estimate (repeatable)
  POST /api/v1/requests/claim         claim code -> the sealed bundle, once
The control plane's puller (Authorization: Bearer <token>; the page keeps only the token's SHA-256):
  GET  /api/v1/puller/intake          the requests not taken yet (at most 20)
  POST /api/v1/puller/report          the numbers, closed or not, and one update per request
The contract (fields, errors, examples) is the table in docs/PUBLIC-PAGE.md; the threats and the test of each one: docs/ADMISSION.md.

    page.py serve --state DIR [--config page.yaml] [--listen 127.0.0.1:8088]
    page.py set-puller-hash --state DIR [--control DIR]   (the token's SHA-256, 64 hex characters, on stdin)
    page.py close | open --state DIR [--control DIR]     the operator's switch on this server (the host's panic closes it from the other side)
    page.py status --state DIR [--config page.yaml]   the public numbers, as JSON

Python standard library only (and the repository's pool.py, admission.py and seal.py next to it).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):   # the repository layout (poc/page) and the deployed one (everything in one directory)
    sys.path.insert(0, str(_p))
from admission import NAME, RESERVED, AdmissionError, clean_text, locked_state  # noqa: E402
from pool import client_ip, identity, load_config, _norm as pool_norm  # noqa: E402
import seal as keys  # noqa: E402

API = 1
DEFAULTS = {
    "difficulty": 18,                    # leading zero bits of sha256(challenge ":" nonce); about 2^18 hashes, a second or two
    "challenge_seconds": 300,            # a challenge expires after this
    "rate_window_seconds": 3600,         # per identity: at most rate_max_requests accepted requests in this window
    "rate_max_requests": 3,
    "request_cooldown_seconds": 120,     # per identity: at least this long between two accepted requests
    "global_max_requests_per_hour": 60,  # whatever the identities: a ceiling for rotating addresses
    "lookup_window_seconds": 600,        # per identity: at most lookup_max_failures wrong claim codes (or puller tokens) in this window
    "lookup_max_failures": 20,
    "max_open": 100,                     # requests not finished yet: over this the page says the list is full
    "max_records": 2000,                 # every request kept (finished ones are forgotten first)
    "max_pow_used": 20000,               # spent challenges remembered until they expire
    "claim_ttl_seconds": 480,            # a ready bundle waits this long for its claim: the join token in it lives 8 minutes at most
    "received_ttl_seconds": 3600,        # a request the control plane never took is dropped after this
    "puller_stale_seconds": 300,         # no report for this long: the page says closed
    "retention_seconds": 90000,          # a finished request is forgotten after this (25 h, as admission.py)
    "identity_ipv4_prefix": 32,
    "identity_ipv6_prefix": 64,
    "max_body_bytes": 4096,              # a visitor's request body
    "max_report_bytes": 524288,          # the puller's report (sealed bundles of a few KB each)
    "max_threads": 32,                   # connections handled at once; more get a 503
    "socket_timeout_seconds": 10,        # a client that sends nothing for this long is dropped
    "request_total_seconds": 20,         # the whole request (headers and body) must arrive within this, however slowly it drips
    "max_per_origin": 4,                 # connections at once from one socket address that is not a trusted proxy
    "trusted_proxies": ["127.0.0.1", "::1"],   # only these may set X-Forwarded-For (the reverse proxy on this machine)
    "access_log": 1,                     # one line per request on stderr: method, path, status (never an address, a query or a body)
}
OPEN_STATES = ("received", "waiting", "deferred", "ready")       # the visitor still has something to wait for
FINAL = ("refused", "ended", "expired", "closed")      # "claimed" is neither: the visitor holds a lease
UPDATE_STATES = ("waiting", "deferred", "refused", "ready", "ended")
CODE_RE = re.compile(r"[A-Z2-7]{26}")
CHALLENGE_RE = re.compile(r"v1\.(\d{10})\.([0-9a-f]{32})\.(\d{1,2})\.([0-9a-f]{64})")
NONCE_RE = re.compile(r"[0-9]{1,20}")
ID_RE = re.compile(r"i-[0-9a-f]{16}")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
                               "form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cache-Control": "no-store",
}
STATIC = {"/static/page.js": "application/javascript; charset=utf-8", "/static/page.css": "text/css; charset=utf-8"}


class Refusal(Exception):
    """An answer that is not 2xx: the HTTP status, a short machine word and a sentence for people. Never repeats the input."""

    def __init__(self, status: int, error: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.error, self.message, self.retry_after = status, error, message, retry_after


def leading_zero_bits(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        return n + 8 - b.bit_length()
    return n


def claim_hash(code: str) -> str:
    return hashlib.sha256(b"k8s-lan-party/claim/v1:" + code.encode()).hexdigest()


def new_claim_code() -> tuple[str, str]:
    """128 random bits as 26 base32 characters, shown in groups (the dashes are optional when typed back)."""
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")
    return "-".join(raw[i:i + 5] for i in range(0, 26, 5)), raw


def normal_code(text) -> str | None:
    if not isinstance(text, str) or len(text) > 64:
        return None
    c = text.upper().replace("-", "").replace(" ", "")
    return c if CODE_RE.fullmatch(c) else None


def iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Page:
    """Everything the page decides, without HTTP (the unit tests drive it directly and through a real server)."""

    def __init__(self, state_dir: str | Path, cfg: dict | None = None, clock=None, control: str | Path | None = None):
        self.dir = Path(state_dir)
        self.control = Path(control) if control else self.dir   # puller.sha256 and the operator's "closed": root's, read-only here
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.clock = clock or (lambda: int(time.time()))
        self.pow_key = self._secret("pow.key")

    # --- files of this server -------------------------------------------------------------------------------------------
    def _secret(self, name: str) -> bytes:
        """The HMAC key of the proof-of-work challenges: made here at the first start, 0600. Not a credential of the mesh: whoever
        has it can only skip this page's own proof of work."""
        p = self.dir / name
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        data = p.read_bytes()
        if len(data) != 32:
            raise SystemExit(f"{p} is not a 32-byte key: delete it and start again")
        return data

    def puller_hash(self) -> str | None:
        try:
            h = (self.control / "puller.sha256").read_text().strip()
        except OSError:
            return None
        return h if re.fullmatch(r"[0-9a-f]{64}", h) else None

    def operator_closed(self) -> bool:
        return (self.control / "closed").exists()

    # --- who is asking --------------------------------------------------------------------------------------------------
    def who(self, peer: str, xff: str | None) -> str:
        """The identity the limits count against: the client from X-Forwarded-For only when the socket peer is a trusted proxy (the
        reverse proxy on this machine), else the peer itself (pool.client_ip); normalized like the control plane does (pool.identity)."""
        try:
            return identity(client_ip(peer, xff, self.cfg["trusted_proxies"]), None, self.cfg["identity_ipv4_prefix"],
                            self.cfg["identity_ipv6_prefix"])
        except ValueError:
            raise Refusal(400, "bad-client", "the client address cannot be read") from None

    def client(self, peer: str, xff: str | None) -> str:
        return client_ip(peer, xff, self.cfg["trusted_proxies"])

    # --- state ----------------------------------------------------------------------------------------------------------
    def prune(self, st: dict, now: int) -> None:
        c = self.cfg
        st.setdefault("intakes", {})
        st["pow_used"] = {k: v for k, v in st.get("pow_used", {}).items() if v >= now}
        for key, window in (("rate", c["rate_window_seconds"]), ("fail", c["lookup_window_seconds"])):
            st[key] = {i: ts for i, ts in ((i, [t for t in v if now - t < window]) for i, v in st.get(key, {}).items()) if ts}
        st["global"] = [t for t in st.get("global", []) if now - t < 3600]
        for iid, r in list(st["intakes"].items()):
            if r["state"] == "ready" and now > r.get("ready_at", now) + c["claim_ttl_seconds"]:
                self._finish(r, "expired", now, "the bundle was not collected in time; its join token is dead")
            elif r["state"] == "received" and now > r["received_at"] + c["received_ttl_seconds"]:
                self._finish(r, "expired", now, "the control plane did not take the request in time")
            elif r["state"] == "claimed" and now > r.get("lease_end", now) + 3600:   # the puller never said "ended": the lease is long over
                self._finish(r, "ended", now, "the lease ended")
            if r["state"] in FINAL and now - r.get("finished_at", now) > c["retention_seconds"]:
                del st["intakes"][iid]
        extra = len(st["intakes"]) - c["max_records"]
        if extra > 0:   # the oldest finished ones go first; open ones are bounded by max_open
            for iid in sorted((i for i, r in st["intakes"].items() if r["state"] in FINAL),
                              key=lambda i: st["intakes"][i]["received_at"])[:extra]:
                del st["intakes"][iid]

    @staticmethod
    def _finish(r: dict, state: str, now: int, reason: str = "") -> None:
        """A request is over: keep its name, times and claim hash (the visitor can still read the end), forget the address, the
        identity, the keys and any sealed bundle."""
        r.update(state=state, finished_at=now, reason=clean_text(reason, 160), sealed=None, ip="", identity="",
                 nebula_public_key="", seal_public_key="")

    def closed_reason(self, st: dict, now: int) -> str | None:
        rep = st.get("report")
        if self.operator_closed():
            return "closed by the operator of this page"
        if not rep:
            return "not open yet: the control plane has not reported"
        if rep.get("closed"):
            return "closed by the host"
        if now - rep["at"] > self.cfg["puller_stale_seconds"]:
            return f"closed: the control plane has not reported for {(now - rep['at']) // 60} minutes"
        return None

    def public_status(self, st: dict, now: int) -> dict:
        rep = st.get("report") or {}
        s = rep.get("status") or {}
        reason = self.closed_reason(st, now)
        total, used = s.get("slots_total", 0), s.get("slots_used", 0)
        return {"api": API, "open": reason is None, "closed_reason": reason, "slots_total": total, "slots_used": used,
                "slots_free": max(0, total - used), "waiting": s.get("waiting", 0), "eligible": s.get("eligible", 0),
                "lease_seconds": s.get("lease_seconds", 0),
                "next_end_in_seconds": [max(0, e - now) for e in s.get("next_ends", [])],
                "reported_at": rep.get("at"), "difficulty": self.cfg["difficulty"]}

    # --- the visitor ----------------------------------------------------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        with locked_state(self.dir) as st:
            self.prune(st, now)
            return self.public_status(st, now)

    def challenge(self) -> dict:
        now = self.clock()
        exp = now + self.cfg["challenge_seconds"]
        d = self.cfg["difficulty"]
        body = f"v1.{exp:010d}.{secrets.token_hex(16)}.{d}"
        mac = hmac.new(self.pow_key, body.encode(), hashlib.sha256).hexdigest()
        return {"challenge": f"{body}.{mac}", "difficulty": d, "expires_at": exp, "algorithm": "sha256-leading-zero-bits",
                "solve": "find a decimal nonce (1 to 20 digits) so that sha256(challenge + ':' + nonce) starts with "
                         f"{d} zero bits"}

    def _pow(self, st: dict, challenge, nonce, now: int) -> str:
        """Checks a solved challenge; returns its key in pow_used (the caller records it: one use only)."""
        if not isinstance(challenge, str) or not isinstance(nonce, str):
            raise Refusal(400, "bad-pow", "challenge and nonce must be strings")
        m = CHALLENGE_RE.fullmatch(challenge)
        if not m or not NONCE_RE.fullmatch(nonce):
            raise Refusal(403, "bad-pow", "not a challenge of this page, or the nonce is not 1 to 20 digits")
        body = challenge.rsplit(".", 1)[0]
        good = hmac.new(self.pow_key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, m.group(4)):
            raise Refusal(403, "bad-pow", "not a challenge of this page")
        if int(m.group(1)) < now:
            raise Refusal(403, "pow-expired", "the challenge expired: ask for a new one")
        if int(m.group(3)) < self.cfg["difficulty"]:
            raise Refusal(403, "bad-pow", "the challenge is easier than the page asks now: ask for a new one")
        key = hashlib.sha256(challenge.encode()).hexdigest()
        if key in st.get("pow_used", {}):
            raise Refusal(409, "pow-used", "this challenge was already used: ask for a new one")
        if leading_zero_bits(hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()) < int(m.group(3)):
            raise Refusal(403, "bad-pow", "the nonce does not solve the challenge")
        return key

    def create(self, peer: str, xff: str | None, body) -> dict:
        now = self.clock()
        ident = self.who(peer, xff)
        ip = self.client(peer, xff)
        c = self.cfg
        with locked_state(self.dir) as st:
            self.prune(st, now)
            reason = self.closed_reason(st, now)
            if reason:   # closed first: whatever was sent, a closed page answers 503
                raise Refusal(503, "closed", reason)
            if not isinstance(body, dict) or set(body) != {"name", "nebula_public_key", "seal_public_key", "challenge", "nonce"}:
                raise Refusal(400, "bad-request", "send exactly: name, nebula_public_key, seal_public_key, challenge, nonce")
            mine = st["rate"].get(ident, [])
            if len(mine) >= c["rate_max_requests"]:
                raise Refusal(429, "rate", "too many requests from your address; try later", min(mine) + c["rate_window_seconds"] - now)
            if mine and now - max(mine) < c["request_cooldown_seconds"]:
                raise Refusal(429, "cooldown", "wait a little before asking again", max(mine) + c["request_cooldown_seconds"] - now)
            if len(st["global"]) >= c["global_max_requests_per_hour"]:
                raise Refusal(429, "busy", "the page took all the requests it takes in an hour; try later",
                              min(st["global"]) + 3600 - now)
            if len(st["pow_used"]) >= c["max_pow_used"]:
                raise Refusal(503, "busy", "the page is busy; try later")
            used = self._pow(st, body["challenge"], body["nonce"], now)
            st["pow_used"][used] = int(body["challenge"].split(".")[1])   # spent from here on, whatever happens next
            try:
                return self._take(st, body, ident, ip, now)
            except Refusal as e:
                err = e   # the spent challenge is written when the block ends; then the refusal goes out
        raise err

    def _take(self, st: dict, body: dict, ident: str, ip: str, now: int) -> dict:
        c = self.cfg
        name = body["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
            raise Refusal(400, "bad-name", "the name: 2 to 20 characters, a-z, 0-9 and '-', starting with a letter "
                                           "(lh, cp, n1, n2 are taken)")
        try:
            neb = keys.nebula_pub(body["nebula_public_key"])
            sk = keys.seal_pub(body["seal_public_key"])
        except keys.KeyRefused as e:
            raise Refusal(400, "bad-key", str(e)) from None
        if neb == sk:
            raise Refusal(400, "bad-key", "the two keys must be different keys")
        live = [r for r in st["intakes"].values() if r["state"] in OPEN_STATES]
        if any(r["identity"] == ident for r in live):
            raise Refusal(409, "already-asked", "your address already has a request: follow it with its claim code")
        if any(r["name"] == name for r in st["intakes"].values()):
            raise Refusal(409, "name-taken", "that name was already asked for here: pick another")
        pubs = {r["nebula_public_key"] for r in live} | {r["seal_public_key"] for r in live}
        if keys.nebula_pem(neb) in pubs or keys.seal_pem(sk) in pubs:
            raise Refusal(409, "key-taken", "that key is already in another request: make your own keys")
        if len(live) >= c["max_open"]:
            raise Refusal(503, "full", "the list is full; try later")
        shown, raw = new_claim_code()
        iid = "i-" + secrets.token_hex(8)
        st["intakes"][iid] = {"id": iid, "name": name, "ip": ip, "identity": ident, "nebula_public_key": keys.nebula_pem(neb),
                              "seal_public_key": keys.seal_pem(sk), "claim": claim_hash(raw), "received_at": now,
                              "state": "received", "updated_at": now}
        st["rate"].setdefault(ident, []).append(now)
        st["global"].append(now)
        return {"claim_code": shown, "name": name, "state": "received",
                "message": "keep this claim code: it is shown only once and it is the only way to collect your bundle. "
                           "The control plane reads new requests about once a minute."}

    def _lookup(self, st: dict, ident: str, code, now: int) -> dict:
        fails = st["fail"].get(ident, [])
        if len(fails) >= self.cfg["lookup_max_failures"]:
            raise Refusal(429, "too-many-tries", "too many wrong claim codes from your address; try later",
                          min(fails) + self.cfg["lookup_window_seconds"] - now)
        raw = normal_code(code)
        found = None
        if raw is not None:
            h = claim_hash(raw)
            for r in st["intakes"].values():   # every record is compared, in constant time: no early exit on a match
                if hmac.compare_digest(r["claim"], h):
                    found = r
        if found is None:
            st["fail"].setdefault(ident, []).append(now)
            raise Refusal(404, "unknown", "no request with that claim code")
        return found

    @staticmethod
    def view(r: dict, now: int) -> dict:
        out = {"name": r["name"], "state": "claimed" if r["state"] == "claimed" else r["state"]}
        for k in ("position", "of", "eta", "lease_end", "reason"):
            if r.get(k) not in (None, ""):
                out[k] = r[k]
        if r.get("eta"):
            out["eta_in_seconds"] = max(0, r["eta"] - now)
        words = {"received": "received; the control plane reads new requests about once a minute",
                 "waiting": f"in the list, position {r.get('position')} of {r.get('of')}",
                 "deferred": f"in the list, position {r.get('position')} of {r.get('of')}, not eligible yet",
                 "ready": "your place is ready: collect the bundle now (claim), its join token dies 8 minutes after approval",
                 "claimed": "collected; your lease ends at the time given",
                 "refused": "refused", "ended": "your lease ended", "expired": "expired", "closed": "the page was closed"}
        out["message"] = words.get(r["state"], r["state"])
        if r.get("eta") and r["state"] in ("waiting", "deferred"):
            out["message"] += f"; estimated around {iso(r['eta'])}"
        return out

    def lookup(self, peer: str, xff: str | None, body, claim: bool = False) -> dict:
        now = self.clock()
        if not isinstance(body, dict) or set(body) != {"claim_code"}:
            raise Refusal(400, "bad-request", "send exactly: claim_code")
        ident = self.who(peer, xff)
        with locked_state(self.dir) as st:
            self.prune(st, now)
            try:
                r = self._lookup(st, ident, body["claim_code"], now)
            except Refusal as e:
                err = e
            else:
                err = None
                if claim:
                    if r["state"] == "ready" and r.get("sealed"):
                        out = {"state": "ready", "name": r["name"], "sealed": r["sealed"], "lease_end": r["lease_end"],
                               "join": f"sudo edge-mesh join ./{r['name']} --accept-golden-rule"}
                        r.update(state="claimed", sealed=None, claimed_at=now, updated_at=now)
                        return out
                    if r["state"] in OPEN_STATES:
                        raise Refusal(409, "not-ready", self.view(r, now)["message"])
                    raise Refusal(410, "gone", f"nothing to collect: {self.view(r, now)['message']}")
                return self.view(r, now)
        raise err   # the failed try is recorded (the block above wrote the state) before the answer goes out

    # --- the puller -----------------------------------------------------------------------------------------------------
    def _auth(self, st: dict, ident: str, header: str | None, now: int) -> None:
        fails = st["fail"].get(ident, [])
        if len(fails) >= self.cfg["lookup_max_failures"]:
            raise Refusal(429, "too-many-tries", "too many failed tries from your address", min(fails) + self.cfg["lookup_window_seconds"] - now)
        want = self.puller_hash()
        if want is None:
            raise Refusal(503, "no-puller", "no puller is set up on this page")
        token = header[7:] if isinstance(header, str) and header.startswith("Bearer ") and len(header) <= 200 else ""
        if not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), want):
            st["fail"].setdefault(ident, []).append(now)
            raise Refusal(401, "unauthorized", "not the puller")

    def intake(self, peer: str, xff: str | None, auth: str | None) -> dict:
        now = self.clock()
        ident = self.who(peer, xff)
        with locked_state(self.dir) as st:
            self.prune(st, now)
            try:
                self._auth(st, ident, auth, now)
            except Refusal as e:
                err = e
            else:
                new = sorted((r for r in st["intakes"].values() if r["state"] == "received"), key=lambda r: (r["received_at"], r["id"]))
                return {"api": API, "intakes": [{k: r[k] for k in ("id", "name", "ip", "nebula_public_key", "seal_public_key",
                                                                   "received_at")} for r in new[:20]]}
        raise err

    def report(self, peer: str, xff: str | None, auth: str | None, body) -> dict:
        now = self.clock()
        ident = self.who(peer, xff)
        with locked_state(self.dir) as st:
            self.prune(st, now)
            try:
                self._auth(st, ident, auth, now)
            except Refusal as e:
                err = e
            else:
                return self._apply(st, body, now)
        raise err

    def _apply(self, st: dict, body, now: int) -> dict:
        if not isinstance(body, dict) or set(body) != {"closed", "status", "updates"} or not isinstance(body["closed"], bool):
            raise Refusal(400, "bad-report", "send exactly: closed (bool), status, updates")
        s, ups = body["status"], body["updates"]
        ints = ("slots_total", "slots_used", "waiting", "eligible", "lease_seconds")
        if not isinstance(s, dict) or set(s) != set(ints) | {"next_ends"} or \
                not all(isinstance(s[k], int) and not isinstance(s[k], bool) and 0 <= s[k] <= 10 ** 6 for k in ints) or \
                not isinstance(s["next_ends"], list) or len(s["next_ends"]) > 100 or \
                not all(isinstance(e, int) and not isinstance(e, bool) and 0 <= e < 10 ** 10 for e in s["next_ends"]):
            raise Refusal(400, "bad-report", "status: slots_total, slots_used, waiting, eligible, lease_seconds (integers) and next_ends")
        if not isinstance(ups, list) or len(ups) > 200:
            raise Refusal(400, "bad-report", "updates: a list of at most 200")
        st["report"] = {"at": now, "closed": body["closed"], "status": {**s, "next_ends": sorted(s["next_ends"])}}
        applied = ignored = 0
        if body["closed"]:   # panic on the control plane: nothing waits here any more, no sealed bundle stays
            for r in st["intakes"].values():
                if r["state"] in OPEN_STATES or r["state"] == "claimed":
                    self._finish(r, "closed", now, "admission was closed by the host")
        for u in ups:
            if self._update(st, u, now):
                applied += 1
            else:
                ignored += 1
        return {"applied": applied, "ignored": ignored}

    def _update(self, st: dict, u, now: int) -> bool:
        if not isinstance(u, dict) or not isinstance(u.get("id"), str) or not ID_RE.fullmatch(u["id"]) or \
                u.get("state") not in UPDATE_STATES or not set(u) <= {"id", "state", "position", "of", "eta", "reason", "lease_end", "sealed"}:
            return False
        r = st["intakes"].get(u["id"])
        if r is None:
            return False
        for k in ("position", "of", "eta", "lease_end"):
            if k in u and not (isinstance(u[k], int) and not isinstance(u[k], bool) and 0 <= u[k] < 10 ** 10):
                return False
        if "reason" in u and not isinstance(u["reason"], str):
            return False
        cur, new = r["state"], u["state"]
        allowed = {"received": UPDATE_STATES, "waiting": UPDATE_STATES, "deferred": UPDATE_STATES, "ready": ("ended",),
                   "claimed": ("ended",)}
        if new not in allowed.get(cur, ()):
            return False
        if new == "ready":
            if not keys.envelope_ok(u.get("sealed")) or "lease_end" not in u:
                return False
            r.update(state="ready", sealed=u["sealed"], lease_end=u["lease_end"], ready_at=now, position=None, of=None, eta=None)
        elif new in ("refused", "ended"):
            self._finish(r, new, now, u.get("reason", ""))
        else:
            r.update(state=new, position=u.get("position"), of=u.get("of"), eta=u.get("eta"), reason=clean_text(u.get("reason", ""), 160))
        r["updated_at"] = now
        return True

    # --- the HTML page --------------------------------------------------------------------------------------------------
    def html(self) -> str:
        s = self.status()
        e = html.escape
        if s["open"]:
            soon = ", ".join(f"{t // 60} min" for t in s["next_end_in_seconds"][:2]) or "none"
            numbers = (f"<p class=numbers><b>{s['slots_free']}</b> of {s['slots_total']} places free &middot; <b>{s['waiting']}</b> "
                       f"in the list ({s['eligible']} eligible now) &middot; next place frees in: {e(soon)} &middot; "
                       f"a place lasts {s['lease_seconds'] // 60} minutes</p>")
        else:
            numbers = f"<p class=closed>Closed: {e(s['closed_reason'] or '')}</p>"
        return TEMPLATE.replace("{{numbers}}", numbers).replace("{{difficulty}}", str(int(s["difficulty"])))


TEMPLATE = """<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>k8s-lan-party: ask for a place</title><link rel=stylesheet href=/static/page.css></head>
<body><main>
<h1>Ask for a place in the mesh</h1>
<p>A shared Kubernetes cluster where people lend machines. A place is one machine of yours as a <code>public</code> node for a limited
time. <b>Golden rule:</b> what runs on someone else's machine can be seen by its owner, so only shareable workloads go to your node.</p>
{{numbers}}
<p id=live></p>
<h2>1. Make your keys (on the machine that will join)</h2>
<pre>nebula-cert keygen -out-key host.key -out-pub host.pub
openssl genpkey -algorithm X25519 -out seal.key &amp;&amp; openssl pkey -in seal.key -pubout -out seal.pub</pre>
<p>The private keys never leave your machine. Paste the two <b>public</b> files below.</p>
<h2>2. Ask</h2>
<form id=ask>
<label>Name (2 to 20 characters: a-z, 0-9, -) <input id=name required maxlength=20 pattern="[a-z][a-z0-9-]{1,19}" autocomplete=off></label>
<label>host.pub <textarea id=neb required rows=3 maxlength=400></textarea></label>
<label>seal.pub <textarea id=seal required rows=3 maxlength=400></textarea></label>
<button>Ask (your browser solves a small puzzle first: {{difficulty}} bits)</button>
</form>
<p id=asked></p>
<h2>3. Follow it and collect</h2>
<form id=check><label>Claim code <input id=code required maxlength=64 autocomplete=off></label><button>Where am I?</button></form>
<p id=where></p>
<p>Collect from the machine with the keys (the bundle is sealed to your seal key, only that machine opens it):</p>
<pre>python3 poc/page/visit.py wait https://THIS-PAGE --keys ./mykeys --code YOUR-CLAIM-CODE</pre>
<p>Without a browser, everything here is a JSON API (<code>/api/v1/...</code>): docs/PUBLIC-PAGE.md in the repository.</p>
<noscript><p>The form needs JavaScript for the proof of work; or use <code>visit.py</code>.</p></noscript>
</main><script src=/static/page.js></script></body></html>
"""


# --- HTTP -------------------------------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    page: Page = None   # set by make_server
    static: dict = {}
    server_version = "k8s-lan-party-page"
    sys_version = ""
    protocol_version = "HTTP/1.0"   # one request per connection: nothing is held open

    def setup(self):
        self.timeout = self.page.cfg["socket_timeout_seconds"]
        super().setup()

    def log_request(self, code="-", size="-"):   # no address, no query, no body
        if self.page.cfg["access_log"]:
            sys.stderr.write(f"{self.command} {self.path.split('?', 1)[0][:80]} {code}\n")

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, (json.dumps(obj, sort_keys=True) + "\n").encode(), "application/json; charset=utf-8", extra)

    def _refuse(self, e: Refusal) -> None:
        extra = {"Retry-After": str(max(1, int(e.retry_after)))} if e.retry_after is not None else None
        self._json(e.status, {"error": e.error, "message": e.message}, extra)

    def _body(self, limit: int):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Refusal(415, "json-only", "send Content-Type: application/json")
        if self.headers.get("Transfer-Encoding"):
            raise Refusal(411, "length-required", "send a Content-Length, no chunked body")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise Refusal(411, "length-required", "send a Content-Length") from None
        if n < 0 or n > limit:
            raise Refusal(413, "too-large", f"the body is limited to {limit} bytes")
        raw = self.rfile.read(n)
        if len(raw) != n:
            raise Refusal(400, "short-body", "the body is shorter than its Content-Length")

        def no_constants(name):   # NaN, Infinity: not JSON
            raise ValueError(name)

        def no_dupes(pairs):
            d = {}
            for k, v in pairs:
                if k in d:
                    raise ValueError("duplicate key")
                d[k] = v
            return d
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes, parse_constant=no_constants)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise Refusal(400, "bad-json", "the body is not one JSON object") from None

    def _peer(self) -> str:
        return self.client_address[0]

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _other(self):
        self._json(405, {"error": "method", "message": "only GET and POST"}, {"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other

    def _route(self, method: str) -> None:
        p, page, xff = self.path.split("?", 1)[0], self.page, self.headers.get("X-Forwarded-For")
        try:
            if method == "GET" and p == "/":
                self._send(200, page.html().encode(), "text/html; charset=utf-8")
            elif method == "GET" and p in self.static:
                body, ctype = self.static[p]
                self._send(200, body, ctype)
            elif method == "GET" and p == "/api/v1/status":
                self._json(200, page.status())
            elif method == "GET" and p == "/api/v1/challenge":
                self._json(200, page.challenge())
            elif method == "POST" and p == "/api/v1/requests":
                self._json(201, page.create(self._peer(), xff, self._body(page.cfg["max_body_bytes"])))
            elif method == "POST" and p in ("/api/v1/requests/status", "/api/v1/requests/claim"):
                self._json(200, page.lookup(self._peer(), xff, self._body(page.cfg["max_body_bytes"]), p.endswith("/claim")))
            elif method == "GET" and p == "/api/v1/puller/intake":
                self._json(200, page.intake(self._peer(), xff, self.headers.get("Authorization")))
            elif method == "POST" and p == "/api/v1/puller/report":
                auth = self.headers.get("Authorization")
                self._json(200, page.report(self._peer(), xff, auth, self._body(page.cfg["max_report_bytes"])))
            elif p in ("/", "/api/v1/status", "/api/v1/challenge", "/api/v1/requests", "/api/v1/requests/status",
                       "/api/v1/requests/claim", "/api/v1/puller/intake", "/api/v1/puller/report") or p in self.static:
                self._other()
            else:
                self._json(404, {"error": "not-found", "message": "no such page"})
        except Refusal as e:
            self._refuse(e)
        except (AdmissionError, OSError, ValueError, KeyError, TypeError) as e:
            sys.stderr.write(f"error: {e.__class__.__name__}\n")
            self._json(500, {"error": "internal", "message": "the page could not answer"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, max_threads: int, total_seconds: float = 20, per_origin: int = 4, trusted: tuple = ()):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.slots = threading.BoundedSemaphore(max_threads)
        self.total, self.per_origin = total_seconds, per_origin
        self.trusted = {pool_norm(t) for t in trusted}
        self.origins: dict[str, int] = {}
        self.lock = threading.Lock()
        super().__init__(addr, handler)

    def _busy(self, request) -> None:
        try:
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 5\r\n\r\n")
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request(self, request, client_address):
        origin = pool_norm(client_address[0])
        if not self.slots.acquire(blocking=False):   # too many at once: answer 503 and close, never queue without a bound
            return self._busy(request)
        with self.lock:   # one origin (not the proxy, which speaks for everybody) cannot hold every slot
            held = self.origins.get(origin, 0)
            if origin not in self.trusted and held >= self.per_origin:
                self.slots.release()
                return self._busy(request)
            self.origins[origin] = held + 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        # a total deadline for the whole request: a client that drips one byte every few seconds beats a per-read timeout but not this
        timer = threading.Timer(self.total, lambda: _hang_up(request))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            with self.lock:
                origin = pool_norm(client_address[0])
                left = self.origins.get(origin, 1) - 1
                if left > 0:
                    self.origins[origin] = left
                else:
                    self.origins.pop(origin, None)
            self.slots.release()


def _hang_up(request) -> None:
    try:
        request.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def make_server(page: Page, listen: str) -> Server:
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    static = {}
    for path, ctype in STATIC.items():
        static[path] = ((HERE / "static" / path.rsplit("/", 1)[1]).read_bytes(), ctype)
    handler = type("PageHandler", (Handler,), {"page": page, "static": static})
    c = page.cfg
    return Server((host, int(port)), handler, c["max_threads"], c["request_total_seconds"], c["max_per_origin"], tuple(c["trusted_proxies"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the public page of the automatic pool (docs/PUBLIC-PAGE.md)")
    ap.add_argument("cmd", choices=["serve", "set-puller-hash", "close", "open", "status"])
    ap.add_argument("--state", required=True)
    ap.add_argument("--config")
    ap.add_argument("--listen", default="127.0.0.1:8088")
    ap.add_argument("--control", help="where puller.sha256 and the 'closed' switch live (default: the state directory); the unit "
                                      "uses /etc/k8s-lan-party-page, which the page process cannot write")
    a = ap.parse_args(argv)
    os.umask(0o077)   # every file this writes holds requests, addresses or keys
    cfg = load_config(a.config) if a.config else {}
    if a.cmd == "set-puller-hash":
        h = (sys.stdin.read(200).split() or [""])[0]
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            print("give the SHA-256 of the puller's token (64 hex characters) on stdin", file=sys.stderr)
            return 2
        ctl = Path(a.control or a.state)
        ctl.mkdir(mode=0o755 if a.control else 0o700, parents=True, exist_ok=True)   # the page's own user reads it
        fd = os.open(ctl / "puller.sha256.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)   # a hash, not the token: readable
        with os.fdopen(fd, "w") as f:
            f.write(h + "\n")
        os.replace(ctl / "puller.sha256.new", ctl / "puller.sha256")
        if a.control:   # root writes it, the page's own user reads it (the umask above would make it 0600)
            os.chmod(ctl, 0o755)
            os.chmod(ctl / "puller.sha256", 0o644)
        print("puller: hash set")
        return 0
    if a.cmd in ("close", "open"):
        f = Path(a.control or a.state) / "closed"
        if a.cmd == "close":
            f.touch(mode=0o600)
        elif f.exists():
            f.unlink()
        print(f"page: {'closed' if a.cmd == 'close' else 'open (if the control plane reports)'}")
        return 0
    page = Page(a.state, cfg, control=a.control)
    if a.cmd == "status":
        print(json.dumps(page.status(), sort_keys=True))
        return 0
    srv = make_server(page, a.listen)
    print(f"page: serving on {a.listen}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
