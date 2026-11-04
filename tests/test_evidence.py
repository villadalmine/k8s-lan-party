"""Tests of the `evidence` check (scripts/evidence.py): pending -> in_progress -> success, one check run per head.

Only the HTTP edge (`evidence.api`) is replaced, by a small in-memory GitHub: PRs, workflow runs and the Checks API,
whose answers have the shapes of the real API. main() runs end to end."""
import contextlib
import io
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pr  # noqa: E402
import evidence  # noqa: E402
from test_check_pr import GOOD  # noqa: E402

REPO, HEAD, OLD = "villadalmine/k8s-lan-party", "a" * 40, "b" * 40
LINK = "- CI: https://github.com/villadalmine/k8s-lan-party/actions/runs/123"
PENDING = GOOD.replace(LINK, "- CI: pending")


class FakeGitHub:
    def __init__(self, body, head=HEAD, run=None, verify=("completed", "success")):
        self.pr = {"number": 79, "body": body, "head": {"sha": head}}
        self.run = run or {"head_sha": head, "path": check_pr.VERIFY_WORKFLOW, "event": "pull_request",
                           "status": verify[0]}
        self.verify = verify
        self.checks, self.calls = [], []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path.split("?")[0]))
        if method == "GET" and path == f"repos/{REPO}/pulls/79":
            return self.pr
        if method == "GET" and path == f"repos/{REPO}/actions/runs/123":
            return self.run
        if method == "GET" and path == f"repos/{REPO}/actions/runs/123/jobs":
            status, conclusion = self.verify
            return {"jobs": [{"name": "verify", "status": status, "conclusion": conclusion}]}
        if method == "GET" and "/check-runs?check_name=evidence" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            return {"check_runs": [c for c in self.checks if c["head_sha"] == sha]}
        if method == "POST" and path == f"repos/{REPO}/check-runs":
            check = {**payload, "id": len(self.checks) + 1, "conclusion": payload.get("conclusion")}
            self.checks.append(check)
            return check
        if method == "PATCH" and path.startswith(f"repos/{REPO}/check-runs/"):
            check = next(c for c in self.checks if c["id"] == int(path.rsplit("/", 1)[1]))
            check.update({"conclusion": None, **payload})  # a payload without a conclusion clears it, as the API does
            return check
        raise AssertionError(f"unexpected call {method} {path}")

    def state(self, head=HEAD):
        return [(c["status"], c["conclusion"]) for c in self.checks if c["head_sha"] == head]


class Evidence(unittest.TestCase):
    def main(self, gh):
        out = io.StringIO()
        argv = ["evidence.py", "--repo", REPO, "--pr", "79"]
        with unittest.mock.patch.object(evidence, "api", gh), unittest.mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            code = evidence.main()
        return code, out.getvalue()

    def test_pending_is_in_progress_then_success_in_the_same_check_run(self):
        gh = FakeGitHub(PENDING)
        for _ in range(3):  # opened, then two labels: nothing new is created, nothing fails
            self.assertEqual(self.main(gh)[0], 0)
        self.assertEqual(gh.state(), [("in_progress", None)])
        gh.pr["body"] = GOOD  # the single edit that cites the green verify run of the head
        self.assertEqual(self.main(gh)[0], 0)
        self.assertEqual(gh.state(), [("completed", "success")])

    def test_a_bad_link_fails_and_the_fix_replaces_the_failure(self):
        for name, gh in {"another head": FakeGitHub(GOOD, run={"head_sha": OLD, "path": check_pr.VERIFY_WORKFLOW,
                                                                "event": "pull_request"}),
                         "red verify": FakeGitHub(GOOD, verify=("completed", "failure")),
                         "another repository": FakeGitHub(GOOD.replace("villadalmine/k8s", "someone/k8s"))}.items():
            self.main(gh)
            self.assertEqual(gh.state(), [("completed", "failure")], name)
            gh.pr["body"] = PENDING  # edited back: the failure is cleared, not left behind
            self.main(gh)
            self.assertEqual(gh.state(), [("in_progress", None)], name)
        gh = FakeGitHub(GOOD, verify=("completed", "failure"))
        self.main(gh)
        gh.verify = ("completed", "success")
        self.main(gh)
        self.assertEqual(gh.state(), [("completed", "success")])

    def test_no_run_cited_never_fails(self):
        link = LINK
        for body in (GOOD.replace(link, "- CI: "), GOOD.replace(link, "- CI: none"),
                     GOOD.replace("### Where it ran\n", "")):
            gh = FakeGitHub(body)
            self.main(gh)
            self.assertEqual(gh.state(), [("in_progress", None)])

    def test_a_new_head_gets_its_own_check_run(self):
        gh = FakeGitHub(GOOD)
        self.main(gh)
        gh.pr["head"]["sha"] = OLD  # a push: the old link is stale for the new head, the old head keeps its run
        gh.pr["body"] = PENDING
        self.main(gh)
        self.assertEqual(gh.state(HEAD), [("completed", "success")])
        self.assertEqual(gh.state(OLD), [("in_progress", None)])

    def test_cited_run_still_running_is_waited_for(self):
        gh = FakeGitHub(GOOD, verify=("in_progress", None))
        gh.pr["body"] = GOOD
        with unittest.mock.patch.object(check_pr, "WAIT_SECONDS", -1):
            self.main(gh)
        self.assertEqual(gh.state(), [("completed", "failure")])  # deadline passed with verify still running

    def test_workflows_name_and_permissions(self):
        """The job never shares the check's name; the fork path is pull_request_target and runs nothing of the PR."""
        import yaml
        root = Path(evidence.__file__).resolve().parent.parent / ".github" / "workflows"
        fmt = yaml.safe_load((root / "pr-format.yml").read_text(encoding="utf-8"))
        fork = yaml.safe_load((root / "evidence.yml").read_text(encoding="utf-8"))
        for wf in (fmt, fork):
            self.assertNotIn(evidence.CHECK_NAME, wf["jobs"])
            self.assertTrue(all(j.get("name", i) != evidence.CHECK_NAME for i, j in wf["jobs"].items()))
        job = fork["jobs"]["evidence-writer-fork"]
        self.assertEqual(sorted((fork.get("on") or fork.get(True))), ["pull_request_target"])
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "read", "actions": "read",
                                              "checks": "write"})
        steps = " ".join(str(s) for s in job["steps"])
        self.assertNotIn("github.event.pull_request.head", steps)  # nothing of the PR is checked out
        self.assertNotIn("ref:", steps)
        self.assertEqual(fmt["jobs"]["evidence-writer"]["permissions"]["checks"], "write")
        self.assertNotIn("checks", fmt["jobs"]["pr-format"]["permissions"])  # pr-format itself stays read-only


if __name__ == "__main__":
    unittest.main()
