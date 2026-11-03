#!/usr/bin/env python3
"""Check a pull request's description: sections, title, scope, declared provenance, DCO.

Adapted from villadalmine/study-cybercirujas (scripts/check_pr.py): the same gates, minus what is
specific to that project (certs/ material, meta.yaml provenance, the site image, the graph bot,
exam versions). The PR format is the same whoever wrote the change, a person or a model
(.github/pull_request_template.md, CONTRIBUTING.md). It fails when:

- a required section is missing or empty (HTML comments do not count);
- the title is not a Conventional Commit;
- the Scope section does not declare exactly one of small | medium | large | security
  (CONTRIBUTING -> How big is my change?);
- the provenance is not declared: `Author:` must be `human` or `ai`, and for `ai` the `Backend:` and
  `Model:` must say which tool and model (the AI disclosure, CONTRIBUTING.md);
- the Authorship section has no `| Author | Human: ... |` or `| Author | AI: ... |` row, that row
  disagrees with Provenance, it has no `### Findings ledger`, or a ledger row is malformed (9 columns,
  a "Found by", and a status: fixed | open | rejected - <reason>);
- Evidence has no `### Where it ran` subsection, or that subsection does not link a green run of
  verify.yml on the PR's head commit. Only on a push (`opened`, `synchronize`, `reopened`) is the link
  advisory: the run to cite does not exist yet, and the proof for that head is the `verify` run
  verify.yml starts for the same event (a required check, never skipped). Every other action, also an
  empty or unknown one, is strict (fail closed);
- a non-merge commit of the PR has no `Signed-off-by:` trailer (Developer Certificate of Origin, the
  `DCO` file) whose email is the commit author's or the PR author's GitHub noreply address. EVERY
  commit of base..HEAD is checked, whatever the PR's number or dates; merged history never is. The
  author email of a commit is set by whoever commits, so nothing is exempt by email;
- the shape: more than 400 changed lines of code (waived by the `size-exception` label, set by a
  maintainer, never by this check);
- the feature gate (docs/FEATURES.yml, issue #62): the changed paths touch features, and Evidence does not
  cite the RESULT PASS of their suites; a change to the cluster (`cluster_paths`) needs the table of
  `make regression` (no FAIL row) or every touched feature with PASS; and `### Docs impact` must name
  every doc and diagram of the touched features, or say "none: <reason>". Like the run link, only a
  warning on a push, strict on every other action.

No model. No network either, except `--verify-runs` (CI only): that mode asks the GitHub API, with
`gh api`, whether the run cited under "Where it ran" is a green run of verify.yml on the PR's head
(it waits for that run's `verify` job if it is still running).

    # by hand
    scripts/check_pr.py --title "docs: x" --body-file body.md --files a b c

    # CI (.github/workflows/pr-format.yml, job pr-format), step 1: the description
    scripts/check_pr.py --event "$GITHUB_EVENT_PATH" --pr-file pr.json --labels-file labels.txt \
        --base origin/<base> --runs-file cited-runs.txt
    # step 2: the cited run, against the API (advisory on opened/synchronize/reopened, strict otherwise)
    scripts/check_pr.py --verify-runs cited-runs.txt --repo <owner/name> --head <sha> --action <action>
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SECTIONS = ("What and why", "Kind", "Scope", "Provenance", "Evidence", "Authorship",
            "How to test it from a clean clone", "Risk and rollback", "Depends on")
TYPES = ("feat", "fix", "docs", "test", "ci", "chore", "refactor", "perf", "build",
         "style", "revert", "design", "security")
TITLE = re.compile(rf"^({'|'.join(TYPES)})(\([\w./,-]+\))?!?: \S.*$")


def sections(body: str) -> dict[str, str]:
    """`## Heading` -> its text, with HTML comments removed."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    found: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return {k: v.strip() for k, v in found.items()}


def declared(provenance: str) -> dict[str, str]:
    """`- Key: value` lines of the Provenance section, lower-cased keys."""
    out = {}
    for line in provenance.splitlines():
        match = re.match(r"^\s*[-*]\s*([A-Za-z ]+):\s*(.*?)\s*$", line)
        if match:
            out[match.group(1).strip().lower()] = match.group(2)
    return out


def disclosure(prov: dict[str, str]) -> list[str]:
    """The AI disclosure, on every PR: `Author: human|ai`, and for ai the tool and the model."""
    author = prov.get("author", "").strip().lower()
    if author not in ("ai", "human"):
        return ["Provenance: 'Author:' must be exactly 'ai' or 'human' (also 'ai' when an AI helped write "
                "it), then say which Backend and Model"]
    if author == "ai":
        empty = [k.capitalize() for k in ("backend", "model")
                 if prov.get(k, "").strip().lower() in ("", "none", "n/a")]
        if empty:
            return [f"Provenance: Author is ai, so name the tool and model ({' and '.join(empty)} "
                    "is empty or 'none')"]
    return []


# --- scope declared in the description ----------------------------------------

SCOPES = ("small", "medium", "large", "security")
SCOPE_LINE = re.compile(r"^\s*[-*]?\s*Scope:\s*(.*?)\s*$", re.M | re.I)


def declared_scope(text: str) -> tuple[str | None, list[str]]:
    """The one scope the Scope section declares, and the problems if it is not exactly one."""
    line = SCOPE_LINE.search(text)
    value = line.group(1).strip("`").lower() if line else ""
    if value in SCOPES:
        return value, []
    return None, [f"Scope: add a 'Scope: <one of {' | '.join(SCOPES)}>' line with exactly one value "
                  "(CONTRIBUTING -> How big is my change?)"]


# --- authorship: who wrote it, who found what, who fixed it -------------------

AUTHOR_ROW = re.compile(r"^\|\s*Author\s*\|([^|]*)\|", re.M | re.I)
LEDGER_COLUMNS = 9  # # | found by | head | severity | finding | fixed by | fix | verified by | status
STATUS = re.compile(r"^(fixed|open|rejected\s*[—–:-]+\s*\S.*)$", re.I)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def ledger_rows(authorship_text: str) -> list[list[str]] | None:
    """The data rows of '### Findings ledger' (None if the subsection is missing)."""
    match = re.search(r"^###\s+Findings ledger\s*$(.*?)(?=^###\s|\Z)", authorship_text, re.M | re.S)
    if not match:
        return None
    rows = []
    for line in match.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _cells(line)
        if cells[0] == "#" or all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue  # header or separator
        rows.append(cells)
    return rows


def authorship(text: str, prov: dict[str, str]) -> list[str]:
    """Who is declared as author agrees with Provenance; the findings ledger is well formed."""
    problems = []
    row = AUTHOR_ROW.search(text)
    who = row.group(1).strip() if row else ""
    kind = re.match(r"(?i)(ai|human)\s*:", who)
    if not kind:
        problems.append("Authorship: add an Author row, '| Author | Human: <who> | ...' or "
                        "'| Author | AI: <agent> · <model> | ...'")
    else:
        said = prov.get("author", "").lower()
        if said in ("ai", "human") and kind.group(1).lower() != said:
            problems.append(f"Authorship: the Author row says {kind.group(1)}, Provenance says Author: {said}")
        models = [m.strip() for m in prov.get("model", "").split(",") if m.strip() and m.strip() != "none"]
        if kind.group(1).lower() == "ai" and models and not any(m in who for m in models):
            problems.append(f"Authorship: the Author row must name the model Provenance declares ({', '.join(models)})")
    rows = ledger_rows(text)
    if rows is None:
        problems.append("Authorship: add a '### Findings ledger' subsection (its table may have no rows yet)")
        return problems
    for cells in rows:
        ref = cells[0] or "?"
        if len(cells) != LEDGER_COLUMNS:
            problems.append(f"Findings ledger row {ref}: {len(cells)} columns, expected {LEDGER_COLUMNS}")
            continue
        if not cells[1]:
            problems.append(f"Findings ledger row {ref}: 'Found by' is empty")
        if not STATUS.match(cells[8]):
            problems.append(f"Findings ledger row {ref}: status must be fixed, open or "
                            f"'rejected — <reason>', not {cells[8]!r}")
        elif cells[8].lower() == "fixed" and not (cells[5] and cells[6]):
            problems.append(f"Findings ledger row {ref}: fixed, but 'Fixed by' or 'Fix' is empty")
    return problems


# --- size: one concern per PR (CONTRIBUTING -> How big is my change?) ----------

CODE_LIMIT = 400                     # changed lines of code
CODE = re.compile(r"^(scripts/|tests/|deploy/|\.github/|Makefile$)")
SIZE_LABEL = "size-exception"        # set by a maintainer, never by the check


def size(code_lines: int | None, labels: set | None = None) -> list[str]:
    labels = labels or set()
    if code_lines is not None and code_lines > CODE_LIMIT and SIZE_LABEL not in labels:
        return [f"size: {code_lines} changed lines of code (limit {CODE_LIMIT}); split it into stacked "
                "PRs ('Depends on: #N')"]
    return []


# --- DCO: a human signs off every commit (CONTRIBUTING -> Sign your commits) ----

SIGNED_OFF = re.compile(r"^Signed-off-by:[ \t]*(.*?)[ \t]*<([^<>\s]+)>[ \t]*$", re.M | re.I)
DCO_FIX = ("fix: `git commit --amend -s` (last commit) or `git rebase --signoff origin/main` (all of "
           "them), then `git push --force-with-lease`; see CONTRIBUTING.md -> Sign your commits (DCO)")


def commits_of(base: str) -> list[dict]:
    """The non-merge commits of base..HEAD: sha, author email, message."""
    result = subprocess.run(["git", "log", "--no-merges", "--format=%H%x1f%ae%x1f%B%x1e", f"{base}..HEAD"],
                            cwd=REPO, capture_output=True, text=True, check=True)
    out = []
    for record in result.stdout.split("\x1e"):
        if record.strip():
            sha, email, message = record.strip("\n").split("\x1f", 2)
            out.append({"sha": sha.strip(), "email": email.strip(), "message": message})
    return out


def noreply(email: str, login: str | None) -> bool:
    """`<id>+<login>@users.noreply.github.com` or `<login>@users.noreply.github.com`."""
    if not login:
        return False
    return re.fullmatch(rf"(\d+\+)?{re.escape(login)}@users\.noreply\.github\.com", email, re.I) is not None


def dco(commits: list[dict], pr_author: str | None = None) -> list[str]:
    """One problem per commit without a valid Signed-off-by (empty: all signed)."""
    problems = []
    for c in commits:
        emails = {m.group(2).lower() for m in SIGNED_OFF.finditer(c["message"])}
        if c["email"].lower() in emails or any(noreply(e, pr_author) for e in emails):
            continue
        subject = c["message"].strip().splitlines()[0] if c["message"].strip() else ""
        problems.append(f"DCO: commit {c['sha'][:7]} ({subject[:60]!r}) has no 'Signed-off-by: Name <email>' "
                        f"for its author <{c['email']}>; {DCO_FIX}")
    return problems


# --- the feature gate: changed paths -> features -> evidence and docs (issue #62) ---

def catalog() -> dict:
    sys.path.insert(0, str(REPO / "scripts"))
    import features  # noqa: E402 (scripts/features.py: the catalog's one reader)
    return features.load()


def word(name: str) -> str:
    return rf"(?<![\w-]){re.escape(name)}(?![\w-])"


def feature_gate(body: str, files: list[str], cat: dict) -> list[str]:
    """What the description lacks for the features the changed files touch (empty when they touch none)."""
    hit = lambda globs: [f for f in files if any(fnmatch.fnmatch(f, g) for g in globs)]  # noqa: E731
    feats = [f for f in cat["features"] if hit(f["paths"])]
    if not feats:
        return []
    clean = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    lines = sections(body).get("Evidence", "").splitlines()
    passed = lambda name, tick: any(re.search(word(f"`{name}`" if tick else name), ln)  # noqa: E731
                                    and re.search(r"RESULT PASS|\|\s*PASS\s*\||:\s*PASS\b", ln) for ln in lines)
    problems, cluster = [], hit(cat.get("cluster_paths") or [])
    names = ", ".join(f["id"] for f in feats)
    if cluster:
        table = any(re.match(r"^\|\s*Feature\s*\|\s*Result\s*\|", ln) for ln in lines)
        failed = [ln.split("|")[1].strip() for ln in lines if re.match(r"^\|\s*`[\w-]+`\s*\|\s*FAIL", ln)]
        missing = [f["id"] for f in feats if not passed(f["id"], True)]
        if table and failed:
            problems.append(f"Evidence: the make regression table has FAIL rows: {', '.join(failed)}")
        elif not table and missing:
            problems.append(f"Evidence: this PR changes the cluster ({', '.join(cluster[:3])}...): paste the table of `make regression`, "
                            f"or list every touched feature with PASS; missing: {', '.join(missing)}")
    else:
        suites = sorted({p["suite"] for f in feats for p in f["proven_by"]})
        missing = [s for s in suites if not passed(s, False)]
        if missing:
            problems.append(f"Evidence: the changed paths touch {names}; cite the RESULT PASS of {', '.join(missing)} "
                            "(one line each, or the make regression table)")
    m = re.search(r"^###\s+Docs impact\s*$(.*?)(?=^##|\Z)", clean, re.M | re.S)
    if not m:
        return problems + [f"add a '### Docs impact' subsection: the changed paths touch {names} (docs/FEATURES.yml)"]
    said = m.group(1)
    if re.search(r"\bnone\b\s*[:—–-]+\s*\S+(\s+\S+){2,}", said, re.I):
        return problems
    listed = sorted({d for f in feats for d in (f.get("docs") or []) + (f.get("diagrams") or [])})
    left = [d for d in listed if d not in said and Path(d).name not in said]
    if left:
        problems.append(f"Docs impact: name what you updated of {', '.join(left)}, or write 'none: <reason>'")
    return problems


# --- the whole description ----------------------------------------------------

RUN_LINK = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")


def cited_runs(body: str) -> list[tuple[str, str]]:
    """(repo, run id) linked under '### Where it ran' inside Evidence."""
    evidence = sections(body).get("Evidence", "")
    match = re.search(r"^###\s+Where it ran.*?$(.*?)(?=^###\s|\Z)", evidence, re.M | re.S)
    return RUN_LINK.findall(match.group(1)) if match else []


# Who must cite the run, and when (issue #60). The run that proves a new head only exists after the
# push, so a push run cannot cite it: on these actions the `verify` run verify.yml starts for the same
# event (required by the ruleset, never skipped) is the proof. They must be exactly verify.yml's
# pull_request types (tests/test_check_pr.py checks it). Everything else is strict: fail closed.
ADVISORY_ACTIONS = ("opened", "synchronize", "reopened")


def citation_required(action: str | None) -> bool:
    """True unless the action is a push (ADVISORY_ACTIONS); empty, unknown and None are strict."""
    return action not in ADVISORY_ACTIONS


def has_where_it_ran(body: str) -> bool:
    evidence = sections(body).get("Evidence", "")
    return re.search(r"^###\s+Where it ran", evidence, re.M) is not None


def check(title: str, body: str, strict: bool = True) -> list[str]:
    """The description's problems. `strict` (an `edited` event, or by hand) also wants the run link."""
    problems: list[str] = []
    if not TITLE.match(title.strip()):
        problems.append(f"title is not a Conventional Commit ({'|'.join(TYPES)}(scope)?: summary): {title!r}")
    found = sections(body)
    for name in SECTIONS:
        if name not in found:
            problems.append(f"missing section: ## {name}")
        elif not found[name]:
            problems.append(f"empty section: ## {name}")
    if found.get("Scope"):
        problems += declared_scope(found["Scope"])[1]
    if "Evidence" in found and not has_where_it_ran(body):
        problems.append("Evidence: add a '### Where it ran' subsection (the template has it); after "
                        "verify passes, paste the line from the 'where-it-ran' comment there")
    elif "Evidence" in found and strict and not cited_runs(body):
        problems.append("Evidence: add a '### Where it ran' subsection linking the CI run on this "
                        "PR's head commit (github.com/<owner>/<repo>/actions/runs/<id>); the "
                        "'where-it-ran' comment CI posts has the line ready to paste")
    if "Provenance" in found:
        problems += disclosure(declared(found["Provenance"]))
    if "Authorship" in found and "Provenance" in found:
        problems += authorship(found["Authorship"], declared(found["Provenance"]))
    return problems


# --- the cited run, checked against the API (the only networked part) ---------

VERIFY_WORKFLOW = ".github/workflows/verify.yml"
PROOF_JOBS = ("verify",)


def judge_run(run: dict, jobs: dict, head_sha: str) -> str | None:
    """Why a cited run does not prove this head, or None if it does.

    A job named `verify` is not enough: any workflow (one the PR itself adds) can name its jobs that
    way, so the run must come from verify.yml and from a pull_request event. The jobs, not the run's
    conclusion: the run may hold other jobs (where-it-ran, which may not post on a fork) that say
    nothing about the code."""
    if run.get("head_sha") != head_sha:
        return f"is on head {str(run.get('head_sha'))[:7]}, not the PR head {head_sha[:7]}"
    if run.get("path") != VERIFY_WORKFLOW:
        return f"comes from {run.get('path')}, not {VERIFY_WORKFLOW}"
    if run.get("event") != "pull_request":
        return f"was triggered by {run.get('event')}, not pull_request"
    states = {j.get("name"): j.get("conclusion") for j in jobs.get("jobs") or [] if j.get("name") in PROOF_JOBS}
    if states != {name: "success" for name in PROOF_JOBS}:
        return "has " + (", ".join(f"{k}={v}" for k, v in sorted(states.items())) or "no verify job")
    return None


def pending(run: dict, jobs: dict, head_sha: str) -> bool:
    """The cited run is the right one (this head, verify.yml, pull_request) but `verify` is not done."""
    if (run.get("head_sha"), run.get("path"), run.get("event")) != (head_sha, VERIFY_WORKFLOW, "pull_request"):
        return False
    states = {j.get("name"): j.get("status") for j in jobs.get("jobs") or [] if j.get("name") in PROOF_JOBS}
    return any(states.get(n) != "completed" for n in PROOF_JOBS)  # not created yet (queued) counts too


def gh_api(path: str) -> dict:
    """`gh api <path>` as JSON; its error goes to the log instead of being swallowed."""
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path}: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


# How long to wait for a cited run of this head that is queued or running: verify's own timeout
# (10 min) plus a margin for the queue. pr-format.yml's timeout-minutes is above it.
WAIT_SECONDS = 1800
POLL_SECONDS = 45     # one /jobs call per poll: at most 40 calls per run (GITHUB_TOKEN budget)


def verify_runs(cited: list[tuple[str, str]], repo: str, head_sha: str, fetch=gh_api,
                wait: float = WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic) -> bool:
    """True if at least one cited run proves this head; prints why each one does or does not.

    A body edited while the verify run of this head is still running is not an error: wait for it."""
    ok = False
    deadline = clock() + wait
    for cited_repo, run in cited:
        if cited_repo.lower() != repo.lower():  # GitHub owner/repo names ignore case
            print(f"run {run} belongs to {cited_repo}, not {repo}")
            continue
        try:
            data = fetch(f"repos/{repo}/actions/runs/{run}")
            jobs = fetch(f"repos/{repo}/actions/runs/{run}/jobs")
            while pending(data, jobs, head_sha) and clock() < deadline:
                print(f"run {run}: verify still running on {head_sha[:7]}, waiting")
                sleep(POLL_SECONDS)
                jobs = fetch(f"repos/{repo}/actions/runs/{run}/jobs")
            reason = judge_run(data, jobs, head_sha)
        except (RuntimeError, ValueError) as exc:
            reason = f"could not be read: {exc}"
        print(f"run {run}: " + (reason or f"{VERIFY_WORKFLOW}, verify green on {head_sha[:7]}"))
        ok = ok or reason is None
    return ok


def read_labels(path: Path) -> set:
    """One label per line, as `gh api .../labels --jq '.[].name'` writes them."""
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def changed_files(base: str) -> list[str]:
    result = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--name-only", f"{base}...HEAD"],
                            cwd=REPO, capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def code_lines_changed(base: str) -> int:
    result = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--numstat", f"{base}...HEAD"],
                            cwd=REPO, capture_output=True, text=True, check=True)
    total = 0
    for line in result.stdout.splitlines():
        added, removed, path = line.split("\t", 2)
        if added != "-" and CODE.match(path):
            total += int(added) + int(removed)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event", help="GitHub event JSON (pull_request)")
    parser.add_argument("--title")
    parser.add_argument("--body-file")
    parser.add_argument("--base", help="git ref to diff against, e.g. origin/main")
    parser.add_argument("--files", nargs="*", default=None)
    parser.add_argument("--runs-file",
                        help="write the CI runs cited under 'Where it ran' here, one 'repo id' per "
                             "line, for the job step that checks them against the API")
    parser.add_argument("--labels-file",
                        help="the PR's current labels, one per line (read live in CI); they replace "
                             "the event payload's, which a re-run replays as they were")
    parser.add_argument("--verify-runs", metavar="RUNS_FILE",
                        help="networked mode, for CI only: check the runs listed in RUNS_FILE "
                             "(from --runs-file) with `gh api`; needs --repo and --head")
    parser.add_argument("--repo", help="owner/name of this repository (with --verify-runs)")
    parser.add_argument("--head", help="the PR's head commit SHA (with --verify-runs)")
    parser.add_argument("--action",
                        help="the pull_request event's action (default: the --event payload's). On "
                             "'edited' the cited run must prove the head; on the others it is advisory")
    parser.add_argument("--pr-file",
                        help="the PR as `gh api repos/<r>/pulls/<n>` returns it, read live in CI: its "
                             "title and body replace the event payload's (a re-run replays old ones)")
    args = parser.parse_args()

    if args.verify_runs:
        cited = [tuple(line.split()) for line in Path(args.verify_runs).read_text(encoding="utf-8").splitlines()
                 if len(line.split()) == 2]
        strict = citation_required(args.action)
        if verify_runs(cited, args.repo, args.head, wait=WAIT_SECONDS if strict else 0):
            return 0
        if not strict:
            print(f"::warning::'Where it ran' does not link a green run of {VERIFY_WORKFLOW} on {args.head[:7]} "
                  f"yet. On '{args.action}' the verify run of the same push is the proof; after it passes, paste "
                  "the line from the where-it-ran comment (the edit re-checks it).")
            return 0
        print(f"::error::'Where it ran' must link a green run of {VERIFY_WORKFLOW} in {args.repo} on the "
              f"head commit {args.head}. Copy the line from the where-it-ran comment and edit the description "
              "(if verify was still queued, edit again once it finishes).")
        return 1

    labels: set = set()
    author = None
    action = args.action
    if args.event:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        action = action or event.get("action")
        if args.pr_file and Path(args.pr_file).is_file() and Path(args.pr_file).stat().st_size:
            live = json.loads(Path(args.pr_file).read_text(encoding="utf-8"))
            pr = {**pr, "title": live.get("title", pr["title"]), "body": live.get("body", pr.get("body"))}
        title, body = pr["title"], pr.get("body") or ""
        author = (pr.get("user") or {}).get("login")
        labels = {label["name"] for label in pr.get("labels") or []}
    else:
        title = args.title or ""
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    if args.labels_file and Path(args.labels_file).is_file():
        labels = read_labels(Path(args.labels_file))  # live labels replace the replayed payload's
    files = args.files if args.files is not None else (changed_files(args.base) if args.base else [])
    code_lines = code_lines_changed(args.base) if args.base and args.files is None else None

    strict = citation_required(action)
    problems = check(title, body, strict) + size(code_lines, labels)
    gate = feature_gate(body, files, catalog()) if files else []
    if strict:
        problems += gate
    for problem in gate if not strict else []:
        print(f"::warning::PR: {problem} (on '{action}' only a warning; the edit after the push checks it)")
    if args.base:
        problems += dco(commits_of(args.base), author)
    if args.runs_file:
        Path(args.runs_file).write_text("".join(f"{repo} {run}\n" for repo, run in cited_runs(body)),
                                        encoding="utf-8")
    for problem in problems:
        print(f"PR: {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s). Template: .github/pull_request_template.md")
        return 1
    if not strict and not cited_runs(body):
        print(f"::notice::'Where it ran' has no run link yet; on '{action}' that is fine: paste the line "
              "from the where-it-ran comment once verify passes.")
    print(f"PR description OK ({len(files)} changed file(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
