---
tipo: runbook
id: k8s-lan-party-github-proteger-main
version: 1.0.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-27
categoria: repo
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: meta.yml
operacion: configurar
requires_maintainer_ok: si
riesgo: "bajo (solo configuracion del repo; se deshace igual)"
desde_donde: "the maintainer's PC, Git Bash, gh logged in as villadalmine"
depende_de: [gh CLI]
docs: [CONTRIBUTING.md]
tags: [github, ruleset, squash, main]
---

# Protect `main`: squash only, ruleset, delete branches on merge

**Where to stand:** the maintainer's PC, Git Bash, `gh` logged in as `villadalmine`.

**Status (2026-10-27): both steps are applied.** On 2026-10-16 step 2 was not: GitHub answers
`403 Upgrade to GitHub Pro or make this repository public to enable this feature` to a ruleset or branch
protection on a private repository of a free account. The repository became public on 2026-10-27 and the ruleset,
kept as code in [`.github/rulesets/main.json`](../../.github/rulesets/main.json), is `active` since then
(`gh api repos/villadalmine/k8s-lan-party/rulesets` lists `main`).

## Step 0: backup

```bash
gh api repos/villadalmine/k8s-lan-party > ~/backups/k8s-lan-party-repo-config-antes.json
gh api repos/villadalmine/k8s-lan-party/rulesets > ~/backups/k8s-lan-party-rulesets-antes.json
```

## Step 1: squash only, PR title as commit, delete the branch on merge, no wiki

```bash
gh api -X PATCH repos/villadalmine/k8s-lan-party -F allow_squash_merge=true -F allow_merge_commit=false \
  -F allow_rebase_merge=false -F delete_branch_on_merge=true -F has_wiki=false \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY \
  --jq '.allow_squash_merge,.allow_merge_commit,.allow_rebase_merge,.delete_branch_on_merge'
```

Expected: `true false false true`.

## Step 2: the ruleset (needs a public repo or Pro)

```bash
gh api -X POST repos/villadalmine/k8s-lan-party/rulesets --input .github/rulesets/main.json --jq '.id,.enforcement'
```

Expected: an id and `active`. Verify: `gh api repos/villadalmine/k8s-lan-party/rulesets --jq '.[].name'` lists `main`.
Admins (repository role 5) may bypass with a pull request, for an emergency.

## Rollback

```bash
gh api -X DELETE repos/villadalmine/k8s-lan-party/rulesets/<id>
gh api -X PATCH repos/villadalmine/k8s-lan-party -F allow_merge_commit=true -F allow_rebase_merge=true -F delete_branch_on_merge=false
```
