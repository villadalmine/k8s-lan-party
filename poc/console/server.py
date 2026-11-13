#!/usr/bin/env python3
"""The host console's API daemon (docs/CONSOLE.md; issue #86: the daemon and its reads).

It listens on a unix socket in the host's state directory and answers only the host's own user (the peer's uid, SO_PEERCRED, must be
this process's): role `local`. It runs the engine (`poc/mesh`, with EMESH_DIRECT=1) as an argument list, never a shell, and only its
pure reads; every response passes through one redaction step. Mutations (`POST /api/v1/actions/{verb}`) come with #87 and answer 501
until then; the web listener comes with #89.

    server.py serve --state DIR [--mesh PATH] [--socket PATH]     (`mesh host console` runs it in the foreground)

    GET /api/v1/health | status | requests | leases | players | players/{name}/activity | parties | events | audit | metrics | actions
    GET /api/v1/jobs/{id}

Python standard library only (and poc/page/page.py's security headers and strict body rules, by import). Linux: SO_PEERCRED.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
POC = HERE.parent
for _p in (HERE, POC / "page", POC):
    sys.path.insert(0, str(_p))
import actions  # noqa: E402
from page import SECURITY_HEADERS  # noqa: E402

VERSION = "0.1.0"
SUN_PATH_MAX = 107                     # bytes of a unix socket path (108 with the terminating NUL)
ENGINE_TIMEOUT = 60                    # seconds a read of the engine may take (it waits for the ledger's lock behind a tick)
MAX_CONNECTIONS = 16                   # handled at once; more get a 503
SOCKET_TIMEOUT = 10                    # a client that sends nothing for this long is dropped
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
ROUTES = ("health", "status", "requests", "leases", "players", "parties", "events", "audit", "metrics", "actions")


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
    def __init__(self, state: Path, mesh: Path, given: Path | None = None):
        self.state, self.mesh = state, mesh
        self.redact = Redactor(state, given)

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

    def get(self, path: str, query: dict) -> tuple[int, object, str]:
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
            raise Refusal(404, "no-such-job", "no such job (mutations come with #87)")
        if path == "health":
            return 200, {"ok": True, "version": VERSION, "role": "local"}, "json"
        if path == "status":
            return 200, self.status(), "json"
        if path in ("requests", "leases", "players", "parties"):
            return 200, self.read(path), "json"
        if path == "metrics":
            return 200, self.read("metrics"), "text"
        if path == "actions":
            return 200, actions.describe("local"), "json"
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
    server_version = "k8s-lan-party-console"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.timeout = SOCKET_TIMEOUT
        super().setup()

    def address_string(self):   # a unix socket has no address
        return "local"

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

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        body = json.dumps(self.console.redact(obj), sort_keys=True) + "\n"
        self._send(status, body.encode(), "application/json; charset=utf-8", extra)

    def _route(self, method: str) -> None:
        raw, _, q = self.path.partition("?")
        try:
            if not raw.startswith("/api/v1/") or "//" in raw or "/../" in raw + "/" or "\\" in raw:
                raise Refusal(404, "not-found", "no such route")
            path = raw[len("/api/v1/"):].rstrip("/")
            try:
                query = parse_qs(q, keep_blank_values=True, strict_parsing=bool(q), max_num_fields=4)
            except ValueError:
                raise Refusal(400, "bad-query", "the query string is not valid") from None
            if method == "POST":
                self._drain()
            if method == "GET" and path.startswith("actions/"):
                self._json(405, {"error": "method", "message": "not allowed here"}, {"Allow": "POST"})
            elif method == "GET":
                status, body, kind = self.console.get(path, query)
                if kind == "text":
                    self._send(status, self.console.redact.text(body).encode(), "text/plain; version=0.0.4; charset=utf-8")
                else:
                    self._json(status, body)
            elif method == "POST" and path.startswith("actions/"):
                verb = path[len("actions/"):]
                if verb not in actions.REGISTRY:
                    raise Refusal(404, "no-such-verb", "not a verb of the registry")
                raise Refusal(501, "not-built", "mutations through the console come with issue #87; use mesh directly")
            elif path in ROUTES or path.startswith(("players/", "jobs/", "actions/")):
                self._json(405, {"error": "method", "message": "not allowed here"}, {"Allow": "POST" if path.startswith("actions/") else "GET"})
            else:
                raise Refusal(404, "not-found", "no such route")
        except Refusal as e:
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


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, handler, uid: int):
        self.uid = uid
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(path, handler)

    def verify_request(self, request, client_address) -> bool:
        """Only the daemon's own user: anyone else is closed before a byte is read (the 0700 directory stops them first)."""
        try:
            return peer_uid(request) == self.uid
        except OSError:
            return False

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


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
    a = ap.parse_args(argv)
    given = Path(os.path.abspath(a.state))
    state = given.resolve()
    path = socket_path(state, a.socket)
    srv = make_server(state, Path(a.mesh).resolve(), path, given)
    print(f"console: listening on {path} (role local; the host's user only)", flush=True)

    def stop(*_):   # systemd's SIGTERM: leave through the finally below, which removes the socket
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
