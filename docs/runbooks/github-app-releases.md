---
tipo: runbook
id: k8s-lan-party-github-app-releases
version: 1.0.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-16
categoria: repo
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: meta.yml
operacion: mantenimiento
requires_maintainer_ok: si
riesgo: "An App with Contents and Pull requests write can write to the repo: if its private key leaks, anyone holding it can open PRs and tags. So: installed on this repo only, no Workflows or Administration permission, the key lives only as a repo secret (never in cloud storage or chat) and is rotated."
desde_donde: "the maintainer's PC: browser with the villadalmine GitHub session, and Git Bash"
depende_de: [gh CLI]
docs: [docs/AUTOMATION-AND-CREDENTIALS.md]
tags: [github-app, release-please, secretos]
---

# Create the release GitHub App for k8s-lan-party (step 9, waiting for the maintainer)

**Why.** release-please opens a Release PR (bumps the version, writes the changelog; merging it creates the tag
and the GitHub Release). A PR opened with the default `GITHUB_TOKEN` starts no workflows, so `verify` and
`pr-format` would never run and the Release PR could not merge. The fix is a GitHub App **of this repo only**
(do not reuse the study-cybercirujas one: each repo gets its own App so one leaked key reaches one repo).

Names the release workflow will expect (do not change them): `RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY`.
The workflow, the release-please config and manifest, and the `check_pr` exemption for the Release PR are a
later PR, opened once this runbook is done.

## Steps

0. **Backup.** `gh secret list -R villadalmine/k8s-lan-party` (names only). Expected: no `RELEASE_APP_*`.
1. **Create the App.** Open https://github.com/settings/apps/new (personal account, not an organisation).
2. **Form, exact values.** GitHub App name `k8s-lan-party-releases`; Homepage URL
   `https://github.com/villadalmine/k8s-lan-party`; Webhook: untick *Active*; Repository permissions:
   **Contents: Read and write**, **Pull requests: Read and write**, Metadata: Read-only (GitHub sets it),
   nothing else; no account or organisation permissions; installable *Only on this account*. Note the **App ID**
   (a number, not a secret).
3. **Private key.** *Private keys* -> *Generate a private key*. A `.pem` lands in Downloads: the only copy; not
   to cloud storage, not to a chat.
4. **Install on this repo only.** *Install App* -> villadalmine -> *Only select repositories* -> `k8s-lan-party`.
5. **Store as repo secrets** (gh does not print values):
   `gh secret set RELEASE_APP_ID -R villadalmine/k8s-lan-party --body '<App ID>'`
   `gh secret set RELEASE_APP_PRIVATE_KEY -R villadalmine/k8s-lan-party < ~/Downloads/<the>.pem`
6. **Keep and delete the `.pem`.** Attach it to a KeePassXC entry `k8s-lan-party-releases (GitHub App)`, then delete the file.
7. **Verify.** `gh secret list -R villadalmine/k8s-lan-party` shows both names with a date. Tell the main session.

## Rollback

`gh secret delete RELEASE_APP_ID -R villadalmine/k8s-lan-party` (and the key); uninstall at
https://github.com/settings/installations; delete the App at https://github.com/settings/apps/k8s-lan-party-releases
(*Advanced* -> *Delete GitHub App*). Rotate the key yearly: generate a new one, repeat steps 5 and 6, delete the old one.
