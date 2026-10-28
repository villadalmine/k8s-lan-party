---
tipo: doc
id: k8s-lan-party-requirements
version: 0.1.0
fecha_alta: 2026-10-21
fecha_ultimo_cambio: 2026-10-21
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
depende_de: [docs/DESIGN.md, poc/stack.env]
lo_usan: [README.md, docs/HOST-YOUR-OWN.md, docs/JOIN.md]
docs: [docs/POC-STATUS.md]
scripts: [poc/stack.env]
tags: [requisitos, versiones, puertos]
---

# Requirements

What you need before you host or join. Everything here was measured in the PoC; where something was **not** tested it says so.
Versions are pinned in `poc/stack.env` (change them there and nowhere else).

## Who needs what

| Role | What it is | Needs |
| :--- | :--- | :--- |
| **Lighthouse** | introduces nodes to each other; no workload | 1 vCPU, 1 GiB RAM, a stable address with **UDP 4242** reachable |
| **Control plane** (the host) | k3s server on its Nebula IP, Kyverno, SPIRE server | 2 vCPU, 3 GiB RAM, 12 GiB disk (the PoC size; Kyverno, Cilium and SPIRE use about 1.5 GiB of it) |
| **Node** (a participant) | k3s agent on its Nebula IP | 2 vCPU, 3 GiB RAM, 12 GiB disk, **privileged Linux** (below) |

These are the sizes the Dell VMs ran with (lighthouse 1 vCPU / 1 GiB, the other three 2 vCPU / 3 GiB). Smaller was not tried. The
lighthouse and the control plane may be the same machine; the PoC keeps them apart so that the lighthouse can sit on a cheap public VPS.

## Host operating system

A joining machine must be **privileged Linux**: it needs `/dev/net/tun` (Nebula), the ability to run k3s (cgroups v2, mounts) and, for the
Cilium step, a kernel with BPF. `edge-mesh join` refuses with exit code 4 if `/dev/net/tun` is missing.

| Host kind | Tested | Notes |
| :--- | :--- | :--- |
| Linux VM (Incus, Ubuntu 24.04 cloud image) | yes, on the Dell (host Ubuntu 26.04, kernel 7.0.0-31, Incus 6.0.5) | the closest to how people join; distro kernel in the guest, kernel 6.8 |
| Privileged Docker (k3s image) on WSL2 | yes (WSL2 kernel 6.18.54, Docker 29.8.0) | `--privileged`, a replaced entrypoint; `/sys/fs/bpf` needs a manual mount (see TROUBLESHOOTING.md) |
| kind v0.33.0 (node image `kindest/node:v1.37.0`) on WSL2 | yes, for the Cilium and SPIRE steps only | Nebula is **not** in the path there |
| WSL2 as a node itself (k3s inside the distro) | **not tested** | |
| macOS with Docker Desktop | **not tested** (its VM kernel is another story) | |
| Bare-metal Linux, Killercoda | **not tested** | |

## Kernel versions tested

| Where | Kernel |
| :--- | :--- |
| Dell host (runs Incus) | 7.0.0-31-generic |
| Incus guests (Ubuntu 24.04 cloud image) | 6.8.0 (seen with `uname -r` in a joined VM) |
| WSL2 | 6.18.54.1-microsoft-standard-WSL2 |

## Tools and pinned versions

| Tool | Version | Where it runs |
| :--- | :--- | :--- |
| Nebula (`nebula`, `nebula-cert`) | 1.11.2 | every machine |
| k3s | v1.37.1+k3s1 (image `rancher/k3s:v1.37.1-k3s1`) | control plane and nodes |
| Cilium (Helm chart) | 1.20.3 (tunnel mode, WireGuard) | cluster |
| SPIRE (inside the Cilium chart, beta) | 1.15.3, trust domain `spiffe.cilium` | control plane |
| Kyverno (app / chart) | 1.19.1 / 3.9.1 | control plane |
| Helm | 4.3.1 | control plane (inside the VM or `alpine/helm:4.3.1`) |
| kubectl image (tools) | `alpine/k8s:1.37.1` | Docker flow |
| Packet capture | `nicolaka/netshoot:v0.16`; `tcpdump` in the VMs | tests only |
| Test pods | `busybox:1.36` | tests only |
| Host side | `bash`, `curl`, `tar`, `python3` (for JSON of `nebula-cert print`) | the machine that runs `poc/` |
| Docker flow | Docker 29.x with `--privileged` | WSL2 / Linux |
| VM flow | Incus 6.x (`incus` without sudo), image `images:ubuntu/24.04/cloud` | the Dell |
| kind flow | kind 0.33.0, kubectl, helm | WSL2 / Linux |

Rules of this repo: check live for the newest stable before bumping any of these; never trust a remembered version.

## Network and the firewall

Nebula is the only thing that has to cross the Internet. Kubernetes is never public: the API listens only on the control plane's Nebula
IP (`10.77.0.2` in the PoC).

**Outside the mesh (underlay)**

| Direction | Port | Why |
| :--- | :--- | :--- |
| every node to the lighthouse | **UDP 4242** | Nebula handshake and introductions. Blocked corporate networks give exit code 5 and a message naming the port |
| node to node | UDP 4242 (random source ports, hole punching) | direct Nebula tunnels; behind symmetric NAT on both sides a relay is needed (not built, issue #15) |

**Inside the mesh (the narrowed Nebula firewall in `issue.sh`, `FW=narrow`)**

| Allowed inbound | Used by |
| :--- | :--- |
| ICMP | health, `ping` |
| **TCP 6443** (members only: groups `own`, `friends`, `public`; never the lighthouse) | the k3s API and supervisor |
| **TCP 10250** (only from group `control-plane`) | kubelet (`logs`, `exec`) |
| **UDP 51871** | Cilium WireGuard (pod-to-pod and node-to-node) |
| **UDP 8472** | flannel VXLAN, only when flannel is the CNI (step 1); not needed with Cilium |

With Cilium, every node also advertises `advmss 860` on the mesh route so host TCP (the agents' mutual-auth handshake) fits the WireGuard device MTU
(TROUBLESHOOTING.md, "Mutual auth ... the sticky failure").

Not needed, proved by the Cilium suite: `TCP 4240` (Cilium health) and `TCP 4250` (mutual-auth handshake) both ride inside WireGuard.
Everything else between hosts is refused (`J13`, `V15`, `C14`). `FW=open` restores any/any, for debugging only.

**Addresses in the PoC.** Nebula range `10.77.0.0/24` (lighthouse `.1`, control plane `.2`, players from `.3`); it must not overlap the pod network
(`10.42.0.0/16`, the k3s default). Nebula MTU 1300, pod MTU 1150 (1300 - VXLAN 50 - WireGuard 60, rounded down).

## Time

The clock matters: a node whose clock is a second behind the issuing machine sees a fresh certificate as "not yet valid". Keep NTP on. The
VM flow sets the clock before it pushes a bundle.

## Limits the PoC kept on the Dell

At most 4 VMs at once, `/` below 85 % full, pause if the CPU package passes 85 degrees C. One kind or Docker cluster at a time on a PC.
