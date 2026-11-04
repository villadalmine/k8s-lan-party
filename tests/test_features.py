"""The feature catalog (docs/FEATURES.yml) and `make regression` (issue #62).

The catalog is the single list of what the project does and which suite steps prove it: it fails here when a Makefile suite is missing from
it, a feature names no suite step, or a step, path, doc or diagram does not exist. The table of a regression run is a pure function of the
catalog, the suites' sources and the run's outputs. The runner itself (poc/regression.sh) is tested in poc/test_regression.py.
"""
from __future__ import annotations

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
        self.assertEqual(len(make), 22)
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

    def test_a_two_runtime_suite_is_proved_on_both(self):
        join = next(f for f in CAT["features"] if f["id"] == "study-lab")
        one = {**join, "proven_by": [p for p in join["proven_by"] if p["runtime"] == "incus"]}
        self.assertIn("feature study-lab: poc-join is proved on incus only, not on docker", features.validate({**CAT, "features": [one]}))
        bare = {**join, "proven_by": [{"suite": "poc-join", "steps": ["J09"]}]}
        self.assertTrue(any("each proof names one of them as its runtime" in p for p in features.validate({**CAT, "features": [bare]})))

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
    (d / "poc-join@incus.status").write_text("incus\tRAN\n")
    (d / "poc-join@incus.out").write_text("".join(f"{'FAIL' if s == 'J11' else 'PASS'} {s:<6} x\n" for s in join) + "----\nRESULT FAIL (1)\n")
    (d / "poc-cilium@docker.status").write_text("docker\tSKIP(docker)\n")


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
        self.assertIn("`poc-join` (docker) J09-J12: NOT RUN(docker); `poc-join` (incus) J09-J12: FAIL", one)
        self.assertIn("| `two-level-ca` | NOT RUN |", one)

    def test_a_suite_that_dies_before_its_result_line_fails_the_run(self):
        with tempfile.TemporaryDirectory() as t:
            fake_runs(Path(t))
            (Path(t) / "poc-ca@incus.status").write_text("incus\tRAN\n")
            (Path(t) / "poc-ca@incus.out").write_text("PASS B01    x\n")
            (Path(t) / "poc-down@incus.status").write_text("incus\tRAN\n")
            self.assertEqual(features.failed_suites(CAT, Path(t)), ["poc-join@incus", "poc-ca@incus"])

    def test_docker_vm_runs_a_suite_on_both_runtimes(self):
        """REGRESSION_RUNTIME=docker-vm: poc-join on incus and on docker (in the VM); both proofs answered, no NOT RUN(docker)."""
        with tempfile.TemporaryDirectory() as t:
            fake_runs(Path(t))
            out = (Path(t) / "poc-join@incus.out").read_text().replace("FAIL J11", "PASS J11").replace("RESULT FAIL (1)", "RESULT PASS")
            for rt in ("incus", "docker"):
                (Path(t) / f"poc-join@{rt}.status").write_text(f"{rt}\tRAN\n")
                (Path(t) / f"poc-join@{rt}.out").write_text(out)
            one = features.table(CAT, Path(t), "abc", "2026-11-03")
        self.assertIn("| `poc-join` | docker | PASS | 17 PASS |\n| `poc-join` | incus | PASS | 17 PASS |", one)
        self.assertIn("| `study-lab` | PASS | `poc-join` (docker) J09-J12: PASS; `poc-join` (incus) J09-J12: PASS |", one)

    def test_each_runtime_answers_with_its_own_skip(self):
        with tempfile.TemporaryDirectory() as t:
            for rt in ("docker", "incus"):
                (Path(t) / f"poc-weakness@{rt}.status").write_text(f"{rt}\tSKIP({rt})\n")
            one = features.table(CAT, Path(t), "abc", "2026-11-03")
        self.assertIn("`poc-weakness` (docker) W00-W06: SKIP(docker); `poc-weakness` (incus) W00-W06: SKIP(incus)", one)

    def test_every_cluster_path_belongs_to_a_feature(self):
        cat = {**CAT, "cluster_paths": CAT["cluster_paths"] + ["README.md"]}
        self.assertIn("cluster_paths: README.md is in no feature's paths (the gate would ask nothing for it)", features.validate(cat))

    def test_a_proof_on_another_game_is_not_run_by_a_regression(self):
        """demo-aws is proved by poc-golden on the AWS game (game: aws): a regression passes poc-golden on poc-vm's game, not that one."""
        golden = features.script_steps("poc/poc-golden.sh")
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "poc-golden@incus.status").write_text("incus\tRAN\n")
            (Path(t) / "poc-golden@incus.out").write_text("".join(f"PASS {s:<6} x\n" for s in golden) + "RESULT PASS\n")
            one = features.table(CAT, Path(t), "abc", "2026-11-03")
        self.assertIn("| `demo-aws` | NOT RUN(aws) |", one)
        self.assertIn("| `golden-rules` | PASS |", one)
        bad = {**CAT["features"][0], "proven_by": [{"suite": "poc-join", "runtime": "incus", "game": "aws", "steps": ["J01"]},
                                                    {"suite": "poc-join", "runtime": "docker", "steps": ["J01"]}]}
        self.assertTrue(any("'game' is only for" in p for p in features.validate({**CAT, "features": [bad]})))

    def test_a_runtime_the_suite_does_not_have_fails(self):
        bad = {**CAT["features"][0], "proven_by": [{"suite": "poc-vm", "runtime": "docker", "steps": ["V01"]}]}
        self.assertTrue(any("poc-vm runs on incus" in p for p in features.validate({**CAT, "features": [bad]})))

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


if __name__ == "__main__":
    unittest.main()
