# Contributing

Adapted from the collaboration standard of `villadalmine/study-cybercirujas` (skill `repo-colaborativo`).
This is a product in **MVP phase 1**. The repository is public since 2026-10-27, at an early stage and not announced
yet, so nothing here promises support or response times beyond the best effort below. Today contributions are
design reviews, threat-model attacks on the paper, and the proof of concept (PoC). See [docs/DESIGN.md](docs/DESIGN.md).

## What you can contribute

| You want to... | Do this | Open first |
|---|---|---|
| **break the design** | an issue with the *Idea* or *Bug* form, or better a PR to `docs/THREAT-MODEL.md` | issue |
| **fix a doc, a script or a check** | a pull request, with a test when it is code | PR |
| **change how the project works** (trust model, join flow, a core dependency, the licence) | a proposal | *Proposal (RFC)* issue |
| **ask a question** | Discussions (Q&A) | - |

### Sign your commits (DCO)

Every commit in a pull request carries a `Signed-off-by:` line:

```bash
git commit -s -m "docs(design): ..."      # adds: Signed-off-by: Your Name <you@example.org>
```

**What it means.** The line is your word that you wrote the change, or have the right to send it,
under this project's licence. It is the [Developer Certificate of Origin](DCO) 1.1, the same one the
Linux kernel uses. It is not a paper to sign and nobody reviews it: `scripts/check_pr.py` only checks
that the line is there and that the email is the commit author's (or your GitHub `noreply` address).

**You forgot it.** The check tells you which commits:

```bash
git commit --amend -s                  # only the last commit
git rebase --signoff origin/main       # every commit of your branch
git push --force-with-lease            # update the PR
```

**Why no CLA.** You keep the copyright of what you wrote; we ask for nothing beyond the DCO.

**AI policy.** AI-assisted work is welcome **when it is declared**. In the PR's *Provenance* section
write `Author: ai` (also when an AI only helped) and name the `Backend` and `Model`; `check_pr.py`
fails the PR if that is missing. The `Signed-off-by:` is a human's: whoever signs takes responsibility
for the change, whatever wrote it. Our own agents sign their commits too.

Every commit of the PR that is not on `main` yet needs it (`git rebase --signoff origin/main` fixes it).
Merged history is never rewritten, and nothing is exempt because of a commit's author email, which
anyone can set.

### Security reports

Do not open a public issue for a vulnerability. How to report one, and when you get an answer, is in
[SECURITY.md](SECURITY.md).

## Check before you push

```bash
make pr-check                                    # the checks CI runs, locally, one line each
make pr-check PR_BODY=body.md PR_TITLE="docs(design): ..."   # also checks your draft description
make install-hooks                               # OPTIONAL: run it before every git push
```

`make test` runs the unit tests of the gates; `make labels-check` checks the labels.
`make pr-check` runs before the push, so like CI's push run it does not ask for the *Where it ran* link yet.

### A PR that touches the cluster proves the whole stack

[`docs/FEATURES.yml`](docs/FEATURES.yml) is the single list of what the project does: one entry per feature, with the suites and step ids
that prove it (`poc-join` J01-J07, `poc-vm` V06c...), the paths that change it, its docs and diagrams, and the runtime it needs (docker,
incus, kind). `make features-check` (and `make test`) fail when a `poc-*` suite of the Makefile is missing from it or a feature names no
step that exists. A new suite, step or feature goes into the catalog in the same PR.

A PR that changes the cluster (the scripts and manifests under `poc/` that build or run a game) runs **every** suite, from the join to `down`:

```bash
make regression                                  # every suite in the catalog's order, one at a time, a failure stops nothing
make regression ONLY="poc-join poc-revoke"       # a subset while you work; the rest are listed as NOT RUN
REGRESSION_RUNTIME=docker-vm make regression     # a host with Incus and no Docker: the Docker and kind suites in a VM of the run
poc/regression.sh down                           # remove what an interrupted run left: only what its marker names
```

Each suite gets its own state directory; the Incus ones run in their own project (`INCUS_PROJECT`, default `emesh-regression`, empty at
the start) on an underlay checked free with `ip -4 -o addr` before anything runs (`UNDERLAY_NET`, default `10.83.0`; `poc-nat` uses the
three /24 that start 20 above it). On a host without Docker the Docker suites are `SKIP(docker)`, never `FAIL`; on a Docker host an existing
network `emesh`, container `lh`/`cp`/`n1`/`n2` or kind cluster `emesh` aborts the run (exit 2). A suite that runs on Docker and Incus proves
its features on both: a run on one of them shows those features `PARTIAL`. With `REGRESSION_RUNTIME=docker-vm` the preflight builds an
Ubuntu VM with Docker and kind (`poc/vm/docker-vm.sh`, versions pinned in `poc/stack.env`) in the run's own project, on its own bridge (the
/24 30 above `UNDERLAY_NET`); the Docker and kind suites run inside it and the docker-or-incus suites run on both, so nothing is skipped for
want of a runtime; the VM goes with the project. The run writes one table,
suite and feature x result with the commit and the date, into [`docs/POC-STATUS.md`](docs/POC-STATUS.md); the same commit on the same host
and UTC day gives the same table (exit 1 when a suite that ran has no `RESULT PASS`). Paste it (or its failing rows) under *Evidence*.

**What `pr-format` asks for (`scripts/check_pr.py`).** It maps the PR's changed paths to the features of `docs/FEATURES.yml` (their `paths`):

- a PR that touches features cites, under *Evidence*, the `RESULT PASS` of every suite those features list, one line each
  (`poc-golden: RESULT PASS`), or the `make regression` table;
- a PR that changes the cluster (`cluster_paths` in the catalog: the files that build or run a game) pastes the `make regression` table, in
  which every touched feature is `PASS` (not `PARTIAL`, `NOT RUN` or `SKIP`, and not left out of a subset table), or lists every touched
  feature with PASS (`` `join`: PASS ``). A pasted table keeps its `Commit `<sha>`` line, and that commit must be one of the PR's;
- a `FAIL` row or a `RESULT FAIL` line for a touched suite or feature fails the check, whatever else passed (a Docker FAIL is not hidden by
  an Incus PASS); the *Proven by* column of the table is not a citation;
- `### Docs impact` (under *Evidence*) names each doc and diagram the catalog lists for those features that the PR updated, or says
  `none: <reason>`.

Like the run link, these are warnings on a push and required on the edit that pastes the link.

**Diagrams cannot go stale.** Every `docs/diagrams/<name>.mmd` has a `<name>.svg` and a `<name>.png` that carry the SHA-256 of the source
(a comment in the SVG, a `tEXt` chunk in the PNG). Change a `.mmd` and render it again: `make diagrams NAME=<name>` (needs mermaid-cli,
`MMDC=/path/to/mmdc` or `mmdc` on the `PATH`; 12.0.0 was used). `docs/diagrams/features.mmd` is generated from `docs/FEATURES.yml`
(`make diagrams` writes it first): the graph suite and steps -> feature -> components and files. `tests/test_diagrams.py` fails when a source
changed and its SVG or PNG was not rendered again, or when the feature graph does not match the catalog; without mermaid-cli it compares the
hashes only.

## Pull requests

One template, [`.github/pull_request_template.md`](.github/pull_request_template.md). Every PR has the same
required sections: **What and why · Kind · Scope · Provenance · Evidence · Authorship · How to test it from a
clean clone · Risk and rollback · Depends on**. `scripts/check_pr.py` runs on every PR (job `pr-format`) and
fails when one is missing, the title is not a Conventional Commit, the provenance is undeclared, a commit has
no sign-off, or (except on a push) the description does not cite a green CI run on the head.

**Where it ran.** Under *Evidence*, `### Where it ran` links the green CI run on the PR's **current head
commit**, and `### Known gaps` says what that evidence does not cover. The flow, with no red check at any step:

1. **Push** (or open the PR). The run to cite does not exist yet, so on `opened`, `synchronize` and `reopened`
   `pr-format` does not ask for it: it checks the format, and the subsection may say `- CI: pending`. A stale
   link only gets a warning. The proof for this head is the `verify` run that the same push starts, a required
   check on the same commit.
2. When `verify` passes, CI posts (or updates) the *where-it-ran* comment with the line to paste.
3. **Edit the description** and paste it. The edit re-runs `pr-format`, which **requires** that the link is a
   green run of `verify.yml` on the exact head, asked to the GitHub API; if that run is queued or running, it
   waits (up to 30 min).

Every action other than a push (an edit, a label, anything new) is strict, so **paste the line before adding or
removing a label**; a label on a stale link makes `pr-format` red until the next edit. `pr-format` lives in its
own workflow (`pr-format.yml`), and `verify.yml` runs only on pushes, so an edit or a label never creates a
skipped `verify` check run that would hide a red one (GitHub counts `skipped` as passing, and takes the newest
check run of each name). A new push makes the old link stale on purpose: paste the new line after its `verify`
passes. Only a newer push cancels a run (its checks are on the old head); edits, labels and pushes to `main`
never cancel one (issue #60).

**Authorship.** A table with the author (`Human: <who>` or `AI: <agent> · <model>`), one row per review
(the `revisor-pr` agent, a person) and the approval and merge (always a human maintainer); and a
`### Findings ledger`, one row per finding: found by · head · severity · fixed by · fix · verified by ·
status (`fixed`, `open`, or `rejected — <reason>`).

### How big is my change?

Every PR declares one **Scope**. Pick the biggest line that applies:

| Scope | It is | Why it matters |
| :--- | :--- | :--- |
| `small` | docs, typos or links | normal flow: CI and an independent review |
| `medium` | one design doc, one script or one lab | normal flow |
| `large` | `.github/`, `Makefile`, `scripts/`, dependencies; more than 400 code lines | the maintainer reads it; never merged from a fork without the maintainer's OK |
| `security` | the security design, the threat model, the join/login flow, admission policy, tokens or certificates | the maintainer reads it, and the review must say what a malicious node can do after the change |

More than 400 changed lines of code (`scripts/`, `tests/`, `deploy/`, `.github/`, `Makefile`) fails the size
check unless a maintainer sets the `size-exception` label. Split it into stacked PRs (`Depends on: #N`). `poc/` is not counted,
its tests (`poc/test_*.py`) included: the PoC is reviewed by what its suites prove, the gates under `scripts/` and `tests/` line by line.

### Labels

The labels are code: [`.github/labels.yml`](.github/labels.yml) is the single source. To add, rename or retire a
label, change that file in a PR; the *Sync labels* workflow applies it on merge (it never deletes; renames go
through `aliases`; retiring is a manual run: dry-run first, then `delete-other-labels`). `make labels-check`
fails when a form, the labeler, a workflow or `check_pr.py` names a label that is not in the file: GitHub
silently drops an unknown label from a form.

| Family | Question | Who sets it |
| :--- | :--- | :--- |
| `type:` | what kind of change | the form or a maintainer; `docs`, `code`, `ci` by path (`actions/labeler`) |
| `area:` | which part (`network`, `control-plane`, `identity`, `security`, `lab`) | a maintainer; `area:security` by path |
| `priority:` | when (`high` blocks the next release, `medium` is planned, `low` is backlog) | a maintainer |
| `severity:` + `from-review` | a follow-up found by a code review | the reviewer |
| `scope:` | size | declared in the PR; `size-exception` is the maintainer's waiver |
| `triage:`, `status:`, `lifecycle:` | where an issue is in its life | `triage:needed` on open; then a maintainer |

`good first issue` and `help wanted` keep those exact names because GitHub's *Contribute* page reads them. To
close without a label use GitHub's *not planned* reason.

### Triage and response times

Best effort by one maintainer, not a contract; written down so nobody has to wonder.

| What | When |
| :--- | :--- |
| First reply to an issue or a pull request from outside the project | 3 working days |
| Decision: accepted, `triage:needs-info`, or closed with the reason | 7 days |
| Review of an outside pull request with green CI | 7 days |
| A security report | as in [SECURITY.md](SECURITY.md) (first answer within 3 days) |
| An issue marked `triage:needs-info`: how long the author has to answer | 14 days |

**The queue.** An issue or a pull request from outside the project carries `triage:needed` from the moment it
is opened (the forms add it; [`triage.yml`](.github/workflows/triage.yml) covers pull requests and issues opened
without a form). A maintainer does one of four things and removes `triage:needed`: accept it (`type:`, `area:`,
`priority:`, a milestone if it enters a release), ask for what is missing (`triage:needs-info`), redirect it (a
question to Discussions, a rule change to an RFC), or close it with the reason.

Pull requests from outside follow the security-first list of the `triage-pr-externa` skill (anything touching
`.github/`, the `Makefile` or `scripts/` is the maintainer's decision alone).

**Stale, kindly.** [`stale.yml`](.github/workflows/stale.yml) runs daily, in `debug-only` mode at first: it only
lists, in its run log, what it would touch. After two weeks of reading that list, one PR turns it on. When on:
`triage:needs-info` issues go stale at 14 days and close 7 later; other issues at 90 and 30 (never with a
milestone, `help wanted`, `good first issue`, `priority:high`, `type:rfc`, `from-review` or `lifecycle:frozen`);
`triage:needs-info` PRs at 30 and 14. A comment from anyone removes the label; a closed issue can be reopened.

### Agents

AI agents (Claude Code and others) open PRs exactly like people: the same template, provenance that names the
model, "Where it ran" on the head commit, the commit trailers their tool adds, and a `Signed-off-by:`.

## Reviews and merging

Every PR gets an independent review by the `revisor-pr` agent before merging. **High and medium findings stop
the merge. Low findings do not become one issue each:** they go to an umbrella issue per area, as sub-issues.

**Merging:** squash merge only, the PR title becomes the single commit on `main` (hence a Conventional Commit),
the branch is deleted on merge: `gh pr merge N --squash --delete-branch`. The merging account keeps its email private, so the
squash commit carries its noreply address; the `authors` job checks it on every push to `main` (runbook
[`docs/runbooks/github-proteger-main.md`](docs/runbooks/github-proteger-main.md), Step 3). Nobody merges their own PR except the
maintainer for maintenance changes; an agent never merges. Chained PRs: merge without deleting the branch,
re-point the next one to `main` and rebase with `--onto origin/main <old parent>`.

`main` is protected by a ruleset ([`.github/rulesets/main.json`](.github/rulesets/main.json), runbook
[`docs/runbooks/github-proteger-main.md`](docs/runbooks/github-proteger-main.md)): PR required, checks `verify`
and `pr-format`, linear history, no force push, no deletion. GitHub enforces it since the repository became public
on 2026-10-27 (on a free account rulesets need a public repository).

## Licence

By contributing you agree your contribution is licensed under [Apache 2.0](LICENSE) (the licence is an open
decision, see `docs/DESIGN.md`) and you certify the [DCO](DCO) with a `Signed-off-by:` on each commit.
