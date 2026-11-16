"""Player console scope and credential-handling checks from RFC PM01-PM05 (#100)."""
from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
sys.path.insert(0, str(HERE))
import player  # noqa: E402
import server  # noqa: E402


def jwt(exp=2000, sub="system:serviceaccount:play-alice:user"):
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return enc(b'{"alg":"none"}') + "." + enc(json.dumps({"exp": exp, "sub": sub}).encode()) + ".sig"


def kubeconfig(exp=2000, namespace="play-alice", name="alice", token=None, api="https://10.77.0.2:6443"):
    token = token or jwt(exp, f"system:serviceaccount:{namespace}:user")
    return (f"apiVersion: v1\nkind: Config\nclusters:\n- name: edge-mesh\n  cluster:\n    server: {api}\n"
            "users:\n- name: alice\n  user:\n    token: " + token + "\n"
            "contexts:\n- name: alice\n  context:\n    cluster: edge-mesh\n    user: alice\n    namespace: " + namespace + "\n"
            "current-context: " + name + "\n")


class FakeKubectl:
    def __init__(self):
        self.calls = []
        self.reply = {"items": []}
        self.returncode = 0
        self.stderr = ""

    def __call__(self, argv, *, input=None, capture_output=False, timeout=None, check=False):
        self.calls.append((argv, input))
        return type("Result", (), {"returncode": self.returncode, "stdout": json.dumps(self.reply), "stderr": self.stderr})()


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = self.home / "kubeconfig"
        self.cfg.write_text(kubeconfig())
        self.cfg.chmod(0o600)
        self.kc = FakeKubectl()
        self.now = [1600]
        self.p = player.Player(self.cfg, kubectl="kubectl", runner=self.kc, clock=lambda: self.now[0])
        state = self.home / ".config" / "edge-mesh"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(state, 0o700)
        (state / "console").mkdir(mode=0o700)
        self.token = "p" * 40
        (state / "console" / "web.token.sha256").write_text(server.digest(self.token))
        self.p.state = state
        handler = type("TestPlayerHandler", (player.Handler,), {"player": self.p, "port": 0, "gate": server.Gate(), "static": {}})
        self.web = server.WebServer(("127.0.0.1", 0), handler)
        handler.port = self.web.server_address[1]
        thread = threading.Thread(target=self.web.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (self.web.shutdown(), self.web.server_close(), thread.join(timeout=2)))
        self.port = handler.port

    def request(self, path, method="GET", *, host=None, origin=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Host": host or f"127.0.0.1:{self.port}", "Authorization": "Bearer " + self.token}
        if origin is not None: h["Origin"] = origin
        if body is not None: h["Content-Type"] = "application/json"
        c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        r = c.getresponse(); result = r.status, r.read(); c.close(); return result

    def test_pm01_loopback_only_address_validation(self):
        self.assertEqual(server.web_address("127.0.0.1:8091"), ("127.0.0.1", 8091))
        for bad in ("0.0.0.0:8091", "192.168.1.9:8091", "localhost:8091", "[::]:8091"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.web_address(bad)

    def test_pm02_countdown_and_node_connection_use_only_own_namespace(self):
        with patch("player.subprocess.run", return_value=type("R", (), {"returncode": 0, "stdout": '[{"operstate":"UP"}]'})()):
            got = self.p.get("status", {})
        self.assertEqual(got["seconds_left"], 400)
        self.assertEqual(got["node"]["nebula"], "up")
        self.assertIn("does not report Kubernetes Node Ready", got["node"]["note"])
        argv = self.kc.calls[-1][0]
        self.assertEqual(argv[3:7], ["get", "pods", "-n", "play-alice"])
        self.assertNotIn("nodes", argv)
        self.now[0] = 2100
        self.assertEqual(self.p.get("status", {})["seconds_left"], 0)
        self.kc.returncode, self.kc.stderr = 1, "Error from server (Unauthorized): token expired"
        expired = self.p.get("status", {})
        self.assertEqual(expired["node"]["api"], "expired or revoked")
        self.assertEqual(expired["seconds_left"], 0)

    def test_pm02_forbidden_read_is_named_as_outside_namespace(self):
        self.kc.returncode, self.kc.stderr = 1, 'Error from server (Forbidden): nodes is forbidden'
        with self.assertRaises(player.PlayerError) as e:
            self.p.get("status", {})
        self.assertEqual((e.exception.status, e.exception.code), (403, "outside-namespace"))

    def test_pm03_rejects_wrong_service_account_namespace_or_public_api_server(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "kubeconfig"
            cfg.write_text(kubeconfig(namespace="play-bob", name="bob", token=jwt()))
            with self.assertRaises(player.PlayerError):
                player.Player(cfg, kubectl="kubectl")
            cfg.write_text(kubeconfig(api="https://example.com:6443"))
            with self.assertRaises(player.PlayerError):
                player.Player(cfg, kubectl="kubectl")

    def test_events_parties_games_are_filtered_to_owned_namespace(self):
        self.kc.reply = {"items": [{"metadata": {"name": "party-bob", "annotations": {"edge-mesh/party-status": "active: ports 10666/UDP"}},
                                    "data": {"peer": "bob", "ports": "10666/UDP"}},
                                   {"metadata": {"name": "party-carol", "annotations": {"edge-mesh/party-status": "waiting for host reconciliation"}},
                                    "data": {"peer": "carol", "ports": ""}},
                                   {"metadata": {"name": "party-bad"}, "data": {"peer": "not valid"}}]}
        self.assertEqual(self.p.get("games", []), [{"peer": "bob", "ports": "10666/UDP", "status": "active: ports 10666/UDP",
                                                     "watching": False}])
        rows = self.p.get("parties", [])
        self.assertEqual([r["peer"] for r in rows], ["bob", "carol"])
        self.assertIn("-n", self.kc.calls[-1][0])
        self.assertIn("play-alice", self.kc.calls[-1][0])

    def test_pm04_party_mutations_are_validated_and_scoped(self):
        self.p.act("party-invite", {"peer": "bob", "ports": "10666/UDP"})
        argv, stdin = self.kc.calls[-1]
        self.assertEqual(argv[3:], ["apply", "-n", "play-alice", "-f", "-"])
        obj = json.loads(stdin)
        self.assertEqual(obj["metadata"]["name"], "party-bob")
        self.assertEqual(obj["metadata"]["labels"], {"edge-mesh/party-consent": "yes"})
        before = len(self.kc.calls)
        for body in ({"peer": "bob;whoami"}, {"peer": "alice"}, {"peer": "bob", "ports": "1/UDP,99999/TCP"}, {"peer": "bob", "extra": "x"}):
            with self.subTest(body=body), self.assertRaises(player.PlayerError):
                self.p.act("party-accept", body)
        self.assertEqual(len(self.kc.calls), before)
        self.p.act("party-leave", {})
        self.assertEqual(self.kc.calls[-1][0][3:], ["delete", "configmaps", "-n", "play-alice", "-l", "edge-mesh/party-consent", "--ignore-not-found", "--wait=false"])

    def test_pm03_api_redacts_token_pem_and_node_key_path(self):
        leaked = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY----- " + jwt() + " /etc/nebula/host.key"
        self.kc.reply = {"items": [{"message": leaked}]}
        status, data = self.request("/api/v1/events")
        self.assertEqual(status, 200)
        for secret in ("BEGIN PRIVATE KEY", jwt(), "/etc/nebula"):
            self.assertNotIn(secret.encode(), data)

    def test_pm04_handwritten_host_action_is_forbidden(self):
        before = len(self.kc.calls)
        status, data = self.request("/api/v1/verbs")
        self.assertEqual(status, 200)
        self.assertTrue(data)
        self.assertTrue(all(not v["allowed"] for v in json.loads(data)))
        status, _ = self.request("/api/v1/actions/kick", "POST", body={"peer": "bob"})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.kc.calls), before)

    def test_pm05_host_and_origin_checks_block_rebinding(self):
        status, _ = self.request("/api/v1/health", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        status, _ = self.request("/api/v1/health", origin="https://evil.example")
        self.assertEqual(status, 403)
        status, data = self.request("/api/v1/verbs")
        self.assertEqual(status, 200)
        self.assertNotIn(b"system:serviceaccount", data)


class InstallKubeconfigTest(unittest.TestCase):
    def test_installs_atomic_user_owned_0600_file_and_refuses_symlink_dir(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            src = root / "bundle-kubeconfig"
            src.write_text(kubeconfig())
            home = root / "home"
            home.mkdir()
            target = player.install_kubeconfig(src, home, os.getuid(), os.getgid())
            self.assertEqual(target.read_text(), src.read_text())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_uid, os.getuid())
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); home = root / "home"; home.mkdir()
            outside = root / "outside"; outside.mkdir()
            (home / ".config").symlink_to(outside, target_is_directory=True)
            src = root / "bundle-kubeconfig"; src.write_text(kubeconfig())
            with self.assertRaises(OSError):
                player.install_kubeconfig(src, home, os.getuid(), os.getgid())


if __name__ == "__main__":
    unittest.main()
