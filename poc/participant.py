#!/usr/bin/env python3
"""A participant's namespace and user access (DESIGN.md sections 8 and 14, issues #9 and #11).

Every invited player gets one namespace, `play-<name>`, and a kubeconfig of their own next to the node credential in the bundle:

- the namespace carries the labels `edge-mesh/participant=<name>` and Pod Security `restricted` (enforced: no privileged pod, no host
  path, no host network, non-root), a ResourceQuota and a LimitRange with the numbers of pool.yaml (no LoadBalancer, no NodePort),
  and a NetworkPolicy: its pods talk to each other and to the cluster DNS, nothing else, and nothing from another namespace reaches them;
  the participant guard (participant-policy.yaml) keeps its private pods on the player's own node;
- a ServiceAccount `user` bound by a Role to that namespace only: pods (logs, exec), deployments, services and configmaps; read-only
  events, replicasets, its quota and its limits. No secrets, no nodes, nothing cluster-wide, no RBAC, no CRDs;
- the kubeconfig holds a token of that ServiceAccount made with the TokenRequest API (`kubectl create token`) for exactly the length of the
  lease, the cluster CA (pinned) and the control plane's Nebula address. Node credentials (join.env: the join token and the node password)
  and the user's credential are kept apart: the kubeconfig carries no client certificate and no join token, and join.env no user token.

When the lease ends (reap, kick, the idle kick or panic: `mesh host kick`) the ServiceAccount and then the namespace are deleted: the
token stops working at once (the API checks that its ServiceAccount still exists), whatever its expiry says.

    participant.py namespace <name>                          play-<name>
    participant.py manifests <name> --config pool.yaml       the objects, as one JSON List (kubectl create -f -)
    participant.py kubeconfig <name> --server URL --ca FILE  the kubeconfig (the token on stdin)
    participant.py seconds <duration>                        90s, 15m, 2h, 1h30m -> seconds (exit 2 if it is not one)
    participant.py activity <name>                           stdin: {"nodes": <kubectl get nodes -o json>, "pods": <get pods -A -o json>}
                                                             prints "active" (exit 0) or "idle: <why>" (exit 1)
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pool import load_config  # noqa: E402

PREFIX = "play-"
SA = "user"
NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")   # the rule of admission.py and `mesh host invite`
TOKEN_MIN = 600                               # the TokenRequest API refuses a token shorter than 10 minutes
# The pool.yaml keys and their defaults (used when pool.yaml does not set one).
QUOTA = {"quota_pods": 10, "quota_cpu_millis": 1000, "quota_memory_mib": 1024, "quota_storage_mib": 2048, "quota_services": 5,
         "quota_configmaps": 10, "quota_deployments": 5,
         "limit_default_cpu_millis": 200, "limit_default_memory_mib": 128, "limit_default_storage_mib": 256,
         "limit_max_cpu_millis": 500, "limit_max_memory_mib": 512, "limit_max_storage_mib": 1024}
ALL = ["get", "list", "watch", "create", "update", "patch", "delete"]
READ = ["get", "list", "watch"]


def namespace(name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise ValueError(f"invalid name {name!r}")
    return PREFIX + name


def seconds(value: str) -> int:
    """A duration as `mesh host invite --ttl` takes it (Nebula's: 90s, 15m, 2h, 1h30m) in seconds; ValueError otherwise."""
    if not re.fullmatch(r"(\d+[hms])+", value or ""):
        raise ValueError(f"not a duration: {value!r} (90s, 15m, 2h, 1h30m)")
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", value))


def numbers(cfg: dict) -> dict:
    return {k: int(cfg.get(k, v)) for k, v in QUOTA.items()}


def manifests(name: str, cfg: dict) -> dict:
    ns, q = namespace(name), numbers(cfg)
    meta = {"namespace": ns, "labels": {"edge-mesh/participant": name}}
    cpu, mem, disk = f"{q['quota_cpu_millis']}m", f"{q['quota_memory_mib']}Mi", f"{q['quota_storage_mib']}Mi"
    rules = [
        {"apiGroups": [""], "resources": ["pods", "services", "configmaps"], "verbs": ALL},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get", "create"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ALL},
        {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": READ},
        {"apiGroups": [""], "resources": ["events", "resourcequotas", "limitranges"], "verbs": READ},
        {"apiGroups": ["events.k8s.io"], "resources": ["events"], "verbs": READ},
    ]
    items = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {
            "edge-mesh/participant": name,
            "pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "latest",
            "pod-security.kubernetes.io/warn": "restricted", "pod-security.kubernetes.io/audit": "restricted"}}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": SA, **meta}, "automountServiceAccountToken": False},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "participant", **meta}, "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "participant", **meta},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "participant"},
         "subjects": [{"kind": "ServiceAccount", "name": SA, "namespace": ns}]},
        {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "pool", **meta}, "spec": {"hard": {
            "pods": str(q["quota_pods"]), "requests.cpu": cpu, "limits.cpu": cpu, "requests.memory": mem, "limits.memory": mem,
            # emptyDir and container layers fill somebody's disk: bounded too (the kubelet evicts a pod over its limit)
            "requests.ephemeral-storage": disk, "limits.ephemeral-storage": disk,
            "services": str(q["quota_services"]), "services.loadbalancers": "0", "services.nodeports": "0",
            # object counts: etcd is the host's, and a player may not fill it
            "configmaps": str(q["quota_configmaps"]), "count/deployments.apps": str(q["quota_deployments"]),
            "persistentvolumeclaims": "0"}}},
        # one player's pods talk only to each other and to the cluster DNS; nothing from another namespace reaches them
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "pool", **meta}, "spec": {
            "podSelector": {}, "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"podSelector": {}}]}],
            "egress": [{"to": [{"podSelector": {}}]},
                       {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}},
        {"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": "pool", **meta}, "spec": {"limits": [{
            "type": "Container",
            "default": {"cpu": f"{q['limit_default_cpu_millis']}m", "memory": f"{q['limit_default_memory_mib']}Mi",
                        "ephemeral-storage": f"{q['limit_default_storage_mib']}Mi"},
            "defaultRequest": {"cpu": f"{q['limit_default_cpu_millis'] // 2}m", "memory": f"{q['limit_default_memory_mib'] // 2}Mi",
                               "ephemeral-storage": f"{q['limit_default_storage_mib'] // 2}Mi"},
            "max": {"cpu": f"{q['limit_max_cpu_millis']}m", "memory": f"{q['limit_max_memory_mib']}Mi",
                    "ephemeral-storage": f"{q['limit_max_storage_mib']}Mi"}}]}},
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def kubeconfig(name: str, server: str, ca_pem: bytes, token: str) -> str:
    """YAML by hand: every value is a URL, base64 or a JWT (no character YAML would read otherwise)."""
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise ValueError("the token is not a JWT")
    if b"BEGIN CERTIFICATE" not in ca_pem:
        raise ValueError("the CA is not a PEM certificate")
    ns, ca = namespace(name), base64.b64encode(ca_pem).decode()
    return (f"# edge-mesh: {name}'s access to the namespace {ns}, valid until the lease ends. Use it over Nebula only:\n"
            f"#   edge-mesh kubectl get pods\n"
            f"apiVersion: v1\nkind: Config\nclusters:\n- name: edge-mesh\n  cluster:\n    server: {server}\n"
            f"    certificate-authority-data: {ca}\nusers:\n- name: {name}\n  user:\n    token: {token}\n"
            f"contexts:\n- name: {name}\n  context:\n    cluster: edge-mesh\n    user: {name}\n    namespace: {ns}\n"
            f"current-context: {name}\n")


def activity(name: str, nodes: dict, pods: dict) -> tuple[bool, str]:
    """(active, why). Active: the participant's Node is Ready AND it has a workload: a pod in its namespace, or a pod outside
    kube-system (not of a DaemonSet) running on its node. Finished pods (Succeeded, Failed) do not count."""
    node = next((n for n in nodes.get("items", []) if n.get("metadata", {}).get("name") == name), None)
    if node is None:
        return False, "no node"
    ready = [c for c in node.get("status", {}).get("conditions", []) if c.get("type") == "Ready"]
    if not ready or ready[0].get("status") != "True":
        return False, "node not Ready"
    ns = namespace(name)
    for p in pods.get("items", []):
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        m = p.get("metadata", {})
        if m.get("namespace") == ns:
            return True, ""
        daemon = any(o.get("kind") == "DaemonSet" for o in m.get("ownerReferences", []))
        if p.get("spec", {}).get("nodeName") == name and m.get("namespace") != "kube-system" and not daemon:
            return True, ""
    return False, "no workload"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="a participant's namespace and kubeconfig (DESIGN.md section 14)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("namespace").add_argument("name")
    p = sub.add_parser("manifests")
    p.add_argument("name")
    p.add_argument("--config", required=True)
    p = sub.add_parser("kubeconfig")
    p.add_argument("name")
    p.add_argument("--server", required=True)
    p.add_argument("--ca", required=True, help="the cluster CA (PEM file)")
    sub.add_parser("seconds").add_argument("duration")
    sub.add_parser("activity").add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "namespace":
            print(namespace(a.name))
        elif a.cmd == "manifests":
            print(json.dumps(manifests(a.name, load_config(a.config)), sort_keys=True))
        elif a.cmd == "kubeconfig":
            sys.stdout.write(kubeconfig(a.name, a.server, Path(a.ca).read_bytes(), sys.stdin.read()))
        elif a.cmd == "seconds":
            print(seconds(a.duration))
        else:
            data = json.load(sys.stdin)
            ok, why = activity(a.name, data.get("nodes", {}), data.get("pods", {}))
            print("active" if ok else f"idle: {why}")
            return 0 if ok else 1
    except ValueError as e:
        print(f"participant: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
