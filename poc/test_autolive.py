"""The live page on its own (#119, poc/page/autolive.py): on the air whenever the cluster is up, off the air when it is down or the live
switch is off; it starts only what is missing of a house the operator made, never makes or opens one, and restarts a dead streamer with a
growing pause, except one the page refused. No cluster: the API server, kubectl and the streamer are fakes."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "page"))
import autolive  # noqa: E402

ALL = list(autolive.HOUSE_PODS)


class Child:
    _n = 1000

    def __init__(self):
        Child._n += 1
        self.pid, self.code, self.terminated = Child._n, None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class Cluster:
    """kubectl and mesh as the fake sees them: is the API ready, does the house exist, which pods it has, what ran."""

    def __init__(self):
        self.up, self.house, self.pods, self.calls, self.silent = True, True, list(ALL), [], False

    def run(self, argv, timeout=60):
        self.calls.append(argv[1:])
        out, code = "", 0
        if argv[1:] == ["get", "--raw", "/readyz"]:
            out, code = ("ok", 0) if self.up else ("", 1)
        elif self.silent and argv[1:2] == ["get"]:
            raise subprocess.TimeoutExpired(argv, timeout)
        elif argv[1:3] == ["get", "namespace"]:
            out = "namespace/house" if self.house else ""
        elif argv[1:3] == ["get", "pods"]:
            out = json.dumps({"items": [{"metadata": {"name": p}} for p in self.pods]}) if self.house else json.dumps({"items": []})
        elif argv[1:] == ["host", "house", "start"]:
            self.pods = list(ALL)
            out = "house: the match runs"
        return subprocess.CompletedProcess(argv, code, out, "")


class AutoTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "page").mkdir()
        (self.dir / "page" / "stream.token").write_text("x" * 40)
        self.cluster, self.children, self.said, self.now = Cluster(), [], [], [0.0]
        a = Namespace(url="https://page.example.org", cacert=None, mesh=str(self.dir / "mesh"), state=str(self.dir), check=30.0)

        def spawn():
            c = Child()
            self.children.append(c)
            return c
        self.auto = autolive.Auto(a, run=self.cluster.run, spawn=spawn, sleep=lambda s: None, clock=lambda: self.now[0],
                                  say=self.said.append)

    def mesh_calls(self):
        return [c for c in self.cluster.calls if c[:1] == ["host"]]

    def test_up_with_its_house_streams(self):
        self.auto.step()
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.mesh_calls(), [], "a house that runs is left alone")
        self.assertEqual((self.dir / "house" / "streamer.pid").read_text().strip(), str(self.children[0].pid),
                         "the pid is where mesh host live status and live off look")
        self.auto.step()
        self.assertEqual(len(self.children), 1, "one streamer, not one per pass")

    def test_a_missing_pod_is_started_again(self):
        self.cluster.pods = ["doom-server"]
        self.auto.step()
        self.assertEqual(self.mesh_calls(), [["host", "house", "start"]])
        self.auto.step()
        self.assertEqual(self.mesh_calls(), [["host", "house", "start"]], "only while something is missing")

    def test_it_never_makes_or_opens_a_house(self):
        self.cluster.house = False
        for _ in range(3):
            self.auto.step()
        self.assertEqual(self.mesh_calls(), [], "no house init, no house open: broadcasting stays the operator's choice")
        self.assertTrue(any("no house" in m for m in self.said))

    def test_a_cluster_down_touches_nothing_and_says_it_once(self):
        self.cluster.up = False
        for _ in range(3):
            self.auto.step()
        self.assertEqual(self.mesh_calls(), [])
        self.assertEqual(sum("does not answer" in m for m in self.said), 1)
        self.cluster.up = True
        self.auto.step()
        self.assertTrue(self.said[-1].endswith("the house match runs") or "runs" in " ".join(self.said[-2:]))

    def test_a_dead_streamer_comes_back_with_a_growing_pause(self):
        self.auto.step()
        self.children[0].code = 3
        self.auto.step()                  # it notices, waits 5 s
        self.assertEqual(len(self.children), 1)
        self.now[0] = 6
        self.auto.step()
        self.assertEqual(len(self.children), 2)
        self.children[1].code = 3
        self.auto.step()
        self.now[0] = 12                  # 10 s now: not yet
        self.auto.step()
        self.assertEqual(len(self.children), 2)
        self.now[0] = 17
        self.auto.step()
        self.assertEqual(len(self.children), 3)
        self.now[0] = 100                 # this one lives past a minute: the pause is short again
        self.auto.step()
        self.children[2].code = 3
        self.auto.step()
        self.now[0] = 106
        self.auto.step()
        self.assertEqual(len(self.children), 4)

    def test_a_refused_token_stops_for_good(self):
        self.auto.step()
        self.children[0].code = autolive.REFUSED
        self.assertEqual(self.auto.loop(), autolive.REFUSED)
        self.assertEqual(len(self.children), 1)
        self.assertTrue(any("refused the stream token" in m for m in self.said))

    def test_the_switch_off_stops_and_keeps_it_off_and_leaves_the_house(self):
        self.auto.step()
        (self.dir / "live.off").write_text("")
        self.auto.step()
        self.assertTrue(self.children[0].terminated, "off stops the streamer (it tells the page offline)")
        self.assertFalse((self.dir / "house" / "streamer.pid").exists())
        for _ in range(3):
            self.now[0] += 400
            self.auto.step()
        self.assertEqual(len(self.children), 1, "kept off")
        self.assertNotIn(["host", "house", "stop"], self.mesh_calls(), "the match and the demo keep running")
        (self.dir / "live.off").unlink()
        self.auto.step()
        self.assertEqual(len(self.children), 2, "on: it streams again at the next pass")

    def test_a_crash_is_not_a_refusal(self):
        self.auto.step()
        self.children[0].code = 1          # what Python exits with on any uncaught error
        self.auto.step()
        self.now[0] = 6
        self.auto.step()
        self.assertEqual(len(self.children), 2, "a crashed streamer is started again; only a refused token (4) stops it")

    def test_a_silent_cluster_starts_nothing(self):
        self.cluster.silent = True         # the API server answers /readyz but times out on the house's reads
        for _ in range(3):
            self.auto.step()
        self.assertEqual(self.mesh_calls(), [], "not knowing the pods is not every pod missing")
        self.assertEqual(sum("not about the house" in m for m in self.said), 1)

    def test_stop_ends_the_child(self):
        self.auto.step()
        self.auto.stop()
        self.assertTrue(self.children[0].terminated)


class MainTest(unittest.TestCase):
    def test_arguments(self):
        self.assertEqual(autolive.main(["run", "--url", "http://page.example.org"]), 2, "the token never crosses a network in clear")
        self.assertEqual(autolive.main(["run", "--url", "https://page.example.org", "--check", "1"]), 2)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.assertEqual(autolive.main(["run", "--url", "https://page.example.org", "--state", d]), 2, "no stream token")


if __name__ == "__main__":
    unittest.main()
