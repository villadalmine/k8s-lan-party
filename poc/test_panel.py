"""The panel (#119, poc/panel.py and poc/page/panelspec.py): real numbers or nothing. What the host reads (kubectl and mesh's read-only
reads are fakes here), what a profile lets out, what the public page accepts from a report and what it shows: counts and states only,
never a name, and never a stale or guessed value."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "page"))
import panel  # noqa: E402
import panelspec  # noqa: E402

GOOD = {"nodes_total": 3, "nodes_ready": 3, "wireguard": True, "house_open": False, "admitted_total": 7}


def node(name, ready=True, participant=False, cpu="4", mem="8Gi"):
    return {"metadata": {"name": name, "labels": {"edge-mesh/participant": "yes"} if participant else {}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "allocatable": {"cpu": cpu, "memory": mem}}}


class Fake:
    """kubectl and mesh as the panel sees them. Any argv it does not know fails, like a real read that fails."""

    def __init__(self):
        self.calls = []
        self.nodes = [node("host"), node("alice-1", participant=True, cpu="1500m", mem="2048Mi"), node("bob-1", ready=False, participant=True)]
        self.stats = {"host": (2_500_000_000, 3 * 1024 ** 3), "alice-1": (500_000_000, 1024 ** 3)}
        self.pods = [("kube-system", "cilium-x"), ("play-alice", "doom-server"), ("play-alice", "doom-client"), ("house", "doom-server"),
                     ("house", "doom-bot-a")]
        self.namespaces = ["kube-system", "play-alice", "play-bob", "house"]
        self.house_open = True
        self.cilium = {"enable-wireguard": "true", "encrypt-node": "false", "mesh-auth-mutual-enabled": "true"}
        self.parties = [{"player": "alice", "peer": "bob", "status": "active: ports 10666/UDP"},
                        {"player": "bob", "peer": "alice", "status": "active: ports 10666/UDP"},
                        {"player": "carol", "peer": "alice", "status": "waiting for alice to accept"},
                        {"player": "house", "peer": "alice", "status": "active"}, {"player": "alice", "peer": "house", "status": "active"},
                        {"player": "house", "peer": "watch-host", "status": "active"},
                        {"player": "house", "peer": "bob", "status": "closed: the house match is closed"}]
        self.metrics = ('# TYPE emesh_admission_events_total counter\nemesh_admission_events_total{type="request"} 9\n'
                        'emesh_admission_events_total{type="approved"} 5\nemesh_admission_events_total{type="expelled"} 2\n'
                        'emesh_admission_events_total{type="refused"} 1\n')
        self.down = set()

    def run(self, argv, timeout=30):
        a = argv[1:]
        self.calls.append(a)
        j = lambda o: json.dumps(o)  # noqa: E731
        if argv[0].endswith("kc"):
            if a[:2] == ["get", "nodes"] and "nodes" not in self.down:
                return j({"items": self.nodes})
            if a[:2] == ["get", "--raw"] and "stats" not in self.down:
                name = a[2].split("/")[4]
                if name in self.stats:
                    c, m = self.stats[name]
                    return j({"node": {"cpu": {"usageNanoCores": c}, "memory": {"workingSetBytes": m}}})
                return None
            if a[:2] == ["get", "pods"] and "pods" not in self.down:
                return j({"items": [{"metadata": {"namespace": ns, "name": n}} for ns, n in self.pods]})
            if a[:2] == ["get", "namespaces"] and "namespaces" not in self.down:
                return j({"items": [{"metadata": {"name": n, "labels": {"edge-mesh/house-open": "yes"} if n == "house" and self.house_open
                                                  else {}}} for n in self.namespaces]})
            if a[:3] == ["get", "configmap", "cilium-config"] and self.cilium is not None:
                return j({"data": self.cilium})
            return None
        if a == ["host", "party", "list", "--read-only", "--json"] and "parties" not in self.down:
            return j(self.parties)
        if a == ["host", "admission-metrics", "--read-only"] and "metrics" not in self.down:
            return self.metrics
        return None


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.r = panel.Reader("/x/poc/mesh", run=self.fake.run)

    def test_every_field_from_a_real_read(self):
        f = panel.snapshot(self.r)
        self.assertEqual(f, {
            "nodes_total": 3, "nodes_ready": 2, "player_nodes": 2,
            "cpu_total_millicores": 4000 + 1500 + 4000, "memory_total_mib": 8192 + 2048 + 8192,
            "cpu_used_millicores": 3000, "memory_used_mib": 4096,
            "pods_running": 5, "player_pods_running": 2, "games_running": 2,
            "player_namespaces": 2, "house_open": True,
            "wireguard": True, "node_encryption": False, "mutual_auth": True,
            "parties_active": 1, "parties_waiting": 1, "house_players": 1,
            "admitted_total": 5, "expelled_total": 2, "refused_total": 1})
        self.assertEqual(sorted(f), sorted(panelspec.FIELDS), "the fake answers everything: every field is filled")
        for a in self.fake.calls:
            self.assertFalse({"apply", "create", "delete", "patch", "label", "annotate", "edit", "replace", "scale"} & set(a), a)
            if a[:1] == ["host"]:
                self.assertIn("--read-only", a, "mesh's reads never reconcile or tidy")

    def test_a_failed_read_leaves_its_fields_out(self):
        self.fake.stats.pop("alice-1")             # one kubelet does not answer: the sum would be a lie
        self.fake.down |= {"parties", "metrics"}
        f = panel.snapshot(self.r)
        for k in ("cpu_used_millicores", "memory_used_mib", "parties_active", "parties_waiting", "house_players", "admitted_total"):
            self.assertNotIn(k, f)
        self.assertEqual(f["nodes_total"], 3)

    def test_no_cluster_no_numbers(self):
        self.fake.down.add("nodes")
        self.assertEqual(panel.snapshot(self.r), {})

    def test_no_cilium_means_no_encryption(self):
        self.fake.cilium = None
        f = panel.snapshot(self.r)
        self.assertEqual((f["wireguard"], f["node_encryption"], f["mutual_auth"]), (False, False, False))

    def test_no_name_leaves_the_host(self):
        out = panel.shape(panel.snapshot(self.r), panel.load_config(None))
        self.assertTrue(set(out) <= set(panelspec.FIELDS))
        self.assertTrue(all(isinstance(v, (int, bool)) for v in out.values()), "numbers and yes/no: no string can carry a name")


class Config(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)

    def cfg(self, text):
        p = self.d / "panel.yaml"
        p.write_text(text)
        return panel.load_config(str(p))

    def test_defaults_publish_nothing(self):
        self.assertEqual(panel.load_config(str(self.d / "none.yaml")), {"profile": "public", "off": [], "publish": False})
        self.assertEqual(panel.load_config(str(HERE / "panel.yaml"))["publish"], False, "the repo's config publishes nothing")

    def test_off_only_narrows(self):
        c = self.cfg("profile: self-hosted\noff: [cpu_used_millicores, memory_used_mib]  # no machine load\npublish: yes\n")
        self.assertEqual(c["profile"], "self-hosted")
        self.assertNotIn("cpu_used_millicores", panel.allowed(c))
        self.assertEqual(len(panel.allowed(c)), len(panelspec.FIELDS) - 2)
        self.assertEqual(panel.shape({"cpu_used_millicores": 5, "nodes_total": 1}, c), {"nodes_total": 1})

    def test_mistakes_are_refused(self):
        for text in ("profile: everything\n", "show: [nodes]\n", "off: [player_names]\n", "off: nodes_total\n", "publish: maybe\n",
                     "profile: host\npublish: yes\n", "just words\n"):
            with self.assertRaises(panel.ConfigError, msg=text):
                self.cfg(text)
            self.assertEqual(panel.main(["check", "--config", str(self.d / "panel.yaml")]), 2)

    def test_shape_keeps_only_declared_types(self):
        c = panel.load_config(None)
        got = panel.shape({"nodes_total": "3", "nodes_ready": True, "wireguard": 1, "pods_running": -1, "games_running": 10 ** 13,
                           "house_open": True, "player_names": 3}, c)
        self.assertEqual(got, {"house_open": True})


class Spec(unittest.TestCase):
    def test_valid_is_the_allowlist(self):
        self.assertTrue(panelspec.valid({}))
        self.assertTrue(panelspec.valid(GOOD))
        for bad in (None, [], {"player_names": 1}, {"nodes_total": "3"}, {"nodes_total": True}, {"wireguard": 1}, {"nodes_total": -1},
                    {"nodes_total": 10 ** 13}, {"nodes_total": 1.5}, {"house_open": "yes"}, {"nodes_total": {"alice": 1}}):
            self.assertFalse(panelspec.valid(bad), bad)

    def test_every_field_has_a_feature_of_the_catalog(self):
        import re
        ids = set(re.findall(r"^  - id: ([a-z0-9-]+)$", (HERE.parent / "docs" / "FEATURES.yml").read_text(), re.M))
        for f, (feature, _) in panelspec.FIELDS.items():
            self.assertIn(feature, ids, f)
        learn = json.loads((HERE / "page" / "whatsnew" / "learn.json").read_text())["features"]
        for f, (feature, _) in panelspec.FIELDS.items():
            self.assertIn(feature, learn, f"{f}: the page shows {feature}'s learn block next to it")


class Page(unittest.TestCase):
    """The public page: a report may carry a panel; the page checks it against the same allowlist, shows it only while the host reports."""

    def setUp(self):
        import test_page
        self.tp = test_page
        self.t = test_page.Base("run")
        self.t.setUp()
        self.addCleanup(self.t.tearDown)

    def send(self, body_panel=None, include=True):
        s = {"slots_total": 2, "slots_used": 0, "waiting": 0, "eligible": 0, "lease_seconds": 3600, "next_ends": []}
        body = {"closed": False, "status": s, "updates": []}
        if include:
            body["panel"] = body_panel
        return self.t.http("POST", "/api/v1/puller/report", body, {"Authorization": f"Bearer {self.tp.TOKEN}"})

    def get(self):
        s, h, j = self.t.http("GET", "/api/v1/panel")
        self.assertEqual(s, 200)
        return j

    def test_nothing_before_a_report(self):
        j = self.get()
        self.assertEqual((j["live"], j["fields"]), (False, {}))
        self.assertEqual([g["title"] for g in j["groups"]], [t for t, _ in panelspec.GROUPS])

    def test_a_report_with_a_panel_is_shown_and_one_without_clears_it(self):
        self.assertEqual(self.send(GOOD)[0], 200)
        j = self.get()
        self.assertEqual((j["live"], j["fields"], j["at"]), (True, GOOD, self.t.now[0]))
        self.assertEqual(self.send(include=False)[0], 200, "an older puller, or the host stopped publishing")
        self.assertEqual(self.get()["fields"], {}, "nothing old is shown")

    def test_a_bad_panel_refuses_the_report(self):
        for bad in ({"player_names": ["alice"]}, {"nodes_total": "3"}, ["x"], None):
            self.assertEqual(self.send(bad)[0], 400, bad)
        self.assertEqual(self.get()["fields"], {})

    def test_a_silent_host_is_not_live(self):
        self.t.page.cfg["puller_stale_seconds"] = 300
        self.send(GOOD)
        self.t.now[0] += 301
        j = self.get()
        self.assertEqual((j["live"], j["fields"], j["at"]), (False, {}, None))

    def test_the_groups_carry_the_learn_blocks(self):
        g = {x["title"]: x for x in self.get()["groups"]}
        enc = g["Encryption between pods"]
        self.assertEqual({c["id"] for c in enc["learn"]}, {"encrypted-pod-network", "mutual-auth"})
        self.assertTrue(all(c.get("concept") for c in enc["learn"]))
        self.assertEqual(enc["fields"][0], {"id": "wireguard", "label": panelspec.LABELS["wireguard"], "feature": "encrypted-pod-network"})

    def test_only_get(self):
        self.assertEqual(self.t.http("POST", "/api/v1/panel", {})[0], 405)

    def test_the_live_page_draws_text_only(self):
        js = (HERE / "page" / "static" / "live.js").read_text()
        self.assertNotIn("innerHTML", js)
        self.assertIn('id=panel', self.t.http("GET", "/live")[2])


class Puller(unittest.TestCase):
    """The puller adds the panel to its report only when the host's config says publish: yes, in a profile meant for it."""

    def panel_of(self, out, code=0):
        import puller
        import subprocess
        p = puller.Puller.__new__(puller.Puller)
        p.mesh = lambda *a, **k: subprocess.CompletedProcess(a, code, out, "")
        return p.panel()

    def test_publish_rules(self):
        ok = {"profile": "public", "publish": True, "up": True, "fields": GOOD}
        self.assertEqual(self.panel_of(json.dumps(ok)), GOOD)
        self.assertEqual(self.panel_of(json.dumps({**ok, "profile": "self-hosted"})), GOOD)
        self.assertIsNone(self.panel_of(json.dumps({**ok, "publish": False})))
        self.assertIsNone(self.panel_of(json.dumps({**ok, "profile": "host"})))
        self.assertIsNone(self.panel_of(json.dumps({**ok, "fields": {"player_names": 1}})))
        self.assertIsNone(self.panel_of("not json"))
        self.assertIsNone(self.panel_of(json.dumps(ok), code=1))


if __name__ == "__main__":
    unittest.main()
