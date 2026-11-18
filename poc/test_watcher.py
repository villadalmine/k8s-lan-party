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
        self.assertEqual(text.count('{"key": dyn("node-role.kubernetes.io/control-plane"), "operator": dyn("DoesNotExist")}'), 2)


if __name__ == "__main__":
    unittest.main()


FAKE_KC = r'''#!/usr/bin/env python3
# a kubectl that knows two things: `get` (from FAKE_GET, a JSON file per resource) and `exec -i ... -- python3 -c CODE PORT`: it runs CODE
# here against a real HTTP server on a free port that checks the bearer secret like watch_stream.py and answers one JPEG
import http.server, json, os, subprocess, sys, threading
a = sys.argv[1:]
open(os.environ["FAKE_KC_LOG"], "a").write(json.dumps(a) + "\n")
if a[0] == "get":
    print(open(os.path.join(os.environ["FAKE_GET"], a[1] + ".json")).read()); sys.exit(0)
if a[0] == "exec":
    want = os.environ["FAKE_WANT"]   # the secret the pod was started with
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer " + want:
                self.send_response(401); self.end_headers(); return
            body = b"\xff\xd8 a frame \xff\xd9"
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *x): pass
    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    cmd = a[a.index("--") + 1:]
    assert cmd[:2] == ["python3", "-c"] and cmd[3] == "17777", cmd
    sys.exit(subprocess.run([sys.executable, "-c", cmd[2], str(s.server_address[1])]).returncode)
sys.exit(9)
'''


class HostViews(unittest.TestCase):
    """The host console's reads (D7's host path): status gets only, and frames come through the host's own pod with the per-pod secret."""

    def setUp(self):
        import os, tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="w"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kc = self.tmp / "kc"
        self.kc.write_text(FAKE_KC)
        self.kc.chmod(0o755)
        self.secret = self.tmp / "ana.token"
        self.secret.write_text("a" * 64 + "\n")
        (self.tmp / "get").mkdir()
        self.log = self.tmp / "log"
        self.env = {"FAKE_KC_LOG": str(self.log), "FAKE_GET": str(self.tmp / "get"), "FAKE_WANT": "a" * 64}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in self._old.items()])

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_status_reads_namespaces_and_pods_only(self):
        (self.tmp / "get" / "namespaces.json").write_text(json.dumps({"items": [{"metadata": {"name": "watch-host"}}]}))
        pod = {"metadata": {"name": "doom-watch-ana"}, "spec": {"nodeName": "hostpc", "containers": [{"command": [
            "/usr/local/bin/watch-client.sh", "doom.play-ana.svc.cluster.local", "10666", "beto", "host"]}]},
            "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]}}
        other = {"metadata": {"name": "guard-probe"}, "spec": {}, "status": {}}
        (self.tmp / "get" / "pods.json").write_text(json.dumps({"items": [pod, other]}))
        parties = [{"player": "watch-host", "peer": "ana", "status": "active: ports"}, {"player": "ana", "peer": "beto", "status": "x"}]
        out = watcher.status(str(self.kc), parties)
        self.assertEqual(out, {"watcher": True, "parties": [{"owner": "ana", "status": "active: ports"}], "spectators": [
            {"owner": "ana", "node": "hostpc", "phase": "Running", "ready": True, "restarts": 0, "following": "beto"}]})
        self.assertTrue(all(c[0] == "get" for c in self.calls()))

    def test_no_watcher_no_pods_read(self):
        (self.tmp / "get" / "namespaces.json").write_text(json.dumps({"items": []}))
        self.assertEqual(watcher.status(str(self.kc), [])["watcher"], False)
        self.assertEqual([c[1] for c in self.calls()], ["namespaces"])

    def test_a_frame_through_the_hosts_own_pod_with_the_secret(self):
        f = watcher.Tap(str(self.kc), "doom-watch-ana", self.secret)
        self.addCleanup(f.close)
        self.assertEqual(f.fetch(), b"\xff\xd8 a frame \xff\xd9")
        self.assertEqual(f.fetch(), b"\xff\xd8 a frame \xff\xd9")   # the same channel, kept open
        ex = [c[:6] for c in self.calls() if c[0] == "exec"]
        self.assertEqual(ex, [["exec", "-i", "-n", "watch-host", "doom-watch-ana", "--"]])
        self.assertNotIn("a" * 64, json.dumps(self.calls()))   # the secret goes on stdin, never on a command line

    def test_a_wrong_secret_gets_no_frame(self):
        f = watcher.Tap(str(self.kc), "doom-watch-ana", self.secret)
        self.addCleanup(f.close)
        f.fetch()
        f.close()
        self.secret.write_text("b" * 64)   # not the pod's secret: 401, no frame
        with self.assertRaises(RuntimeError):
            f.fetch()

    def test_only_spectator_pods_and_a_real_secret(self):
        with self.assertRaises(ValueError):
            watcher.Tap(str(self.kc), "doom-ana", self.secret)
        self.secret.write_text("not a secret")
        with self.assertRaises(RuntimeError):
            watcher.Tap(str(self.kc), "doom-watch-ana", self.secret).fetch()
