"""Tests of the PR gate (scripts/check_pr.py): a guard is tested against real cases before it is required."""
import sys
import unittest
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
- CI: https://github.com/villadalmine/edge-mesh-lab/actions/runs/123
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
        body = GOOD.replace("github.com/villadalmine/edge-mesh-lab/actions/runs/123", "none")
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


if __name__ == "__main__":
    unittest.main()
