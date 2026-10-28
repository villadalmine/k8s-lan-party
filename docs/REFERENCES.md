---
tipo: doc
id: k8s-lan-party-references
version: 0.1.0
fecha_alta: 2026-10-19
fecha_ultimo_cambio: 2026-10-19
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: meta.yml
depende_de: [docs/DESIGN.md]
lo_usan: [README.md, docs/DESIGN.md]
docs: [docs/DESIGN.md]
tags: [referencias, prior-art, nebula, cilium, wireguard]
---

# Prior art and what we reuse

Every link below was opened on 2026-10-19 (HTTP 200 or the repository API answered). For each project: what it is, what we
take, what we do differently. Where a lesson changed the design it is marked **applied** and cited in
[DESIGN.md](DESIGN.md) as "learned from X".

## Overlay networks for Kubernetes

| Project | What it is | What we take | What we do differently |
| :--- | :--- | :--- | :--- |
| [Kilo](https://github.com/squat/kilo) (`squat/kilo`) | A multi-cloud network overlay for Kubernetes built on WireGuard. Its pod network stays fully connected when nodes are in different networks or behind NAT, and it can run as an add-on beside the CNI already installed (its README uses Flannel as the example). | The topology idea: group nodes **by location** and let the overlay take care of the stretch between locations while the CNI takes care of the stretch inside one. It is also the proof that an overlay can coexist with Flannel. **Applied:** step 1 runs flannel over the Nebula interface exactly so the overlay and the CNI stay separate layers. | Kilo is WireGuard with keys managed as cluster objects. We use Nebula for **identity** (certificates from our CA with groups and expiry) and for NAT traversal, so joining is "one bundle", and revoking is a blocklist. |
| [WireKube](https://github.com/inerplat/wirekube) (`inerplat/wirekube`) | A serverless WireGuard mesh for Kubernetes nodes across any network, coordinated through CRDs. Agents discover their endpoint with STUN, detect the NAT type, and keep a **relay path warm** while probing direct connectivity; a per-peer state machine moves Relay, Warm, Direct. | The principle that a **relay is always available and direct is an optimisation**, and that NAT type decides the path. **Applied:** the PoC tests a node with no direct path (the lighthouse blackholed) and requires a clear message; the relay (Nebula `am_relay` on the lighthouse VPS) is part of the design, not an afterthought (DESIGN section 16). | WireKube needs a Kubernetes cluster to exist before the mesh does (the CRDs live in the API). Our mesh has to exist **before** the node can reach the API, so Nebula runs on the host, outside the cluster. |
| [wg-overlay](https://github.com/tyler-lloyd/wg-overlay) (`tyler-lloyd/wg-overlay`) | A small WireGuard overlay: each node annotates itself with its WireGuard public key and overlay IP; every other node watches the annotations and adds it as a peer. Last push 2022-11. | Node annotation as the key exchange is simple and needs no extra server. | We do **not** let a node publish its own identity into the cluster: a kubelet may only set labels outside `node-restriction.kubernetes.io/`, and a hostile node would lie. The issuing side (our CA) is the authority, and the trust label is applied by the control plane. |
| [k3s: distributed multicloud with the embedded Tailscale integration](https://docs.k3s.io/networking/distributed-multicloud) | The official k3s way to bind nodes to an overlay: `--vpn-auth="name=tailscale,joinKey=..."` (optionally a self-hosted control server such as Headscale), and `--node-external-ip` set to the node's overlay IP. The page warns that latency can hurt cluster health and that **embedded etcd is not supported** in this type of deployment. | **Applied:** the same shape for Nebula: the node IP is the overlay IP (`--node-ip`, `--advertise-address`, `--tls-san` on the server; `--node-ip` and `--flannel-iface nebula1` on the agent). We keep a **single** server and do not use embedded etcd across networks. | The VPN is brought up by `edge-mesh join` before k3s starts, rather than by a k3s flag, because we also need our certificate, config and blocklist handling. |
| [Nebula docs](https://nebula.defined.net/docs/) ([lighthouse](https://nebula.defined.net/docs/config/lighthouse/), [relay](https://nebula.defined.net/docs/config/relay/)) | Overlay with a certificate per host (IP, groups, expiry), a group-based firewall, lighthouses for discovery and relays for peers that cannot connect directly. | Everything in DESIGN section 3.1: lighthouse on a public host, relay for hard NAT, groups as roles, expiry plus blocklist for revocation. **Applied:** `pki.disconnect_invalid: true`, blocklist reload by SIGHUP (the PoC proves a revoked certificate is refused at the next handshake, and the lesson that the reload must really be sent, because `pkill` is missing in the k3s image). | Nebula is the whole join layer for us; we add nothing to it. |

## CNI and encryption

| Project | What we take | What we do differently |
| :--- | :--- | :--- |
| [Cilium: WireGuard transparent encryption](https://docs.cilium.io/en/stable/security/network/encryption-wireguard/) | Pod-to-pod encryption across nodes without touching the workload (node-to-node is an opt-in setting). | Always on, as step 2. |
| [Cilium: mutual authentication](https://docs.cilium.io/en/stable/network/servicemesh/mutual-authentication/mutual-authentication/) | `authentication.mode: required` per policy, SPIFFE/SPIRE identities. The page says "This is a beta feature." | Per policy and on request, never required; proven in the PoC before it is promised. Mutual auth authenticates, WireGuard encrypts. |
| [Cilium: performance tuning](https://docs.cilium.io/en/stable/operations/performance/tuning/) | Routing modes and MTU: the page covers MTU auto-detection, which cannot see a tunnel inside a tunnel. **Applied:** the MTU is set once and derived: Nebula 1300, minus the tunnel header (50 for VXLAN), minus WireGuard overhead (60) gives the pod MTU (`stack.env`), and step 1 asserts 1300 / 1250. | Tunnel mode is the default so joiners configure nothing; native routing is an optimisation for the maintainer's own nodes only. |

## Edge nodes with unreliable links

| Project | What we take | What we do differently |
| :--- | :--- | :--- |
| [Kubernetes taint-based evictions](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) | The default is `tolerationSeconds: 300` for `not-ready` and `unreachable`; the node lifecycle controller marks a node unhealthy after its grace period. **Applied:** DESIGN section 16 defines a **tolerance profile** (per-pod `tolerationSeconds`, node grace period) and the PoC measures flapping with `tc netem` before any value is promised. | Values come from our measurements, not from a table. |
| [OpenYurt](https://openyurt.io/docs/) and [KubeEdge](https://kubeedge.io/docs/) (CNCF) | **Node autonomy**: an edge node keeps running its pods when it loses the control plane, through a local cache. This is the model for phase 2 and for "the control plane is down: running pods keep running". | Phase 1 does not install either. We document the weakness and measure it; autonomy is a phase 2 option. |

## Several clusters (phase 2, design only)

| Project | What it is | Our view |
| :--- | :--- | :--- |
| [Liqo](https://docs.liqo.io/) | Peering makes a remote cluster look like a virtual node. | Option for phase 2: each host keeps its own game and lends capacity by peering. |
| [Kueue MultiKueue](https://kueue.sigs.k8s.io/docs/concepts/multikueue/) | A manager cluster dispatches batch jobs to worker clusters. | Option for phase 2: best fit for "send this lab somewhere with spare compute". |
| [CCC 2024 session: home hosted locally distributed kubernetes](https://events.ccc.de/congress/2024/hub/event/home-hosted-locally-distributed-kubernetes-on_7d68) | A self-organised planning session: a cluster on single board computers in the homes of trusted people, joined by WireGuard, starting from **five masters in five locations**; open questions about the board, the system image, Calico and BGP, DNS, identity. A proposal, not a product. | The same hope (friends host nodes at home). The trade-off we avoid in phase 1: five control planes means five etcd members over home links; we start with **one** control plane and treat distributing it as phase 2. |

## What we are not

| Project | What it is | Why we are not it |
| :--- | :--- | :--- |
| [Akash](https://akash.network/) | A decentralised compute marketplace with its own token. | We are a place to learn together: no tokens, no billing, no marketplace. |
| [BOINC](https://boinc.berkeley.edu/) | Volunteer computing: donated cycles for a project's own work units. | We do not run a central project's jobs on volunteers; each person runs their own labs. |
| [Killercoda](https://killercoda.com/) and Instruqt | Hosted lab environments. | We are self-hosted end to end: you (or a friend) host the game, nothing runs in a vendor's cloud. |
