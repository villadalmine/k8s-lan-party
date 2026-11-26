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

The host's watcher (D6 of docs/CONSOLE-MODES.md, issue #101): the host is not a player, so to watch a game "through a party" it has an
identity of its own, the namespace `watch-host` labelled `edge-mesh/watcher=host` (`poc/watcher.py`; its pods run on an `own` node, never
the control plane). Here it is one more peer called `watch-host`: the host's consent is `party-<owner>` in `watch-host`, the owner's is
`party-watch-host` in `play-<owner>`, and the rest is the same rule (both consents, common ports, closed when either side goes). It has
no lease: it lives as long as its namespace. No player may take the name (admission.py RESERVED, `mesh host invite`), and a player
namespace labelled with it is ignored here, so `watch-host` always means the host.

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
WATCHER, WATCHER_LABEL = "watch-host", "edge-mesh/watcher"   # the host's watcher identity (D6): a namespace of that name, not play-*
HOUSE, HOUSE_LABEL = "house", "edge-mesh/house"                # the house match (docs/LIVE-PAGE.md L1): the host's game, broadcast
HOUSE_OPEN = "edge-mesh/house-open"                            # on the house namespace: "yes" = the house offers a party to every player
HOST_PEERS = {WATCHER: WATCHER_LABEL, HOUSE: HOUSE_LABEL}      # the host's own members: a namespace of that name with that label = "host"
FOREVER = 2 ** 62                                              # the host's members have no lease


def nsof(who: str) -> str:
    """The namespace of a party member: the host's watcher and the house have their own, every player play-<name>."""
    return who if who in HOST_PEERS else PREFIX + who


def member(namespace: str) -> str:
    """The member a namespace belongs to, or "" when it is neither the host's (watcher, house) nor a player's."""
    if namespace in HOST_PEERS:
        return namespace
    who = namespace[len(PREFIX):] if namespace.startswith(PREFIX) else ""
    return "" if who in HOST_PEERS else who   # play-watch-host and play-house are nobody's: the names are reserved


def ports(text, allowed: list[str]) -> list[str] | None:
    """The ports of a consent ("" = every allowed one) within the allowed list; None when it names something that is not a port."""
    items = [p.strip() for p in (text or "").split(",") if p.strip()]
    if any(not PORT.fullmatch(p) or not 0 < int(p.split("/")[0]) < 65536 for p in items):
        return None
    return sorted(set(items or allowed) & set(allowed), key=lambda p: (int(p.split("/")[0]), p))


def policy(me: str, peer: str, plist: list[str]) -> dict:
    ns, sel = nsof(me), {"namespaceSelector": {"matchLabels": {
        "kubernetes.io/metadata.name": nsof(peer), NSLABEL + me: "yes"}}}
    pp = [{"protocol": p.split("/")[1], "port": int(p.split("/")[0])} for p in plist]
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": f"party-{peer}", "namespace": ns, "labels": {PARTY: ".".join(sorted([me, peer]))}},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [sel], "ports": pp}], "egress": [{"to": [sel], "ports": pp}]}}


def offer(player: str) -> dict:
    """The house's half of a party with a player: the same ConfigMap `mesh host watch invite` writes, in the house's namespace."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"party-{player}", "namespace": HOUSE, "labels": {CONSENT: "yes"}},
            "data": {"peer": player, "ports": ""}}


def plan(cfg: dict, now: int, data: dict) -> dict:
    """What to change, from what the cluster holds. Pure: the same input gives the same plan, and a second plan after applying it is empty."""
    on = int(cfg.get("parties", 1)) == 1
    raw = [str(p) for p in cfg.get("party_ports") or DEFAULT_PORTS]
    allowed = ports(",".join(raw), raw) or []   # a pool.yaml that names something else than ports opens nothing
    players = {}   # name -> (uid, live, labels)
    for ns in data["namespaces"].get("items", []):
        md = ns["metadata"]; labels = md.get("labels") or {}
        if md["name"] in HOST_PEERS:   # the host's members only with the host's label; never a player that names itself so
            who = md["name"] if labels.get(HOST_PEERS[md["name"]]) == "host" else ""
        else:
            who = labels.get("edge-mesh/participant", "")
            who = "" if who in HOST_PEERS else who
        if NAME.fullmatch(who) and md["name"] == nsof(who):
            players[who] = (md.get("uid", ""), ns.get("status", {}).get("phase") == "Active" and not md.get("deletionTimestamp"),
                            md.get("labels") or {})
    ends = {**data.get("ends", {}), **{h: FOREVER for h in HOST_PEERS}}
    cons = {}      # (me, peer) -> dict(cm, ports, status, uid)
    act = {"apply": [], "delete": [], "label": [], "annotate": [], "say": []}
    for cm in data["configmaps"].get("items", []):
        md = cm["metadata"]; me = member(md["namespace"])
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
    # the house (L8): while open it offers a party to every player in the game (its half only: nothing opens until the player
    # accepts); closed, it withdraws every offer, which also closes the parties of whoever was playing in it
    house = players.get(HOUSE)
    house_open = bool(house and house[1] and house[2].get(HOUSE_OPEN) == "yes" and on)
    for (me, peer) in sorted(cons):
        if me == HOUSE and not house_open:
            gone.add((me, peer, "closed: the house match is closed"))
    if house_open:
        for who, (_, live, _) in sorted(players.items()):
            if who not in HOST_PEERS and live and ends.get(who, 0) > now and (HOUSE, who) not in cons:
                act["apply"].append(offer(who))
                act["say"].append(f"{HOUSE} -> {who}: offered (the house match is broadcast; {who} accepts with: edge-mesh party accept {HOUSE})")
    for me, peer, why in sorted(gone):
        act["delete"].append(["configmap", nsof(me), f"party-{peer}"]); act["say"].append(f"{me} -> {peer}: {why}")
        cons.pop((me, peer), None)
    for (me, peer), c in sorted(cons.items()):
        other, p = cons.get((peer, me)), players.get(peer)
        common = sorted(set(c["ports"] or []) & set((other or {}).get("ports") or []), key=lambda x: (int(x.split("/")[0]), x))
        if not on:
            s = "off: the host has turned parties off"
        elif me == peer:
            s = "refused: a party needs another player"
        elif peer == WATCHER and not p:
            s = f"waiting: the host has no watcher yet ({WATCHER})"
        elif peer == HOUSE and not p:
            s = f"waiting: the host has no house match yet ({HOUSE})"
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
            act["annotate"].append([nsof(me), c["cm"], {STATUS: s, **({PEER_UID: uid} if uid and not c["uid"] else {})}])
            if s.split(":")[0] != c["status"].split(":")[0]:
                act["say"].append(f"{me} -> {peer}: {s}")
    have = {(np["metadata"]["namespace"], np["metadata"]["name"]): np for np in data["netpols"].get("items", [])}
    for (me, peer), plist in sorted(want.items()):
        obj, cur = policy(me, peer, plist), have.pop((nsof(me), f"party-{peer}"), None)
        if not cur or cur.get("spec") != obj["spec"] or (cur["metadata"].get("labels") or {}).get(PARTY) != obj["metadata"]["labels"][PARTY]:
            act["apply"].append(obj)
    for ns, name in sorted(have):
        act["delete"].append(["networkpolicy", ns, name])
    for who, (_, live, labels) in sorted(players.items()):
        need = {NSLABEL + me for (me, peer) in want if peer == who}
        for key in sorted(k for k in labels if k.startswith(NSLABEL) and k not in need):
            act["label"].append([nsof(who), key + "-"])
        for key in sorted(need - set(labels)):
            act["label"].append([nsof(who), key + "=yes"])
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
    players = json.loads(kc(a, "get", "namespaces", "-l", "edge-mesh/participant", "-o", "json"))
    for label in HOST_PEERS.values():   # the host's members (a house carries the watcher label too: the same guard; once is enough)
        got = json.loads(kc(a, "get", "namespaces", "-l", f"{label}=host", "-o", "json"))
        seen = {n["metadata"]["name"] for n in players.get("items", [])}
        players["items"] = players.get("items", []) + [n for n in got.get("items", []) if n["metadata"]["name"] not in seen]
    data = {"namespaces": players,
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
            kc(a, "delete", "configmap", f"party-{y}", "-n", nsof(x), "--ignore-not-found")
            kc(a, "delete", "configmap", f"party-{x}", "-n", nsof(y), "--ignore-not-found")
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
