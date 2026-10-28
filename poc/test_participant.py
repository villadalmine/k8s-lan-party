"""Unit tests of poc/participant.py: the participant's namespace, RBAC, quota and kubeconfig (issues #9, #11), and what the idle kick
reads. No cluster: the objects are checked as data."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import participant
from participant import activity, kubeconfig, manifests, namespace, seconds

CA = b"-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
CFG = {"quota_pods": 7, "quota_cpu_millis": 900, "quota_memory_mib": 768, "limit_default_cpu_millis": 100,
       "limit_default_memory_mib": 64, "limit_max_cpu_millis": 400, "limit_max_memory_mib": 256}


def kinds(lst):
    return {i["kind"]: i for i in lst["items"]}


class NamespaceTest(unittest.TestCase):
    def test_a_prefix_keeps_players_out_of_system_namespaces(self):
        self.assertEqual(namespace("alice"), "play-alice")
        self.assertEqual(namespace("kube-system"), "play-kube-system")   # a valid player name, never the real one
        for bad in ("", "A", "a", "a;b", "../x", "alice\n", "x" * 21):
            with self.assertRaises(ValueError, msg=bad):
                namespace(bad)


class ManifestsTest(unittest.TestCase):
    def setUp(self):
        self.k = kinds(manifests("alice", CFG))

    def test_every_object_lives_in_play_alice_and_is_labelled(self):
        self.assertEqual(sorted(self.k), ["LimitRange", "Namespace", "ResourceQuota", "Role", "RoleBinding", "ServiceAccount"])
        self.assertEqual(self.k["Namespace"]["metadata"]["name"], "play-alice")
        for kind, obj in self.k.items():
            if kind != "Namespace":
                self.assertEqual(obj["metadata"]["namespace"], "play-alice", kind)
            self.assertEqual(obj["metadata"]["labels"]["edge-mesh/participant"], "alice", kind)

    def test_pod_security_restricted_is_enforced(self):
        labels = self.k["Namespace"]["metadata"]["labels"]
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertFalse(self.k["ServiceAccount"]["automountServiceAccountToken"])

    def test_the_role_grants_no_secrets_nodes_rbac_or_wildcards(self):
        rules = self.k["Role"]["rules"]
        resources = {r for rule in rules for r in rule["resources"]}
        groups = {g for rule in rules for g in rule["apiGroups"]}
        verbs = {v for rule in rules for v in rule["verbs"]}
        self.assertTrue({"pods", "deployments", "services", "configmaps"} <= resources)
        for never in ("secrets", "nodes", "serviceaccounts", "serviceaccounts/token", "roles", "rolebindings", "namespaces",
                      "endpoints", "endpointslices", "persistentvolumeclaims", "*"):
            self.assertNotIn(never, resources)
        self.assertFalse(groups & {"rbac.authorization.k8s.io", "apiextensions.k8s.io", "*"})
        self.assertFalse(verbs & {"*", "escalate", "bind", "impersonate"})
        rb = self.k["RoleBinding"]
        self.assertEqual(rb["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "participant"})
        self.assertEqual(rb["subjects"], [{"kind": "ServiceAccount", "name": "user", "namespace": "play-alice"}])

    def test_quota_and_limits_come_from_pool_yaml(self):
        hard = self.k["ResourceQuota"]["spec"]["hard"]
        self.assertEqual(hard["pods"], "7")
        self.assertEqual((hard["requests.cpu"], hard["limits.cpu"], hard["limits.memory"]), ("900m", "900m", "768Mi"))
        self.assertEqual((hard["services.loadbalancers"], hard["services.nodeports"], hard["persistentvolumeclaims"]), ("0", "0", "0"))
        self.assertEqual((hard["limits.ephemeral-storage"], hard["count/deployments.apps"], hard["configmaps"]), ("2048Mi", "5", "10"))
        lim = self.k["LimitRange"]["spec"]["limits"][0]
        self.assertEqual(lim["default"], {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "256Mi"})
        self.assertEqual(lim["defaultRequest"], {"cpu": "50m", "memory": "32Mi", "ephemeral-storage": "128Mi"})
        self.assertEqual(lim["max"], {"cpu": "400m", "memory": "256Mi", "ephemeral-storage": "1024Mi"})

    def test_missing_keys_take_the_defaults(self):
        hard = kinds(manifests("bob", {}))["ResourceQuota"]["spec"]["hard"]
        self.assertEqual((hard["pods"], hard["limits.cpu"]), ("10", "1000m"))

    def test_the_output_is_deterministic(self):
        self.assertEqual(json.dumps(manifests("alice", CFG), sort_keys=True), json.dumps(manifests("alice", CFG), sort_keys=True))


class KubeconfigTest(unittest.TestCase):
    def test_a_token_and_the_pinned_ca_and_no_client_certificate(self):
        kc = kubeconfig("alice", "https://10.77.0.2:6443", CA, JWT + "\n")
        self.assertIn("    server: https://10.77.0.2:6443\n", kc)
        self.assertIn(f"    token: {JWT}\n", kc)
        self.assertIn("    namespace: play-alice\n", kc)
        self.assertIn("current-context: alice\n", kc)
        ca = kc.split("certificate-authority-data: ", 1)[1].split("\n", 1)[0]
        self.assertEqual(base64.b64decode(ca), CA)
        for never in ("client-certificate", "client-key", "K10", "NODE_PASSWORD", "insecure-skip-tls-verify"):
            self.assertNotIn(never, kc)

    def test_a_token_that_is_not_a_jwt_or_a_ca_that_is_not_pem_is_refused(self):
        for bad in ("", "abc def", "x\nusers: []", "a.b.c\n- name: evil"):
            with self.assertRaises(ValueError, msg=bad):
                kubeconfig("alice", "https://10.77.0.2:6443", CA, bad)
        with self.assertRaises(ValueError):
            kubeconfig("alice", "https://10.77.0.2:6443", b"not a cert", JWT)


class SecondsTest(unittest.TestCase):
    def test_durations(self):
        for value, want in (("90s", 90), ("15m", 900), ("2h", 7200), ("1h30m", 5400), ("900s", 900), ("24h0m0s", 86400)):
            self.assertEqual(seconds(value), want, value)
        for bad in ("", "10", "1.5h", "-5m", "5 m", "5min", "5m;id"):
            with self.assertRaises(ValueError, msg=bad):
                seconds(bad)


def node(name, ready="True"):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


def pod(ns, node_name="", phase="Running", owner=None):
    meta = {"namespace": ns, "name": "p"}
    if owner:
        meta["ownerReferences"] = [{"kind": owner}]
    return {"metadata": meta, "spec": {"nodeName": node_name}, "status": {"phase": phase}}


class ActivityTest(unittest.TestCase):
    def check(self, nodes, pods, want):
        self.assertEqual(activity("alice", {"items": nodes}, {"items": pods}), want)

    def test_no_node_not_ready_or_no_workload_is_idle(self):
        self.check([], [pod("play-alice", "alice")], (False, "no node"))
        self.check([node("alice", "Unknown")], [pod("play-alice", "alice")], (False, "node not Ready"))
        self.check([node("alice")], [], (False, "no workload"))

    def test_what_counts_as_a_workload(self):
        self.check([node("alice")], [pod("play-alice", "bob")], (True, ""))          # a pod of hers, wherever it runs
        self.check([node("alice")], [pod("play-bob", "alice")], (True, ""))          # somebody's pod on her node
        self.check([node("alice")], [pod("kube-system", "alice")], (False, "no workload"))
        self.check([node("alice")], [pod("cilium", "alice", owner="DaemonSet")], (False, "no workload"))
        self.check([node("alice")], [pod("play-alice", "alice", phase="Succeeded")], (False, "no workload"))
        self.check([node("alice")], [pod("play-alice", "", phase="Pending")], (True, ""))

    def test_the_command_line(self):
        def run(*args, stdin=""):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                return participant.main(list(args)), out.getvalue()
        data = json.dumps({"nodes": {"items": [node("alice")]}, "pods": {"items": []}})
        self.assertEqual(run("activity", "alice", stdin=data), (1, "idle: no workload\n"))
        data = json.dumps({"nodes": {"items": [node("alice")]}, "pods": {"items": [pod("play-alice")]}})
        self.assertEqual(run("activity", "alice", stdin=data), (0, "active\n"))
        self.assertEqual(run("activity", "alice", stdin="not json")[0], 2)
        self.assertEqual(run("seconds", "15m"), (0, "900\n"))
        self.assertEqual(run("seconds", "15")[0], 2)
        self.assertEqual(run("namespace", "Bad")[0], 2)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "pool.yaml").write_text("quota_pods: 3\n")
            code, out = run("manifests", "alice", "--config", str(Path(d) / "pool.yaml"))
        self.assertEqual(code, 0)
        self.assertEqual(kinds(json.loads(out))["ResourceQuota"]["spec"]["hard"]["pods"], "3")


if __name__ == "__main__":
    unittest.main()
