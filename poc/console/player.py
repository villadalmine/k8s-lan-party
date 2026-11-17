#!/usr/bin/env python3
"""Player-side console: a loopback-only view backed only by the player's namespace kubeconfig.

The player daemon must run as the player, never root. It does not use host state, host keys, or the host console. The token expiry is
decoded only for the displayed countdown; Kubernetes remains the authority and can revoke access sooner.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import http.client
import http.server
import ipaddress
import json
import os
import pwd
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
POC = HERE.parent
sys.path.insert(0, str(HERE))
import actions  # noqa: E402
import server  # noqa: E402

NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")
PEER = re.compile(r"[a-z][a-z0-9-]{1,19}")
PORT_LIST = re.compile(r"[0-9]{1,5}/(?:TCP|UDP)(?:,[0-9]{1,5}/(?:TCP|UDP))*")
NEBULA_PATH = re.compile(r"(?<![\w.-])/etc/nebula(?:/[^\s\"'<>]*)?")
DOOM_IMAGE = re.compile(r"localhost/emesh-doom:12\.3\.0-[a-f0-9]{12}")
DEFAULT_PORTS = ["10666/UDP"]
MAX_BODY = 4096
STATIC = HERE / "player-static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/player.js": ("player.js", "application/javascript; charset=utf-8"),
                "/player.css": ("player.css", "text/css; charset=utf-8")}
PLAYER_SECURITY_HEADERS = {
    **server.SECURITY_HEADERS,
    # Frames are fetched with the console bearer token and displayed from short-lived Blob URLs, never from token-bearing URLs.
    "Content-Security-Policy": server.SECURITY_HEADERS["Content-Security-Policy"].replace("img-src 'self'", "img-src 'self' blob:"),
}


class PlayerError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "edge-mesh"


def kubeconfig_path(home: Path | None = None) -> Path:
    return config_dir(home) / "kubeconfig"


def namespace_from_kubeconfig(path: Path) -> tuple[str, str]:
    """Read the namespace and identity from the generated context without loading client credentials into the API model."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise PlayerError(2, "kubeconfig", "no player kubeconfig; join first with edge-mesh") from None
    # participant.kubeconfig writes a deliberately small, stable YAML document. Do not accept arbitrary contexts or namespaces.
    ns = re.findall(r"^    namespace: (play-[a-z][a-z0-9-]{1,19})$", raw, re.M)
    current = re.findall(r"^current-context: ([a-z][a-z0-9-]{1,19})$", raw, re.M)
    if (len(ns) != 1 or len(current) != 1 or ns[0] != "play-" + current[0] or raw.count("\n- name:") != 3 or
            len(re.findall(r"^    server:", raw, re.M)) != 1 or len(re.findall(r"^    token:", raw, re.M)) != 1):
        raise PlayerError(2, "kubeconfig", "the kubeconfig is not an edge-mesh player credential")
    return ns[0], current[0]


def token_claims(path: Path) -> dict:
    """Read the JWT payload from the generated kubeconfig. This is a display hint only, never an authorization check."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise PlayerError(2, "kubeconfig", "no player kubeconfig; join first with edge-mesh") from None
    match = re.search(r"^    token: ([A-Za-z0-9_.-]+)$", raw, re.M)
    try:
        parts = match.group(1).split(".") if match else []
        if len(parts) != 3:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp, sub = payload.get("exp"), payload.get("sub")
        if type(exp) is not int or exp <= 0 or not isinstance(sub, str):
            raise ValueError
        return payload
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        raise PlayerError(2, "kubeconfig", "the player token has no readable expiry") from None


def token_exp(path: Path) -> int:
    return token_claims(path)["exp"]


def api_server(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
        match = re.search(r"^    server: (https://[^\s]+)$", raw, re.M)
        if not match:
            raise ValueError
        from urllib.parse import urlsplit
        u = urlsplit(match.group(1))
        addr = ipaddress.ip_address(u.hostname or "")
        if (u.scheme != "https" or u.port != 6443 or u.path not in ("", "/") or u.query or u.fragment or
                not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast):
            raise ValueError
        return match.group(1)
    except (OSError, ValueError):
        raise PlayerError(2, "kubeconfig", "the kubeconfig API must be an HTTPS private IP on port 6443") from None


def _open_dir(path: Path) -> int:
    """Open an existing absolute directory component by component without following symlinks."""
    if not path.is_absolute():
        raise ValueError("directory must be absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise ValueError("unsafe path")
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child_dir(parent_fd: int, name: str, uid: int, gid: int, *, create: bool) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.chown(name, uid, gid, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    st = os.fstat(fd)
    if st.st_uid != uid or st.st_mode & 0o022:
        os.close(fd)
        raise PermissionError(f"{name} must be owned by the player and not group/world writable")
    if name == "edge-mesh" and stat.S_IMODE(st.st_mode) != 0o700:
        os.fchmod(fd, 0o700)
    return fd


def install_kubeconfig(source: Path, home: Path, uid: int, gid: int) -> Path:
    """Atomically install the player's user credential at ~/.config/edge-mesh/kubeconfig, mode 0600."""
    ns, _ = namespace_from_kubeconfig(source)
    claims = token_claims(source)
    if claims.get("sub") != f"system:serviceaccount:{ns}:user":
        raise ValueError("the bundle kubeconfig is not this player's namespace credential")
    api_server(source)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        src_st = os.fstat(source_fd)
        if not stat.S_ISREG(src_st.st_mode) or src_st.st_size > 1_000_000:
            raise ValueError("player kubeconfig must be a regular file under 1 MB")
        chunks, remaining = [], src_st.st_size
        while remaining:
            chunk = os.read(source_fd, min(65536, remaining))
            if not chunk:
                raise ValueError("player kubeconfig changed while being read")
            chunks.append(chunk); remaining -= len(chunk)
        content = b"".join(chunks)
    finally:
        os.close(source_fd)
    # Resolve normal system aliases first, then reject any symlink introduced after this point as components are opened.
    home = home.resolve(strict=True)
    root_fd = _open_dir(home)
    config_fd = edge_fd = -1
    try:
        home_st = os.fstat(root_fd)
        if home_st.st_uid != uid:
            raise PermissionError("player home directory is not owned by that account")
        config_fd = _child_dir(root_fd, ".config", uid, gid, create=True)
        edge_fd = _child_dir(config_fd, "edge-mesh", uid, gid, create=True)
        try:
            old = os.stat("kubeconfig", dir_fd=edge_fd, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode) or old.st_uid != uid:
                raise PermissionError("existing player kubeconfig is not a regular file owned by that account")
        except FileNotFoundError:
            pass
        tmp = ".kubeconfig." + secrets.token_hex(8)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=edge_fd)
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            os.unlink(tmp, dir_fd=edge_fd)
            raise
        finally:
            os.close(fd)
        os.replace(tmp, "kubeconfig", src_dir_fd=edge_fd, dst_dir_fd=edge_fd)
        os.fsync(edge_fd)
    finally:
        for fd in (edge_fd, config_fd, root_fd):
            if fd >= 0:
                os.close(fd)
    return home / ".config" / "edge-mesh" / "kubeconfig"


class Player:
    def __init__(self, kubeconfig: Path, kubectl: str | None = None, clock=time.time, runner=subprocess.run):
        self.kubeconfig = kubeconfig
        self.state = config_dir()
        if kubectl:
            self.kubectl = [kubectl]
        elif shutil.which("kubectl"):
            self.kubectl = [shutil.which("kubectl")]
        elif shutil.which("k3s"):
            self.kubectl = [shutil.which("k3s"), "kubectl"]
        else:
            raise PlayerError(2, "kubectl", "install kubectl or k3s before starting the player console")
        self.clock = clock
        self.runner = runner
        self.namespace, self.name = namespace_from_kubeconfig(kubeconfig)
        claims = token_claims(kubeconfig)
        self.expires_at = claims["exp"]
        if claims.get("sub") != f"system:serviceaccount:{self.namespace}:user":
            raise PlayerError(2, "kubeconfig", "the kubeconfig token is not this player's namespace credential")
        self.api_server = api_server(kubeconfig)
        self.redact = server.Redactor(self.state)

    def _run(self, args: list[str], *, stdin: bytes | None = None):
        try:
            r = self.runner([*self.kubectl, "--kubeconfig", str(self.kubeconfig), *args], input=stdin,
                            capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            raise PlayerError(504, "timeout", "the cluster did not answer in 15 seconds") from None
        except OSError:
            raise PlayerError(502, "kubectl", "kubectl could not be started") from None
        if r.returncode:
            err = (r.stderr or b"").decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
            if "Unauthorized" in err or "must be logged in" in err:
                raise PlayerError(401, "expired", "your access ended or was revoked; ask the host for a new place")
            if "Forbidden" in err:
                raise PlayerError(403, "outside-namespace", "outside your namespace: the API refused this request")
            raise PlayerError(502, "api", "the Kubernetes API request failed")
        out = r.stdout if isinstance(r.stdout, str) else r.stdout.decode("utf-8", "replace")
        try:
            return json.loads(out)
        except ValueError:
            raise PlayerError(502, "api", "the Kubernetes API returned invalid JSON") from None

    def get(self, route: str, query: dict):
        now = int(self.clock())
        if route == "health":
            return {"ok": True, "role": "player", "name": self.name}
        if route == "status":
            # The player RBAC is intentionally namespace-only. Show the local Nebula link and own namespace API reachability,
            # never query cluster-scoped Nodes.
            try:
                local = subprocess.run(["ip", "-json", "addr", "show", "dev", "nebula1"], capture_output=True, text=True, timeout=3)
                nebula = local.returncode == 0 and '"operstate":"UP"' in local.stdout.replace(" ", "")
            except (OSError, subprocess.TimeoutExpired):
                nebula = False
            try:
                pods = self._run(["get", "pods", "-n", self.namespace, "-o", "json"])
                api_state, count = "reachable", len(pods.get("items", [])) if isinstance(pods, dict) else 0
            except PlayerError as e:
                if e.status != 401:
                    raise
                api_state, count = "expired or revoked", 0
            return {"name": self.name, "namespace": self.namespace, "expires_at": self.expires_at,
                    "seconds_left": max(0, self.expires_at - now), "node": {"nebula": "up" if nebula else "down",
                    "api": api_state, "note": "local Nebula and own namespace API connectivity; does not report Kubernetes Node Ready"}, "workloads": count}
        if route == "events":
            return self._run(["get", "events", "-n", self.namespace, "--sort-by=.lastTimestamp", "-o", "json"])
        if route in ("parties", "games"):
            data = self._run(["get", "configmaps", "-n", self.namespace, "-l", "edge-mesh/party-consent", "-o", "json"])
            watchers = {p.get("metadata", {}).get("name", "")[len("doom-watch-"):] for p in self._watch_pods()}
            rows = []
            for cm in data.get("items", []):
                meta, body = cm.get("metadata", {}), cm.get("data", {})
                peer = body.get("peer", "")
                if not PEER.fullmatch(peer) or meta.get("name") != "party-" + peer:
                    continue
                status = (meta.get("annotations") or {}).get("edge-mesh/party-status", "waiting for host reconciliation")
                rows.append({"peer": peer, "ports": body.get("ports", ""), "status": status,
                             "watching": peer in watchers})
            if route == "games":
                # a party with watch-host is the host watching this player's game: it serves no game to watch (PR #112 review)
                active = [r for r in rows if r["status"].startswith("active:") and r["peer"] != "watch-host"]
                active_peers = {r["peer"] for r in active}
                active.extend({"peer": peer, "ports": "", "status": "inactive: party ended; stop the spectator", "watching": True}
                              for peer in sorted(watchers - active_peers))
                return active
            return rows
        if route == "actions":
            return [{"verb": v, "summary": desc, "params": params} for v, desc, params in (
                ("party-invite", "ask a player to form a party", {"peer": "player name", "ports": "comma-separated allowed ports (optional)"}),
                ("party-accept", "accept a player's invitation", {"peer": "player name", "ports": "comma-separated allowed ports (optional)"}),
                ("party-leave", "withdraw one or all of your consents", {"peer": "player name (optional)"}),)]
        if route == "verbs":
            return actions.verbs("player")
        raise PlayerError(404, "not-found", "no such player view")

    def _watch_pod(self, peer: str):
        data = self._run(["get", "pods", "-n", self.namespace, "-o", "json"])
        for pod in data.get("items", []):
            if pod.get("metadata", {}).get("name") == "doom-watch-" + peer:
                return pod
        return None

    def _watch_pods(self):
        data = self._run(["get", "pods", "-n", self.namespace, "-o", "json"])
        return [pod for pod in data.get("items", [])
                if re.fullmatch(r"doom-watch-[a-z][a-z0-9-]{1,19}", pod.get("metadata", {}).get("name", ""))]

    def watch_frame(self, peer: str) -> bytes:
        if not PEER.fullmatch(peer) or peer == self.name:
            raise PlayerError(400, "bad-peer", "peer must be a game participant")
        if not any(g["peer"] == peer and g["status"].startswith("active:") for g in self.get("games", {})):
            raise PlayerError(404, "watch-unavailable", "the party is no longer active")
        pod = self._watch_pod(peer)
        spec = pod.get("spec", {}) if pod else {}
        status = pod.get("status", {}) if pod else {}
        containers = spec.get("containers", [])
        env = {item.get("name"): item.get("value") for item in containers[0].get("env", [])} if containers else {}
        token = env.get("WATCH_TOKEN", "")
        try:
            addr = ipaddress.ip_address(status.get("podIP", ""))
        except ValueError:
            addr = None
        if (not pod or pod.get("status", {}).get("phase") != "Running" or spec.get("nodeName") != self.name or
                not addr or not addr.is_private or addr.is_loopback or
                addr.is_link_local or addr.is_multicast or not re.fullmatch(r"[a-f0-9]{64}", token)):
            raise PlayerError(404, "watch-unavailable", "the spectator is not ready")
        conn = http.client.HTTPConnection(addr.compressed, 17777, timeout=2)
        try:
            conn.request("GET", "/frame", headers={"Authorization": "Bearer " + token, "Connection": "close"})
            response = conn.getresponse()
            data = response.read(2_000_001)
            if response.status != 200 or len(data) > 2_000_000 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise PlayerError(502, "watch-frame", "the spectator returned an invalid frame")
            return data
        except (TimeoutError, OSError, http.client.HTTPException):
            raise PlayerError(503, "watch-frame", "the spectator frame is not available yet") from None
        finally:
            conn.close()

    def act(self, verb: str, body):
        allowed = {"peer", "ports"} if verb.startswith("party-") else {"peer", "image"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise PlayerError(400, "bad-body", "send only the fields for this action")
        peer = body.get("peer", "")
        if verb == "watch-start":
            image = body.get("image", "")
            if not isinstance(peer, str) or not PEER.fullmatch(peer) or peer in (self.name, "watch-host"):
                raise PlayerError(400, "bad-peer", "peer must be a game participant")
            if not any(game["peer"] == peer for game in self.get("games", {})):
                raise PlayerError(403, "not-a-game", "watching needs an active party with that game")
            if not isinstance(image, str) or not DOOM_IMAGE.fullmatch(image):
                raise PlayerError(400, "bad-image", "build or import the pinned local Doom image first")
            existing = self._watch_pods()
            if existing:
                raise PlayerError(409, "watch-exists", "stop the existing spectator before starting another")
            token = secrets.token_hex(32)
            env = {**os.environ, "WATCH_TOKEN": token}
            try:
                rendered = subprocess.run([str(POC / "doom" / "pod.sh"), "watcher", image, peer, self.name],
                                          capture_output=True, timeout=10, check=False, env=env)
            except (OSError, subprocess.TimeoutExpired):
                raise PlayerError(502, "watcher", "could not prepare the spectator pod") from None
            if rendered.returncode:
                raise PlayerError(400, "watcher", "the spectator manifest was rejected")
            self._run(["apply", "-n", self.namespace, "-f", "-", "-o", "json"], stdin=rendered.stdout)
            return {"message": f"spectator starting for {peer}"}
        if verb == "watch-stop":
            if not isinstance(peer, str) or not PEER.fullmatch(peer) or peer == self.name:
                raise PlayerError(400, "bad-peer", "peer must be a game participant")
            self._run(["delete", "pod", "doom-watch-" + peer, "-n", self.namespace,
                       "--ignore-not-found", "--wait=false"])
            return {"message": f"spectator stopped for {peer}"}
        if verb in ("party-invite", "party-accept"):
            if not isinstance(peer, str) or not PEER.fullmatch(peer):
                raise PlayerError(400, "bad-peer", "peer must be a player name")
            if peer == self.name:
                raise PlayerError(400, "bad-peer", "a party needs another player")
            ports = body.get("ports", "")
            if not isinstance(ports, str) or len(ports) > 256 or (ports and (not PORT_LIST.fullmatch(ports) or
                    any(not 0 < int(p.split("/")[0]) < 65536 for p in ports.split(",")))):
                raise PlayerError(400, "bad-ports", "ports must be comma-separated port/TCP or port/UDP values")
            cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "party-" + peer,
                  "labels": {"edge-mesh/party-consent": "yes"}}, "data": {"peer": peer, "ports": ports}}
            self._run(["apply", "-n", self.namespace, "-f", "-"], stdin=json.dumps(cm).encode())
            return {"message": f"consent recorded for {peer}; the host opens the party after both sides agree"}
        if verb == "party-leave":
            if peer and (not isinstance(peer, str) or not PEER.fullmatch(peer)):
                raise PlayerError(400, "bad-peer", "peer must be a player name")
            if peer:
                self._run(["delete", "configmap", "party-" + peer, "-n", self.namespace, "--ignore-not-found", "--wait=false"])
            else:
                self._run(["delete", "configmaps", "-n", self.namespace, "-l", "edge-mesh/party-consent", "--ignore-not-found", "--wait=false"])
            return {"message": "consent withdrawn; the host closes the party during reconciliation"}
        raise PlayerError(403, "role", "that action is not available to the player role")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "edge-mesh-player-console"
    sys_version = ""
    player: Player = None
    port = 0
    gate: server.Gate = None
    static: dict = {}

    def log_message(self, fmt, *args):
        pass

    def send(self, status: int, obj, *, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(obj, bytes):
            obj = redact(obj, self.player.redact)
        body = obj if isinstance(obj, bytes) else (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        self.send_response(status)
        for k, v in {**PLAYER_SECURITY_HEADERS, "Cache-Control": "no-store", **(headers or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def host_ok(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        origin = self.headers.get("Origin")
        return host in allowed and (origin is None or origin in {"http://" + h for h in allowed})

    def route(self):
        raw, _, query_text = self.path.partition("?")
        if not self.host_ok():
            return self.send(403, {"error": "host", "message": "use the loopback address printed by edge-mesh console"})
        if self.command == "GET" and raw in self.static:
            body, ctype = self.static[raw]
            return self.send(200, body, ctype=ctype)
        if self.command not in ("GET", "POST"):
            return self.send(405, {"error": "method", "message": "only GET and POST"}, headers={"Allow": "GET, POST"})
        if self.gate.locked(self.stored()):
            return self.send(429, {"error": "locked", "message": "too many wrong tokens; wait 10 minutes or run edge-mesh console url"},
                             headers={"Retry-After": str(server.LOCKOUT)})
        auth = self.headers.get("Authorization", "")
        want = self.stored()
        if not auth.startswith("Bearer "):
            return self.send(401, {"error": "token", "message": "open the one-time link printed by edge-mesh console url"},
                             headers={"WWW-Authenticate": "Bearer"})
        if not want or not secrets.compare_digest(server.digest(auth[7:].strip()), want):
            self.gate.count()
            return self.send(401, {"error": "token", "message": "wrong or expired console token"},
                             headers={"WWW-Authenticate": "Bearer"})
        if not raw.startswith("/api/v1/") or "//" in raw or "/../" in raw + "/" or "\\" in raw:
            return self.send(404, {"error": "not-found", "message": "no such route"})
        route = raw[len("/api/v1/"):]
        try:
            if self.command == "GET":
                match = re.fullmatch(r"watch/([a-z][a-z0-9-]{1,19})/frame", route)
                if match:
                    if query_text:
                        return self.send(400, {"error": "query", "message": "the frame route takes no query parameters"})
                    return self.send(200, self.player.watch_frame(match.group(1)), ctype="image/jpeg")
                query = parse_qs(query_text, keep_blank_values=True, strict_parsing=bool(query_text), max_num_fields=4)
                return self.send(200, self.player.get(route, query))
            n = int(self.headers.get("Content-Length", "0"))
            if n < 1 or n > MAX_BODY or self.headers.get_content_type() != "application/json":
                return self.send(400, {"error": "bad-body", "message": "send a JSON object no larger than 4096 bytes"})
            body = json.loads(self.rfile.read(n))
            if not route.startswith("actions/"):
                return self.send(403, {"error": "role", "message": "the player console has no host actions"})
            return self.send(202, self.player.act(route[len("actions/"):], body))
        except PlayerError as e:
            return self.send(e.status, {"error": e.code, "message": e.message})
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return self.send(400, {"error": "bad-body", "message": "the request body must be valid JSON"})
        except Exception as e:
            sys.stderr.write(f"player console: {e.__class__.__name__}\n")
            return self.send(500, {"error": "internal", "message": "the player console could not answer"})

    def do_GET(self): self.route()
    def do_HEAD(self): self.route()
    def do_POST(self): self.route()
    def do_PUT(self): self.route()
    def do_DELETE(self): self.route()
    def do_PATCH(self): self.route()
    def do_OPTIONS(self): self.route()
    def do_TRACE(self): self.route()
    def do_CONNECT(self): self.route()

    def stored(self) -> str:
        try:
            return server.token_file(self.player.state).read_text(encoding="ascii").strip()
        except OSError:
            return ""


def redact(obj, redactor):
    if isinstance(obj, str):
        return NEBULA_PATH.sub("<node-credential-path>", redactor.text(obj))
    if isinstance(obj, list):
        return [redact(v, redactor) for v in obj]
    if isinstance(obj, dict):
        return {redact(k, redactor): redact(v, redactor) for k, v in obj.items()}
    return obj


def serve(addr: str, kubeconfig: Path):
    if os.geteuid() == 0:
        raise server.fail("player console must run as the player's user, never root")
    host, port = server.web_address(addr)
    st = kubeconfig.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise server.fail("the player kubeconfig must be a regular file owned by this user with mode 0600")
    player = Player(kubeconfig)
    state = config_dir()
    if not state.is_dir():
        raise server.fail(f"{state} does not exist; edge-mesh join installs the player kubeconfig")
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise server.fail(f"{state} must be a 0700 directory owned by this user")
    handler = type("PlayerWebHandler", (Handler,), {"player": player, "port": port, "gate": server.Gate(),
                    "static": {r: ((STATIC / n).read_bytes(), c) for r, (n, c) in STATIC_FILES.items()}})
    web = server.WebServer((host, port), handler)
    handler.port = web.server_address[1]
    tok = server.new_token(state)
    shown = f"[{host}]" if ":" in host else host
    print(f"player console: http://{shown}:{port}/#t={tok}", flush=True)
    print(f"player console: listening on loopback as {getpass.getuser()} for {player.namespace}; Ctrl-C to stop", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        server.token_file(state).unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--web", default="127.0.0.1:8091")
    p.add_argument("--kubeconfig", type=Path, default=kubeconfig_path())
    p = sub.add_parser("url")
    p.add_argument("--web", default="127.0.0.1:8091")
    p = sub.add_parser("rotate-token")
    p.add_argument("--state", type=Path, default=config_dir())
    p = sub.add_parser("install-kubeconfig")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--user", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "install-kubeconfig":
        if os.geteuid() != 0:
            raise server.fail("install-kubeconfig is called by edge-mesh join running as root")
        try:
            account = pwd.getpwnam(a.user)
            if account.pw_uid == 0 or not account.pw_dir.startswith("/"):
                raise ValueError("choose a non-root local user with an absolute home directory")
            target = install_kubeconfig(a.source, Path(account.pw_dir), account.pw_uid, account.pw_gid)
        except (KeyError, OSError, ValueError) as e:
            raise server.fail(f"cannot safely install the player kubeconfig for {a.user}: {e}") from None
        print(f"player kubeconfig: installed for {a.user} at ~/.config/edge-mesh/kubeconfig (0600)")
        return 0
    if a.cmd == "url":
        if os.geteuid() == 0:
            raise server.fail("player console must run as the player's user, never root")
        host, port = server.web_address(a.web)
        state = config_dir()
        if not state.is_dir() or state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
            raise server.fail("~/.config/edge-mesh must be a 0700 directory owned by this user")
        tok = server.new_token(state)
        shown = f"[{host}]" if ":" in host else host
        print(f"http://{shown}:{port}/#t={tok}")
        return 0
    if a.cmd == "rotate-token":
        if os.geteuid() == 0:
            raise server.fail("player console must run as the player's user, never root")
        server.new_token(a.state)
        print("player console: token replaced")
        return 0
    try:
        serve(a.web, a.kubeconfig)
    except SystemExit:
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
