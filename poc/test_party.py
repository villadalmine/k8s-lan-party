"""party.py plan (issue #75, part A): consent of both, ports, closing, idempotence. Pure: no cluster, the cluster's JSON is built here."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import party  # noqa: E402

CFG = {"parties": 1, "party_ports": ["8080/TCP", "10666/UDP"]}
NOW = 1000


def ns(name, uid=None, phase="Active", labels=None):
    return {"metadata": {"name": "play-" + name, "uid": uid or "uid-" + name,
                         "labels": {"edge-mesh/participant": name, **(labels or {})}}, "status": {"phase": phase}}


def cm(me, peer, ports="", status="", uid=""):
    ann = {k: v for k, v in ((party.STATUS, status), (party.PEER_UID, uid)) if v}
    return {"metadata": {"name": "party-" + peer, "namespace": "play-" + me, "annotations": ann}, "data": {"peer": peer, "ports": ports}}


def world(nss, cms, nps=(), ends=None):
    return {"namespaces": {"items": list(nss)}, "configmaps": {"items": list(cms)}, "netpols": {"items": list(nps)},
            "ends": ends if ends is not None else {n["metadata"]["name"][5:]: NOW + 600 for n in nss}}


def applied(data, act):
    """The world after the plan: what reconcile() does to the cluster, on the JSON."""
    out = {k: {"items": [dict(o) for o in v["items"]]} for k, v in data.items() if k != "ends"}
    out["ends"] = data["ends"]
    for kind, nsname, name in act["delete"]:
        key = "configmaps" if kind == "configmap" else "netpols"
        out[key]["items"] = [o for o in out[key]["items"] if (o["metadata"]["namespace"], o["metadata"]["name"]) != (nsname, name)]
    for obj in act["apply"]:
        key = "configmaps" if obj["kind"] == "ConfigMap" else "netpols"   # the house's offers are ConfigMaps (docs/LIVE-PAGE.md L8)
        out[key]["items"] = [o for o in out[key]["items"]
                             if (o["metadata"]["namespace"], o["metadata"]["name"]) != (obj["metadata"]["namespace"], obj["metadata"]["name"])]
        out[key]["items"].append({**obj, "metadata": {**obj["metadata"], "annotations": {}}} if key == "configmaps" else obj)
    for nsname, change in act["label"]:
        n = next(o for o in out["namespaces"]["items"] if o["metadata"]["name"] == nsname)
        labels = dict(n["metadata"]["labels"])
        if change.endswith("-"):
            labels.pop(change[:-1], None)
        else:
            k, v = change.split("="); labels[k] = v
        n["metadata"] = {**n["metadata"], "labels": labels}
    for nsname, name, ann in act["annotate"]:
        c = next(o for o in out["configmaps"]["items"] if (o["metadata"]["namespace"], o["metadata"]["name"]) == (nsname, name))
        c["metadata"] = {**c["metadata"], "annotations": {**c["metadata"]["annotations"], **ann}}
    return out


def settle(data, cfg=CFG, now=NOW):
    act = party.plan(cfg, now, data)
    return applied(data, act), act


def empty(act):
    return not (act["apply"] or act["delete"] or act["label"] or act["annotate"])


class Party(unittest.TestCase):
    def test_nothing_without_consent_or_with_one(self):
        d, act = settle(world([ns("alice"), ns("bob")], []))
        self.assertTrue(empty(act))
        d, act = settle(world([ns("alice"), ns("bob")], [cm("alice", "bob")]))
        self.assertEqual(act["apply"], [])
        self.assertEqual(act["status"], [["alice", "bob", "waiting for bob to accept"]])

    def test_both_consents_open_both_ways_on_common_ports_and_settle(self):
        d, act = settle(world([ns("alice"), ns("bob"), ns("carol")], [cm("alice", "bob"), cm("bob", "alice", "8080/TCP,9090/TCP")]))
        self.assertEqual(sorted(o["metadata"]["namespace"] for o in act["apply"]), ["play-alice", "play-bob"])
        np = next(o for o in act["apply"] if o["metadata"]["namespace"] == "play-alice")
        self.assertEqual(np["spec"]["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8080}])   # 9090 is not in pool.yaml
        self.assertEqual(np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "play-bob", "party.edge-mesh/alice": "yes"})
        self.assertEqual(sorted(act["label"]), [["play-alice", "party.edge-mesh/bob=yes"], ["play-bob", "party.edge-mesh/alice=yes"]])
        _, again = settle(d)
        self.assertTrue(empty(again), again)   # idempotent

    def test_leave_kick_lease_end_and_off_close_it(self):
        d, _ = settle(world([ns("alice"), ns("bob")], [cm("alice", "bob"), cm("bob", "alice")]))
        left = dict(d, configmaps={"items": [c for c in d["configmaps"]["items"] if c["metadata"]["namespace"] == "play-alice"]})
        after, act = settle(left)
        self.assertIn(["configmap", "play-alice", "party-bob"], act["delete"])
        self.assertEqual(sorted(x[0] for x in act["delete"]), ["configmap", "networkpolicy", "networkpolicy"])
        self.assertTrue(empty(settle(after)[1]))
        kicked = dict(d, namespaces={"items": [ns("alice", labels={"party.edge-mesh/bob": "yes"}), ns("bob", phase="Terminating")]})
        self.assertIn(["configmap", "play-alice", "party-bob"], settle(kicked)[1]["delete"])
        ended = dict(d, ends={"alice": NOW + 600, "bob": NOW})
        self.assertIn(["configmap", "play-alice", "party-bob"], settle(ended)[1]["delete"])
        off, act = settle(d, cfg={**CFG, "parties": 0})
        self.assertEqual([x[0] for x in act["delete"]], ["networkpolicy", "networkpolicy"])   # consents kept
        self.assertEqual({s for _, _, s in act["status"]}, {"off: the host has turned parties off"})

    def test_a_new_player_with_the_same_name_inherits_nothing(self):
        d, _ = settle(world([ns("alice"), ns("bob")], [cm("alice", "bob")]))
        self.assertEqual(d["configmaps"]["items"][0]["metadata"]["annotations"][party.PEER_UID], "uid-bob")
        new = dict(d, namespaces={"items": [ns("alice"), ns("bob", uid="uid-bob-2")]},
                   configmaps={"items": d["configmaps"]["items"] + [cm("bob", "alice")]})
        _, act = settle(new)
        self.assertEqual(act["apply"], [])
        self.assertIn(["configmap", "play-alice", "party-bob"], act["delete"])

    def test_refusals(self):
        _, act = settle(world([ns("alice"), ns("bob")], [cm("alice", "bob", "22/TCP"), cm("bob", "alice", "8080/TCP")]))
        self.assertEqual(act["apply"], [])
        _, act = settle(world([ns("alice")], [cm("alice", "alice"), cm("alice", "bob", "x")]))
        self.assertEqual([s for _, _, s in act["status"]], ["refused: a party needs another player", "refused: ports are <port>/TCP or /UDP, of 8080/TCP,10666/UDP"])
        bad = cm("alice", "bob"); bad["metadata"]["name"] = "party-carol"   # the name must be party-<peer>
        _, act = settle(world([ns("alice"), ns("bob")], [bad, cm("bob", "alice")]))
        self.assertEqual(act["apply"], [])



def watcher(uid="uid-watch", labels=None, phase="Active"):
    """The host's watcher namespace (D6): named watch-host, labelled edge-mesh/watcher=host, no participant label."""
    return {"metadata": {"name": "watch-host", "uid": uid, "labels": {"edge-mesh/watcher": "host", **(labels or {})}},
            "status": {"phase": phase}}


def wcm(peer, ports="", status="", uid=""):
    """The host's consent, in watch-host."""
    c = cm("x", peer, ports, status, uid); c["metadata"]["namespace"] = "watch-host"
    return c


class Watcher(unittest.TestCase):
    """D6 (#101): the host watches through a party like any peer, as `watch-host`, with the owner's consent."""

    def test_both_consents_open_the_watcher_namespace_and_settle(self):
        d, act = settle(world([ns("alice"), watcher()], [cm("alice", "watch-host"), wcm("alice")]))
        self.assertEqual(sorted(o["metadata"]["namespace"] for o in act["apply"]), ["play-alice", "watch-host"])
        np = next(o for o in act["apply"] if o["metadata"]["namespace"] == "play-alice")
        self.assertEqual(np["metadata"]["name"], "party-watch-host")
        self.assertEqual(np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "watch-host", "party.edge-mesh/alice": "yes"})
        self.assertEqual(sorted(act["label"]), [["play-alice", "party.edge-mesh/watch-host=yes"], ["watch-host", "party.edge-mesh/alice=yes"]])
        self.assertEqual(act["status"], [["alice", "watch-host", "active: ports 8080/TCP,10666/UDP"],
                                         ["watch-host", "alice", "active: ports 8080/TCP,10666/UDP"]])
        _, again = settle(d)
        self.assertTrue(empty(again), again)

    def test_the_host_alone_opens_nothing(self):
        _, act = settle(world([ns("alice"), watcher()], [wcm("alice")]))
        self.assertEqual(act["apply"], [])
        self.assertEqual(act["status"], [["watch-host", "alice", "waiting for alice to accept"]])

    def test_an_owner_consent_waits_for_a_watcher(self):
        _, act = settle(world([ns("alice")], [cm("alice", "watch-host")]))
        self.assertEqual(act["apply"], [])
        self.assertEqual(act["status"], [["alice", "watch-host", "waiting: the host has no watcher yet (watch-host)"]])

    def test_the_watcher_has_no_lease_but_the_owner_has(self):
        d, _ = settle(world([ns("alice"), watcher()], [cm("alice", "watch-host"), wcm("alice")], ends={"alice": NOW + 600}))
        self.assertEqual(len(d["netpols"]["items"]), 2)   # no lease entry for watch-host: still active
        _, act = settle(dict(d, ends={"alice": NOW}))
        self.assertIn(["configmap", "watch-host", "party-alice"], act["delete"])
        self.assertIn(["configmap", "play-alice", "party-watch-host"], act["delete"])

    def test_the_owner_leaving_closes_it(self):
        d, _ = settle(world([ns("alice"), watcher()], [cm("alice", "watch-host"), wcm("alice")]))
        left = dict(d, configmaps={"items": [c for c in d["configmaps"]["items"] if c["metadata"]["namespace"] == "watch-host"]})
        _, act = settle(left)
        self.assertIn(["configmap", "watch-host", "party-alice"], act["delete"])
        self.assertIn(["networkpolicy", "watch-host", "party-alice"], act["delete"])

    def test_a_new_watcher_namespace_inherits_nothing(self):
        # down + init gives watch-host a new UID: the owner's consent to the old one is deleted, as for a player of the same name
        d, _ = settle(world([ns("alice"), watcher()], [cm("alice", "watch-host"), wcm("alice")]))
        new = dict(d, namespaces={"items": [ns("alice", labels={"party.edge-mesh/watch-host": "yes"}), watcher(uid="uid-watch-2")]},
                   configmaps={"items": [c for c in d["configmaps"]["items"] if c["metadata"]["namespace"] == "play-alice"]})
        _, act = settle(new)
        self.assertIn(["configmap", "play-alice", "party-watch-host"], act["delete"])
        self.assertEqual([o for o in act["apply"]], [])

    def test_the_name_cannot_be_taken_by_a_player(self):
        # a player namespace play-watch-host, however labelled, is not the watcher, and its consents count for nobody
        fake = ns("watch-host")
        _, act = settle(world([ns("alice"), fake], [cm("alice", "watch-host"), cm("watch-host", "alice")]))
        self.assertEqual(act["apply"], [])
        self.assertEqual(act["status"], [["alice", "watch-host", "waiting: the host has no watcher yet (watch-host)"]])
        # nor is a namespace called watch-host without the host's label
        bare = watcher(); bare["metadata"]["labels"] = {"edge-mesh/participant": "watch-host"}
        _, act = settle(world([ns("alice"), bare], [cm("alice", "watch-host"), wcm("alice")]))
        self.assertEqual(act["apply"], [])


FAKE_KC = r"""#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
data = json.load(open(os.environ["FAKE_WORLD"]))
if sys.argv[1] == "get":
    kind = {"namespaces": "namespaces", "configmaps": "configmaps", "networkpolicies": "netpols"}[sys.argv[2]]
    print(json.dumps(data[kind]))
"""


class ReadOnlyList(unittest.TestCase):
    """`party list --read-only --json` (the host console, issue #86): only `kubectl get`, never a reconcile."""

    def setUp(self):
        import json, os, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        kc = self.tmp / "kc"
        kc.write_text(FAKE_KC)
        kc.chmod(0o755)
        cfg = self.tmp / "pool.yaml"
        cfg.write_text("parties: 1\nparty_ports: [8080/TCP, 10666/UDP]\n")
        # both agreed and nothing is open yet: a reconcile would apply NetworkPolicies and labels
        w = world([ns("ana"), ns("beto")], [cm("ana", "beto", "8080/TCP"), cm("beto", "ana", "8080/TCP")])
        (self.tmp / "world.json").write_text(json.dumps(w))
        self.log = self.tmp / "calls"
        os.environ.update(FAKE_LOG=str(self.log), FAKE_WORLD=str(self.tmp / "world.json"))
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_LOG", "FAKE_WORLD")])
        cert = self.tmp / "nebula-cert"   # every lease ends an hour after the epoch, long after NOW
        cert.write_text('#!/bin/sh\necho \'[{"details": {"notAfter": "1970-01-01T01:00:00Z"}}]\'\n')
        cert.chmod(0o755)
        for n in ("ana", "beto"):
            (self.tmp / "bundles" / n).mkdir(parents=True)
            (self.tmp / "bundles" / n / "host.crt").write_text(n)
        self.args = ["--config", str(cfg), "--bundles", str(self.tmp / "bundles"), "--nebula-cert", str(cert), "--kc", str(kc),
                     "--now", str(NOW)]

    def run_main(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = party.main([*argv, *self.args])
        return code, out.getvalue()

    def test_read_only_gets_only(self):
        import json
        code, out = self.run_main("list", "--read-only", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(sorted(r["player"] for r in rows), ["ana", "beto"])
        verbs = {line.split()[0] for line in self.log.read_text().splitlines()}
        self.assertEqual(verbs, {"get"})

    def test_plain_list_still_reconciles(self):
        self.run_main("list")
        self.assertIn("apply", self.log.read_text())

    def test_flags_only_with_list(self):
        self.assertEqual(self.run_main("reconcile", "--read-only")[0], 2)
        self.assertEqual(self.run_main("end", "ana", "beto", "--json")[0], 2)


def house(open_=True, uid="uid-house", phase="Active", label="host"):
    """The house match's namespace (docs/LIVE-PAGE.md L1): named house, labelled edge-mesh/house=host (and the watcher guard's label)."""
    labels = {"edge-mesh/house": label, "edge-mesh/watcher": "host", **({party.HOUSE_OPEN: "yes"} if open_ else {})}
    return {"metadata": {"name": "house", "uid": uid, "labels": labels}, "status": {"phase": phase}}


def hcm(peer, status=""):
    c = cm("x", peer, "", status); c["metadata"]["namespace"] = "house"
    return c


def offers(act):
    return sorted(o["data"]["peer"] for o in act["apply"] if o["kind"] == "ConfigMap")


class House(unittest.TestCase):
    """docs/LIVE-PAGE.md L8 (LV09): the open house offers a party to every player; only the player's own accept opens it."""

    def test_an_open_house_offers_to_every_player_and_nothing_opens(self):
        d, act = settle(world([ns("alice"), ns("bob"), house()], []))
        self.assertEqual(offers(act), ["alice", "bob"])
        self.assertFalse([o for o in act["apply"] if o["kind"] == "NetworkPolicy"])   # one consent opens nothing (LV09)
        d, act = settle(d)
        self.assertEqual(offers(act), [])                                                # offered once
        self.assertEqual(act["status"], [["house", "alice", "waiting for alice to accept"], ["house", "bob", "waiting for bob to accept"]])
        self.assertFalse([o for o in act["apply"] if o["kind"] == "NetworkPolicy"])

    def test_the_player_accepting_opens_it(self):
        d, _ = settle(world([ns("alice"), house()], []))
        d["configmaps"]["items"].append(cm("alice", "house"))
        d, act = settle(d)
        np = next(o for o in act["apply"] if o["metadata"]["namespace"] == "play-alice")
        self.assertEqual(np["metadata"]["name"], "party-house")
        self.assertIn(["alice", "house", "active: ports 8080/TCP,10666/UDP"], act["status"])
        _, again = settle(d)
        self.assertTrue(empty(again), again)

    def test_a_closed_house_offers_nothing_and_withdraws(self):
        _, act = settle(world([ns("alice"), house(open_=False)], []))
        self.assertEqual(offers(act), [])
        d, _ = settle(world([ns("alice"), house()], [cm("alice", "house")]))
        d, _ = settle(d)
        d["namespaces"]["items"] = [ns("alice"), house(open_=False)]
        _, act = settle(d)
        self.assertIn(["configmap", "house", "party-alice"], act["delete"])
        self.assertIn(["networkpolicy", "house", "party-alice"], act["delete"])

    def test_no_offer_without_the_hosts_label_nor_to_an_ended_lease_nor_with_parties_off(self):
        _, act = settle(world([ns("alice"), house(label="alice")], []))
        self.assertEqual(offers(act), [])                                                # a namespace called house is not the house
        _, act = settle(world([ns("alice"), house()], [], ends={"alice": NOW}))
        self.assertEqual(offers(act), [])
        _, act = settle(world([ns("alice"), house()], []), cfg={**CFG, "parties": 0})
        self.assertEqual(offers(act), [])

    def test_a_player_cannot_be_the_house(self):
        fake = ns("house")                                                               # play-house, labelled participant=house
        _, act = settle(world([ns("alice"), fake], [cm("alice", "house")]))
        self.assertIn(["alice", "house", "waiting: the host has no house match yet (house)"], act["status"])
        self.assertEqual(party.member("play-house"), "")


if __name__ == "__main__":
    unittest.main()
