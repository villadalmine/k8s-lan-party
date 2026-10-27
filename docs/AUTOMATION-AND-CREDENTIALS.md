---
tipo: doc
id: k8s-lan-party-automation-and-credentials
version: 1.0.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-16
categoria: repo
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: meta.yml
docs: [CONTRIBUTING.md, docs/runbooks/github-proteger-main.md, docs/runbooks/github-app-releases.md]
tags: [credenciales, github-actions, github-app]
---

# Automation and credentials

Which credential exists, what for, how far it reaches and how to rotate it. **Never a personal access
token of the maintainer**: bots use the workflow's `GITHUB_TOKEN`; if a bot must trigger CI or push
elsewhere, a GitHub App or a deploy key scoped to this one repository is used.

| Credential | Used by | For | Reach | Rotate |
| :--- | :--- | :--- | :--- | :--- |
| `GITHUB_TOKEN` (automatic) | `verify.yml`, `triage.yml`, `stale.yml`, `sync-labels.yml`, `labeler.yml` | read the repo, label issues and PRs, post the where-it-ran comment | this repo, per-job `permissions:` (least privilege) | per run, nothing to do |
| `RELEASE_APP_ID`, `RELEASE_APP_PRIVATE_KEY` (**not created yet**) | release-please (step 9, waiting) | open the Release PR so that its checks run (a PR opened with `GITHUB_TOKEN` starts no workflows) | GitHub App `k8s-lan-party-releases`, installed only on this repo, Contents and Pull requests RW, no webhook | yearly or on any doubt: runbook [github-app-releases](runbooks/github-app-releases.md) |

Workflows that read pull request data use `pull_request_target` only for labelling (`labeler.yml`,
`triage.yml`) and never check out or run the PR's code. Third-party actions are pinned by commit when they
write (label-sync, stale) and by major tag when they only read.
