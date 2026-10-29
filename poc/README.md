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
scripts: [poc/poc-join.sh, poc/poc-host.sh, poc/poc-weakness.sh, poc/poc-ca.sh, poc/poc-revoke.sh, poc/poc-placement.sh, poc/poc-cilium.sh, poc/poc-lighthouse.sh, poc/kind-cilium.sh, poc/vm/poc-vm.sh, poc/vm/run-all.sh, poc/up.sh, poc/down.sh, poc/issue.sh, poc/revoke.sh, poc/mesh, poc/edge-mesh, poc/lighthouse-vps.sh, poc/pool.py, poc/admission.py, poc/poc-admission.sh, poc/poc-admission-auto.sh, poc/participants.py, poc/poc-participants.sh, poc/poc-participants-e2e.sh, poc/poc-nat.sh, poc/poc-golden.sh, poc/golden.py, poc/game-env.sh]
tags: [poc, determinista, nebula, k3s, cilium, kyverno, poc/vm/poc-bgp.sh, poc/gateway.sh]
tags: [poc, determinista, nebula, k3s, cilium, kyverno, bgp, gateway]
---

# poc/: the deterministic proof of concept

A proof of concept, not a product: it shows on one machine (Docker for the basic flow, Incus VMs for the full matrix) that the design works.
Versions are pinned in `stack.env`; every step prints `PASS` or `FAIL`, and the output is identical across runs (numbers go to the state
directory). Where each suite passed and how often is in [docs/POC-STATUS.md](../docs/POC-STATUS.md).

| Command | Script | Checks | What it proves |
| :--- | :--- | :--- | :--- |
| `make poc-join` | `poc-join.sh` | 17 (J01-J17) | **step 1**: Nebula CA + lighthouse + control plane; two outside nodes join with one bundle and one command; a study lab (CKA 1.1) runs on them; narrowed firewall, token reuse, expired certificate, blocked UDP and revocation behave |
| `make poc-host` | `poc-host.sh` | 11 (H01-H11) | **host your own game**: `mesh host init / invite / players / kick / down` |
| `make poc-golden` | `poc-golden.sh` | 29 (G1.1-G10.1; 26 run, 3 wait for #8, #9 and #13) | **golden path**: one check per golden rule against a game that is already up (yours or a stranger's); `GOLDEN_ENV="<column>"` writes the table in POC-STATUS.md; it builds nothing |
| `make poc-weakness` | `poc-weakness.sh` | 8 (W00-W06, W03b) | what one control plane costs: flapping, tolerance, a lossy link, control plane down, the supervised k3s agent |
| `make poc-ca` | `poc-ca.sh` | 13 (B01-B13) | two CAs (persistent and pool); the pool CA is constrained to group `public`; pulling it cuts only the pool |
| `make poc-revoke` | `poc-revoke.sh` | 10 (R01-R10) | `mesh host revoke` is one command and idempotent; the negative test (`kubectl delete node` without the blocklist does not revoke, R03); time to loss of access |
| `make poc-placement` | `poc-placement.sh` | 31 (L01-L53, L52b), 15 of them negative | **the golden rule**: Kyverno trust-based placement plus a node-side `ValidatingAdmissionPolicy` |
| `make poc-cilium` | `poc-cilium.sh` | 23 (C01-C97) | **step 2** on k3s in Docker: Cilium tunnel + WireGuard over Nebula, encryption on the wire, SPIRE mutual auth (non-root, PVC); the deny half and the CiliumNode guard are in `poc-vm` only |
| `make poc-vm` | `vm/poc-vm.sh` | 34 (V01-V103) | step 2 on Incus VMs, each with its own kernel, remote nodes tainted as in a real join: `cilium-dbg encrypt status` with every other node a peer, the MTU set once, a remote node's Cilium token cannot rewrite another node's WireGuard key (V06c), mutual auth allows an authenticated peer and drops an unauthenticated one (V98-V102) |
| `make poc-bgp` | `vm/poc-bgp.sh` | 20 (G01-G19, G09b), 6 of them negative | **issue #13**: Envoy Gateway and cert-manager only on `own` nodes, the listener's certificate issued from the Gateway by itself, Cilium BGP only from `own` nodes to a test FRR router; a remote node never announces (no BGP instance, cannot select itself, its Cilium token writes no BGP object, the router refuses it) (Incus VMs) |
| `make poc-kind` | `kind-cilium.sh` | 20 (K01-K97) | step 2 on kind (no Nebula): Cilium itself works on this kernel |
| `make poc-lighthouse` | `poc-lighthouse.sh` | 23 (P01-P18, P09b, P12b-P12e) | a lighthouse on a separate public server (a VM plays it): `lighthouse-vps.sh` prepare / sign / deploy idempotent, a player joins through it, `revoke` reaches it; `mesh host refresh` with no change, with a change that needs a restart (the API comes back) and with one read on reload |
| `make poc-admission` | `poc-admission.sh` | 31 (A01-A31) | **admission with manual approval** ([docs/ADMISSION.md](../docs/ADMISSION.md)): request, approve, join, a burned join token refused to a second machine, a non-cooperative copy of a bundle refused by the node guard, another guest's name refused by its node password, a name whose node password k3s dropped refused, places full, the lease ends, `reap` expels, the next one in line enters, `reap --all` (Incus VMs) |
| `make poc-admission-auto` | `poc-admission-auto.sh` | 37 (AA01-AA37) | **the automatic pool, phase B1** ([docs/ADMISSION.md](../docs/ADMISSION.md)): `auto on`, one tick approves two and the third waits, the outbox bundle, both join, a copy of a bundle refused by the node guard, one tick expels at the end (no stray Node left) and only then promotes, the copy's tunnel cut, a burned token refused, auto off only expels, the daily cap refuses a third request, `panic`, the notify hook in order, the metrics (Incus VMs) |
| `make poc-participants` | `poc-participants.sh` | 33 (S00-S30, S09b, S28b) | **join by signed PR** (issue #12, [participants/README.md](../participants/README.md)), offline: the real cosign against a **local TEST CA** playing Fulcio; the PR gate (identity, account id, schema, unique node, trust ceiling from the base policy) passes a valid PR and refuses another identity, a key instead of keyless, an unsigned or tampered file, a replay, trust `own` (never through a PR), a removal by a re-registered login; the issuing side (`participants.py sync`, the real `issue.sh`/`revoke.sh` against a stub control plane) re-verifies main, delivers the bundle age-encrypted and cosign-signed, refuses a forged one, and a removal revokes and a failed revoke blocks the new certificate |
| `make poc-participants-e2e` | `poc-participants-e2e.sh` | 11 (E01-E11) | the same on Incus VMs: the participant's keys made on her machine, she opens her bundle and joins (Ready), her file leaves main, one sync and she reaches neither the lighthouse nor the API |
| `make poc-nat` | `poc-nat.sh` | 26 (N01-N26) | **relay and NAT** (issue #15, Incus): two routers, the control plane behind one, a player behind the other; port-preserving NAT gives a direct tunnel; symmetric NAT on both sides gives no path without a relay (negative); `lighthouse-vps.sh sign --relay`: the player reaches the control plane through the lighthouse, which really carries the bytes and does not bypass the far end's firewall; a real player joins, Ready, the API reaches its kubelet; `revoke` cuts it, relay included (and exits 5 when the lighthouse could not be told). Latency, bandwidth and the relay's cost in `results/nat-measurements.txt` (DESIGN.md 15.1). `poc-nat.sh down` removes it |
| `vm/run-all.sh` | | 89 | join, host, weakness, CAs, revoke and placement on Incus VMs in one go |
| `make poc-down` | `down.sh` | | remove every container, network and the state |
| `make test` | `test_pool.py`, `test_admission.py`, `test_admission_auto.py`, `../tests/` | | the pool logic (1 h lease, per-identity rules, daily cap, FIFO waitlist), the admission ledger (the file, the lock, names), the automatic tick (switch, panic, events and hook, outbox, metrics, 8 concurrent ticks), the participants' schema, policy, PR gate and issuing plan, the PR gates, every PoC script keeps its executable bit, two state directories do not touch each other |

Not covered by a check yet: macOS (issue #14) and the owner of a joined node claiming another workload's identity toward mutual auth (docs/DESIGN.md section 3.3). Native
WSL2 as a node was run by hand (docs/REQUIREMENTS.md, the host matrix); the refusal of a joiner without root or `/dev/net/tun` is
`tests/test_join_refusal.py`.

## What is here

| File | What |
| :--- | :--- |
| `stack.env` | pinned versions and addresses |
| `lib.sh` | shared helpers (`step`, `wait_for`, containers and VMs, Nebula reload and restart). `EMESH_STATE` picks the state directory (default `poc/.state`) |
| `up.sh`, `down.sh` | bring the Docker flow up (idempotent) and tear it down |
| `issue.sh` | the host's side of a join: sign a Nebula certificate (persistent or pool CA), create a short-lived join token, write the bundle |
| `edge-mesh` | the participant's one command: `edge-mesh join <bundle> --accept-golden-rule` (supervises the k3s agent) |
| `revoke.sh` | revocation in one command: blocklist, reload every host (and the external lighthouse), delete the node |
| `mesh` | the host's CLI: `host init / invite / players / revoke / kick / refresh / down`, the admission commands `host request / waitlist / approve / reject / leases / reap / admission-status` and the automatic pool `host auto --once / auto on / auto off / auto status / panic / bundle / admission-metrics`, and `play` (local simulation of a player) |
| `lighthouse-vps.sh` | a lighthouse on a separate public server: `prepare` (key made on the server), `sign`, `deploy`, `install`, `verify`, `sync`, `remove` |
| `kc` | `kubectl` against the PoC control plane, whatever the runtime |
| `install-placement.sh`, `placement/` | install Kyverno and the trust-placement policies on your own control plane |
| `cilium.sh`, `checks-cilium.sh`, `spire-args.sh` | step 2: the Cilium install (`BGP=yes` adds the BGP control plane, `MUTUAL=yes` SPIRE), its assertions (shared by Docker and VMs), the hardened SPIRE arguments |
| `node-trust-freeze.yaml` | only the host (`system:masters`) may change a node's `node-restriction.kubernetes.io/` labels or `trust` taint, on a Node or its status: closes the k3s controller credential on every joined machine; `up.sh` applies it |
| `cilium-node-guard.yaml` | a Cilium agent's token may write only the `CiliumNode` of its own node (where the node publishes its WireGuard key); `cilium.sh` applies it |
| `gateway.sh`, `gateway/` | issue #13: `gateway.sh bgp` (BGP from own nodes only, the pool by Service label, `gateway/bgp.yaml`), `install` (Envoy Gateway, cert-manager, a local CA and the Gateway, all on own nodes, `gateway/gateway.yaml`), `router` (the test FRR router, `gateway/frr.conf`) |
| `kind-cilium.sh` | step 2 on kind |
| `vm/` | the Incus VM flow: `up.sh`, `down.sh`, `lib-vm.sh`, `poc-vm.sh`, `poc-bgp.sh`, `poc-placement.sh`, `run-all.sh` |
| `pool.py`, `pool.yaml`, `test_pool.py` | the capacity pool for the public demo (pure logic, simulated time) |
| `admission.py`, `test_admission.py`, `test_admission_auto.py`, `poc-admission.sh`, `poc-admission-auto.sh` | admission with manual approval and the automatic tick (B1): the ledger behind `mesh host request / approve / reap` |
| `participants.py`, `test_participants.py`, `poc-participants.sh`, `poc-participants-e2e.sh`, `participants-tools.sh`, `participants-live-smoke.sh` | join by signed PR: `check` (the CI gate), `validate`, `sync` (the issuing side, on the maintainer's host), `open` (the participant), `lint`; the suites and the pinned cosign, age and nebula-cert they fetch; the CI job that runs the verification code against the public Sigstore |
| `sigstore_test_ca.py` | **TEST ONLY**: a local CA that plays Fulcio for the offline fixture (keys made in the state directory, never committed) |
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
- (2026-10-28) k3s 1.37 installs the Gateway API CRDs itself (standard channel) together with Gateway API's `safe-upgrades` policy, which
  refuses experimental CRDs on top: Envoy Gateway's main chart (experimental channel) goes in with `--skip-crds`, its own CRDs from its CRD
  chart, rendered and applied server-side (as a Helm release they pass the 1 MiB limit of the release Secret).
- (2026-10-28) In the VM flow `k` is `incus exec`, which reads its standard input: a `$(k ...)` inside the arguments of a command that reads a
  pipe swallows the pipe (`echo "$out" | grep "$(k get ...)"` never matches). Read the value first, then pipe.
- (2026-10-28) The SPIRE agent did not tolerate the `trust` taint, so on tainted remote nodes (a real join) pods got no identity and every
  policy requiring authentication dropped them. `spire-args.sh` adds the toleration; `poc-vm` now runs with tainted remote nodes.
