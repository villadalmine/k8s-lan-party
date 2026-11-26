#!/usr/bin/env python3
"""The host's watcher identity (D6 of docs/CONSOLE-MODES.md, issue #101).

The host is not a player: it has no `play-<name>` namespace, and a process on the control plane is a remote node for a game server's
pod, so the party NetworkPolicies drop it. To watch a game "through a party" (D2) the host gets one namespace of its own, `watch-host`:

- labelled `edge-mesh/watcher=host` (party.py takes it as the peer `watch-host`; no player may take that name), Pod Security
  `restricted`, a ResourceQuota (the cap on spectators: `watch_quota_cpu_millis` and `watch_quota_pods` of pool.yaml, or a player's
  numbers when unset), a LimitRange and a NetworkPolicy like a player's: its pods talk to each other and to the cluster DNS, nothing
  else, until a party with a game's owner opens that one game's ports;
- its pods run only on a node of trust `own` that is not the control plane (DESIGN.md: no workload on the control plane). The placement
  policy (poc/placement/) already forces `trust=own` on a private pod; poc/watcher-policy.yaml adds the rest: a pod there may not name
  its node, may not tolerate the control plane's taint, and must carry the affinity that keeps it off every control-plane node.
- it has no ServiceAccount token, no Role and no kubeconfig: only the host (cluster-admin) writes there. The host's own views reach its
  spectator pods through the API server (D7: allowed for the host's own views only, which run on the host's own machines; see Tap).

A host without an `own` node that is not the control plane cannot watch: `mesh host watch init` refuses.

    watcher.py manifests --config pool.yaml     the objects, as one JSON List (kubectl apply -f -)
    watcher.py nodes                            stdin: kubectl get nodes -o json; prints the nodes a watcher pod may use (exit 1: none)
    watcher.py affinity                         the nodeAffinity every watcher pod carries, as JSON
    watcher.py status --kc KC --parties JSON    the host console's read: the watcher, its parties and its spectators (gets only)
    watcher.py frame --kc KC --pod P --token-file F   one JPEG of a spectator on stdout (Tap: kubectl exec into the host's own pod)

The host's views (D7, docs/CONSOLE-MODES.md): through the API server and the kubelet of the host's own machine to the spectator pod in
watch-host, never the mesh: allowed for the host's own views only, which run on the host's own machines anyway. `kubectl exec -i` into the
host's own spectator, which asks its own endpoint at 127.0.0.1 (a port-forward's local port would open wherever kubectl runs: in the control
plane's VM, which the console cannot reach). The endpoint still demands its per-pod secret (`mesh host watch start` writes it, 0600, under
the host's state, and the Tap sends it on stdin, never on a command line).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import participant  # noqa: E402
from party import HOUSE_LABEL, WATCHER, WATCHER_LABEL  # noqa: E402
from pool import load_config  # noqa: E402

TRUST = "node-restriction.kubernetes.io/trust"
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


def affinity() -> dict:
    """An own node that is not a control plane: both in one term, so a node must satisfy both."""
    return {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
        {"key": TRUST, "operator": "In", "values": ["own"]},
        {"key": CONTROL_PLANE, "operator": "DoesNotExist"}]}]}}


# the house (docs/LIVE-PAGE.md): its server, two robots and its spectator (1500m and 1280 MiB of limits); a ConfigMap per player it offers
HOUSE_QUOTA = {"cpu_millis": 2000, "pods": 6, "memory_mib": 1536, "configmaps": 60}


def manifests(cfg: dict, house: bool = False) -> dict:
    """A player's namespace objects without the player: no ServiceAccount, Role or binding; the watcher's own quota numbers. With
    house: the house match's namespace (docs/LIVE-PAGE.md L1), `house`, labelled edge-mesh/house=host and, for the same guard (own node,
    never the control plane: poc/watcher-policy.yaml), edge-mesh/watcher=host; its quota is house_quota_* of pool.yaml."""
    q = dict(cfg)
    prefix = "house_quota_" if house else "watch_quota_"
    for key in (("cpu_millis", "pods", "memory_mib", "configmaps") if house else ("cpu_millis", "pods")):
        if f"{prefix}{key}" in cfg:
            q[f"quota_{key}"] = cfg[f"{prefix}{key}"]
        elif house:
            q[f"quota_{key}"] = HOUSE_QUOTA[key]
    name, labels = ("house", {WATCHER_LABEL: "host", HOUSE_LABEL: "host"}) if house else (WATCHER, {WATCHER_LABEL: "host"})
    keep = {"Namespace", "ResourceQuota", "NetworkPolicy", "LimitRange"}
    items = []
    for obj in participant.manifests("watcher", q)["items"]:
        if obj["kind"] not in keep:
            continue
        obj = json.loads(json.dumps(obj))
        md = obj["metadata"]
        if obj["kind"] == "Namespace":
            md["name"] = name
            md["labels"].pop("edge-mesh/participant")
            md["labels"].update(labels)
        else:
            md["namespace"] = name
            md["labels"] = dict(labels)
        items.append(obj)
    return {"apiVersion": "v1", "kind": "List", "items": items}


def nodes(data: dict) -> list[str]:
    """The Ready nodes of trust own that are not a control plane, by name."""
    out = []
    for n in data.get("items", []):
        labels = n.get("metadata", {}).get("labels") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", []))
        taints = {t.get("key") for t in n.get("spec", {}).get("taints") or []}
        if labels.get(TRUST) == "own" and CONTROL_PLANE not in labels and CONTROL_PLANE not in taints and ready:
            out.append(n["metadata"]["name"])
    return sorted(out)


POD = re.compile(r"doom-watch-[a-z][a-z0-9-]{1,19}")
TOKEN = re.compile(r"[a-f0-9]{64}")
MAX_FRAME = 2_000_000
TIMEOUT = 20                           # seconds for one frame, the exec's start included


def status(kc: str, parties: list) -> dict:
    """What the host console shows: is there a watcher, which parties has it, which spectators run (and where, following whom)."""
    def get(*args):
        r = subprocess.run([kc, *args], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200])
        return json.loads(r.stdout)
    ns = get("get", "namespaces", "-l", f"{WATCHER_LABEL}=host", "-o", "json").get("items", [])
    rows = [{"owner": p["peer"], "status": p["status"]} for p in parties if p.get("player") == WATCHER]
    if not ns:
        return {"watcher": False, "parties": rows, "spectators": []}
    specs = []
    for pod in get("get", "pods", "-n", WATCHER, "-o", "json").get("items", []):
        name = pod.get("metadata", {}).get("name", "")
        if not POD.fullmatch(name):
            continue
        st = pod.get("status", {}); cs = (st.get("containerStatuses") or [{}])[0]
        cmd = ((pod.get("spec", {}).get("containers") or [{}])[0].get("command") or [])
        specs.append({"owner": name[len("doom-watch-"):], "node": pod.get("spec", {}).get("nodeName", ""), "phase": st.get("phase", ""),
                      "ready": bool(cs.get("ready")), "restarts": int(cs.get("restartCount") or 0),
                      "following": cmd[3] if len(cmd) > 3 else ""})
    return {"watcher": True, "parties": rows, "spectators": sorted(specs, key=lambda x: x["owner"])}


# What runs in the spectator's own container for the host's view: it asks the pod's own endpoint (127.0.0.1, with the secret it reads on
# its first stdin line) once per request line and writes a 4-byte length and the JPEG (length 0: no frame). Python of the image only.
TAP = (
    "import struct,sys,urllib.request\n"
    "port=int(sys.argv[1]);tok=sys.stdin.readline().strip()\n"
    "for _ in sys.stdin:\n"
    "    try:\n"
    "        b=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(port)+'/frame',headers={'Authorization':'Bearer '+tok}),timeout=5).read(MAXF)\n"
    "    except Exception:\n"
    "        b=b''\n"
    "    sys.stdout.buffer.write(struct.pack('>I',len(b))+b);sys.stdout.flush()\n"
).replace("MAXF", str(MAX_FRAME + 1))


class NoFrame(RuntimeError):
    """The channel answered, but with no picture yet (the spectator is starting, or refused the secret): keep the channel."""


class Tap:
    """The host's view of one of its spectators (D7's host path): one `kubectl exec -i` into the host's own spectator pod, kept open and
    started again when it ends. The frames cross the API server and the kubelet of the host's own machine, never the mesh; no port is
    opened anywhere (a port-forward's local port would sit wherever kubectl runs: in the control plane's VM, out of the console's reach).
    fetch() returns one JPEG."""

    def __init__(self, kc: str, pod: str, token_file: Path, namespace: str = WATCHER):
        if not POD.fullmatch(pod):
            raise ValueError("not a spectator pod name")
        if namespace not in (WATCHER, "house"):   # the host's own namespaces only (D6; the house of docs/LIVE-PAGE.md)
            raise ValueError("not a namespace of the host")
        self.kc, self.pod, self.token_file, self.namespace = kc, pod, Path(token_file), namespace
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self, token: str) -> None:
        self.close()
        self.proc = subprocess.Popen([self.kc, "exec", "-i", "-n", self.namespace, self.pod, "--", "python3", "-c", TAP, "17777"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True)   # kc is a script: close() ends its whole group
        self.proc.stdin.write(token.encode() + b"\n")
        self.proc.stdin.flush()

    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.proc.stdout.read(n - len(out))
            if not chunk:
                raise OSError("the spectator's channel closed")
            out += chunk
        return out

    def _one(self) -> bytes:
        done: list = []

        def ask():
            try:
                self.proc.stdin.write(b"f\n")
                self.proc.stdin.flush()
                n = struct.unpack(">I", self._read(4))[0]
                done.append(self._read(n) if 0 < n <= MAX_FRAME else b"")
            except (OSError, ValueError, struct.error):
                done.append(None)
        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(TIMEOUT)
        if not done or done[0] is None:
            self.close()
            raise OSError("the spectator's channel did not answer")
        return done[0]

    def fetch(self) -> bytes:
        token = self.token_file.read_text().strip()
        if not TOKEN.fullmatch(token):
            raise RuntimeError("the spectator's secret is missing")
        with self.lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start(token)
                try:
                    body = self._one()
                except OSError:
                    if attempt == 2:
                        raise RuntimeError("the spectator did not answer") from None
                    continue
                if not (body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9")):
                    raise NoFrame("no frame (the spectator is starting, or refused the secret)")
                return body
        raise RuntimeError("the spectator did not answer")

    def close(self) -> None:
        # the process group first: a reader that timed out still holds stdout's lock until the pipe ends, so closing the pipes first
        # would wait for the child to exit on its own (and TIMEOUT would bound nothing)
        if self.proc is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    self.proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass
        self.proc = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["manifests", "nodes", "affinity", "status", "frame"])
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "pool.yaml"))
    ap.add_argument("--kc", default="kubectl")
    ap.add_argument("--parties", default="[]", help="status: party.py list --read-only --json")
    ap.add_argument("--pod", default="")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--house", action="store_true", help="manifests: the house match's namespace (docs/LIVE-PAGE.md) instead of watch-host")
    ap.add_argument("--namespace", default=WATCHER, help="frame: watch-host (default) or house")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        try:
            print(json.dumps(status(a.kc, json.loads(a.parties)), sort_keys=True))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            print(f"watcher: {e}", file=sys.stderr); return 3
        return 0
    if a.cmd == "frame":
        f = Tap(a.kc, a.pod, Path(a.token_file), a.namespace)
        try:
            sys.stdout.buffer.write(f.fetch())
        except (RuntimeError, ValueError, OSError) as e:
            print(f"watcher: {e}", file=sys.stderr); return 3
        finally:
            f.close()
        return 0
    if a.cmd == "manifests":
        print(json.dumps(manifests(load_config(a.config), a.house), indent=1, sort_keys=True))
        return 0
    if a.cmd == "affinity":
        print(json.dumps(affinity(), sort_keys=True))
        return 0
    found = nodes(json.load(sys.stdin))
    if not found:
        print("no node can host the watcher: it needs a Ready node of trust own that is not the control plane", file=sys.stderr)
        return 1
    print("\n".join(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
