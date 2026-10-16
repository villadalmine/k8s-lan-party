---
tipo: runbook
id: edge-mesh-lab-github-proteger-main
version: 1.0.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-16
categoria: repo
dueno_agente: casa-orquestador
pieza: edge-mesh-lab
meta: meta.yml
operacion: configurar
requires_maintainer_ok: si
riesgo: "bajo (solo configuracion del repo; se deshace igual)"
desde_donde: "PC del mantenedor, Git Bash, gh logueado como villadalmine"
depende_de: [gh CLI]
docs: [CONTRIBUTING.md]
tags: [github, ruleset, squash, main]
---

# Protect `main`: squash only, ruleset, delete branches on merge

**Where to stand:** the maintainer's PC, Git Bash, `gh` logged in as `villadalmine`.

**Status (2026-10-16): step 1 is applied; step 2 is NOT.** GitHub answers
`403 Upgrade to GitHub Pro or make this repository public to enable this feature` to a ruleset or branch
protection on a private repository of a free account. The ruleset is kept as code in
[`.github/rulesets/main.json`](../../.github/rulesets/main.json) and is applied the day the repository is public
(the maintainer's OK) or the account has Pro. Until then the rules are followed by hand: PR only, `verify` and
`pr-format` green, squash, no force push.

## Step 0: backup

```bash
gh api repos/villadalmine/edge-mesh-lab > ~/backups/edge-mesh-lab-repo-config-antes.json
gh api repos/villadalmine/edge-mesh-lab/rulesets > ~/backups/edge-mesh-lab-rulesets-antes.json
```

## Step 1: squash only, PR title as commit, delete the branch on merge, no wiki

```bash
gh api -X PATCH repos/villadalmine/edge-mesh-lab -F allow_squash_merge=true -F allow_merge_commit=false \
  -F allow_rebase_merge=false -F delete_branch_on_merge=true -F has_wiki=false \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY \
  --jq '.allow_squash_merge,.allow_merge_commit,.allow_rebase_merge,.delete_branch_on_merge'
```

Expected: `true false false true`.

## Step 2: the ruleset (needs a public repo or Pro)

```bash
gh api -X POST repos/villadalmine/edge-mesh-lab/rulesets --input <repo-root>/.github/rulesets/main.json --jq '.id,.enforcement'
```

Expected: an id and `active`. Verify: `gh api repos/villadalmine/edge-mesh-lab/rulesets --jq '.[].name'` lists `main`.
Admins (repository role 5) may bypass with a pull request, for an emergency.

## Rollback

```bash
gh api -X DELETE repos/villadalmine/edge-mesh-lab/rulesets/<id>
gh api -X PATCH repos/villadalmine/edge-mesh-lab -F allow_merge_commit=true -F allow_rebase_merge=true -F delete_branch_on_merge=false
```
