"""Tests of the PR gate (scripts/check_pr.py): a guard is tested against real cases before it is required."""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_pr  # noqa: E402

GOOD = """## What and why
Adds the design.

## Kind
- Kind: docs

## Scope
- Scope: security

## Provenance
- Author: ai
- Backend: claude
- Model: claude-sonnet-5-5
- Date: 2026-10-16

## Evidence
### Where it ran
- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123
### Known gaps
none

## Authorship
| Role | Who | What | Link |
| :--- | :--- | :--- | :--- |
| Author | AI: agent · claude-sonnet-5-5 | commits a..b | |

### Findings ledger
| # | Found by | Head | Severity | Finding | Fixed by | Fix | Verified by | Status |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | revisor-pr | abc | low | x | agent | def | revisor-pr | fixed |

## How to test it from a clean clone
make pr-check

## Risk and rollback
Revert.

## Depends on
nothing
"""


class CheckPr(unittest.TestCase):
    def test_good_body_passes(self):
        self.assertEqual(check_pr.check("docs(design): add it", GOOD), [])

    def test_title_must_be_conventional(self):
        self.assertTrue(check_pr.check("Add the design", GOOD))

    def test_missing_section(self):
        body = GOOD.replace("## Depends on\nnothing\n", "")
        self.assertIn("missing section: ## Depends on", check_pr.check("docs: x", body))

    def test_scope_needs_exactly_one_value(self):
        body = GOOD.replace("- Scope: security", "- Scope: small | medium")
        self.assertTrue(any(p.startswith("Scope:") for p in check_pr.check("docs: x", body)))

    def test_ai_needs_model(self):
        body = GOOD.replace("- Model: claude-sonnet-5-5", "- Model: none")
        self.assertTrue(any("name the tool and model" in p for p in check_pr.check("docs: x", body)))

    def test_ledger_row_columns(self):
        body = GOOD.replace("| 1 | revisor-pr | abc | low | x | agent | def | revisor-pr | fixed |", "| 1 | a | b |")
        self.assertTrue(any("columns" in p for p in check_pr.check("docs: x", body)))

    def test_where_it_ran_required(self):
        body = GOOD.replace("github.com/villadalmine/k8s-lan-party/actions/runs/123", "none")
        self.assertTrue(any("Where it ran" in p for p in check_pr.check("docs: x", body)))

    def test_dco(self):
        ok = {"sha": "a" * 40, "email": "a@b.c", "message": "x\n\nSigned-off-by: A <a@b.c>"}
        bad = {"sha": "b" * 40, "email": "a@b.c", "message": "x"}
        self.assertEqual(check_pr.dco([ok]), [])
        self.assertEqual(len(check_pr.dco([ok, bad])), 1)

    def test_dco_noreply_of_the_pr_author(self):
        c = {"sha": "c" * 40, "email": "other@x.y",
             "message": "x\n\nSigned-off-by: A <1+me@users.noreply.github.com>"}
        self.assertEqual(check_pr.dco([c], "me"), [])
        self.assertEqual(len(check_pr.dco([c], "someone-else")), 1)

    def test_size_waiver(self):
        self.assertTrue(check_pr.size(401))
        self.assertEqual(check_pr.size(401, {"size-exception"}), [])

    def test_judge_run(self):
        run = {"head_sha": "abc", "path": ".github/workflows/verify.yml", "event": "pull_request"}
        jobs = {"jobs": [{"name": "verify", "conclusion": "success"}]}
        self.assertIsNone(check_pr.judge_run(run, jobs, "abc"))
        self.assertIn("not the PR head", check_pr.judge_run(run, jobs, "def"))
        self.assertIn("comes from", check_pr.judge_run({**run, "path": "x.yml"}, jobs, "abc"))
        self.assertIn("verify=failure", check_pr.judge_run(
            run, {"jobs": [{"name": "verify", "conclusion": "failure"}]}, "abc"))

    # --- issue #60 and the review of #61: no red check on push-then-edit, verify never skipped -----

    def test_citation_advisory_only_on_a_push(self):
        for action in ("opened", "synchronize", "reopened"):
            self.assertFalse(check_pr.citation_required(action), action)
        for action in ("edited", "labeled", "unlabeled", "ready_for_review", "", "bogus", None):  # fail closed
            self.assertTrue(check_pr.citation_required(action), action)

    def test_verify_is_never_skipped(self):
        """A skipped `verify` counts as passing and, as the newest of its name, hides a red one. So the only
        job named verify, in verify.yml, has no `if:` and no `needs:`, and runs only on the advisory actions."""
        import yaml
        root = Path(check_pr.__file__).resolve().parent.parent / ".github" / "workflows"
        wfs = {f.name: yaml.safe_load(f.read_text(encoding="utf-8")) for f in root.glob("*.yml")}
        named = [(n, job) for n, wf in wfs.items() for i, job in (wf.get("jobs") or {}).items()
                 if "verify" in (i, job.get("name"))]
        self.assertEqual([n for n, _ in named], ["verify.yml"], "a job named verify outside verify.yml")
        self.assertFalse({"if", "needs"} & set(named[0][1]), "verify can be skipped (if:/needs:)")
        on = {n: (wfs[n].get("on") or wfs[n].get(True))["pull_request"]["types"]  # YAML 1.1: `on` is True
              for n in ("verify.yml", "pr-format.yml")}
        self.assertEqual(sorted(on["verify.yml"]), sorted(check_pr.ADVISORY_ACTIONS))
        self.assertTrue({"edited", "labeled", "unlabeled"} <= set(on["pr-format.yml"]))
        self.assertNotIn("pr-format", wfs["verify.yml"]["jobs"])
        # the wait covers verify's timeout plus queue time, and fits in pr-format's own timeout
        self.assertGreater(check_pr.WAIT_SECONDS, wfs["verify.yml"]["jobs"]["verify"]["timeout-minutes"] * 60 + 300)
        self.assertGreater(wfs["pr-format.yml"]["jobs"]["pr-format"]["timeout-minutes"] * 60, check_pr.WAIT_SECONDS)

    def test_link_needed_only_when_strict(self):
        body = GOOD.replace("- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123", "- CI: pending")
        self.assertEqual(check_pr.check("docs: x", body, strict=False), [])
        self.assertTrue(any("Where it ran" in p for p in check_pr.check("docs: x", body, strict=True)))
        no_heading = GOOD.replace("### Where it ran\n", "")
        self.assertTrue(any("Where it ran" in p for p in check_pr.check("docs: x", no_heading, strict=False)))

    @staticmethod
    def _fetch(*job_states, head="abc"):
        """A fake `gh api`; each jobs call returns the next verify (status, conclusion), the last repeats."""
        states = list(job_states)

        def fetch(path):
            status, conclusion = states[0]
            if not path.endswith("/jobs"):
                return {"head_sha": head, "path": check_pr.VERIFY_WORKFLOW, "event": "pull_request", "status": status}
            if len(states) > 1:
                states.pop(0)
            return {"jobs": [{"name": "verify", "status": status, "conclusion": conclusion}]}
        return fetch

    def test_wait_for_the_verify_run_of_the_head(self):
        cited, repo, slept = [("villadalmine/k8s-lan-party", "1")], "villadalmine/k8s-lan-party", []
        run = ("in_progress", None)
        fetch = self._fetch(run, run, ("completed", "success"))
        self.assertTrue(check_pr.verify_runs(cited, repo, "abc", fetch=fetch, sleep=slept.append, clock=lambda: 0))
        self.assertEqual(len(slept), 2)
        times = iter([0, 0, 9999])  # deadline
        self.assertFalse(check_pr.verify_runs(cited, repo, "abc", fetch=self._fetch(run), sleep=lambda s: None,
                                              clock=lambda: next(times)))
        slept.clear()  # another head: no wait
        self.assertFalse(check_pr.verify_runs(cited, repo, "def", fetch=self._fetch(run), sleep=slept.append))
        self.assertEqual(slept, [])
        self.assertFalse(check_pr.verify_runs(cited, repo, "abc", fetch=self._fetch(("completed", "failure"))))

    def _main(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["check_pr.py", *argv]), contextlib.redirect_stdout(out):
            code = check_pr.main()
        return code, out.getvalue()

    def test_main_modes(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runs, event, live, nobody = (Path(tmp) / n for n in ("runs.txt", "event.json", "pr.json", "nb.json"))
            runs.write_text("villadalmine/k8s-lan-party 1\n", encoding="utf-8")
            with unittest.mock.patch.object(check_pr, "gh_api", self._fetch(("completed", "success"), head="old")):
                common = ("--verify-runs", str(runs), "--repo", "villadalmine/k8s-lan-party", "--head", "new")
                self.assertEqual(self._main(*common, "--action", "synchronize")[0], 0)  # a stale link warns
                for action in ("edited", "labeled", ""):
                    self.assertEqual(self._main(*common, "--action", action)[0], 1, action)
            stale = GOOD.replace("- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123", "- CI: ")
            event.write_text(json.dumps({"action": "edited", "pull_request": {
                "title": "docs: x", "body": stale, "user": {"login": "me"}, "labels": []}}))
            live.write_text(json.dumps({"title": "docs: x", "body": GOOD}))
            nobody.write_text(json.dumps({"title": "docs: x"}))
            self.assertEqual(self._main("--event", str(event))[0], 1)  # edited, no link
            self.assertEqual(self._main("--event", str(event), "--pr-file", str(live))[0], 0)
            code, out = self._main("--event", str(event), "--pr-file", str(nobody))  # falls back to the payload
            self.assertEqual(code, 1)
            self.assertNotIn("missing section", out)
            self.assertEqual(self._main("--event", str(event), "--action", "synchronize")[0], 0)
        src = (Path(check_pr.__file__).parent / "pr_check.py").read_text(encoding="utf-8")
        self.assertIn('"--action", "synchronize"', src)  # make pr-check runs before the push


CAT = check_pr.catalog()
GATED = GOOD.replace("### Known gaps\nnone\n", "poc-golden: RESULT PASS\n### Known gaps\nnone\n### Docs impact\n- docs/GOLDEN-RULES.md, POC-STATUS.md\n")


class FeatureGate(unittest.TestCase):
    """Issue #62: the changed paths -> features (docs/FEATURES.yml) -> the suites' RESULT PASS, the regression table, Docs impact."""

    def gate(self, body: str, *files: str) -> list[str]:
        return check_pr.feature_gate(body, list(files), CAT)

    def test_paths_that_touch_no_feature_ask_nothing(self):
        self.assertEqual(self.gate(GOOD, "README.md", "scripts/check_pr.py"), [])

    def test_the_suites_of_the_touched_features_must_pass(self):
        self.assertEqual(self.gate(GATED, "docs/GOLDEN-RULES.md"), [])
        failed = " ".join(self.gate(GATED.replace("RESULT PASS", "RESULT FAIL (1)"), "docs/GOLDEN-RULES.md"))
        self.assertIn("FAIL for what this PR touches: poc-golden", failed)
        self.assertIn("cite the RESULT PASS of poc-golden", failed)
        self.assertIn("poc-placement", self.gate(GATED, "poc/vm/poc-placement.sh")[0])   # placement: poc-placement and poc-golden

    LIB = ("join", "host-your-own", "join-token-burn", "node-guard", "two-level-ca")   # the features poc/lib.sh touches

    def table(self, result: dict, commit: str = "abcdef012345") -> str:
        rows = "".join(f"| `{f}` | {result.get(f, 'PASS')} | `poc-join` J01: x |\n" for f in self.LIB)
        return GOOD.replace("### Known gaps", f"Commit `{commit}`, 2026-11-03, left behind: nothing.\n\n| Feature | Result | Proven by |\n"
                            f"| :--- | :--- | :--- |\n{rows}### Docs impact\nnone: a comment in lib.sh, no behaviour changes\n### Known gaps")

    def test_a_cluster_change_needs_the_regression_table_or_every_feature(self):
        none = GOOD.replace("### Known gaps", "### Docs impact\nnone: a comment in lib.sh, no behaviour changes\n### Known gaps")
        self.assertIn("paste the table of `make regression`", self.gate(none, "poc/lib.sh")[0])
        self.assertEqual(check_pr.feature_gate(self.table({}), ["poc/lib.sh"], CAT, ["abcdef0123456789"]), [])
        listed = none.replace("### Docs impact", "".join(f"- `{f}`: PASS\n" for f in self.LIB) + "### Docs impact")
        self.assertEqual(self.gate(listed, "poc/lib.sh"), [])

    def test_every_touched_feature_must_be_pass_in_the_table(self):
        for verdict in ("PARTIAL", "NOT RUN", "SKIP(docker)", "FAIL"):
            self.assertIn(f"join ({verdict})", " ".join(self.gate(self.table({"join": verdict}), "poc/lib.sh")), verdict)
        subset = self.table({}).replace("| `node-guard` | PASS | `poc-join` J01: x |\n", "")   # a make regression ONLY=... table
        self.assertIn("node-guard (missing)", self.gate(subset, "poc/lib.sh")[0])

    def test_the_table_must_come_from_a_commit_of_the_pr(self):
        self.assertIn("a commit of this PR", check_pr.feature_gate(self.table({}), ["poc/lib.sh"], CAT, ["1234567890ab"])[0])

    def test_a_fail_for_a_touched_suite_is_never_hidden_by_a_pass(self):
        pair = GATED.replace("poc-golden: RESULT PASS", "| Suite | Runtime | Result | Steps |\n| :--- | :--- | :--- | :--- |\n"
                             "| `poc-golden` | docker | FAIL (1) | x |\n| `poc-golden` | incus | PASS | x |")
        self.assertIn("FAIL for what this PR touches: poc-golden", self.gate(pair, "docs/GOLDEN-RULES.md")[0])
        both = GATED.replace("poc-golden: RESULT PASS", "poc-golden: RESULT PASS\npoc-golden (rerun): RESULT FAIL (2)")
        self.assertIn("poc-golden", self.gate(both, "docs/GOLDEN-RULES.md")[0])
        proven_by = GATED.replace("poc-golden: RESULT PASS", "| `golden-rules` | PARTIAL | `poc-golden` G1.1: PASS |")
        self.assertIn("cite the RESULT PASS of poc-golden", self.gate(proven_by, "docs/GOLDEN-RULES.md")[0])

    def test_known_gaps_is_not_evidence(self):
        gap = GATED.replace("### Known gaps\nnone\n", "### Known gaps\npoc-golden: RESULT FAIL (1) once, before the fix of J11\n")
        self.assertEqual(self.gate(gap, "docs/GOLDEN-RULES.md"), [])
        only_gap = gap.replace("poc-golden: RESULT PASS\n", "").replace("### Known gaps\n", "### Known gaps\npoc-golden: RESULT PASS\n")
        self.assertIn("cite the RESULT PASS of poc-golden", self.gate(only_gap, "docs/GOLDEN-RULES.md")[0])

    def test_docs_impact_names_the_listed_docs_or_says_none_with_a_reason(self):
        self.assertIn("add a '### Docs impact'", self.gate(GOOD.replace("### Known gaps", "poc-golden: RESULT PASS\n### Known gaps"),
                                                           "docs/GOLDEN-RULES.md")[0])
        self.assertIn("docs/POC-STATUS.md", self.gate(GATED.replace(", POC-STATUS.md", ""), "docs/GOLDEN-RULES.md")[0])
        self.assertTrue(self.gate(GATED.replace("- docs/GOLDEN-RULES.md, POC-STATUS.md", "none"), "docs/GOLDEN-RULES.md"))
        self.assertEqual(self.gate(GATED.replace("- docs/GOLDEN-RULES.md, POC-STATUS.md", "none: the check ids did not change"),
                                   "docs/GOLDEN-RULES.md"), [])

    def test_only_a_warning_on_a_push_strict_on_the_edit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "b.md"
            body.write_text(GOOD, encoding="utf-8")
            args = ("--title", "docs: x", "--body-file", str(body), "--files", "docs/GOLDEN-RULES.md")
            code, out = CheckPr._main(self, *args, "--action", "synchronize")
            self.assertEqual(code, 0, out)
            self.assertIn("::warning::PR: Evidence", out)
            self.assertEqual(CheckPr._main(self, *args, "--action", "edited")[0], 1)

if __name__ == "__main__":
    unittest.main()
