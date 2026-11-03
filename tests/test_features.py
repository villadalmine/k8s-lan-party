"""The feature catalog (docs/FEATURES.yml) and `make regression` (issue #62).

The catalog is the single list of what the project does and which suite steps prove it: it fails here when a Makefile suite is missing from
it, a feature names no suite step, or a step, path, doc or diagram does not exist. The table of a regression run is a pure function of the
catalog, the suites' sources and the run's outputs. poc/regression.sh refuses a reserved or busy underlay before it runs anything, and on a
host without Docker the Docker suites are SKIP(docker), not FAIL (Docker and Incus are stubs here; nothing is started).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import features  # noqa: E402

CAT = features.load()


class Catalog(unittest.TestCase):
    def test_the_catalog_is_valid(self):
        self.assertEqual(features.validate(CAT), [])

    def test_every_makefile_suite_is_listed_in_the_order_it_runs(self):
        make = features.makefile_suites()
        self.assertEqual(len(make), 20)
        self.assertEqual(sorted(make), sorted(s["id"] for s in CAT["suites"]))
        self.assertEqual(CAT["suites"][-1]["role"], "teardown")

    def test_a_suite_missing_from_the_catalog_fails(self):
        make = {**features.makefile_suites(), "poc-new": "poc/poc-new.sh"}
        self.assertIn("suite poc-new: in the Makefile but not in docs/FEATURES.yml", features.validate(CAT, make=make))

    def test_a_feature_without_a_suite_step_fails(self):
        cat = {**CAT, "features": CAT["features"] + [{"id": "x", "description": "d", "paths": ["Makefile"], "docs": ["README.md"],
                                                       "proven_by": [{"suite": "poc-join", "steps": []}]}]}
        self.assertIn("feature x: poc-join names no step", features.validate(cat))
        cat["features"][-1]["proven_by"] = []
        self.assertIn("feature x: 'proven_by' is empty", features.validate(cat))

    def test_a_step_the_suite_does_not_define_fails(self):
        cat = {**CAT, "features": [{**CAT["features"][0], "proven_by": [{"suite": "poc-join", "steps": ["J01-J99"]}]}]}
        self.assertTrue(any("step J99 is not defined" in p for p in features.validate(cat)))

    def test_a_suite_no_feature_proves_fails(self):
        cat = {**CAT, "features": [f for f in CAT["features"] if f["id"] != "weakness"]}
        self.assertIn("suite poc-weakness: no feature names one of its steps", features.validate(cat))

    def test_ranges_follow_the_source_order(self):
        order = features.script_steps("poc/poc-weakness.sh")
        self.assertEqual(features.expand(["W03-W03b"], order), ["W03", "W04", "W03b"])
        self.assertEqual(features.expand(["G1.1-G1.2"], features.script_steps("poc/poc-golden.sh")), ["G1.1", "G1.2"])
        with self.assertRaises(ValueError):
            features.expand(["W05-W01"], order)


def fake_runs(d: Path) -> None:
    """poc-join ran on incus and failed J11; poc-cilium skipped (no Docker); the rest not run."""
    join = features.script_steps("poc/poc-join.sh")
    (d / "poc-join.status").write_text("incus\tRAN\n")
    (d / "poc-join.out").write_text("".join(f"{'FAIL' if s == 'J11' else 'PASS'} {s:<6} x\n" for s in join) + "----\nRESULT FAIL (1)\n")
    (d / "poc-cilium.status").write_text("docker\tSKIP(docker)\n")


class Table(unittest.TestCase):
    def test_same_input_same_table(self):
        with tempfile.TemporaryDirectory() as t:
            fake_runs(Path(t))
            one = features.table(CAT, Path(t), "abc", "2026-11-03", "env")
            self.assertEqual(one, features.table(CAT, Path(t), "abc", "2026-11-03", "env"))
        rows = {line.split("|")[1].strip(): line for line in one.splitlines() if line.startswith("| `")}
        self.assertIn("| incus | FAIL (1) | 16 PASS, 1 FAIL (J11) |", rows["`poc-join`"])
        self.assertIn("| docker | SKIP(docker) |", rows["`poc-cilium`"])
        self.assertIn("| `study-lab` | FAIL |", one)           # J11 failed
        self.assertIn("| `nebula-firewall` | PARTIAL |", one)   # J13 passed, poc-cilium skipped, poc-vm not run
        self.assertIn("| `weakness` | NOT RUN |", one)

    def test_a_step_the_run_never_printed_is_a_failure(self):
        self.assertEqual(features.proof_result("TIMEOUT", {"J01": "PASS"}, ["J01", "J02"]), "FAIL")
        self.assertEqual(features.proof_result("RAN", {"G3.4": "SKIP"}, ["G3.4"]), "SKIP")

    def test_the_table_goes_between_the_markers(self):
        with tempfile.TemporaryDirectory() as t:
            doc = Path(t) / "s.md"
            doc.write_text(f"a\n{features.BEGIN} x -->\nold\n{features.END}\nb\n")
            features.write_between(doc, "new\n")
            features.write_between(doc, "new\n")
            self.assertEqual(doc.read_text(), f"a\n{features.BEGIN} x -->\nnew\n{features.END}\nb\n")


STUB = """#!/bin/sh
case "$1" in info) exit {code} ;; list) exit 0 ;; network) exit 1 ;; project) exit 0 ;; esac
exit 0
"""


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash")
class Runner(unittest.TestCase):
    def run_regression(self, env: dict, ip_addr: str = "") -> tuple[subprocess.CompletedProcess, Path]:
        t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, t)
        (t / "bin").mkdir()
        for name, text in {"docker": "#!/bin/sh\nexit 1\n", "incus": STUB.replace("{code}", "0"),
                           "ip": f"#!/bin/sh\nprintf '%s\\n' '{ip_addr}'\n"}.items():
            (t / "bin" / name).write_text(text)
            (t / "bin" / name).chmod(0o755)
        full = {**os.environ, "PATH": f"{t / 'bin'}:{os.environ['PATH']}", "REGRESSION_DIR": str(t / "reg"),
                "REGRESSION_NO_WRITE": "1", "INCUS_PROJECT": "emesh-test", **env}
        r = subprocess.run(["bash", str(ROOT / "poc/regression.sh")], env=full, capture_output=True, text=True, timeout=120)
        return r, t / "reg"

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
        self.assertEqual((reg / "runs/poc-cilium.status").read_text(), "docker\tSKIP(docker)\n")
        self.assertEqual((reg / "runs/poc-kind.status").read_text(), "kind\tSKIP(kind)\n")
        self.assertIn("| `kind` | SKIP(kind) |", r.stdout)


if __name__ == "__main__":
    unittest.main()
