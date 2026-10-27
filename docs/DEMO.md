---
tipo: runbook
id: edge-mesh-lab-demo
version: 0.1.0
fecha_alta: 2026-10-21
fecha_ultimo_cambio: 2026-10-27
categoria: plataforma
dueno_agente: casa-orquestador
pieza: edge-mesh-lab
meta: ../meta.yml
depende_de: [docs/DESIGN.md, docs/HOST-YOUR-OWN.md, poc/pool.yaml]
lo_usan: [README.md]
docs: [docs/GOLDEN-RULES.md, docs/POC-STATUS.md]
scripts: [poc/pool.py, poc/pool.yaml, poc/issue.sh, poc/revoke.sh]
runbooks: []
tags: [demo, publico, pool, diseno]
---

# The public demo (lighthouse and control plane applied; admission, pool and opening not built)

**Status (2026-10-27): the lighthouse and the control plane are up; the admission page, the pool's cluster side and the public opening are not.**
Applied on the maintainer's own machines: steps 1 and 2 below (a lighthouse on a small public server with its public name) and a persistent control plane
at home, reachable from the internet through that lighthouse. A visitor on a network that is not the maintainer's reached the control plane's API over
Nebula (a test run from outside on 2026-10-27; the home router keeps the UDP source port, so hole punching works
without a relay). **Not built:** the demo page, admission, the reaper that expels at lease end, quotas (issue #11) and the relay for symmetric NAT (issue #15).
So nobody can join the pool yet, and this repository is still private: making it public needs the maintainer's explicit "open it". The PoC itself runs
locally and on the maintainer's lab VMs.

The idea in one picture: a small public server with a few slots that rotate. Anyone can try the mesh for one hour, with a fixed number of
slots, no persistence, and no way to reach anything of the host's.

```mermaid
flowchart LR
  V["visitor"] -->|"HTTPS: demo page, slot status, request an invite"| E["border / reverse proxy"]
  E --> A["demo app: admission + pool logic (pool.py)"]
  A -->|"signs with the POOL CA (online key, group public only)"| B["bundle, 1 h certificate, one-time token"]
  V -->|"Nebula UDP 4242"| L["lighthouse (public address)"]
  V -.->|"inside the mesh only"| C["control plane (API on its Nebula IP, never public)"]
  A --> R["reaper: at lease end kick, blocklist, free the slot"]
```

## What is public and what never is

| Public | Never public |
| :--- | :--- |
| the demo page (HTTPS), the slot status | the Kubernetes API (reachable only over Nebula) |
| the lighthouse, **UDP 4242** | the persistent CA key (offline); SSH and any management port |
| | anything of the maintainer's home network |

The demo uses the **pool CA** only. Its key sits with the automatic issuer and can mint `public` certificates and nothing else (the CA is
created with `-groups public`, proved in `poc-ca.sh`). If it leaks, you drop the pool CA from the trust bundle and only the pool goes down
(`B10`-`B13`); persistent friends are untouched.

The lighthouse server is public, and today it is also a full member of the mesh (a `nebula1` address, outbound `any`): whoever takes it over can reach the
Kubernetes API over Nebula, where a credential is still required. Running it as an introducer only (`tun: {disabled: true}`) is a hardening step not done yet
(DESIGN.md section 6, issue #20).

## Configuration (the numbers live in `poc/pool.yaml`)

| Setting | PoC value | Meaning |
| :--- | :--- | :--- |
| `slots` | 1 | at most N participant nodes; a join over capacity goes to a FIFO waiting list |
| `namespaces` | 1 | at most M demo namespaces |
| `lease_seconds` | 3600 | the 1 h lease; the Nebula certificate has the same expiry |
| `cooldown_seconds` | 3600 | an IP waits this long after its lease |
| `rate_window_seconds` / `rate_max_requests` | 600 / 5 | per-IP invite rate limit |
| `idle_kick_seconds` | 900 | NotReady or no workload for this long: evicted early |
| `trusted_proxies` | the reverse proxy | only this address may set `X-Forwarded-For` |

Also in the design: ResourceQuota and LimitRange per demo namespace (no LoadBalancer, no NodePort), one node per invite, at most one active lease
per source IP. IP limits are weak (carrier NAT hurts honest users, a VPN bypasses them): they stop casual abuse. An optional GitHub-OAuth lane
would limit per account.

**What exists today:** the decision logic (`pool.py`, unit tests: the 1 h lease, per-IP rules, the FIFO list) and the CA split. **What does not:**
the cluster side (reaper, quotas, idle kick, the page), the admission service, and the relay for symmetric NAT (issues #11 and #15). The demo needs these built and
tested before the checklist below can pass.

## The runbook (do not run without the OK)

Each step is a separate change with its own rollback; the maintainer approves each one.

**Prerequisite: the go-public checklist (issue #16).**
- [ ] `make poc-join`, `poc-ca`, `poc-placement` and the Cilium suite green and identical on two runs
- [ ] the README commands verified from a clean clone
- [ ] `SECURITY.md` present; branch ruleset ready; git history scanned for secrets
- [ ] the pool's cluster side built and tested; the persistent CA key offline; the pool CA online key stored as a protected secret

**1. A small public server for the lighthouse.** A VPS with a stable address, `nebula` 1.11.2, the `lh` bundle, **UDP 4242** open and nothing else except SSH from known addresses.
Rollback: destroy the VPS; no bundle is valid without a lighthouse. **Automated and tested on a VM: `poc/lighthouse-vps.sh` (`make poc-lighthouse`, 23 checks); applied on the maintainer's server 2026-10-27.**

**2. A public name for the lighthouse and the demo page** (a subdomain). *Needs the maintainer's OK: DNS.* Rollback: remove the record. **Done 2026-10-27** (one `A` record, TTL 300).

**3. The demo page behind the border** (HTTPS reverse proxy to the admission app; the app trusts `X-Forwarded-For` only from the proxy's address). *Needs the
maintainer's OK: the home border.* Rollback: remove the route; the page disappears, the mesh keeps working for current leases.

**4. If the control plane stays at home:** a NAT/port-forward is **not** required for the control plane, because nodes reach it over Nebula via the
lighthouse. Only the lighthouse (UDP 4242) is public. If the lighthouse is at home instead of a VPS, forwarding UDP 4242 through NAT is the single
change to the edge and needs the OK. Rollback: remove the forward.

**5. Switch on admission** (slots 1 to start, lease 1 h). Watch the page and `mesh host players`. Rollback: set `slots` to 0 and `mesh host kick` the active leases.

**6. Abuse switch.** One command kicks everyone and stops new invites: `slots: 0` plus `./revoke.sh <name>` per node. Practise it before announcing.

## Limits and honest warnings

- A visitor's machine is `public`: private workloads never land on it, and the visitor can see whatever runs on it (golden rule). Demo workloads must be shareable only.
- 1 hour is a hard lease. Nothing persists. No Secrets, no hostPath, no privileged pods on shared nodes (Kyverno, section 5 of DESIGN.md).
- The demo shows the mesh and the placement rule; it is not a compute marketplace.
