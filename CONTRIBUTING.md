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

## Pull requests

One template, [`.github/pull_request_template.md`](.github/pull_request_template.md). Every PR has the same
required sections: **What and why · Kind · Scope · Provenance · Evidence · Authorship · How to test it from a
clean clone · Risk and rollback · Depends on**. `scripts/check_pr.py` runs on every PR (job `pr-format`) and
fails when one is missing, the title is not a Conventional Commit, the provenance is undeclared, a commit has
no sign-off, or the evidence does not cite a green CI run.

**Where it ran.** Under *Evidence*, `### Where it ran` links the green CI run on the PR's **current head
commit**, and `### Known gaps` says what that evidence does not cover. After each push CI posts a
*where-it-ran* comment with the line to paste; a new push makes the old link stale on purpose.

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
check unless a maintainer sets the `size-exception` label. Split it into stacked PRs (`Depends on: #N`).

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

**Merging:** rebase merge by default: every commit of the PR lands on `main` as it is, with its own author, so
each one must be a Conventional Commit with its sign-off (squash your fixups locally before asking for review):
`gh pr merge N --rebase --delete-branch --match-head-commit <sha>`. A squash merge made from the API authors the
commit with the merging account's email unless that account keeps its email private, which is why it is not the
default; it stays allowed for a contributor's PR with an untidy history (the PR title becomes the single commit).
The branch is deleted on merge. Nobody merges their own PR except the
maintainer for maintenance changes; an agent never merges. Chained PRs: merge without deleting the branch,
re-point the next one to `main` and rebase with `--onto origin/main <old parent>`.

`main` is protected by a ruleset ([`.github/rulesets/main.json`](.github/rulesets/main.json), runbook
[`docs/runbooks/github-proteger-main.md`](docs/runbooks/github-proteger-main.md)): PR required, checks `verify`
and `pr-format`, linear history, no force push, no deletion. GitHub enforces it since the repository became public
on 2026-10-27 (on a free account rulesets need a public repository).

## Licence

By contributing you agree your contribution is licensed under [Apache 2.0](LICENSE) (the licence is an open
decision, see `docs/DESIGN.md`) and you certify the [DCO](DCO) with a `Signed-off-by:` on each commit.
