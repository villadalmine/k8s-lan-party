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
        out["netpols"]["items"] = [o for o in out["netpols"]["items"]
                                   if (o["metadata"]["namespace"], o["metadata"]["name"]) != (obj["metadata"]["namespace"], obj["metadata"]["name"])]
        out["netpols"]["items"].append(obj)
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


if __name__ == "__main__":
    unittest.main()
