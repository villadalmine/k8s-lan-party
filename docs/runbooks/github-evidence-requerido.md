---
tipo: runbook
id: k8s-lan-party-github-evidence-requerido
version: 1.0.0
fecha_alta: 2026-11-04
fecha_ultimo_cambio: 2026-11-04
categoria: repo
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: meta.yml
operacion: configurar
requires_maintainer_ok: si
riesgo: "bajo (config del repo; se deshace con el respaldo)"
desde_donde: "the maintainer's PC, gh logged in as villadalmine"
depende_de: [gh CLI, jq]
docs: [CONTRIBUTING.md, .github/rulesets/main.json]
scripts: [scripts/evidence.py]
tags: [github, ruleset, evidence, pr-format]
---

# Require the `evidence` check on `main` (issue #79)

**Where to stand:** the maintainer's PC, `gh` logged in as `villadalmine`, any directory (the commands use no relative paths
except the repo file in step 4). **Do it after** the PR that adds `scripts/evidence.py` is merged and at least one PR has shown an
`evidence` check run (`gh api repos/villadalmine/k8s-lan-party/commits/<sha>/check-runs --jq '.check_runs[].name'`): a required
check that never ran blocks every PR. The ruleset is `main`, id `25951412`.

Why: `pr-format` only warns while the description says `CI: pending`, so it cannot stop that merge. `evidence` stays `in_progress`
while no green verify run of the head is cited, which blocks the merge without leaving a failure in `filter=all`.

## Step 0: backup

```bash
mkdir -p ~/backups && gh api repos/villadalmine/k8s-lan-party/rulesets/25951412 > ~/backups/k8s-lan-party-ruleset-main-antes.json
```

## Step 1: add `evidence` to the required checks

```bash
gh api repos/villadalmine/k8s-lan-party/rulesets/25951412 \
  | jq '{name, target, enforcement, conditions, bypass_actors,
         rules: (.rules | map(if .type == "required_status_checks"
                 then .parameters.required_status_checks += [{"context": "evidence"}] else . end)
                 | map(del(.id)))}' > ~/backups/k8s-lan-party-ruleset-main-nuevo.json
jq '.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks' ~/backups/k8s-lan-party-ruleset-main-nuevo.json
```

Expected: `verify`, `pr-format` and `evidence`. Apply:

```bash
gh api -X PUT repos/villadalmine/k8s-lan-party/rulesets/25951412 --input ~/backups/k8s-lan-party-ruleset-main-nuevo.json \
  --jq '.id, (.rules[] | select(.type=="required_status_checks") | [.parameters.required_status_checks[].context])'
```

## Step 2: verify

On an open PR, the checks list shows `evidence` as required; a PR with `CI: pending` shows it in progress and cannot be merged,
and merges after the edit that cites the green run.

## Step 3: rollback

```bash
gh api -X PUT repos/villadalmine/k8s-lan-party/rulesets/25951412 --input ~/backups/k8s-lan-party-ruleset-main-antes.json
```

(If the PUT rejects the file's `id`/`source` fields, delete them: `jq 'del(.id,.source,.source_type,.node_id,._links,.created_at,.updated_at,.current_user_can_bypass)'`.)

## Step 4: keep the code in sync

Add `{"context": "evidence"}` to `required_status_checks` in `.github/rulesets/main.json` by a pull request, so the file stays the
record of what is applied.
