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

    # --- issue #60: no red check on a normal push-then-edit flow, the guarantee kept ----------

    def test_citation_required_only_on_edit(self):
        self.assertTrue(check_pr.citation_required("edited"))
        self.assertTrue(check_pr.citation_required(None))  # by hand (make pr-check): the final state
        for action in ("opened", "synchronize", "reopened", "labeled", "unlabeled"):
            self.assertFalse(check_pr.citation_required(action), action)

    def test_push_run_does_not_need_the_link(self):
        body = GOOD.replace("- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123",
                            "- CI: pending, pasted after verify passes")
        self.assertEqual(check_pr.check("docs: x", body, strict=False), [])
        self.assertTrue(any("Where it ran" in p for p in check_pr.check("docs: x", body, strict=True)))

    def test_where_it_ran_subsection_always_required(self):
        body = GOOD.replace("### Where it ran\n", "")
        self.assertTrue(any("Where it ran" in p for p in check_pr.check("docs: x", body, strict=False)))

    RUN = {"head_sha": "abc", "path": ".github/workflows/verify.yml", "event": "pull_request",
           "status": "completed"}

    def _fetch(self, states):
        """A fake `gh api`: each call to the jobs endpoint returns the next (status, conclusion)."""
        states = list(states)

        def fetch(path):
            status, conclusion = states[0] if path.endswith("/jobs") else (None, None)
            if path.endswith("/jobs"):
                if len(states) > 1:
                    states.pop(0)
                return {"jobs": [{"name": "verify", "status": status, "conclusion": conclusion}]}
            run_status = states[0][0]
            return {**self.RUN, "status": "completed" if run_status == "completed" else "in_progress"}
        return fetch

    def test_edit_waits_for_the_verify_run_of_the_head(self):
        slept = []
        fetch = self._fetch([("in_progress", None), ("in_progress", None), ("completed", "success")])
        ok = check_pr.verify_runs([("villadalmine/k8s-lan-party", "1")], "villadalmine/k8s-lan-party", "abc",
                                  fetch=fetch, wait=600, sleep=slept.append, clock=lambda: 0)
        self.assertTrue(ok)
        self.assertEqual(len(slept), 2)

    def test_wait_has_a_deadline(self):
        times = iter([0, 0, 1000, 1000])
        fetch = self._fetch([("in_progress", None)])
        ok = check_pr.verify_runs([("villadalmine/k8s-lan-party", "1")], "villadalmine/k8s-lan-party", "abc",
                                  fetch=fetch, wait=600, sleep=lambda s: None, clock=lambda: next(times))
        self.assertFalse(ok)

    def test_no_wait_for_a_run_of_another_head(self):
        slept = []
        fetch = self._fetch([("in_progress", None)])
        ok = check_pr.verify_runs([("villadalmine/k8s-lan-party", "1")], "villadalmine/k8s-lan-party", "def",
                                  fetch=fetch, wait=600, sleep=slept.append, clock=lambda: 0)
        self.assertFalse(ok)
        self.assertEqual(slept, [])

    def test_failed_verify_never_proves_the_head(self):
        fetch = self._fetch([("completed", "failure")])
        self.assertFalse(check_pr.verify_runs([("villadalmine/k8s-lan-party", "1")], "villadalmine/k8s-lan-party",
                                              "abc", fetch=fetch, wait=0))

    def _main(self, *argv):
        import contextlib, io
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["check_pr.py", *argv]), contextlib.redirect_stdout(out):
            code = check_pr.main()
        return code, out.getvalue()

    def test_verify_runs_mode_advisory_on_push_strict_on_edit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "cited-runs.txt"
            runs.write_text("villadalmine/k8s-lan-party 1\n", encoding="utf-8")
            stale = {"head_sha": "old", "path": ".github/workflows/verify.yml", "event": "pull_request",
                     "status": "completed"}
            jobs = {"jobs": [{"name": "verify", "status": "completed", "conclusion": "success"}]}
            with unittest.mock.patch.object(check_pr, "gh_api",
                                            lambda path: jobs if path.endswith("/jobs") else stale):
                common = ("--verify-runs", str(runs), "--repo", "villadalmine/k8s-lan-party", "--head", "new")
                code, out = self._main(*common, "--action", "synchronize")
                self.assertEqual(code, 0)
                self.assertIn("::warning::", out)
                code, out = self._main(*common, "--action", "edited")
                self.assertEqual(code, 1)
                self.assertIn("::error::", out)

    def test_event_mode_reads_the_live_body_and_the_action(self):
        import json, tempfile
        stale_body = GOOD.replace("- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123", "- CI: ")
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            live = Path(tmp) / "pr.json"
            event.write_text(json.dumps({"action": "edited", "pull_request": {
                "title": "docs: x", "body": stale_body, "user": {"login": "me"}, "labels": []}}))
            live.write_text(json.dumps({"title": "docs: x", "body": GOOD}))
            self.assertEqual(self._main("--event", str(event))[0], 1)            # edited, no link
            self.assertEqual(self._main("--event", str(event), "--pr-file", str(live))[0], 0)
            self.assertEqual(self._main("--event", str(event), "--action", "synchronize")[0], 0)


if __name__ == "__main__":
    unittest.main()
