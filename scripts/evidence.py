#!/usr/bin/env python3
"""The `evidence` check: the merge-time guarantee that a PR cites a green verify run of its exact head.

Why it exists (issue #79, review of #81). `pr-format` is advisory while the description still says
`- CI: pending` (the documented intermediate state), so it cannot stop that merge: a required check that
reports neutral or success counts as passing. `evidence` is a separate check run, written with the Checks API:

- no run cited yet (pending, empty link, `none`, no heading): `in_progress`. A required check that has not
  completed blocks the merge, and it leaves no failure in `check-runs?filter=all`;
- a cited run that is a green `verify` of the PR's head, from verify.yml on a pull_request event: `success`;
- a cited run that is not (wrong head, red, wrong workflow, another repository): `failure`.

One check run per head: it is found by name and head sha and UPDATED on every event (opened, synchronize,
edited, labeled...), so an old `failure` is replaced by the later `success`, never left next to it. The title
and body are read live, like pr-format does, so a re-run checks the current description.

It reads the description and calls the API; it never checks out or runs anything of the PR. Run from
.github/workflows/pr-format.yml (job evidence-writer, same-repository PRs) and from evidence.yml
(pull_request_target, fork PRs, whose GITHUB_TOKEN cannot write checks under pull_request).

    scripts/evidence.py --repo <owner/name> --pr <number>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import check_pr

CHECK_NAME = "evidence"


def api(method: str, path: str, payload: dict | None = None) -> dict:
    """The one HTTP edge: `gh api`. Tests replace this function and nothing else."""
    cmd = ["gh", "api", "-X", method, path] + (["--input", "-"] if payload is not None else [])
    result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def verdict(body: str, repo: str, head: str, wait: float, fetch) -> tuple[str, str | None, str]:
    """(status, conclusion, summary) for this description and head."""
    cited = check_pr.cited_runs(body)
    if not cited:
        return ("in_progress", None, "No run cited yet. Write `- CI: pending` under '### Where it ran' while "
                "verify runs, then edit once to paste the line from the where-it-ran comment.")
    if check_pr.verify_runs(cited, repo, head, fetch=fetch, wait=wait):
        return "completed", "success", f"A green verify run of {head[:7]} is cited."
    return ("completed", "failure", f"None of the cited runs is a green {check_pr.VERIFY_WORKFLOW} run of "
            f"{head[:7]} (see the pr-format log of this head for the reason of each).")


def publish(repo: str, head: str, status: str, conclusion: str | None, summary: str, call=None) -> int:
    """Create the check run of this head, or update the one that is there. Returns its id."""
    call = call or api
    output = {"title": "Evidence of the head", "summary": summary}
    found = call("GET", f"repos/{repo}/commits/{head}/check-runs?check_name={CHECK_NAME}&filter=all")
    runs = [r for r in found.get("check_runs") or [] if r.get("name") == CHECK_NAME and r.get("head_sha") == head]
    payload = {"name": CHECK_NAME, "head_sha": head, "status": status, "output": output}
    if conclusion:
        payload["conclusion"] = conclusion
    if runs:
        run_id = max(r["id"] for r in runs)
        # GitHub keeps the old conclusion when a completed run goes back to in_progress unless it is
        # replaced, so the update carries the whole state.
        call("PATCH", f"repos/{repo}/check-runs/{run_id}", payload)
        return run_id
    return call("POST", f"repos/{repo}/check-runs", payload)["id"]


def run(repo: str, pr_number: str, wait: float | None = None, call=None) -> int:
    call = call or api
    wait = check_pr.WAIT_SECONDS if wait is None else wait
    pr = call("GET", f"repos/{repo}/pulls/{pr_number}")  # live: a re-run replays an old event
    head = pr["head"]["sha"]
    status, conclusion, summary = verdict(pr.get("body") or "", repo, head, wait, call_get(call))
    run_id = publish(repo, head, status, conclusion, summary, call)
    print(f"evidence {head[:7]}: {conclusion or status} (check run {run_id})")
    return 0


def call_get(call):
    """check_pr.verify_runs fetches with a one-argument GET."""
    return lambda path: call("GET", path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    args = parser.parse_args()
    return run(args.repo, args.pr)


if __name__ == "__main__":
    sys.exit(main())
