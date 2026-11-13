#!/usr/bin/env python3
"""The host console's API daemon (docs/CONSOLE.md; issues #86 and #87: the daemon, its reads and its mutations).

It listens on a unix socket in the host's state directory and answers only the host's own user (the peer's uid, SO_PEERCRED, must be
this process's): role `local`. It runs the engine (`poc/mesh`, with EMESH_DIRECT=1) as an argument list, never a shell: for a GET only
pure reads; every response passes through one redaction step. Mutations are registry verbs run as jobs, with a plan and a confirm token
for the dangerous ones (#87, poc/console/mutate.py). With --web (#89) a second listener, on a loopback address only, serves the web
console (poc/console/static/, no auth: it holds nothing) and the same API to role `console`, behind a bearer token whose SHA-256 is
the only thing kept (<state>/console/web.token.sha256), Host and Origin checks, and a lockout after 10 wrong tokens.

    server.py serve --state DIR [--mesh PATH] [--socket PATH] [--web 127.0.0.1:8090]   (`mesh host console` runs it in the foreground)
    server.py url --state DIR [--web ADDR]       a new web token (the old one dies), printed once in a link: `mesh host console url`
    server.py rotate-token --state DIR           a new web token, never printed: the old one dies (`mesh host console rotate-token`)

    GET /api/v1/health | status | requests | leases | players | players/{name}/activity | parties | events | audit | metrics | actions
    GET /api/v1/jobs/{id}

    POST /api/v1/actions/{verb}   {"args": {...}} -> a job (202), or a plan and a confirm token (200); again with
                                  {"args": ..., "confirm": "<token>", "typed": "<name>|all"} -> the job (poc/console/mutate.py)

Python standard library only (and poc/page/page.py's security headers and strict body rules, by import). Linux: SO_PEERCRED.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
POC = HERE.parent
for _p in (HERE, POC / "page", POC):
    sys.path.insert(0, str(_p))
import actions  # noqa: E402
import mutate  # noqa: E402
from page import SECURITY_HEADERS, Handler as PageHandler, Refusal as PageRefusal  # noqa: E402

VERSION = "0.1.0"
SUN_PATH_MAX = 107                     # bytes of a unix socket path (108 with the terminating NUL)
ENGINE_TIMEOUT = 60                    # seconds a read of the engine may take (it waits for the ledger's lock behind a tick)
MAX_CONNECTIONS = 16                   # handled at once; more get a 503
SOCKET_TIMEOUT = 10                    # a client that sends nothing for this long is dropped
REQUEST_READ_SECONDS = 20              # the whole request (headers and body) must arrive within this, however slowly it drips
MAX_LINES = 500
MAX_BODY = 4096                        # page.py's limit for a request body
NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")
JOB = re.compile(r"[0-9a-f]{16}")
READS = {   # route -> the engine's pure read (argv after poc/mesh) and how its stdout is read
    "status": (["host", "admission-status", "--json", "--read-only"], "json"),
    "requests": (["host", "waitlist", "--json", "--read-only"], "json"),
    "leases": (["host", "leases", "--json", "--read-only"], "json"),
    "players": (["host", "players", "--json", "--read-only"], "json"),
    "parties": (["host", "party", "list", "--read-only", "--json"], "json"),
    "metrics": (["host", "admission-metrics", "--read-only"], "text"),
}
WEB_DEFAULT = "127.0.0.1:8090"
BAD_TOKENS, BAD_WINDOW, LOCKOUT = 10, 600, 600   # 10 wrong web tokens in 10 minutes: 429 on the web listener for 10 minutes
STATIC = HERE / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/console.js": ("console.js", "application/javascript; charset=utf-8"),
                "/console.css": ("console.css", "text/css; charset=utf-8")}
ROUTES = ("health", "status", "requests", "leases", "players", "parties", "events", "audit", "metrics", "actions")


class HungUp(Exception):
    """The read deadline hung this request up: nothing more is done for it, not even an answer."""


class Refusal(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


# --- redaction: what never reaches a client (docs/CONSOLE.md, "Redaction") ---------------------------------------------------

PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(-----END [A-Z0-9 ]+-----|\Z)", re.S)
K3S_TOKEN = re.compile(r"K10[0-9a-f]{8,}::[A-Za-z0-9._:-]+")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
CLAIM = re.compile(r"\b(?:[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]|[A-Z2-7]{26})\b")   # shown, or typed back raw
HOME = re.compile(r"(?<![\w.-])(?:/var)?/(?:home|Users)/[\w.-]+")


class Redactor:
    """Literal secrets read once at start (LH_RSH, the page puller's token), then patterns, then paths."""

    def __init__(self, state: Path, given: Path | None = None):
        # the state directory as the engine names it ($STATE, maybe through a symlink: /home vs /var/home) and as resolved
        self.states = sorted({str(state), str(given or state)}, key=len, reverse=True)
        self.literals: list[tuple[str, str]] = []
        for raw, value in (env_value(state / "lighthouse.env", "LH_RSH"), (os.environ.get("LH_RSH", ""),) * 2):
            for lit in {raw, value} - {""}:   # the file's %q form and the command it stands for (what revoke.sh prints)
                if len(lit) >= 8:   # a one-word value (poc-nat.sh sets LH_RSH=false) is no secret, and would eat "falsely"
                    self.literals.append((lit, "<LH_RSH>"))
        self.literals.sort(key=lambda kv: len(kv[0]), reverse=True)
        try:
            tok = (state / "page" / "puller.token").read_text(encoding="utf-8").strip()
            if tok:
                self.literals.append((tok, "<token>"))
        except OSError:
            pass

    def text(self, s: str) -> str:
        for lit, rep in self.literals:   # whole words only: never in the middle of a longer token
            s = re.sub(r"(?<![\w.-])" + re.escape(lit) + r"(?![\w.-])", rep, s)
        s = PEM.sub("<pem>", s)
        s = K3S_TOKEN.sub("<token>", s)
        s = JWT.sub("<token>", s)
        s = CLAIM.sub("<code>", s)
        for st in self.states:
            s = s.replace(st, "<state>")
        return HOME.sub("~", s)

    def __call__(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self(v) for v in obj]
        if isinstance(obj, dict):
            return {self.text(k): self(v) for k, v in obj.items()}
        return obj


def env_value(path: Path, key: str) -> tuple[str, str]:
    """(raw, value) of KEY=... in a shell env file the daemon never runs: raw as written (lighthouse-vps.sh writes `printf %q`, so
    `ssh\\ -i\\ key ...`), value with the shell's quoting undone by shlex (the command revoke.sh prints). ("", "") when absent."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                raw = line.split("=", 1)[1].strip()
                try:
                    value = " ".join(shlex.split(raw))
                except ValueError:
                    value = raw
                return raw, value
    except OSError:
        pass
    return "", ""


# --- the console: reads -------------------------------------------------------------------------------------------------------

class Console:
    def __init__(self, state: Path, mesh: Path, given: Path | None = None, kill_grace: int = mutate.KILL_GRACE):
        self.state, self.mesh = state, mesh
        self.redact = Redactor(state, given)
        self.plans = mutate.Plans()
        self.runner = mutate.Runner(state, mesh, self.redact, mutate.console_notify, kill_grace)

    def reads(self, route: str):
        return self.status() if route == "status" else self.read(route)

    def act(self, verb: str, body, role: str, cred: str, source: str, uid: int) -> tuple[int, dict]:
        """POST /api/v1/actions/{verb}: (status, body). Everything is checked before anything runs."""
        if not isinstance(body, dict) or set(body) - {"args", "confirm", "typed"}:
            raise Refusal(400, "bad-body", 'the body is {"args": {...}} and, to confirm, "confirm" and "typed"')
        args = body.get("args", {})
        argv = actions.argv(verb, args, role)   # unknown verb 404, role 403, bad args 400
        a = actions.REGISTRY[verb]
        self.runner.rate(role)
        if a.confirm == "none":
            job = mutate.Job(verb, argv, role, source, uid, args, False, a.timeout_seconds)
            return 202, self.runner.submit(job).view(self.redact)
        if "confirm" not in body:
            will = mutate.plan(verb, args, self.reads)
            tok = self.plans.issue(verb, args, cred, mutate.plan_hash(will))
            typed = {"type:name": args.get("name"), "type:all": "all"}.get(a.confirm)
            return 200, {"verb": verb, "args": args, "will": will, "confirm": tok, "expires_in": mutate.CONFIRM_SECONDS,
                         "type_to_confirm": typed}
        typed = {"type:name": args.get("name"), "type:all": "all"}.get(a.confirm)
        if typed is not None and body.get("typed") != typed:   # before the token is spent: a typo does not cost a new plan
            raise Refusal(400, "typed", f"type {typed!r} to confirm")
        want = self.plans.redeem(body["confirm"], verb, args, cred)
        if verb != "panic" and mutate.plan_hash(mutate.plan(verb, args, self.reads)) != want:   # panic: whatever the leases now
            raise Refusal(409, "plan-changed", "the state changed since the plan: ask for a new one")
        recheck = lambda: mutate.plan_hash(mutate.plan(verb, args, self.reads)) == want   # noqa: E731 - again when the job starts
        job = mutate.Job(verb, argv, role, source, uid, args, True, a.timeout_seconds, None if verb == "panic" else recheck)
        return 202, self.runner.submit(job).view(self.redact)

    def engine(self, args: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ, EMESH_DIRECT="1")
        try:
            return subprocess.run([str(self.mesh), *args], capture_output=True, text=True, timeout=ENGINE_TIMEOUT, env=env,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Refusal(504, "engine-timeout", f"the engine did not answer in {ENGINE_TIMEOUT} s") from None
        except OSError:
            raise Refusal(502, "engine", "the engine could not be started") from None

    def read(self, route: str):
        args, kind = READS[route]
        r = self.engine(args)
        if r.returncode != 0:
            raise Refusal(502, "engine", f"the engine's read failed (exit {r.returncode})")
        if kind == "text":
            return r.stdout
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise Refusal(502, "engine", "the engine's read was not JSON") from None

    def status(self) -> dict:
        s = self.read("status")
        if not isinstance(s, dict):
            raise Refusal(502, "engine", "the engine's status was not an object")
        s.pop("requests", None)   # GET /requests has them
        try:
            s["last_tick"] = int((self.state / "admission" / "last-tick").read_text().strip() or 0)
        except (OSError, ValueError):
            s["last_tick"] = 0
        return s

    def activity(self, name: str) -> dict:
        r = self.engine(["host", "activity", name])
        line = (r.stdout.strip().splitlines() or [""])[0]
        if r.returncode == 0:
            return {"name": name, "state": "active", "why": None}
        if r.returncode == 1 and line.startswith("idle"):
            return {"name": name, "state": "idle", "why": line.partition(":")[2].strip() or None}
        return {"name": name, "state": "unknown", "why": line.partition(":")[2].strip() or None}

    def tail(self, path: Path, limit: int, since: int = 0) -> list:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            ts = item.get("ts", 0) if isinstance(item, dict) else 0
            if isinstance(item, dict) and (not since or (isinstance(ts, int) and ts >= since)):
                out.append(item)
        return out[-limit:]

    def raw_job(self, view, role: str) -> dict | None:
        """A job's stdout, stderr and output as the engine printed them, for the `local` role (the host's own user, who can read the
        state directory anyway): the CLI through the API prints exactly what it prints direct (#88). Every other role: None."""
        if role != "local" or not isinstance(view, dict) or not isinstance(view.get("id"), str):
            return None
        job = self.runner.get(view["id"])
        return job.raw() if job is not None else None

    def get(self, path: str, query: dict, role: str = "local") -> tuple[int, object, str]:
        """(status, body, kind) for a GET; kind is json or text."""
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "players" and parts[1] != "":
            raise Refusal(404, "not-found", "no such route")
        if len(parts) == 3 and parts[0] == "players" and parts[2] == "activity":
            if not NAME.fullmatch(parts[1]):
                raise Refusal(400, "bad-name", "a name is 2 to 20 characters: a-z, 0-9 and '-', starting with a letter")
            return 200, self.activity(parts[1]), "json"
        if len(parts) == 2 and parts[0] == "jobs":
            if not JOB.fullmatch(parts[1]):
                raise Refusal(400, "bad-job", "a job id is 16 hex characters")
            job = self.runner.get(parts[1])
            if job is None:
                raise Refusal(404, "no-such-job", "no such job (jobs are kept an hour)")
            return 200, job.view(self.redact), "json"
        if path == "health":
            return 200, {"ok": True, "version": VERSION, "role": role}, "json"
        if path == "status":
            return 200, self.status(), "json"
        if path in ("requests", "leases", "players", "parties"):
            return 200, self.read(path), "json"
        if path == "metrics":
            return 200, self.read("metrics"), "text"
        if path == "actions":
            return 200, actions.describe(role), "json"
        if path in ("events", "audit"):
            limit = number(query, "limit", 100, 1, MAX_LINES)
            since = number(query, "since", 0, 0, 2 ** 40) if path == "events" else 0
            f = self.state / ("admission/events.jsonl" if path == "events" else "console/audit.jsonl")
            return 200, self.tail(f, limit, since), "json"
        raise Refusal(404, "not-found", "no such route")


def number(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = query.get(key)
    if v is None:
        return default
    if len(v) != 1 or not re.fullmatch(r"[0-9]{1,13}", v[0]) or not lo <= int(v[0]) <= hi:
        raise Refusal(400, "bad-query", f"{key} must be a whole number from {lo} to {hi}")
    return int(v[0])


# --- HTTP over the unix socket ---------------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    console: Console = None
    role = "local"        # the unix socket's role; a listener of another role (#89) sets its own
    server_version = "k8s-lan-party-console"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.timeout = SOCKET_TIMEOUT
        super().setup()

    def address_string(self):   # a unix socket has no address
        return "local"

    def who(self) -> tuple[str, str, int | None]:
        """(the credential a confirm token is bound to, the audit's source, the audit's uid): the socket's peer."""
        uid = peer_uid(self.connection)
        return f"uid:{uid}", "socket", uid

    def log_request(self, code="-", size="-"):   # no query, no body
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

    def _json(self, status: int, obj, extra: dict | None = None, raw: dict | None = None) -> None:
        """Everything redacted; `raw` (a job's own output, for the local role only: the #85 decision) is added after that step."""
        out = self.console.redact(obj)
        if raw:
            out = {**out, **raw}
        body = json.dumps(out, sort_keys=True) + "\n"
        self._send(status, body.encode(), "application/json; charset=utf-8", extra)

    def read_done(self) -> None:
        """The request is read: from here the work may take long (a plan's reads), and the read deadline no longer applies. If the
        deadline already hung it up (in the instant between the last byte and here), nothing is done for it: no job behind its back."""
        if not self.server.read_done(self.request):
            raise HungUp

    def _route(self, method: str) -> None:
        raw, _, q = self.path.partition("?")
        if method != "POST":
            self.read_done()
        try:
            if not raw.startswith("/api/v1/") or "//" in raw or "/../" in raw + "/" or "\\" in raw:
                raise Refusal(404, "not-found", "no such route")
            path = raw[len("/api/v1/"):].rstrip("/")
            try:
                query = parse_qs(q, keep_blank_values=True, strict_parsing=bool(q), max_num_fields=4)
            except ValueError:
                raise Refusal(400, "bad-query", "the query string is not valid") from None
            if method == "POST" and path.startswith("actions/"):
                verb = path[len("actions/"):]
                if verb not in actions.REGISTRY:
                    self._drain()
                    raise Refusal(404, "no-such-verb", "not a verb of the registry")
                try:
                    body = PageHandler._body(self, MAX_BODY)
                    self.read_done()
                except PageRefusal as e:
                    if e.status == 415:   # refused before the body was read: read it, so a client still sending it gets our answer
                        self._drain()
                    raise Refusal(e.status, e.error, e.message) from None
                cred, source, uid = self.who()
                status, out = self.console.act(verb, body, self.role, cred, source, uid)
                return self._json(status, out, raw=self.console.raw_job(out, self.role))
            if method == "POST":
                self._drain()
            if method == "GET" and path.startswith("actions/"):
                self._json(405, {"error": "method", "message": "not allowed here"}, {"Allow": "POST"})
            elif method == "GET":
                status, body, kind = self.console.get(path, query, self.role)
                if kind == "text":
                    self._send(status, self.console.redact.text(body).encode(), "text/plain; version=0.0.4; charset=utf-8")
                else:
                    self._json(status, body, raw=self.console.raw_job(body, self.role) if path.startswith("jobs/") else None)
            elif path in ROUTES or path.startswith(("players/", "jobs/", "actions/")):
                self._json(405, {"error": "method", "message": "not allowed here"}, {"Allow": "POST" if path.startswith("actions/") else "GET"})
            else:
                raise Refusal(404, "not-found", "no such route")
        except HungUp:
            return
        except (Refusal, actions.Refused) as e:
            self._json(e.status, {"error": e.error, "message": e.message})
        except Exception as e:   # never a traceback to the client
            sys.stderr.write(f"error: {e.__class__.__name__}\n")
            self._json(500, {"error": "internal", "message": "the console could not answer"})

    def _drain(self) -> None:
        """Read a small body we do not use yet, so the client sees our answer and not a reset connection (#87 parses it)."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _other(self):
        self._json(405, {"error": "method", "message": "only GET and POST"}, {"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other


def peer_uid(sock: socket.socket) -> int:
    """The connecting process's uid (Linux SO_PEERCRED: pid, uid, gid)."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


class Slots:
    """At most MAX_CONNECTIONS handled at once; more get a 503 at once; and a request that is not read within REQUEST_READ_SECONDS
    is hung up (a client dripping a byte every few seconds beats the per-read timeout, not this). A mixin of both listeners."""
    def read_done(self, request) -> bool:
        """Stop the request's read deadline: False when it already fired (the request was hung up)."""
        with self.timers_lock:
            t = self.timers.pop(id(request), None)
        if t:
            t.cancel()
        return t is not None

    def expire(self, request) -> None:
        with self.timers_lock:
            if self.timers.pop(id(request), None) is None:
                return   # read in time: the deadline lost the race
        _hang_up(request)

    def handle_error(self, request, client_address):
        """A client gone (hung up by the deadline, or it closed): one line, not a traceback for each."""
        e = sys.exc_info()[1]
        if isinstance(e, (OSError, ValueError)):
            sys.stderr.write(f"console: a client went away ({e.__class__.__name__})\n")
        else:
            super().handle_error(request, client_address)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        timer = threading.Timer(REQUEST_READ_SECONDS, self.expire, (request,))
        timer.daemon = True
        with self.timers_lock:
            self.timers[id(request)] = timer
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.read_done(request)
            self.slots.release()


def _hang_up(request) -> None:
    try:
        request.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Server(Slots, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, handler, uid: int):
        self.uid = uid
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.timers, self.timers_lock = {}, threading.Lock()
        super().__init__(path, handler)

    def verify_request(self, request, client_address) -> bool:
        """Only the daemon's own user: anyone else is closed before a byte is read (the 0700 directory stops them first)."""
        try:
            return peer_uid(request) == self.uid
        except OSError:
            return False

# --- the web listener (#89): loopback only, role console ---------------------------------------------------------------------------

def web_address(text: str) -> tuple[str, int]:
    """`127.0.0.1:8090` or `[::1]:8090`: a loopback IP literal and a port, or exit 2. Names are refused (one that resolves to a loopback
    today may not tomorrow), and so are 0.0.0.0, ::, the Nebula IP and any LAN address."""
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:([0-9]{1,5})|([0-9.]+):([0-9]{1,5})", text or "")
    if not m:
        raise fail(f"--web {text!r}: give a loopback address and a port, 127.0.0.1:8090 or [::1]:8090")
    host, port = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise fail(f"--web {text!r}: {host} is not an IP address") from None
    if not ip.is_loopback or isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        raise fail(f"--web {text!r}: the web console listens on the loopback only (127.0.0.0/8 or ::1); reach it through an SSH tunnel")
    if not 0 < int(port) < 65536:
        raise fail(f"--web {text!r}: no such port")
    return str(ip), int(port)


def token_file(state: Path) -> Path:
    return state / "console" / "web.token.sha256"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(state: Path) -> str:
    """32 random bytes (base64url); only its SHA-256 is written (0600, atomically): the old token dies at once."""
    d = state / "console"
    if not d.is_dir():
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".web.token.")   # 0600, a fresh name: a file left by a killed run is never in the way
    try:
        with os.fdopen(fd, "w") as f:
            f.write(digest(token) + "\n")
        os.replace(tmp, token_file(state))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return token


class Gate:
    """Wrong web tokens: 10 in 10 minutes lock the web listener for 10 minutes (the right token too). A missing token counts nothing."""
    def __init__(self, clock=None):
        self.clock = clock or __import__("time").monotonic
        self.wrong: collections.deque = collections.deque()
        self.until = 0.0
        self.seen = None   # the token hash the count is about
        self.lock = threading.Lock()

    def locked(self, current: str = "") -> bool:
        """Locked now? A new token (`mesh host console url` or `rotate-token` on the host) lifts the lockout and starts a new count:
        the lockout can only be the work of someone who lacks the new token, so it says nothing about it."""
        with self.lock:
            if current != self.seen:
                self.seen, self.until, self.wrong = current, 0.0, collections.deque()
            return self.clock() < self.until

    def count(self) -> None:
        with self.lock:
            now = self.clock()
            self.wrong.append(now)
            while self.wrong and self.wrong[0] <= now - BAD_WINDOW:
                self.wrong.popleft()
            if len(self.wrong) >= BAD_TOKENS:
                self.until, self.wrong = now + LOCKOUT, collections.deque()


class WebHandler(Handler):
    role = "console"
    port = 0
    bound = "127.0.0.1"   # the address it listens on, as a Host header writes it ([::1] for IPv6)
    gate: Gate = None
    static: dict = {}

    def address_string(self):
        return self.client_address[0]

    def who(self) -> tuple[str, str, int | None]:
        return f"web:{self.cred}", "web", None   # every web peer is 127.0.0.1 behind the tunnel: the token's hash is the caller

    def hosts(self) -> set[str]:
        return {f"{self.bound}:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}", f"localhost:{self.port}"}

    def foreign(self) -> bool:
        """A Host or an Origin that is not this listener's: DNS rebinding or another site. Answered 403, whatever the method."""
        host, origin = self.headers.get("Host", ""), self.headers.get("Origin")
        if host in self.hosts() and (origin is None or origin in {f"http://{h}" for h in self.hosts()}):
            return False
        self._drain()
        self._json(403, {"error": "host", "message": f"use http://{self.bound}:{self.port}/ (through an SSH tunnel)"})
        return True

    def _other(self):
        if not self.foreign():
            super()._other()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other

    def stored(self) -> str:
        try:
            return token_file(self.console.state).read_text().strip()
        except OSError:
            return ""

    def _route(self, method: str) -> None:
        if self.foreign():
            return
        raw = self.path.partition("?")[0]
        if method == "GET" and raw in self.static:
            body, ctype = self.static[raw]
            return self._send(200, body, ctype)
        want = self.stored()
        if self.gate.locked(want):
            self._drain()
            return self._json(429, {"error": "locked", "message": "too many wrong tokens: `mesh host console url` on the host makes a "
                                    "new link, which lifts this; or wait 10 minutes"}, {"Retry-After": str(LOCKOUT)})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._drain()   # a page that merely points at the loopback cannot lock the operator out
            return self._json(401, {"error": "token", "message": "no token: open the link of `mesh host console url`"},
                              {"WWW-Authenticate": "Bearer"})
        given = digest(auth[len("Bearer "):].strip())
        if not want or not hmac.compare_digest(given, want):
            self.gate.count()
            self._drain()
            return self._json(401, {"error": "token", "message": "wrong or old token: `mesh host console url` on the host"},
                              {"WWW-Authenticate": "Bearer"})
        self.cred = given[:32]
        super()._route(method)


class WebServer(Slots, socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], handler):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.timers, self.timers_lock = {}, threading.Lock()
        super().__init__(addr, handler)


def static_files() -> dict:
    return {route: ((STATIC / name).read_bytes(), ctype) for route, (name, ctype) in STATIC_FILES.items()}


def make_web(console_handler, addr: tuple[str, int]) -> WebServer:
    handler = type("WebConsoleHandler", (WebHandler,), {"console": console_handler.console, "port": addr[1], "gate": Gate(),
                                                        "static": static_files()})
    try:
        srv = WebServer(addr, handler)
    except OSError as e:
        raise fail(f"--web {addr[0]}:{addr[1]}: {e.strerror or e}") from None
    handler.port = srv.server_address[1]   # the Host a browser sends (port 0 in tests: the one the kernel chose)
    handler.bound = f"[{addr[0]}]" if ":" in addr[0] else addr[0]
    return srv


def fail(message: str) -> SystemExit:
    """A setup error: exit 2, which the systemd unit does not restart (RestartPreventExitStatus=2)."""
    print(f"console: {message}", file=sys.stderr)
    return SystemExit(2)


def socket_path(state: Path, given: str | None) -> Path:
    p = Path(given or os.environ.get("EMESH_CONSOLE_SOCK") or state / "console" / "api.sock")
    if len(os.fsencode(str(p))) > SUN_PATH_MAX:
        raise fail(f"the socket path {p} is longer than {SUN_PATH_MAX} bytes (a unix socket's limit): "
                   "set EMESH_CONSOLE_SOCK to a shorter path, for example under $XDG_RUNTIME_DIR")
    return p


def prepare(path: Path) -> None:
    """The socket's directory: made here 0700 when missing (its parent must exist: nothing else is created), or an existing one
    that is already ours and 0700 (never chmod'ed: a --socket pointing into another directory does not change that directory).
    No stale socket: a dead one is removed, a live one means another daemon (refused)."""
    d = path.parent
    if not d.parent.is_dir():
        raise fail(f"{d.parent} does not exist (is the game's state directory right?)")
    try:
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)   # mkdir's mode goes through the umask
    except FileExistsError:
        pass
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise fail(f"{d} is not a directory of this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise fail(f"{d} must be mode 0700 (it is {stat.S_IMODE(st.st_mode):o}); use a directory of its own")
    if path.is_symlink() or (path.exists() and not stat.S_ISSOCK(path.lstat().st_mode)):
        raise fail(f"{path} exists and is not a socket")
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            raise fail(f"another daemon answers on {path}")
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink()
        finally:
            probe.close()


def make_server(state: Path, mesh: Path, path: Path, given: Path | None = None) -> Server:
    if not hasattr(socket, "SO_PEERCRED"):
        raise fail("needs Linux (SO_PEERCRED to check who connects)")
    prepare(path)
    handler = type("ConsoleHandler", (Handler,), {"console": Console(state, mesh, given)})
    old = os.umask(0o077)
    try:
        srv = Server(str(path), handler, os.getuid())
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the host console's API daemon (docs/CONSOLE.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--state", required=True, help="the game's state directory (poc/.state, or EMESH_STATE)")
    p.add_argument("--mesh", default=str(POC / "mesh"))
    p.add_argument("--socket", help="default: EMESH_CONSOLE_SOCK, else <state>/console/api.sock")
    p.add_argument("--web", help="also serve the web console on this loopback address, e.g. 127.0.0.1:8090 (role console)")
    u = sub.add_parser("url", help="a new web token, printed once in a link (the old one dies)")
    u.add_argument("--state", required=True)
    u.add_argument("--web", default=WEB_DEFAULT, help=f"the address the daemon serves (default {WEB_DEFAULT})")
    r = sub.add_parser("rotate-token", help="a new web token, never printed: the old one dies")
    r.add_argument("--state", required=True)
    a = ap.parse_args(argv)
    given = Path(os.path.abspath(a.state))
    state = given.resolve()
    if a.cmd in ("url", "rotate-token"):
        if not state.is_dir():
            raise fail(f"{state} does not exist (is the game's state directory right?)")
        if a.cmd == "rotate-token":
            new_token(state)
            print("console: the web token was replaced; `mesh host console url` makes a link with a new one")
            return 0
        host, port = web_address(a.web)
        shown = f"[{host}]" if ":" in host else host
        print(f"http://{shown}:{port}/#t={new_token(state)}")
        print(f"console: open it through an SSH tunnel to the same port (ssh -N -L {port}:{shown}:{port} you@this-host); "
              "the previous link no longer works", file=sys.stderr)
        return 0
    web = web_address(a.web) if a.web else None   # refused before anything is created
    path = socket_path(state, a.socket)
    srv = make_server(state, Path(a.mesh).resolve(), path, given)
    print(f"console: listening on {path} (role local; the host's user only)", flush=True)
    wsrv = None
    if web:
        try:
            wsrv = make_web(srv.RequestHandlerClass, web)
        except SystemExit:   # the port is taken: leave nothing behind
            srv.server_close()
            path.unlink(missing_ok=True)
            raise
        shown = f"[{web[0]}]" if ":" in web[0] else web[0]
        print(f"console: web console on http://{shown}:{web[1]}/ (role console; `mesh host console url` prints a link)", flush=True)
        threading.Thread(target=wsrv.serve_forever, daemon=True).start()

    def stop(*_):   # systemd's SIGTERM: leave through the finally below, which removes the socket
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if wsrv:
            wsrv.shutdown()
            wsrv.server_close()
        srv.server_close()
        try:
            path.unlink()
        except OSError:
            pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
