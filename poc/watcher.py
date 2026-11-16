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
  spectator pods with `kubectl port-forward` (D7: allowed for the host's own views only, which run on the host's own machines).

A host without an `own` node that is not the control plane cannot watch: `mesh host watch init` refuses.

    watcher.py manifests --config pool.yaml     the objects, as one JSON List (kubectl apply -f -)
    watcher.py nodes                            stdin: kubectl get nodes -o json; prints the nodes a watcher pod may use (exit 1: none)
    watcher.py affinity                         the nodeAffinity every watcher pod carries, as JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import participant  # noqa: E402
from party import WATCHER, WATCHER_LABEL  # noqa: E402
from pool import load_config  # noqa: E402

TRUST = "node-restriction.kubernetes.io/trust"
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


def affinity() -> dict:
    """An own node that is not a control plane: both in one term, so a node must satisfy both."""
    return {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [
        {"key": TRUST, "operator": "In", "values": ["own"]},
        {"key": CONTROL_PLANE, "operator": "DoesNotExist"}]}]}}


def manifests(cfg: dict) -> dict:
    """A player's namespace objects without the player: no ServiceAccount, Role or binding; the watcher's own quota numbers."""
    q = dict(cfg)
    for key in ("cpu_millis", "pods"):
        if f"watch_quota_{key}" in cfg:
            q[f"quota_{key}"] = cfg[f"watch_quota_{key}"]
    keep = {"Namespace", "ResourceQuota", "NetworkPolicy", "LimitRange"}
    items = []
    for obj in participant.manifests("watcher", q)["items"]:
        if obj["kind"] not in keep:
            continue
        obj = json.loads(json.dumps(obj))
        md = obj["metadata"]
        if obj["kind"] == "Namespace":
            md["name"] = WATCHER
            md["labels"].pop("edge-mesh/participant")
            md["labels"][WATCHER_LABEL] = "host"
        else:
            md["namespace"] = WATCHER
            md["labels"] = {WATCHER_LABEL: "host"}
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["manifests", "nodes", "affinity"])
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "pool.yaml"))
    a = ap.parse_args(argv)
    if a.cmd == "manifests":
        print(json.dumps(manifests(load_config(a.config)), indent=1, sort_keys=True))
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
