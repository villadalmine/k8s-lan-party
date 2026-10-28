---
tipo: doc
id: k8s-lan-party-poc
version: 0.3.0
fecha_alta: 2026-10-19
fecha_ultimo_cambio: 2026-10-28
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
depende_de: [docs/DESIGN.md]
lo_usan: [README.md]
docs: [docs/DESIGN.md, docs/GOLDEN-RULES.md, docs/POC-STATUS.md, docs/HOST-YOUR-OWN.md, docs/JOIN.md, docs/TROUBLESHOOTING.md]
scripts: [poc/poc-join.sh, poc/poc-host.sh, poc/poc-weakness.sh, poc/poc-ca.sh, poc/poc-revoke.sh, poc/poc-placement.sh, poc/poc-cilium.sh, poc/poc-lighthouse.sh, poc/kind-cilium.sh, poc/vm/poc-vm.sh, poc/vm/run-all.sh, poc/up.sh, poc/down.sh, poc/issue.sh, poc/revoke.sh, poc/mesh, poc/edge-mesh, poc/lighthouse-vps.sh, poc/pool.py, poc/admission.py, poc/poc-admission.sh, poc/poc-admission-auto.sh, poc/poc-pool.sh, poc/participant.py, poc/participant-policy.yaml, poc/participants.py, poc/poc-participants.sh, poc/poc-participants-e2e.sh]
tags: [poc, determinista, nebula, k3s, cilium, kyverno]
---

# poc/: the deterministic proof of concept

A proof of concept, not a product: it shows on one machine (Docker for the basic flow, Incus VMs for the full matrix) that the design works.
Versions are pinned in `stack.env`; every step prints `PASS` or `FAIL`, and the output is identical across runs (numbers go to the state
directory). Where each suite passed and how often is in [docs/POC-STATUS.md](../docs/POC-STATUS.md).

| Command | Script | Checks | What it proves |
| :--- | :--- | :--- | :--- |
| `make poc-join` | `poc-join.sh` | 17 (J01-J17) | **step 1**: Nebula CA + lighthouse + control plane; two outside nodes join with one bundle and one command; a study lab (CKA 1.1) runs on them; narrowed firewall, token reuse, expired certificate, blocked UDP and revocation behave |
| `make poc-host` | `poc-host.sh` | 11 (H01-H11) | **host your own game**: `mesh host init / invite / players / kick / down` |
| `make poc-weakness` | `poc-weakness.sh` | 8 (W00-W06, W03b) | what one control plane costs: flapping, tolerance, a lossy link, control plane down, the supervised k3s agent |
| `make poc-ca` | `poc-ca.sh` | 13 (B01-B13) | two CAs (persistent and pool); the pool CA is constrained to group `public`; pulling it cuts only the pool |
| `make poc-revoke` | `poc-revoke.sh` | 10 (R01-R10) | `mesh host revoke` is one command and idempotent; the negative test (`kubectl delete node` without the blocklist does not revoke, R03); time to loss of access |
| `make poc-placement` | `poc-placement.sh` | 30 (L01-L53), 14 of them negative | **the golden rule**: Kyverno trust-based placement plus a node-side `ValidatingAdmissionPolicy` |
| `make poc-cilium` | `poc-cilium.sh` | 23 (C01-C97) | **step 2** on k3s in Docker: Cilium tunnel + WireGuard over Nebula, encryption on the wire, SPIRE mutual auth (non-root, PVC) |
| `make poc-vm` | `vm/poc-vm.sh` | 24 (V01-V97) | step 2 on Incus VMs, each with its own kernel |
| `make poc-kind` | `kind-cilium.sh` | 20 (K01-K97) | step 2 on kind (no Nebula): Cilium itself works on this kernel |
| `make poc-lighthouse` | `poc-lighthouse.sh` | 23 (P01-P18, P09b, P12b-P12e) | a lighthouse on a separate public server (a VM plays it): `lighthouse-vps.sh` prepare / sign / deploy idempotent, a player joins through it, `revoke` reaches it; `mesh host refresh` with no change, with a change that needs a restart (the API comes back) and with one read on reload |
| `make poc-admission` | `poc-admission.sh` | 31 (A01-A31) | **admission with manual approval** ([docs/ADMISSION.md](../docs/ADMISSION.md)): request, approve, join, a burned join token refused to a second machine, a non-cooperative copy of a bundle refused by the node guard, another guest's name refused by its node password, a name whose node password k3s dropped refused, places full, the lease ends, `reap` expels, the next one in line enters, `reap --all` (Incus VMs) |
| `make poc-admission-auto` | `poc-admission-auto.sh` | 37 (AA01-AA37) | **the automatic pool, phase B1** ([docs/ADMISSION.md](../docs/ADMISSION.md)): `auto on`, one tick approves two and the third waits, the outbox bundle, both join, a copy of a bundle refused by the node guard, one tick expels at the end (no stray Node left) and only then promotes, the copy's tunnel cut, a burned token refused, auto off only expels, the daily cap refuses a third request, `panic`, the notify hook in order, the metrics (Incus VMs) |
| `make poc-participants` | `poc-participants.sh` | 33 (S00-S30, S09b, S28b) | **join by signed PR** (issue #12, [participants/README.md](../participants/README.md)), offline: the real cosign against a **local TEST CA** playing Fulcio; the PR gate (identity, account id, schema, unique node, trust ceiling from the base policy) passes a valid PR and refuses another identity, a key instead of keyless, an unsigned or tampered file, a replay, trust `own` (never through a PR), a removal by a re-registered login; the issuing side (`participants.py sync`, the real `issue.sh`/`revoke.sh` against a stub control plane) re-verifies main, delivers the bundle age-encrypted and cosign-signed, refuses a forged one, and a removal revokes and a failed revoke blocks the new certificate |
| `make poc-participants-e2e` | `poc-participants-e2e.sh` | 11 (E01-E11) | the same on Incus VMs: the participant's keys made on her machine, she opens her bundle and joins (Ready), her file leaves main, one sync and she reaches neither the lighthouse nor the API |
| `vm/run-all.sh` | | 89 | join, host, weakness, CAs, revoke and placement on Incus VMs in one go |
| `make poc-down` | `down.sh` | | remove every container, network and the state |
| `make test` | `test_pool.py`, `test_admission.py`, `test_admission_auto.py`, `../tests/` | | the pool logic (1 h lease, per-identity rules, daily cap, FIFO waitlist), the admission ledger (the file, the lock, names), the automatic tick (switch, panic, events and hook, outbox, metrics, 8 concurrent ticks), the participants' schema, policy, PR gate and issuing plan, the PR gates, every PoC script keeps its executable bit, two state directories do not touch each other |
| `make poc-pool` | `poc-pool.sh` | 29 (PL01-PL25, PL07b, PL09b, PL13b, PL21b) | **the pool's cluster side and the player's own access** (issues #9, #11): a namespace `play-<name>` per player (Pod Security restricted, quota, limits, NetworkPolicy, the participant guard: private pods pinned to the player's own node) and a kubeconfig whose token lives exactly the lease, kept apart from the node credential; the error matrix rows 6, 7, 8, 9 and 11 with their messages and exit codes (`edge-mesh kubectl`); no secret, node or other namespace; the idle kick; the second request of an address held while its lease runs and through the cooldown; the rate limit; `X-Forwarded-For`; the lease end removes node, namespace and token and the place is reused (Incus VMs) |
| `vm/run-all.sh` | | 89 | join, host, weakness, CAs, revoke and placement on Incus VMs in one go |
| `make poc-down` | `down.sh` | | remove every container, network and the state |
| `make test` | `test_pool.py`, `test_admission.py`, `test_admission_auto.py`, `test_participant.py`, `test_edge_mesh.py`, `../tests/` | | the pool logic (1 h lease, per-identity rules, daily cap, FIFO waitlist), the admission ledger (the file, the lock, names), the automatic tick (switch, panic, events and hook, outbox, metrics, 8 concurrent ticks, the idle kick, the rate limit), the player's namespace objects and kubeconfig, `edge-mesh kubectl`'s exit codes, the PR gates, every PoC script keeps its executable bit, two state directories do not touch each other |

Not covered by a check yet: the deny half of mutual auth (a peer without a SPIFFE identity is dropped), native WSL2 as a node, macOS,
relays and symmetric NAT (issues #8, #14, #15).

## What is here

| File | What |
| :--- | :--- |
| `stack.env` | pinned versions and addresses |
| `lib.sh` | shared helpers (`step`, `wait_for`, containers and VMs, Nebula reload and restart). `EMESH_STATE` picks the state directory (default `poc/.state`) |
| `up.sh`, `down.sh` | bring the Docker flow up (idempotent) and tear it down |
| `issue.sh` | the host's side of a join: sign a Nebula certificate (persistent or pool CA), create a short-lived join token, write the bundle |
| `edge-mesh` | the participant's one command: `edge-mesh join <bundle> --accept-golden-rule` (supervises the k3s agent, installs the player's kubeconfig) and `edge-mesh kubectl ...` (their own access, with the error matrix's messages and exit codes 10-14) |
| `revoke.sh` | revocation in one command: blocklist, reload every host (and the external lighthouse), delete the node |
| `mesh` | the host's CLI: `host init / invite / players / revoke / kick / refresh / down`, the admission commands `host request / waitlist / approve / reject / leases / reap / admission-status` and the automatic pool `host auto --once / auto on / auto off / auto status / panic / bundle / admission-metrics / activity`, and `play` (local simulation of a player) |
| `lighthouse-vps.sh` | a lighthouse on a separate public server: `prepare` (key made on the server), `sign`, `deploy`, `install`, `verify`, `sync`, `remove` |
| `kc` | `kubectl` against the PoC control plane, whatever the runtime |
| `install-placement.sh`, `placement/` | install Kyverno and the trust-placement policies on your own control plane |
| `cilium.sh`, `checks-cilium.sh`, `spire-args.sh` | step 2: the Cilium install, its assertions (shared by Docker and VMs), the hardened SPIRE arguments |
| `kind-cilium.sh` | step 2 on kind |
| `vm/` | the Incus VM flow: `up.sh`, `down.sh`, `lib-vm.sh`, `poc-vm.sh`, `poc-placement.sh`, `run-all.sh` |
| `pool.py`, `pool.yaml`, `test_pool.py` | the capacity pool for the public demo (pure logic, simulated time) |
| `admission.py`, `test_admission.py`, `test_admission_auto.py`, `poc-admission.sh`, `poc-admission-auto.sh` | admission with manual approval and the automatic tick (B1): the ledger behind `mesh host request / approve / reap` |
| `participants.py`, `test_participants.py`, `poc-participants.sh`, `poc-participants-e2e.sh`, `participants-tools.sh`, `participants-live-smoke.sh` | join by signed PR: `check` (the CI gate), `validate`, `sync` (the issuing side, on the maintainer's host), `open` (the participant), `lint`; the suites and the pinned cosign, age and nebula-cert they fetch; the CI job that runs the verification code against the public Sigstore |
| `sigstore_test_ca.py` | **TEST ONLY**: a local CA that plays Fulcio for the offline fixture (keys made in the state directory, never committed) |
| `participant.py`, `participant-policy.yaml`, `test_participant.py`, `test_edge_mesh.py`, `poc-pool.sh` | the player's namespace `play-<name>` (Pod Security, quota, limits, NetworkPolicy, RBAC), their kubeconfig (TokenRequest, the lease's length), what the idle kick reads (`mesh host activity`), and the participant guard (ClusterIP only, no externalIPs, no `nodeName`, no toleration of the control plane) |
| `node-guard.yaml`, `nodes_sweep.py`, `test_nodes_sweep.py` | only Nodes named after an issued bundle, at its Nebula IP, may register; what `revoke` sweeps |
| `node_password.py`, `test_node_password.py` | the k3s node password the host makes for each bundle name (only the bundle carries it) |
| `labs/cka-1.1/` | a copy of the study lab, from [study-cybercirujas](https://github.com/villadalmine/study-cybercirujas) (Apache-2.0), commit `ec0dd8a` |

## Lo aprendido

- (2026-10-19) The k3s image has no `pkill`: reload Nebula with `kill -HUP $(pidof nebula)`, otherwise the blocklist is written and never applied.
- (2026-10-19) With a replaced entrypoint the k3s image does not move processes out of the cgroup root: kubelet then fails with "cannot enter
  cgroupv2 ... invalid state". `up.sh` does the same move the image's entrypoint does.
- (2026-10-19) `iptables` rules in `DOCKER-USER` do not filter container-to-container traffic on the same bridge; blocking the lighthouse is
  simulated with a blackhole route in the joining container.
- (2026-10-19) The Nebula range must not overlap the pod network (k3s uses 10.42.0.0/16): the PoC uses 10.77.0.0/24.
- (2026-10-19) Cilium on k3s in Docker first left its pods in `CreateContainerError` (CNI paths). Fixed later with a containerd drop-in
  (docs/TROUBLESHOOTING.md); step 2 now passes on Docker, kind and VMs.
- A revoked certificate is refused at the next Nebula handshake ("certificate is in the block list"); `kubectl delete node` alone does
  not keep a node out (R03 shows the machine stays in the mesh).
- (2026-10-27) When Nebula restarts, `nebula1` loses its address and the embedded flannel makes the k3s server exit. `mesh host refresh` therefore
  reloads (SIGHUP) unless `stats` or `listen` changed, and on the control plane stops k3s before the restart and starts it again once `nebula1` is back (P12d).
