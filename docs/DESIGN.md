---
tipo: doc
id: edge-mesh-lab-design
version: 0.3.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-19
categoria: plataforma
dueno_agente: casa-orquestador
pieza: edge-mesh-lab
meta: meta.yml
estado: MVP fase 1 (decisiones del mantenedor 2026-10-16; abiertas al final)
depende_de: []
lo_usan: [docs/GOLDEN-RULES.md, README.md]
docs: [README.md, CONTRIBUTING.md, docs/GOLDEN-RULES.md, docs/REFERENCES.md]
scripts: []
runbooks: []
tags: [diseno, nebula, k3s, cilium, wireguard, kyverno, seguridad, mvp, regla-de-oro]
---

# Design: MVP phase 1 (collaborative labs for learning)

Status: **MVP phase 1, 2026-10-16, private repository.** This is a product, not a study lab. The architecture below
was decided by the maintainer on 2026-10-16; what is still open is listed in section 11. Versions are not pinned
here: they are looked up live and pinned in the PoC's `stack.env`. Claims marked **(prove in the PoC)** are the
ones the proof of concept must confirm or kill. The non-negotiables are in [GOLDEN-RULES.md](GOLDEN-RULES.md).

## 0. Purpose, in one picture: a LAN party for Kubernetes labs

This project is **collaborative labs for learning**. It is not a production compute marketplace and it has no tokens.
The model is a **LAN party**: someone opens a game, friends join over a virtual LAN, and everybody plays (runs their
study labs) on the same network.

| In a LAN game | Here |
| :--- | :--- |
| A **game** (session) | a lab cluster a **host** opens; the host runs the control plane (the "server") and invites a group of friends |
| Entering the server address and password | **joining**: one bundle and one command |
| Hamachi or ZeroTier, "everyone is on the same network" | **Nebula**, the virtual LAN, self-hosted with our own CA |
| A public server with limited slots that rotate | the **free pool**: a fixed number of slots, a 1 h lease, a FIFO waitlist |
| The regulars on a friends list | **persistent participants**, admitted by a signed pull request |
| The game client runs on the player's own PC | **the trust rule**: what runs on someone else's machine can be seen by its owner, so keep private things on your own machine |
| Several game servers, one per region | **phase 2**: several control planes |
| Listing your server so others can find it | **publishing** the public lighthouse and demo, after the private game works |

```mermaid
flowchart LR
  host(["Host: opens a game (control plane)"]) -->|"invite: one bundle"| f1["friend 1"]
  host -->|"invite: one bundle"| f2["friend 2"]
  subgraph lan["virtual LAN (Nebula): everyone looks like they are on the same network"]
    host
    f1
    f2
  end
  f1 --> lab1["runs a study lab"]
  f2 --> lab2["runs a study lab"]
  host --> lab3["runs a study lab"]
```

Security stays honest, in game terms: the host decides who gets an invite; an invite expires; a player can be kicked in one
command; and the machine's **owner** can see what runs on it: a player sees what runs on their own PC, and a friend who lends a PC sees what runs on that PC (the golden rule below).

## 1. Golden rule (MVP)

> **What runs on someone else's machine can be seen by its owner.**

No encryption protects a pod from the owner of the node it runs on, not even TLS terminated inside the pod: the
keys and the plaintext sit in memory on that host, and the owner is root. Kubernetes does not defend a pod from its
own node. Therefore the architecture does not try to hide workloads from node owners; it decides **where
workloads may run**:

- Each **node** declares a trust level: `own`, `friends` or `public`. **Trust is relative to the workload's owner**: a node is `own` for
  the person who owns that machine (label `node-restriction.kubernetes.io/owner=<github-user>`, set by the issuing side, never by the
  kubelet) and `friends` or `public` (the level its owner offers) for everybody else. The maintainer's nodes are `own` for the
  maintainer only. A participant's `private` pods run only on nodes whose `owner` is that participant; a participant with no node of
  their own can run `shareable` workloads only. Later: `confidential` (AMD SEV-SNP or Intel
  TDX with Kata Confidential Containers and attestation; optional, never required).
- Each **workload** declares a sensitivity: `shareable` or `private`. A missing label means `private`.
- By default a user's pods run **only on their own nodes**.
- **Kyverno plus labels and taints enforce the placement**; it does not depend on goodwill.
- **Cilium protects transit and tenant isolation** (deny by default, plus mutual auth by policy). It never claims
  to protect a pod from its host.
- The rule is stated plainly at join time: *"what runs on someone else's machine can be seen by its owner"*.

**Worked example.** A runs a frontend, a backend and a db. The frontend is `shareable` and may run on B's node.
The backend and the db are `private` and stay on A's nodes. B's owner sees the frontend's memory, keys and
traffic, and holds the frontend's identity, so B can act as the frontend toward the backend: the backend must authorize
each request (the network identity alone is not a credential). See diagram 1.

![Who sees what](diagrams/who-sees-what.svg)

*Diagram 1 ([source](diagrams/who-sees-what.mmd)): the three-pod example.*

## 2. What this is

A collaborative, "torrent-like" Kubernetes mesh. People run k3s at home (in a VM, privileged Docker or WSL2) and
join the maintainer's control plane to share compute. It must be:

- **self-sovereign**: no third-party SaaS in the trust path (Nebula, our CA, our control plane);
- **secure**: a participant's machine is not trusted by default, and neither is anybody else's;
- **simple to join**: **one bundle and one command**.

Out of scope for phase 1: marketplaces, billing, storage, GPUs, scheduling fairness.

## 3. Layers, one job each

| Layer | Its one job | Always on? | Replaces |
| :--- | :--- | :--- | :--- |
| **Nebula** | join/"login" (node identity: certificate from our CA with expiry and groups) and NAT traversal/connectivity | yes; it always encrypts (Noise) and cannot be turned off, which is fine: the team does not manage it | Tailscale, Headscale, NetBird, zrok, ngrok, ClusterMesh |
| **Cilium, tunnel mode** (VXLAN/Geneve over the Nebula IPs) | the CNI; joiners configure nothing extra. MTU is set once. Native routing is **not** the default; it is an optional optimisation for the maintainer's own nodes | yes | flannel (kept only as plan B) |
| **Cilium WireGuard** (transparent encryption) | encrypt pod-to-pod across nodes, plus node-to-node | **always on** | app-level TLS for transit |
| **Cilium mutual authentication** (SPIFFE/SPIRE, "mTLS") | authenticate workload identity; WireGuard provides the encryption | per policy, `authentication.mode: required`, when a user asks (beta: prove it in the PoC) | a service mesh |
| **Kyverno + labels + taints** | placement by trust (section 4) | yes | goodwill |

Each layer does one job. Nebula does not do placement; Cilium does not do the identity of machines; Kyverno does
not encrypt. The layers do not conflict: each protects a different stretch (section 6).

### 3.1 Nebula

- Peer-to-peer UDP with hole punching (works behind home NAT); a certificate per host carrying its **Nebula IP,
  groups and expiry**; a **group-based firewall** on every host. No central service must be trusted at runtime:
  the CA signs, the lighthouse only introduces.
- **Own CA and self-hosted lighthouse** (stable public UDP). The CA key is the crown jewel (decision 5, section 11).
- **Groups as roles**: `control-plane`, `own`, `friends`, `public` (nodes), `person` (kubectl clients).
- **Expiry is the soft revocation**: short lifetimes plus renewal; `pki.disconnect_invalid: true` makes expiry also
  cut open tunnels **(prove in the PoC)**. The hard revocation is the blocklist (section 9).
- The Nebula firewall must leave the CNI data plane open between nodes (VXLAN 8472/udp or Geneve 6081/udp,
  WireGuard 51871/udp, Cilium health 4240) **(prove in the PoC)**: with Cilium in tunnel mode nodes must reach each
  other; tenant isolation is Cilium's job, not Nebula's.

### 3.2 k3s over the Nebula IP

The API server listens on, and advertises, the **Nebula IP** only (`--bind-address`, `--advertise-address`,
`--tls-san`); there is no public Kubernetes endpoint. Agents use `K3S_URL=https://<nebula-ip>:6443` and `--node-ip`
set to the Nebula IP. k3s keeps a per-node password, so a second machine cannot take over an existing node name
**(prove in the PoC)**.

### 3.3 Cilium

Tunnel mode over the Nebula interface; pod MTU derived once from the Nebula MTU (default 1300) minus the tunnel
header. WireGuard encryption with node-to-node enabled. Mutual authentication needs the SPIRE server and agents; it
is beta, so it is **opt-in per policy**. Cilium's own service account can read Secrets cluster-wide unless
`tls.readSecretsOnlyFromSecretsNamespace: true` is set; the PoC inventories what each node-side credential can do.

## 4. Trust and placement policy

| | `own` node | `friends` node | `public` node |
| :--- | :--- | :--- | :--- |
| `private` workload | yes, if the node's `owner` is the workload's owner (default) | **never** | **never** |
| `shareable` workload | yes | yes, if its owner opts in | yes, if its owner opts in |

Mechanics (all of them required):

1. **Node labels** `node-restriction.kubernetes.io/trust=own|friends|public` and `.../owner=<github-user>`. The kubelet cannot set labels
   under that prefix (NodeRestriction applies on create too), so **the issuing side labels the node right after it registers**, and a
   node cannot upgrade itself. Until it is labelled a node receives nothing: every placement below is a *positive* requirement
   (`nodeAffinity` on the labels), so an unlabelled node matches no workload.
2. **Taint** `trust=<level>:NoSchedule` on every `friends` and `public` node, applied at registration (`--node-taint` in the join
   command; a kubelet may set its own taints). The taint is relative too: it keeps *other people's* private pods off, while the
   owner's own `private` pods tolerate it (rule 3). A Node that registers without the expected taint is deleted by the issuing side within
   seconds, and the positive affinity above covers the window.
3. **Workload label** `edge-mesh/sensitivity=shareable|private`. **Kyverno** validates and mutates: `private` (or no
   label) gets a required `nodeAffinity` on `owner=<the workload's owner>` (so a node its owner shares as `friends` still runs
   the owner's own `private` pods) and a toleration only for the taints of those nodes; `shareable` gets the toleration for
   the levels its owner allowed. A pod that tolerates a taint of a node it does not own, without being `shareable`, is rejected.
4. **System pods** (Kyverno, CoreDNS, the CNI control components, SPIRE server) are pinned to `own`.
5. **No bypass of the scheduler**: tenants cannot create `pods/binding`, and Kyverno denies `spec.nodeName` on tenant pods
   (`nodeName` skips the scheduler, so it would skip `nodeAffinity` and `NoSchedule`); policies run with `failurePolicy: Fail`.
6. **Kyverno on shareable workloads**: no `hostPath`, `privileged`, host namespaces or added capabilities; **no
   Secret volumes or `secretKeyRef`**; `automountServiceAccountToken: false`; resource limits; image allow-list.
7. **Cilium**: deny by default per namespace, plus mutual auth where a policy asks for it.

![The pattern with Kyverno and trust levels](diagrams/placement-kyverno.svg)

*Diagram 2 ([source](diagrams/placement-kyverno.mmd)): the correct pattern.*

**Node-side credentials.** Whoever owns a node also holds what lives on it outside NodeRestriction: the k3s controller and kube-proxy
identities (the controller can read pod specs cluster-wide and patch node status) and the CNI's service account. That is accepted,
unfixable risk on a node you do not own, so **the automatic public demo runs on its own disposable control plane** that holds
nothing of the maintainer's (no secrets, no real workloads), never on the cluster that has the maintainer's own nodes.

What this does **not** do: it does not make a `friends` node safe for a `private` workload. That is the golden rule;
the answers are `confidential` nodes (later, optional) or keeping the workload on `own`.

## 5. Credentials at join

Credentials travel encrypted at every step: the Nebula handshake first, then the k3s token over TLS with the CA
pinned in the token (`K10<hash>`). Tokens are **one-time, per node and expiring**: the **issuing side** (server-side, where `k3s token delete`
lives) deletes the token as soon as the node is Ready; the join command, running on the participant's machine, cannot. The shared `K3S_TOKEN` is a master key and is **never** handed to participants.

**Joining = one bundle + one command.** The bundle holds the Nebula certificate, `config.yml` and a one-time k3s
token; the command is `edge-mesh join <bundle>`. It requires **privileged Linux**: a VM, privileged Docker or WSL2.
The PoC measures all three. The command shows the golden rule and refuses to continue until it is accepted.

![Layers, endpoints and the join flow](diagrams/layers-endpoints-join.svg)

*Diagram 3 ([source](diagrams/layers-endpoints-join.mmd)): layers, endpoints and the join flow.*

### 5.1 Who may join, and how

| Kind of participant | Trust level | Admission |
| :--- | :--- | :--- |
| **Public demo** | `public` | automatic while a slot is free, no cosign and no PR (section 8): 1 h lease, per-IP rules, FIFO waitlist, automatic kick |
| **Persistent participant** | `friends` or above | **only by pull request**, signed with cosign (below) |

**Join by PR** (decided 2026-10-16: "apruebo por PR, sé quién es; validan el PR con cosign").

1. The participant adds `participants/<github-user>.yaml`: node name, resources, requested trust level, and their
   **Nebula public key** (a certificate *request*; they keep the private key).
2. They sign the file with **cosign keyless** (GitHub Actions OIDC through Fulcio, with the Rekor transparency log):
   `cosign sign-blob --bundle participants/<user>.sigstore.json participants/<user>.yaml` run by that workflow. Commits carry the DCO
   sign-off.
3. **CI verifies, deterministically**: `cosign verify-blob` with `--certificate-identity-regexp` bound to the PR author's repository
   and `--certificate-oidc-issuer https://token.actions.githubusercontent.com`. The participant signs from **a GitHub Actions workflow in
   their own fork** (`.github/workflows/sign.yml`), so the certificate identity is that workflow's URL, which contains their GitHub
   login: `^https://github.com/<pr-author>/edge-mesh-lab/.github/workflows/sign.yml@refs/heads/.*$`. (Signing interactively with
   the GitHub OAuth issuer would put the signer's *email* in the certificate, which CI cannot map to the PR author, so a file signed
   by anybody for somebody else's name would pass.); the YAML schema; the
   node name is unique; the requested trust level is allowed. Anything above `public` happens only when the
   maintainer merges, and CODEOWNERS for `participants/` is the maintainer.
4. **The maintainer approves by merging.** The issuing workflow then signs the Nebula certificate with the CA (the
   CA key is an offline step or a protected secret: decision 5), creates a one-time k3s token, **signs the bundle
   with cosign** so the participant can verify it came from the maintainer, and delivers it **privately, never in
   the public repo** (mechanism: decision 9).
5. **Removal** is a PR that deletes their file; merging triggers the cleanup and the Nebula blocklist (section 9).

## 6. Endpoints the maintainer owns

The **AI gateway** and the **Cilium LoadBalancer with BGP to the MikroTik** run **only on the maintainer's own nodes**: the BGP
peering policy has a `nodeSelector` for `own` nodes, and the IP pools select Services by label (a `CiliumLoadBalancerIPPool` has a
`serviceSelector`, not a node selector); Services that need the pool run only on the maintainer's own nodes. **Remote nodes never announce.** TLS comes from cert-manager,
issued automatically. The encryption layers do not conflict because each protects a different stretch: client to
gateway (TLS), gateway to pods (WireGuard, plus mutual auth where asked), and Nebula underneath everything.

## 7. Risks to prove in the PoC

| Risk | What the PoC must show |
| :--- | :--- |
| **Double encapsulation and MTU** (Nebula under the Cilium tunnel, plus WireGuard) | the pod MTU that works; large TLS responses do not hang; throughput cost measured |
| **Mutual auth is beta** | a policy with `authentication.mode: required` denies an unauthenticated peer and allows an authenticated one; failure modes noted |
| **Cilium on Docker and WSL2** | does it start, which kernel options are missing, which of VM, privileged Docker and WSL2 pass |
| **Privileges** | the minimal capabilities and devices a joiner needs; a joiner without them gets a clear refusal |
| **A node owner is root over its pods** | the malicious-node tests (section 10) show the placement holds and transit is unreadable |

## 8. Public demo: capacity pool

The free demo must not fill up: a fixed pool that empties itself every hour. Settings live in one file
(`poc/pool.yaml`).

- **Fixed size.** At most `N` participant nodes and `M` demo namespaces. When the pool is full a join does **not**
  get in: the person joins a **FIFO waitlist** and sees their position ("full, try later").
- **Lease: 1 hour, hard.** The Nebula certificate is issued with the same expiry; the k3s token is one-time. At
  expiry the **reaper** (CronJob or controller) kicks the node, in the order of section 9: certificate fingerprint on the Nebula
  blocklist first, then cordon, drain and `kubectl delete node`; deletes the participant's namespace and workloads; frees the slot,
  which goes to the head of the waitlist. `kick <node>` is the manual one-command version with the same effect.
- **Idle kick.** A node that is NotReady, or has had no workload for `X` minutes, is evicted early.
- **Quotas.** ResourceQuota and LimitRange per namespace (CPU, RAM, pods; no LoadBalancer, no NodePort); at most
  one node per invite.
- **Per-IP rules**: at most **1 active lease per source IP**; a **cooldown** after a lease ends (default 1 h) before
  the same IP can join again; a **per-IP invite rate limit**.
- **Which IP.** The client IP at the edge. Behind an L4 proxy (HAProxy with `send-proxy-v2`) Caddy must read the PROXY protocol
  (`listener_wrappers { proxy_protocol }`, allowing only the proxy's address) to see the real client; otherwise every visitor looks
  like the proxy and the first lease blocks everybody. Caddy then sets `X-Forwarded-For`, and the pool service trusts that header
  **only when the socket peer is Caddy** (`trusted_proxies` of the pool service, a different list from Caddy's own); from anywhere
  else it is ignored and the socket address is used.
- **Visibility.** The demo page shows slots used and free, each lease's time left, and the waitlist length.

**IP limits are weak, and we say so.** Carrier-grade NAT or a shared office makes several legitimate users look like
one IP and blocks them; a VPN bypasses the limits. They stop casual abuse, not a determined one. For that there is an
optional lane: **sign in with GitHub (OAuth)**, with a slightly longer lease (default 2 h) and a limit **per
account** instead of per IP. Cosign and PRs stay **only for persistent participants** (section 5.1).

```mermaid
stateDiagram-v2
  [*] --> Requested: invite (rate-limited per IP)
  Requested --> Refused: IP has an active lease or is in cooldown
  Refused --> [*]
  Requested --> Waitlist: pool full
  Waitlist --> Active: slot freed (FIFO)
  Requested --> Active: slot free, cert with 1 h TTL, one-time token
  Active --> Reaping: lease expired, kick, idle or NotReady
  Reaping --> Free: cordon, drain, delete node and namespace, blocklist cert
  Free --> Cooldown: IP cooldown starts
  Free --> Waitlist: slot goes to the next in line
  Cooldown --> [*]: IP may join again after 1 h
```

## 9. Revocation: one command

`edge-mesh revoke <node>` does, in this order, and prints what it did:

1. add the certificate fingerprint to the **Nebula blocklist** and distribute it. **This is the only hard
   revocation**: Kubernetes has no certificate revocation list, and a kubelet that can still reach the API
   re-creates its Node object after a delete;
2. `kubectl delete node <node>`; a ValidatingAdmissionPolicy denies `CREATE nodes` for revoked names;
3. delete the node's token if it still exists. **Keep** the node-password secret: deleting it would let anybody take
   over that node name.

**Limit, stated plainly:** the Nebula blocklist is per host, so it reaches the hosts that run our agent (the lighthouse, the control
plane, our relays and every node that pulls the signed blocklist from the lighthouse every minute); a peer that has not pulled it keeps
an open tunnel to the revoked node **until the certificate expires**. That is why certificates for `friends` and `public` nodes are
short for the demo (1 h, from the online CA) and, for persistent participants whose certificates come from the offline,
hand-signed CA (decision 5), as short as hand-signing allows (weeks, renewed with each approved PR). For them the signed blocklist
pull and removal by PR are the real revocation, and the pool, Cilium and placement isolate tenants even when a tunnel is open.

Success: after `revoke`, the node cannot reach the API, cannot rejoin with the old token, cannot re-register under
its old name, and the time to loss of access is measured. A negative test runs `kubectl delete node` without the
blocklist and records that the node comes back. Expiry of a lease (section 8) runs the same steps in the same order (blocklist first).

## 10. Proof of concept

The WSL test is a **proof of concept**, not a product claim. The PoC's Nebula firewall is open (inbound any) on purpose: group-based
firewalling and the isolation between tenants are later steps. It uses k3s in Docker containers (not kind), flannel over the Nebula
interface, and pinned versions (`poc/stack.env`); every step prints `PASS` or `FAIL`, and the output is byte-identical across runs
except for the numbers it records separately.

| Command | What it asserts today |
| :--- | :--- |
| `make poc-join` | step 1: join with a bundle and one command, nodes Ready, pod-to-pod and DNS across nodes, MTU, a study lab running on a joined node, token reuse refused, expired certificate refused, blocked UDP gives a clear message, revocation in one command |
| `make poc-host` | `mesh host init / invite / players / kick / down` |
| `make poc-weakness` | flapping, tolerance, a lossy link, control plane down |
| `make test` | the pool logic (`poc/pool.py`): fixed slots, 1 h lease, per-IP rules, FIFO waitlist |

**Not built yet, and tracked as milestone epics (section 12):** Cilium with WireGuard and `cilium-dbg encrypt status` (a first attempt
left the Cilium pods in `CreateContainerError` on k3s in Docker), mutual auth by policy, Kyverno placement, the cluster side of the
pool (reaper, quotas, idle kick), join by signed PR, and the PoC matrix across VM, privileged Docker and WSL2. The PoC cannot use the
real Fulcio offline: when join by PR is built, a local test CA (or `--insecure-ignore-tlog`) will be used **only in the PoC fixture,
marked as such**, and the real flow will run in CI on the PR.

## 11. Open decisions

| # | Decision | Notes |
| :--- | :--- | :--- |
| 1 | **Name** | `edge-mesh-lab` is a working name; a product name is needed before going public |
| 2 | **Licence** | Apache-2.0 is a placeholder; alternatives AGPL-3.0, MPL-2.0 |
| 3 | **When it goes public** | private until the maintainer says so (it holds a security design) |
| 4 | **Lighthouse location** | small VPS (stable public UDP) vs the home edge (NAT/CGNAT dependent) |
| 5 | **CA custody** | the automatic demo needs an **online** CA key, so one offline CA cannot do both jobs. Proposal: **two CAs** in `pki.ca`: an offline one for persistent participants (hand-signed after a merge) and an online one for the `public` demo only, whose certificates carry the `public` group and nothing else |
| 6 | **Keycloak or Dex** | login for people (OIDC); Keycloak heavier and complete, Dex lighter |
| 7 | **Pool numbers** | `N` nodes, `M` namespaces, idle minutes, cooldown for the public demo |
| 8 | **GitHub OAuth lane** | build it in phase 1 or leave it for later |
| 9 | **Bundle delivery** | encrypted to the participant's key vs fetched over an authenticated endpoint |

## 12. Build order: steps, one layer at a time

The simple idea first: **it joins, and a study lab runs on it.** Security layers go on one at a time, each with its own test.

| Step | What | Test | State |
| :--- | :--- | :--- | :--- |
| **1. It joins** | Nebula CA and lighthouse, a one-command bundle, k3s on the Nebula IP, flannel over the Nebula interface, a real study lab (CKA 1.1) running on a joined node | `make poc-join` (16 steps) | built, measured |
| **1b. Host your own game** | `mesh host init / invite / players / kick / down` | `make poc-host` | built |
| **1c. Weaknesses of one control plane** | flapping, tolerance tuning, lossy links, control plane down | `make poc-weakness` | built, numbers in section 13 |
| 2. Cilium + WireGuard | tunnel mode, encryption always on, `cilium-dbg encrypt status` | planned | design (first attempt hit a CNI path problem, see "Lo aprendido" in the PoC README) |
| 3. Per-namespace RBAC and kubeconfig | user access (section 14) | planned | design |
| 4. Trust levels + Kyverno placement | sections 4, 1 | planned | design |
| 5. The 1 h pool and per-IP limits | section 8; `poc/pool.py` has unit tests today | planned | logic built, cluster side planned |
| 6. Mutual auth by policy | section 3 | planned | design |
| 7. Signed-PR participants | section 5.1 | planned | design |
| 8. The public opening | the runbook with the go-public checklist | planned | design |

Every step is a milestone epic, so nothing is lost.

## 13. Control planes: one now, several later

**Phase 1 (MVP): a single control plane, the host's.** It is the simplest thing that works and the PoC **measures its
weaknesses and publishes them as data** (`make poc-weakness`, numbers below). **Phase 2 (design only):** participants can
run their own control planes.

### 13.1 The weaknesses, measured

The PoC runs on one machine (WSL2, Docker 29.8, kernel 6.18), so latency and loss are simulated with `tc netem`.
Measured on 2026-10-19 by `make poc-weakness`; the exact numbers vary run to run, the behaviour is asserted.

| Measurement | Result |
| :--- | :--- |
| Nebula latency, same machine (baseline) | avg 0.472 ms |
| 5 MB pod to pod, baseline | 0 s |
| Simulated intercontinental link (150 ms delay, 2 % loss): Nebula latency | avg 152.310 ms |
| Same link: 5 MB pod to pod | 2 s, completes |
| A node drops (its Nebula tunnel dies): time to NotReady | 44 s (kubelet status interval 10 s; the controller's node-monitor-grace-period defaults to 50 s since Kubernetes 1.32) |
| Default eviction toleration for not-ready and unreachable | 300 s (asserted) |
| Pod with tolerationSeconds=20: time until it is evicted after the drop | 70 s |
| Control plane down: kubectl | `The connection to the server 10.77.0.2:6443 was refused - did you specify the right host or port?` |
| Control plane down: running pods | keep answering over the pod network; nothing can be managed |

What this says: with defaults a dropped node is NotReady after about 40-50 s and its pods are evicted after about five
minutes; `tolerationSeconds` tunes the second. A lossy intercontinental link works but is slow. If the control plane dies,
running pods keep answering, but **nothing can be managed** until it is back: it is a single point of failure, and the
host's machine is the game server.

### 13.2 Phase 2: several control planes

Options, none built: **regional control planes federated with [Liqo](https://docs.liqo.io/)** (peering), **[Kueue
MultiKueue](https://kueue.sigs.k8s.io/docs/concepts/multikueue/)** (a manager dispatches jobs to worker clusters), or node
autonomy with **[OpenYurt](https://openyurt.io/docs/) or [KubeEdge](https://kubeedge.io/docs/)**.

**What Nebula decides and what it does not.** Nebula provides **identity and reachability**: who can reach whom (its groups and
firewall) and the best path to get there (direct, or through a relay). It does **not** decide which control plane or region
runs a workload: that belongs to the scheduling and federation layer (Liqo, MultiKueue, the scheduler). Nebula's groups can,
however, restrict which nodes may reach which control plane, and that is part of the answer: a node in group `region-eu`
can reach only the EU control plane's port.

Open questions for phase 2: **governance** of shared control planes (who may add a host, who may remove one); **trust between
them** (the same trust and placement rule applied per control plane); and the **CA hierarchy** (one Nebula CA for everybody,
or one per host with cross-signing, which changes revocation).

## 14. Using the cluster after joining (user API access)

**Access model.** The k3s API is never public. Users reach it **only over Nebula**, at the control plane's Nebula IP; the
kubeconfig's `server:` is that IP and its CA is pinned.

- **Free 1 h pool:** the bundle includes a kubeconfig built from a ServiceAccount token issued with the TokenRequest API,
  expiring with the lease. RBAC binds it to the participant's demo namespace only: create/get/list/delete pods, deployments,
  ClusterIP services and configmaps; no cluster-wide secrets, no nodes, no CRDs. Quotas and Kyverno apply.
- **Persistent participants (signed-PR flow):** OIDC (Keycloak or Dex, still open) through kubelogin; groups map to RBAC:
  their own namespaces plus `friends`-level permissions. No long-lived tokens.
- **Node credentials and user credentials are different things.** The k3s agent's node credential lets a *machine* be a node;
  the user's kubeconfig lets a *person* manage objects in a namespace. A user never gets the node credential and a node never
  authenticates a human.

The flow, in commands:

```bash
edge-mesh join ./bundle --accept-golden-rule     # brings up Nebula and the agent (a player machine)
export KUBECONFIG=./bundle/kubeconfig
kubectl get pods -n <your-namespace>
kubectl run web --image=nginx -n <your-namespace> -l edge-mesh/sensitivity=shareable   # or =private
```

### 14.1 Error matrix

One test per row, deterministic. **Step 1** means the local PoC tests it now; **later** needs a later layer or the public setup.

| # | What fails | What the user sees, and what we tell them | Tested |
| :-: | :--- | :--- | :--- |
| 1 | UDP to the lighthouse is blocked (corporate firewall) | `edge-mesh join` exits 5: "cannot reach the lighthouse; UDP 4242 must be allowed; ask for the relay option" | step 1: J15 |
| 2 | Symmetric NAT on both sides | falls back to a relay; latency measured | later: needs a relay (section 16) |
| 3 | Nebula certificate expired or not yet valid (clock skew) | Nebula refuses the certificate; check NTP | step 1: J14 (expiry) |
| 4 | Certificate revoked or blocklisted (after a kick) | handshake refused: "certificate is in the block list" | step 1: J16 |
| 5 | MTU or fragmentation: handshake works, big packets hang | pod network MTU fixed at 1250 (Nebula 1300 minus 50) | step 1: J08 |
| 6 | The kubeconfig server is unreachable because Nebula is down | a clear error, not a generic timeout: "Nebula is down; run `edge-mesh join`" | later: with the user kubeconfig (step 3) |
| 7 | x509 CA mismatch (wrong bundle or a man in the middle) | `x509: certificate signed by unknown authority`; re-download your bundle | later (step 3) |
| 8 | The token expired at the end of the lease, mid-session (401) | friendly message and how to rejoin | later (step 5) |
| 9 | RBAC forbidden outside your namespace (403) | `forbidden`; use your namespace | later (step 3) |
| 10 | A Kyverno denial: privileged pod, hostPath, a private pod on a shared node, LoadBalancer/NodePort | the policy's message, naming the rule | later (step 4) |
| 11 | ResourceQuota exceeded | `exceeded quota`; lower requests or delete something | later (step 5) |
| 12 | Pool full (waitlist), same IP already active, cooldown, rate limit | "full, position N" or the reason | pool logic: `poc/test_pool.py`; cluster side later (step 5) |
| 13 | The node joins but stays NotReady: Cilium fails on WSL2 or privileged Docker (eBPF, WireGuard module, BPF mounts) | `edge-mesh join` prints which prerequisite is missing | later (step 2); a first attempt is recorded |
| 14 | The k3s agent rejects the token: one-time token already used, or expired | the agent log says unauthorized; ask the host for a new invite | step 1: J13 |
| 15 | Duplicate node name or Nebula IP conflict | the second joiner is refused | later |
| 16 | Image pull fails or is slow (no registry access or mirror) | pods stay `ImagePullBackOff`; document a mirror | step 1 (observed: pulls worked); mirror later |
| 17 | DNS inside pods across nodes (CoreDNS over the tunnel) | resolves cluster services | step 1: J07 |
| 18 | The control plane is down | running pods continue; kubectl fails with `connection refused` on the Nebula IP (a friendlier message is planned for the kubeconfig wrapper) | step 1c: W06 |
| 19 | OIDC login fails (browser, wrong group) for persistent users | message from kubelogin; check your group | later (step 7) |
| 20 | Flapping on a lossy link | no eviction storm once the tolerances are tuned (section 16) | partial: W02 (a lossy link still transfers), W03 and W05 (a hard drop and a tuned eviction) are measured; a test that flaps the link and counts evictions is later |

## 15. What the operator needs (and where traffic goes)

| Traffic | Goes through the operator? | Size (measured in the PoC, see 15.1) |
| :--- | :--- | :--- |
| Pod to pod across nodes | **No**: peer to peer over Nebula (direct UDP) | the payload plus about 9 % overhead; the lighthouse sees none of it |
| Node to API | yes (the control plane) | light: kilobytes per second per node |
| `kubectl` | yes | negligible |
| The lighthouse | yes, for discovery only | tiny: handshakes |
| **Relays** | **yes, and they carry full traffic** | the bandwidth risk: every relayed byte lands on the relay host |
| Image pulls | no (nodes pull from the registry directly) | the largest transfers, but not through the operator |
| The operator's public services (demo page, invite API) | yes | small |

Recommendations:

- **Lighthouse and relay on a VPS, not at home**: it hides the home IP and relay bandwidth lands on the VPS.
- **The control plane at home**, reachable only over Nebula; check the home IP is static or use DDNS.
- **etcd on a healthy SSD.** The Dell's current SSD is worn; the planned NVMe is where etcd belongs. (A single k3s server uses
  SQLite by default, so this matters when moving to etcd.)
- **Protection and monitoring**: the existing border protection, and alerts on relay bandwidth and on node flapping.
- **Purchases**: none now; the NVMe **before opening widely**; a bigger VPS or a second relay if the relay saturates; a second
  site in phase 2.

### 15.1 Measured bandwidth

Measured by `make poc-join` on one machine (Docker, WSL2); steady state, from the second of two runs (the first 30 s window after a node starts is higher because of the initial sync). The idle numbers are over 30 s with the cluster running and no lab; the transfer is 50 MiB pod to pod across nodes.

| What | Bytes | Per second |
| :--- | ---: | ---: |
| Idle node n1 (kubelet, flannel, Nebula keepalives), both directions | 112850 in 30 s | about 3761 B/s |
| Idle node n2, both directions | 29845 in 30 s | about 994 B/s |
| Lighthouse, whole container (eth0), idle, both directions | 2488 in 30 s | about 82 B/s |
| 50 MiB pod to pod: sender's Nebula tunnel | 57000071 sent | overhead about 9 % over the payload |
| 50 MiB pod to pod: receiver's Nebula tunnel | 56897207 received | |
| 50 MiB pod to pod: lighthouse, whole container (eth0), both directions | 696 | the transfer did not go through the lighthouse |

Relay use: not measured in step 1 (no relay is configured); the lighthouse counters above show that a direct path carries the data. The control plane's own traffic is in the idle rows: the node-to-API cost per node is a few KB/s. (50 MB pod-to-pod over nebula: 1 s.)


## 16. Relay and tolerance profile

**Relay (learned from WireKube and Nebula's docs):** a relay is the guaranteed path and direct is the optimisation. The
lighthouse VPS also runs a Nebula relay (`relay: am_relay: true`); players list it under `relay: relays:` (`use_relays` already defaults to true). Relays carry full
traffic, so the VPS bandwidth is the budget (section 15). Not built in step 1; the error matrix row 2 tracks it.

**Tolerance profile** (to be set from the measurements in 13.1, not before): the per-pod `tolerationSeconds` for
`node.kubernetes.io/unreachable` and `not-ready` (default 300 s, asserted by `make poc-weakness` W04); the node lifecycle
controller's grace period (default 50 s since Kubernetes 1.32) and the kubelet's status interval (default 10 s). Lab pods on shared nodes are
disposable, so a short `tolerationSeconds` (tens of seconds) is right for them and the default stays for system pods on `own`
nodes. The PoC shows a 20 s toleration working (W05).

## 17. Prior art and how we differ

The full notes, each link verified, are in [REFERENCES.md](REFERENCES.md). In short: [Kilo](https://github.com/squat/kilo),
[WireKube](https://github.com/inerplat/wirekube) and [wg-overlay](https://github.com/tyler-lloyd/wg-overlay) are WireGuard
overlays for Kubernetes nodes; k3s has an [embedded Tailscale
integration](https://docs.k3s.io/networking/distributed-multicloud) (`--vpn-auth`); a [CCC 2024
session](https://events.ccc.de/congress/2024/hub/event/home-hosted-locally-distributed-kubernetes-on_7d68) proposed friends
hosting nodes at home over WireGuard (a proposal, not a product); [Liqo](https://docs.liqo.io/) peers clusters;
[Akash](https://akash.network/) is a token-based compute marketplace; [BOINC](https://boinc.berkeley.edu/) is volunteer
computing; [Killercoda](https://killercoda.com/) and Instruqt host labs.

**Our difference:** it is for **learning together**, with LAN-party "games" that anyone can host, joined in **one command**,
**self-hosted end to end** (Nebula, our own CA, no SaaS), with the **trust-based placement golden rule** and **deterministic
tests**, and integrated with the labs of [study.cybercirujas.club](https://study.cybercirujas.club). It is not a compute
marketplace and has no tokens.
