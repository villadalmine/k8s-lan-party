<!-- Title: a Conventional Commit, e.g. "docs(design): add the phase 1 design". English is the standard.
     Every section below is required: scripts/check_pr.py fails the PR if one is missing or empty. -->

## What and why

<!-- One concern per PR. Link the issue or RFC it answers. -->

## Kind

<!-- docs | code | ci | design | security. Area (network / control-plane / identity / security / lab): -->
- Kind: 

## Scope

<!-- How big is this change? Keep exactly ONE value on the first line, and delete the others.
     scripts/check_pr.py fails the PR without it. Rules: CONTRIBUTING -> How big is my change? -->
- Scope: small | medium | large | security

- `security`: the security design, the threat model, the join/login flow, admission policy, tokens or certificates.
- `large`: `.github/`, `Makefile`, `scripts/`, dependencies; more than 400 code lines.
- `medium`: one design doc, one script or one lab.
- `small`: docs, typos or links.

## Provenance

<!-- Who or what wrote it. -->
- Author: human | ai  <!-- 'ai' also when an AI helped write it; then Backend and Model are required -->
- Backend: none | claude | ...
- Model: none | <model id, if an AI wrote it>
- Date: YYYY-MM-DD
- Cost: <!-- e.g. "subscription quota" / "$0.07 API" / "none" -->
- Official source: <!-- the upstream documentation this is based on -->

## Evidence

<!-- Commands you ran and their short output. -->
```
make pr-check   # CI's PR checks, locally (CONTRIBUTING -> Check before you push)
```

### Where it ran

<!-- The CI run on this PR's HEAD commit, green: github.com/villadalmine/edge-mesh-lab/actions/runs/<id>.
     CI posts a 'where-it-ran' comment with this line ready to paste after each push; a new push
     makes the old link stale and pr-format fails until it is updated. Add the machine or local
     environment too if you ran anything there. -->
- CI: 
- Local: 

### Known gaps

<!-- What this evidence does NOT cover. Always present; 'none' is a claim, not a default. -->

## Authorship

<!-- Who did what, updated on every review round (CONTRIBUTING -> Authorship). The Author
     row starts with "Human:" or "AI:" and agrees with Provenance; for AI it names the
     agent and the model. One Reviewer row per review (revisor-pr, a person). -->
| Role | Who | What | Link |
| :--- | :--- | :--- | :--- |
| Author | Human: @you · or · AI: <agent> · <model id> | commits `<first>..<last>` | |
| Reviewer | | head `<sha>` - result | |
| Approval and merge | maintainer (human) | pending | |

### Findings ledger

<!-- One row per finding, from any reviewer, CI or a person. Status: fixed |
     rejected - <reason> | open. "Verified by" is who confirmed the fix next round.
     Low findings go to the area's umbrella issue, not one issue each. -->
| # | Found by | Head | Severity | Finding | Fixed by | Fix | Verified by | Status |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |

## How to test it from a clean clone

```bash
git clone https://github.com/villadalmine/edge-mesh-lab.git && cd edge-mesh-lab
git fetch origin pull/<N>/head:pr && git checkout pr
make pr-check
```

## Risk and rollback

- [ ] Commits signed off (DCO): `git commit -s`; fix a missing one with `git rebase --signoff origin/main`

<!-- What could break, for whom, and how to undo it (usually: revert this PR). -->

## Depends on

<!-- "#N", or "nothing". -->
