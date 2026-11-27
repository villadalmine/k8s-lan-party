#!/usr/bin/env python3
"""The panel (#119, v0.8): what the game's proofs of concept are doing right now, read from the real cluster, for the public page and for
the host. It shows only counts and states, never a name, an address, a key or a token, and only what is live: a value it cannot read is
left out, never guessed.

Each field belongs to a feature of docs/FEATURES.yml, so the page can show it next to that feature's learn block (concept, analogy,
real world): what you see running is what you learn.

What it may show is set by a profile in a config the operator writes (poc/panel.yaml):

    profile: public          # public | self-hosted | host
    off: []                  # fields of the profile to leave out, e.g. [cpu_used_millicores, memory_used_mib]
    publish: yes             # the puller sends the panel to the public page (public and self-hosted only)

A profile is an allowlist and the config can only narrow it: `off` removes fields, nothing adds one. An unknown key, profile or field
is refused at start (exit 2): a typo never shows more than was meant.

    panel.py [--config poc/panel.yaml] [--mesh poc/mesh] [--budget 45] snapshot [--if-published]
                                                                       the panel as JSON (a pure read); --if-published (the puller):
                                                                       read nothing at all unless the config publishes it
    panel.py [--config poc/panel.yaml] check                           the config is valid (exit 0) or not (exit 2)

Exit codes: 0 done; 1 the cluster could not be read at all; 2 a bad config or arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "page"))
from panelspec import FIELDS, MAX_INT  # noqa: E402  (the page checks with the same list)

PROFILES = {"public": tuple(FIELDS), "self-hosted": tuple(FIELDS), "host": tuple(FIELDS)}
CONFIG_KEYS = {"profile", "off", "publish"}


class ConfigError(Exception):
    pass


def load_config(path: str | None) -> dict:
    """The operator's config, or the defaults (public, nothing off, not published) when there is no file."""
    cfg = {"profile": "public", "off": [], "publish": False}
    if not path or not Path(path).is_file():
        return cfg
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ConfigError(f"{path}:{n}: 'key: value'")
        k, v = (x.strip() for x in line.split(":", 1))
        if k not in CONFIG_KEYS:
            raise ConfigError(f"{path}:{n}: unknown key {k!r} (profile, off, publish)")
        if k == "profile":
            if v not in PROFILES:
                raise ConfigError(f"{path}:{n}: profile is one of {', '.join(PROFILES)}")
            cfg["profile"] = v
        elif k == "off":
            if not (v.startswith("[") and v.endswith("]")):
                raise ConfigError(f"{path}:{n}: off is a list, e.g. [cpu_used_millicores]")
            cfg["off"] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            if v not in ("yes", "no", "true", "false"):
                raise ConfigError(f"{path}:{n}: publish is yes or no")
            cfg["publish"] = v in ("yes", "true")
    unknown = [f for f in cfg["off"] if f not in FIELDS]
    if unknown:
        raise ConfigError(f"{path}: off names fields that do not exist: {', '.join(unknown)}")
    if cfg["publish"] and cfg["profile"] == "host":
        raise ConfigError(f"{path}: the host profile is never published (public or self-hosted)")
    return cfg


def allowed(cfg: dict) -> list[str]:
    return [f for f in PROFILES[cfg["profile"]] if f not in cfg["off"]]


# --- the reads ---------------------------------------------------------------------------------------------------------------------

class Reader:
    """Every read is a pure read: kubectl get, the kubelets' stats through the API server, and mesh's --read-only reads. The whole
    panel has a budget (--budget, 45 s: under the console's 60 s for an engine read): a read that would pass it is not started, and its
    fields are left out, like any failed read."""

    def __init__(self, mesh: str, run=None, budget: float = 45.0, clock=time.monotonic):
        self.mesh, self.kc = mesh, str(Path(mesh).parent / "kc")
        self.run = run or self._run
        self.clock, self.deadline = clock, clock() + budget

    def left(self) -> float:
        return self.deadline - self.clock()

    def _run(self, argv: list[str], timeout: int = 30) -> str | None:
        t = min(timeout, self.left())
        if t < 1:
            return None
        # its own process group, killed whole at the timeout: kc is a shell over incus exec and kubectl, which would be left behind
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        except OSError:
            return None
        try:
            out, _ = p.communicate(timeout=t)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return None
        return out if p.returncode == 0 else None

    def kubectl(self, *args: str, timeout: int = 15) -> str | None:
        return self.run([self.kc, *args, "--request-timeout=10s"], timeout)

    def json(self, *args: str, timeout: int = 15):
        out = self.kubectl(*args, timeout=timeout)
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None


def cpu_millicores(q: str) -> int | None:
    m = re.fullmatch(r"(\d+)(m?)", str(q))
    return None if not m else int(m.group(1)) * (1 if m.group(2) else 1000)


def mib(q: str) -> int | None:
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi|Ti)?", str(q))
    if not m:
        return None
    return int(m.group(1)) * {None: 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}[m.group(2)] // 1024 ** 2


def snapshot(r: Reader) -> dict:
    """The fields that could be read now. A read that fails leaves its fields out: the page says "not live", never a guess."""
    f: dict = {}
    nodes = r.json("get", "nodes", "-o", "json")
    if nodes is None:
        return f
    items = nodes.get("items", [])
    f["nodes_total"] = len(items)
    ready = [n for n in items if any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", []))]
    f["nodes_ready"] = len(ready)
    f["player_nodes"] = sum(1 for n in items if n.get("metadata", {}).get("labels", {}).get("edge-mesh/participant"))
    # the machines that are up: the totals and the use are both over the Ready nodes (the kubelet's working set includes its system)
    cpu_total = [cpu_millicores(n.get("status", {}).get("allocatable", {}).get("cpu", "")) for n in ready]
    mem_total = [mib(n.get("status", {}).get("allocatable", {}).get("memory", "")) for n in ready]
    if ready and None not in cpu_total and None not in mem_total:
        f["cpu_total_millicores"], f["memory_total_mib"] = sum(cpu_total), sum(mem_total)
    used_cpu = used_mem = 0
    for n in ready:
        s = r.json("get", "--raw", f"/api/v1/nodes/{n['metadata']['name']}/proxy/stats/summary", timeout=15)
        node = (s or {}).get("node", {})
        c, m = node.get("cpu", {}).get("usageNanoCores"), node.get("memory", {}).get("workingSetBytes")
        if not isinstance(c, int) or not isinstance(m, int):
            used_cpu = None
            break
        used_cpu += c // 1_000_000
        used_mem += m // 1024 ** 2
    if used_cpu is not None and ready:
        f["cpu_used_millicores"], f["memory_used_mib"] = used_cpu, used_mem
    pods = r.json("get", "pods", "-A", "--field-selector=status.phase=Running", "-o", "json")
    if pods is not None:
        ps = pods.get("items", [])
        ns = lambda p: p.get("metadata", {}).get("namespace", "")  # noqa: E731
        f["pods_running"] = len(ps)
        f["player_pods_running"] = sum(1 for p in ps if ns(p).startswith("play-"))
        f["games_running"] = sum(1 for p in ps if p.get("metadata", {}).get("name") == "doom-server" and (ns(p).startswith("play-") or ns(p) == "house"))
    namespaces = r.json("get", "namespaces", "-o", "json")
    if namespaces is not None:
        nss = namespaces.get("items", [])
        f["player_namespaces"] = sum(1 for n in nss if n.get("metadata", {}).get("name", "").startswith("play-"))
        house = [n for n in nss if n.get("metadata", {}).get("name") == "house"]
        md = house[0].get("metadata", {}) if house else {}
        labels = md.get("labels") or {}
        # as party.py decides it: the host's house (its label), Active, not being deleted, and opened
        f["house_open"] = bool(house) and labels.get("edge-mesh/house") == "host" and labels.get("edge-mesh/house-open") == "yes" and \
            house[0].get("status", {}).get("phase") == "Active" and not md.get("deletionTimestamp")
    # the configuration Cilium runs with (its ConfigMap; checked at run time by poc-cilium). Not there at all (flannel): nothing
    # encrypts pod traffic. The read failing (a timeout, RBAC): the fields are left out, never a guessed "no"
    raw = r.kubectl("get", "configmap", "cilium-config", "-n", "kube-system", "--ignore-not-found", "-o", "json")
    try:
        cm = json.loads(raw) if raw and raw.strip() else ({} if raw is not None else None)
    except ValueError:
        cm = None
    if cm == {}:
        f["wireguard"] = f["node_encryption"] = f["mutual_auth"] = False
    elif isinstance(cm, dict):
        d = cm.get("data", {})
        f["wireguard"] = d.get("enable-wireguard") == "true"
        f["node_encryption"] = d.get("encrypt-node") == "true"
        f["mutual_auth"] = d.get("mesh-auth-mutual-enabled") == "true"   # authentication.mutual.spire.enabled
    parties = r.run([r.mesh, "host", "party", "list", "--read-only", "--json"], 60)
    try:
        rows = json.loads(parties) if parties else None
    except ValueError:
        rows = None
    if isinstance(rows, list):
        st = lambda p: str(p.get("status", ""))  # noqa: E731
        host_side = {"house", "watch-host"}   # the house match and the host's watcher: their own fields, not a party between players
        active = {frozenset((p.get("player"), p.get("peer"))) for p in rows if isinstance(p, dict) and st(p).startswith("active")
                  and not {p.get("player"), p.get("peer")} & host_side}
        f["parties_active"] = len(active)
        f["parties_waiting"] = sum(1 for p in rows if isinstance(p, dict) and st(p).startswith("waiting")
                                   and not {p.get("player"), p.get("peer")} & host_side)
        # players (not the host's watcher) in an open party with the house, whichever side the row is written from
        f["house_players"] = len({p.get("peer") if p.get("player") == "house" else p.get("player") for p in rows
                                  if isinstance(p, dict) and "house" in (p.get("player"), p.get("peer")) and st(p).startswith("active")}
                                 - {"house", "watch-host"})
    metrics = r.run([r.mesh, "host", "admission-metrics", "--read-only"], 60)
    if metrics:
        ev = {m.group(1): int(m.group(2)) for m in re.finditer(r'^emesh_admission_events_total\{type="(\w+)"\} (\d+)$', metrics, re.M)}
        if ev:
            f["admitted_total"], f["expelled_total"], f["refused_total"] = ev.get("approved", 0), ev.get("expelled", 0), ev.get("refused", 0)
    return f


def shape(fields: dict, cfg: dict) -> dict:
    """Only the profile's allowed fields, each of its declared type and bounded: what leaves the host."""
    out = {}
    for k in allowed(cfg):
        v = fields.get(k)
        want = FIELDS[k][1]
        if want is bool and isinstance(v, bool):
            out[k] = v
        elif want is int and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_INT:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["snapshot", "check"])
    ap.add_argument("--config", default=str(HERE / "panel.yaml"))
    ap.add_argument("--mesh", default=str(HERE / "mesh"))
    ap.add_argument("--budget", type=float, default=45.0)
    ap.add_argument("--if-published", action="store_true", help="read nothing unless the config publishes the panel (the puller)")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"panel: {e}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        print(f"panel: profile {cfg['profile']}, {len(allowed(cfg))} fields, publish {'yes' if cfg['publish'] else 'no'}")
        return 0
    if a.if_published and not cfg["publish"]:   # the puller asks every minute: not published costs no read at all
        print(json.dumps({"at": int(time.time()), "profile": cfg["profile"], "publish": False, "up": None, "fields": {}}))
        return 0
    fields = snapshot(Reader(a.mesh, budget=a.budget))
    if not fields:
        print(json.dumps({"at": int(time.time()), "profile": cfg["profile"], "publish": cfg["publish"], "up": False, "fields": {}}))
        return 1
    print(json.dumps({"at": int(time.time()), "profile": cfg["profile"], "publish": cfg["publish"], "up": True,
                      "fields": shape(fields, cfg)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
