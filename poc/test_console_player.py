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
        self.replies = []
        self.returncode = 0
        self.stderr = ""

    def __call__(self, argv, *, input=None, capture_output=False, timeout=None, check=False):
        self.calls.append((argv, input))
        reply = self.replies.pop(0) if self.replies else self.reply
        return type("Result", (), {"returncode": self.returncode, "stdout": json.dumps(reply), "stderr": self.stderr})()


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
                                                     "watching": False, "playing": False}])
        rows = self.p.get("parties", [])
        self.assertEqual([r["peer"] for r in rows], ["bob", "carol"])
        self.assertIn("-n", self.kc.calls[-1][0])
        self.assertIn("play-alice", self.kc.calls[-1][0])

    def test_a_watcher_stays_stoppable_when_the_host_closes_its_party(self):
        self.kc.replies = [
            {"items": []},
            {"items": [{"metadata": {"name": "doom-watch-bob"}, "status": {"phase": "Running"}}]},
        ]
        self.assertEqual(self.p.get("games", {}), [{"peer": "bob", "ports": "",
                         "status": "inactive: party ended; stop the spectator", "watching": True, "playing": False}])

    def test_watch_host_is_the_hosts_watcher_not_a_game(self):
        # a party with watch-host is the host watching the owner; it serves no game, so no spectator is built for it (PR #112 review)
        with patch.object(self.p, "get", return_value=[{"peer": "watch-host", "status": "active: ports 10666/UDP"}]):
            with self.assertRaises(player.PlayerError) as e:
                self.p.act("watch-start", {"peer": "watch-host", "image": "localhost/emesh-doom:12.3.0-abcdef012345"})
        self.assertEqual((e.exception.status, e.exception.code), (400, "bad-peer"))
        self.assertEqual(self.kc.calls, [])

    def test_the_hosts_watcher_is_not_listed_as_a_game(self):
        # the owner's party with watch-host is the host watching them: no Watch button for it (PR #112 verification)
        self.kc.replies = [{"items": [
            {"metadata": {"name": "party-watch-host", "annotations": {"edge-mesh/party-status": "active: ports 10666/UDP"}},
             "data": {"peer": "watch-host", "ports": ""}},
            {"metadata": {"name": "party-bob", "annotations": {"edge-mesh/party-status": "active: ports 10666/UDP"}},
             "data": {"peer": "bob", "ports": ""}}]}, {"items": []}]
        self.assertEqual([g["peer"] for g in self.p.get("games", [])], ["bob"])

    def test_frames_stop_when_the_party_is_no_longer_active(self):
        with patch.object(self.p, "get", return_value=[{"peer": "bob", "status": "inactive: party ended; stop it"}]):
            with self.assertRaises(player.PlayerError) as e:
                self.p.watch_frame("bob")
        self.assertEqual((e.exception.status, e.exception.code), (404, "watch-unavailable"))
        self.assertEqual(self.kc.calls, [])

    def test_play_start_needs_a_game_and_renders_a_private_player_pod(self):
        """#102: the tab plays only a game the player has an active party with, or their own server; one at a time."""
        img = "localhost/emesh-doom:12.3.0-abcdef012345"
        with patch.object(self.p, "get", return_value=[]):
            with self.assertRaises(player.PlayerError) as e:
                self.p.act("play-start", {"peer": "bob", "image": img})
        self.assertEqual(e.exception.code, "not-a-game")
        with self.assertRaises(player.PlayerError) as e:
            self.p.act("play-start", {"peer": "alice", "image": img})   # own game, but no server
        self.assertEqual(e.exception.code, "no-game")
        with patch.object(self.p, "get", return_value=[{"peer": "bob", "status": "active: ports 10666/UDP"}]):
            self.kc.calls.clear()
            self.p.act("play-start", {"peer": "bob", "image": img})
        argv, stdin = self.kc.calls[-1]
        self.assertIn("apply", argv)
        pod = stdin.decode()
        self.assertIn("name: doom-play-bob", pod)
        self.assertIn('"doom.play-bob.svc.cluster.local", "10666", "alice"', pod)
        self.assertNotIn("hostPort", pod)
        self.kc.reply = {"items": [{"metadata": {"name": "doom-play-bob"}}]}
        with patch.object(self.p, "get", return_value=[{"peer": "bob", "status": "active: ports 10666/UDP"}]):
            with self.assertRaises(player.PlayerError) as e:
                self.p.act("play-start", {"peer": "bob", "image": img})
        self.assertEqual(e.exception.code, "play-exists")

    def test_keys_are_only_the_games_and_go_to_the_own_pod(self):
        for bad in ({"down": ["`"]}, {"down": ["Enter"]}, {"down": ["a"]}, {"text": "kill"}, {"down": ["ArrowUp"] * 9}, {"down": "ArrowUp"}):
            with self.assertRaises(player.PlayerError) as e:
                self.p.keys("bob", bad)
            self.assertEqual(e.exception.code, "bad-keys", bad)
        self.assertEqual(self.kc.calls, [])   # refused before any cluster call
        pod = {"metadata": {"name": "doom-play-bob"}, "spec": {"nodeName": "bob-node", "containers": [{"env": [
            {"name": "WATCH_TOKEN", "value": "a" * 64}]}]}, "status": {"phase": "Running", "podIP": "10.42.1.5"}}
        self.kc.reply = {"items": [pod]}
        with patch.object(self.p, "get", return_value=[{"peer": "bob", "status": "active: ports 10666/UDP"}]):
            with self.assertRaises(player.PlayerError) as e:   # on another node than this player's: never reached
                self.p.keys("bob", {"down": ["ArrowUp"]})
        self.assertEqual(e.exception.code, "play-unavailable")

    def test_the_pod_endpoint_is_reused_for_a_few_seconds_only(self):
        """A frame or a key does not ask the API each time (a tab would get one frame a second); after ENDPOINT_TTL, or play-stop, it asks again."""
        with patch.object(self.p, "_lookup", return_value=("10.42.1.5", "a" * 64)) as look:
            self.p._endpoint("play", "bob"); self.p._endpoint("play", "bob")
            self.assertEqual(look.call_count, 1)
            self.now[0] += player.ENDPOINT_TTL
            self.p._endpoint("play", "bob")
            self.assertEqual(look.call_count, 2)
            self.p.act("play-stop", {"peer": "bob"})
            self.p._endpoint("play", "bob")
            self.assertEqual(look.call_count, 3)
            self.p._endpoint("watch", "bob")   # a spectator is another pod
            self.assertEqual(look.call_count, 4)

    def test_a_game_client_left_behind_keeps_a_row_and_nothing_starts_on_an_ended_party(self):
        """PR #114 review: a play pod whose party ended stays on the page (to leave it); play-start and watch-start need an active party."""
        with patch.object(self.p, "_watch_pods", return_value=[]), \
                patch.object(self.p, "_play_pods", return_value=[{"metadata": {"name": "doom-play-bob"}}]), \
                patch.object(self.p, "_pod", return_value=None):
            self.kc.reply = {"items": [{"metadata": {"name": "party-bob", "annotations": {"edge-mesh/party-status": "inactive: closed"}},
                                        "data": {"peer": "bob", "ports": ""}}]}
            rows = self.p.get("games", [])
        self.assertEqual(rows, [{"peer": "bob", "ports": "", "status": "inactive: party ended; leave the game", "watching": False,
                                 "playing": True}])
        img = "localhost/emesh-doom:12.3.0-abcdef012345"
        with patch.object(self.p, "get", return_value=rows):
            for verb in ("play-start", "watch-start"):
                with self.assertRaises(player.PlayerError) as e:
                    self.p.act(verb, {"peer": "bob", "image": img})
                self.assertEqual(e.exception.code, "not-a-game", verb)

    def test_a_pod_not_ready_is_remembered_too(self):
        """A tab asks 10 times a second: a 'not ready' answer is kept ENDPOINT_TTL seconds, not 5 kubectl calls each time."""
        with patch.object(self.p, "_lookup", side_effect=player.PlayerError(404, "play-unavailable", "not ready")) as look:
            for _ in range(5):
                with self.assertRaises(player.PlayerError):
                    self.p._endpoint("play", "bob")
            self.assertEqual(look.call_count, 1)
            self.now[0] += player.ENDPOINT_TTL
            with self.assertRaises(player.PlayerError):
                self.p._endpoint("play", "bob")
            self.assertEqual(look.call_count, 2)

    def test_the_three_key_lists_are_one(self):
        """The page, the player's daemon and the pod each check the keys: the three lists must say the same (PR #114 review)."""
        import ast, re as _re
        pod = Path(__file__).resolve().parent / "doom" / "watch_stream.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("watch_stream_keys", pod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        keys = mod.KEYS
        js = (Path(__file__).resolve().parent / "console" / "player-static" / "player.js").read_text()
        page = set(ast.literal_eval("[" + _re.search(r"const PLAY_KEYS = new Set\(\[(.*?)\]\)", js).group(1) + "]"))
        self.assertEqual(set(keys), player.PLAY_KEYS)
        self.assertEqual(page, player.PLAY_KEYS)
        for never in ("`", "Enter", "Escape", "Tab", "a", "t", "y"):
            self.assertNotIn(never, player.PLAY_KEYS)

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
