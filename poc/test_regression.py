"""poc/regression.sh (make regression, issue #62) with Docker, Incus and `ip` as stubs: nothing is started, every call is logged.

It refuses the live game's underlay (10.79.0) and an underlay in use before it runs anything; on a host without Docker the Docker and kind
suites are SKIP, not FAIL; a Docker game on the host (network emesh) aborts the run; `down` removes only what this run's marker
(project.created) names, and never the project or bridge of the live game (emesh-demo, emeshdemo), even with a marker; an unknown suite in
ONLY is refused.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# every call is logged; `incus list` prints $INCUS_LIST (a foreign instance), `docker info` and `docker network inspect` exit as told
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >>"$STUB_LOG"
case "$(basename "$0") $1" in "incus list") printf '%s' "${INCUS_LIST:-}" ;; "incus network") [ "$2" != show ] || exit 1 ;;
  "incus query") echo '{"space": {"used": 1, "total": 10}}' ;; "docker info") exit "${DOCKER_INFO:-1}" ;;
  "docker network") exit "${DOCKER_NET:-1}" ;; docker*) exit 1 ;; esac
exit 0
"""


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash")
class Runner(unittest.TestCase):
    def run_regression(self, env: dict, ip_addr: str = "", *args: str, mark: str = "") -> tuple[subprocess.CompletedProcess, Path]:
        t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, t)
        (t / "bin").mkdir()
        (t / "reg").mkdir()
        if mark:
            (t / "reg" / "project.created").write_text(mark)
        for name, text in {"docker": STUB, "incus": STUB, "ip": f"#!/bin/sh\nprintf '%s\\n' '{ip_addr}'\n"}.items():
            (t / "bin" / name).write_text(text)
            (t / "bin" / name).chmod(0o755)
        self.log = t / "calls"
        full = {**os.environ, "PATH": f"{t / 'bin'}:{os.environ['PATH']}", "REGRESSION_DIR": str(t / "reg"), "STUB_LOG": str(self.log),
                "REGRESSION_NO_WRITE": "1", "INCUS_PROJECT": "emesh-test", **env}
        r = subprocess.run(["bash", str(ROOT / "poc/regression.sh"), *args], env=full, capture_output=True, text=True, timeout=120)
        return r, t / "reg"

    def removed(self) -> list[str]:
        calls = self.log.read_text().splitlines() if self.log.is_file() else []
        return [c for c in calls if " delete" in c or c.startswith("docker rm") or "kind delete" in c]

    def test_down_without_this_runs_marker_removes_nothing(self):
        r, _ = self.run_regression({"INCUS_LIST": "emesh-cp\n"}, "", "down")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("nothing removed", r.stdout)
        self.assertEqual(self.removed(), [])

    def test_the_live_games_project_is_never_touched_even_with_a_marker(self):
        mark = "project=emesh-demo\ncreated=yes\nbridges=emregbr emregpub emrega emregb emregdvm\n"
        r, _ = self.run_regression({"INCUS_PROJECT": "emesh-demo", "INCUS_LIST": "emesh-cp\n"}, "", "down", mark=mark)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("INCUS_PROJECT=emesh-demo: refused", r.stdout)
        self.assertEqual(self.removed(), [])
        r, _ = self.run_regression({"VMNET": "emeshdemo"}, "", "down")
        self.assertIn("bridge emeshdemo: refused", r.stdout)

    def test_down_removes_what_its_marker_names(self):
        mark = "project=emesh-test\ncreated=yes\nbridges=emregbr emregpub emrega emregb emregdvm\n"
        r, reg = self.run_regression({"INCUS_LIST": "emesh-cp\n"}, "", "down", mark=mark)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("incus delete -f emesh-cp", self.removed())
        self.assertIn("incus project delete emesh-test", self.removed())
        self.assertFalse((reg / "project.created").exists())

    def test_a_docker_game_on_the_host_aborts_the_run(self):
        r, reg = self.run_regression({"DOCKER_INFO": "0", "DOCKER_NET": "0", "ONLY": "poc-cilium"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("docker network emesh exists", r.stdout)
        self.assertEqual(self.removed(), [])
        self.assertEqual(list((reg / "runs").iterdir()), [])

    def test_docker_vm_builds_the_vm_runs_the_docker_suite_in_it_and_removes_it(self):
        r, reg = self.run_regression({"REGRESSION_RUNTIME": "docker-vm", "ONLY": "poc-cilium poc-kind"})
        calls = self.log.read_text()
        self.assertIn("incus init images:ubuntu/24.04/cloud regdocker --vm", calls)
        self.assertIn("incus exec regdocker --cwd /root/repo ./poc/vm/docker-vm.sh", calls)
        self.assertIn("incus exec regdocker --cwd /root/repo --env RUNTIME=docker --env EMESH_STATE=/root/state/poc-cilium", calls)
        self.assertIn("./poc/kind-cilium.sh", calls)
        self.assertEqual((reg / "runs/poc-cilium@docker.status").read_text(), "docker\tRAN\n")
        self.assertEqual((reg / "runs/poc-kind@kind.status").read_text(), "kind\tRAN\n")
        self.assertIn("incus delete -f regdocker", self.removed())
        self.assertIn("incus network delete emregdvm", self.removed())
        self.assertFalse((reg / "project.created").exists())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)   # the stub prints no RESULT line: that fails the run
        self.assertIn("no RESULT PASS: poc-cilium@docker, poc-kind@kind", r.stderr)

    def test_an_unknown_suite_in_only_is_refused(self):
        r, _ = self.run_regression({"ONLY": "poc-jion"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_the_maintainers_live_underlay_is_refused(self):
        r, _ = self.run_regression({"UNDERLAY_NET": "10.79.0", "ONLY": "poc-cilium"})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("10.79.0.0/24 is reserved", r.stdout)

    def test_a_busy_underlay_aborts_before_anything_runs(self):
        r, reg = self.run_regression({"ONLY": "poc-join"}, ip_addr="2: br0    inet 10.83.0.1/24 brd 10.83.0.255 scope global br0")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("10.83.0.0/24 is in use on this host", r.stdout)
        self.assertEqual(list((reg / "runs").iterdir()), [])

    def test_docker_suites_on_a_host_without_docker_are_skipped_not_failed(self):
        r, reg = self.run_regression({"ONLY": "poc-cilium poc-kind"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((reg / "runs/poc-cilium@docker.status").read_text(), "docker\tSKIP(docker)\n")
        self.assertEqual((reg / "runs/poc-kind@kind.status").read_text(), "kind\tSKIP(kind)\n")
        self.assertIn("| `kind` | SKIP(kind) |", r.stdout)


if __name__ == "__main__":
    unittest.main()
