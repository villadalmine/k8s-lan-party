"""watcher.py (D6, issue #101): the host's watcher namespace and where its pods may run. Pure: no cluster."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import watcher  # noqa: E402


def node(name, trust="own", cp=False, ready="True", taint=False):
    labels = {"node-restriction.kubernetes.io/trust": trust} if trust else {}
    if cp:
        labels["node-role.kubernetes.io/control-plane"] = "true"
    spec = {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]} if taint else {}
    return {"metadata": {"name": name, "labels": labels}, "spec": spec,
            "status": {"conditions": [{"type": "Ready", "status": ready}]}}


class Manifests(unittest.TestCase):
    def setUp(self):
        self.items = {o["kind"]: o for o in watcher.manifests({"quota_cpu_millis": 1000, "watch_quota_cpu_millis": 800})["items"]}

    def test_a_namespace_of_the_host_not_a_player(self):
        ns = self.items["Namespace"]["metadata"]
        self.assertEqual(ns["name"], "watch-host")
        self.assertEqual(ns["labels"]["edge-mesh/watcher"], "host")
        self.assertNotIn("edge-mesh/participant", ns["labels"])
        self.assertEqual(ns["labels"]["pod-security.kubernetes.io/enforce"], "restricted")

    def test_no_credential_for_anyone_but_the_host(self):
        self.assertEqual(set(self.items), {"Namespace", "ResourceQuota", "NetworkPolicy", "LimitRange"})

    def test_its_own_quota_is_the_cap(self):
        hard = self.items["ResourceQuota"]["spec"]["hard"]
        self.assertEqual((hard["limits.cpu"], hard["services.nodeports"]), ("800m", "0"))
        self.assertEqual(self.items["ResourceQuota"]["metadata"]["namespace"], "watch-host")

    def test_default_deny_like_a_player(self):
        np = self.items["NetworkPolicy"]["spec"]
        self.assertEqual(np["ingress"], [{"from": [{"podSelector": {}}]}])


class Nodes(unittest.TestCase):
    def test_own_ready_and_not_the_control_plane(self):
        data = {"items": [node("cp", cp=True), node("tainted", taint=True), node("n1", trust="friends"), node("n2", ready="False"),
                          node("n3", trust=None), node("own1"), node("own0")]}
        self.assertEqual(watcher.nodes(data), ["own0", "own1"])

    def test_no_such_node_refuses(self):
        r = subprocess.run([sys.executable, str(HERE / "watcher.py"), "nodes"], input=json.dumps({"items": [node("cp", cp=True)]}),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not the control plane", r.stderr)

    def test_the_affinity_keeps_off_the_control_plane(self):
        exprs = watcher.affinity()["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertIn({"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}, exprs)
        self.assertIn({"key": "node-restriction.kubernetes.io/trust", "operator": "In", "values": ["own"]}, exprs)

    def test_the_policy_says_the_same(self):
        text = (HERE / "watcher-policy.yaml").read_text()
        self.assertIn("matchLabels: {edge-mesh/watcher: host}", text)
        self.assertIn("node-role.kubernetes.io/control-plane\", \"operator\": \"DoesNotExist\"", text)


if __name__ == "__main__":
    unittest.main()
