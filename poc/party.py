#!/usr/bin/env python3
"""LAN party mode, part A of issue #75 (DESIGN.md section 14.2): two players who BOTH agreed reach each other's namespace, on the party's
ports only, and nobody else does.

Consent is an object a player can already write with the kubeconfig of their own namespace, and nothing more: a ConfigMap
`party-<peer>` labelled `edge-mesh/party-consent` in `play-<me>` (`edge-mesh party invite|accept <peer>`; `leave` deletes it). Nobody can
write one in another player's namespace (RBAC), so a party needs both people. The host reconciles (`mesh host party reconcile`, run by the
admission tick and by every kick): when `play-a` names b and `play-b` names a, both namespaces are live and both leases still run, it labels
each namespace `party.edge-mesh/<other>=yes` and puts a NetworkPolicy `party-<other>` in each one that lets its pods and the other's talk on
the ports both consents name, within `party_ports` of pool.yaml. Players have no right on NetworkPolicies or namespace labels, so they cannot
widen one. The policy selects the other namespace by name AND by that label, so a later player who gets the same name gets no access from
a policy left over. The host stamps the peer's namespace UID on the consent the first time it sees one (`edge-mesh/party-peer-uid`): if
that namespace goes (kick, lease end) the consent is deleted, so an invitation never passes to whoever takes the name next. A party that was
active and stops qualifying (one side left, kicked, a lease ended) deletes both consents: playing again takes a new invite and accept.
`parties: 0` in pool.yaml closes every party and keeps the consents (status `off`). Placement, quotas and RBAC are not touched.

    party.py reconcile --config pool.yaml --bundles DIR --nebula-cert BIN --kc KC [--quiet]   apply; prints what changed (or "unchanged")
    party.py list ...                     one row per consent: player, peer, status (after a reconcile)
    party.py list --read-only [--json] ...   the same rows from what the cluster says now, without reconciling (the host console's read)
    party.py end <a> <b> ...              the host ends a party: both consents deleted, then a reconcile
    party.py plan --config pool.yaml --now N   stdin: {"namespaces", "configmaps", "netpols", "ends"} -> the actions as JSON (tests)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pool import load_config  # noqa: E402

NAME = re.compile(r"[a-z][a-z0-9-]{1,19}")
PORT = re.compile(r"([0-9]{1,5})/(TCP|UDP)")
PREFIX, CONSENT, PARTY = "play-", "edge-mesh/party-consent", "edge-mesh/party"
STATUS, PEER_UID, NSLABEL = "edge-mesh/party-status", "edge-mesh/party-peer-uid", "party.edge-mesh/"
DEFAULT_PORTS = ["10666/UDP"]   # Odamex, the Doom demo of part B


def ports(text, allowed: list[str]) -> list[str] | None:
    """The ports of a consent ("" = every allowed one) within the allowed list; None when it names something that is not a port."""
    items = [p.strip() for p in (text or "").split(",") if p.strip()]
    if any(not PORT.fullmatch(p) or not 0 < int(p.split("/")[0]) < 65536 for p in items):
        return None
    return sorted(set(items or allowed) & set(allowed), key=lambda p: (int(p.split("/")[0]), p))


def policy(me: str, peer: str, plist: list[str]) -> dict:
    ns, sel = PREFIX + me, {"namespaceSelector": {"matchLabels": {
        "kubernetes.io/metadata.name": PREFIX + peer, NSLABEL + me: "yes"}}}
    pp = [{"protocol": p.split("/")[1], "port": int(p.split("/")[0])} for p in plist]
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": f"party-{peer}", "namespace": ns, "labels": {PARTY: ".".join(sorted([me, peer]))}},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [sel], "ports": pp}], "egress": [{"to": [sel], "ports": pp}]}}


def plan(cfg: dict, now: int, data: dict) -> dict:
    """What to change, from what the cluster holds. Pure: the same input gives the same plan, and a second plan after applying it is empty."""
    on = int(cfg.get("parties", 1)) == 1
    raw = [str(p) for p in cfg.get("party_ports") or DEFAULT_PORTS]
    allowed = ports(",".join(raw), raw) or []   # a pool.yaml that names something else than ports opens nothing
    players = {}   # name -> (uid, live, labels)
    for ns in data["namespaces"].get("items", []):
        md = ns["metadata"]; who = (md.get("labels") or {}).get("edge-mesh/participant", "")
        if NAME.fullmatch(who) and md["name"] == PREFIX + who:
            players[who] = (md.get("uid", ""), ns.get("status", {}).get("phase") == "Active" and not md.get("deletionTimestamp"),
                            md.get("labels") or {})
    ends = data.get("ends", {})
    cons = {}      # (me, peer) -> dict(cm, ports, status, uid)
    act = {"apply": [], "delete": [], "label": [], "annotate": [], "say": []}
    for cm in data["configmaps"].get("items", []):
        md = cm["metadata"]; me = md["namespace"][len(PREFIX):] if md["namespace"].startswith(PREFIX) else ""
        peer = (cm.get("data") or {}).get("peer", "")
        if me not in players or not NAME.fullmatch(peer) or md["name"] != f"party-{peer}":
            continue
        ann = md.get("annotations") or {}
        cons[(me, peer)] = {"cm": md["name"], "ports": ports((cm.get("data") or {}).get("ports", ""), allowed),
                            "status": ann.get(STATUS, ""), "uid": ann.get(PEER_UID, "")}
    want, status, gone = {}, {}, set()
    for (me, peer), c in sorted(cons.items()):
        p = players.get(peer)
        if not players[me][1]:
            gone.add((me, peer, "closed: you left the game"))
        elif c["uid"] and (not p or p[0] != c["uid"] or not p[1]):
            gone.add((me, peer, f"closed: {peer} left the game"))
        elif ends.get(me, 0) <= now or (p and ends.get(peer, 0) <= now):
            gone.add((me, peer, "closed: a lease ended"))
        elif c["status"].startswith("active") and on and (peer, me) not in cons:
            gone.add((me, peer, f"closed: {peer} left the party"))
    for me, peer, why in sorted(gone):
        act["delete"].append(["configmap", PREFIX + me, f"party-{peer}"]); act["say"].append(f"{me} -> {peer}: {why}")
        cons.pop((me, peer), None)
    for (me, peer), c in sorted(cons.items()):
        other, p = cons.get((peer, me)), players.get(peer)
        common = sorted(set(c["ports"] or []) & set((other or {}).get("ports") or []), key=lambda x: (int(x.split("/")[0]), x))
        if not on:
            s = "off: the host has turned parties off"
        elif me == peer:
            s = "refused: a party needs another player"
        elif c["ports"] is None:
            s = f"refused: ports are <port>/TCP or /UDP, of {','.join(allowed)}"
        elif not p:
            s = f"waiting: no player called {peer} yet"
        elif not other:
            s = f"waiting for {peer} to accept"
        elif not common:
            s = f"refused: no port in common with {peer} (allowed: {','.join(allowed)})"
        else:
            s = "active: ports " + ",".join(common); want[(me, peer)] = common
        status[(me, peer)] = s
        uid = p[0] if p else ""
        if s != c["status"] or (uid and not c["uid"]):
            act["annotate"].append([PREFIX + me, c["cm"], {STATUS: s, **({PEER_UID: uid} if uid and not c["uid"] else {})}])
            if s.split(":")[0] != c["status"].split(":")[0]:
                act["say"].append(f"{me} -> {peer}: {s}")
    have = {(np["metadata"]["namespace"], np["metadata"]["name"]): np for np in data["netpols"].get("items", [])}
    for (me, peer), plist in sorted(want.items()):
        obj, cur = policy(me, peer, plist), have.pop((PREFIX + me, f"party-{peer}"), None)
        if not cur or cur.get("spec") != obj["spec"] or (cur["metadata"].get("labels") or {}).get(PARTY) != obj["metadata"]["labels"][PARTY]:
            act["apply"].append(obj)
    for ns, name in sorted(have):
        act["delete"].append(["networkpolicy", ns, name])
    for who, (_, live, labels) in sorted(players.items()):
        need = {NSLABEL + me for (me, peer) in want if peer == who}
        for key in sorted(k for k in labels if k.startswith(NSLABEL) and k not in need):
            act["label"].append([PREFIX + who, key + "-"])
        for key in sorted(need - set(labels)):
            act["label"].append([PREFIX + who, key + "=yes"])
    act["status"] = sorted([me, peer, s] for (me, peer), s in status.items())
    return act


def kc(a, *args: str, stdin: str | None = None) -> str:
    r = subprocess.run([a.kc, *args], input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def lease_ends(a) -> dict:
    """Each player's lease end: the notAfter of the certificate in its bundle (the same instant as its kubeconfig token, poc-pool PL06)."""
    out = {}
    for crt in sorted(Path(a.bundles).glob("*/host.crt")):
        r = subprocess.run([a.nebula_cert, "print", "-json", "-path", str(crt)], capture_output=True, text=True)
        if r.returncode == 0:
            na = json.loads(r.stdout)[0]["details"]["notAfter"]
            out[crt.parent.name] = int(dt.datetime.fromisoformat(na.replace("Z", "+00:00")).timestamp())
    return out


def observe(a, cfg: dict) -> dict:
    """The plan for the cluster as it is, from reads only (kubectl get, nebula-cert print): nothing is applied."""
    data = {"namespaces": json.loads(kc(a, "get", "namespaces", "-l", "edge-mesh/participant", "-o", "json")),
            "configmaps": json.loads(kc(a, "get", "configmaps", "-A", "-l", CONSENT, "-o", "json")),
            "netpols": json.loads(kc(a, "get", "networkpolicies", "-A", "-l", PARTY, "-o", "json")), "ends": lease_ends(a)}
    return plan(cfg, a.now or int(dt.datetime.now(dt.timezone.utc).timestamp()), data)


def reconcile(a, cfg: dict) -> dict:
    act = observe(a, cfg)
    for kind, ns, name in act["delete"]:
        kc(a, "delete", kind, name, "-n", ns, "--ignore-not-found")
    if act["apply"]:
        kc(a, "apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": act["apply"]}))
    for ns, change in act["label"]:
        kc(a, "label", "namespace", ns, change, "--overwrite")
    for ns, cm, ann in act["annotate"]:
        kc(a, "annotate", "configmap", cm, "-n", ns, "--overwrite", *[f"{k}={v}" for k, v in ann.items()])
    return act


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["reconcile", "list", "end", "plan"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--config", required=True)
    ap.add_argument("--bundles", default="")
    ap.add_argument("--nebula-cert", default="nebula-cert")
    ap.add_argument("--kc", default="kubectl")
    ap.add_argument("--now", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--read-only", action="store_true", help="list only: what the cluster says now, without reconciling first (the host console)")
    ap.add_argument("--json", action="store_true", help="list only: as JSON")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "plan":
        print(json.dumps(plan(cfg, a.now, json.load(sys.stdin)), indent=1, sort_keys=True))
        return 0
    if (a.read_only or a.json) and a.cmd != "list":
        print("party: --read-only and --json go with list only", file=sys.stderr); return 2
    try:
        if a.cmd == "list" and a.read_only:
            act = observe(a, cfg)
        elif a.cmd == "end":
            if len(a.names) != 2 or not all(NAME.fullmatch(n) for n in a.names):
                print("usage: mesh host party end <player> <player>"); return 2
            x, y = a.names
            kc(a, "delete", "configmap", f"party-{y}", "-n", PREFIX + x, "--ignore-not-found")
            kc(a, "delete", "configmap", f"party-{x}", "-n", PREFIX + y, "--ignore-not-found")
            print(f"party {x}+{y}: ended by the host")
            act = reconcile(a, cfg)
        else:
            act = reconcile(a, cfg)
    except RuntimeError as e:
        print(f"party: {e}", file=sys.stderr); return 3
    if a.cmd == "list" and a.json:
        print(json.dumps([{"player": me, "peer": peer, "status": st} for me, peer, st in act["status"]], sort_keys=True))
        return 0
    if a.cmd == "list":
        print(f"{'PLAYER':<20} {'PEER':<20} STATUS")
        for me, peer, s in act["status"]:
            print(f"{me:<20} {peer:<20} {s}")
        return 0
    for line in act["say"]:
        print(f"party: {line}")
    if not a.quiet and not (act["say"] or act["apply"] or act["delete"] or act["label"] or act["annotate"]):
        print("party: unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
