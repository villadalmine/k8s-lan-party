---
tipo: doc
id: k8s-lan-party-requirements
version: 0.2.0
fecha_alta: 2026-10-21
fecha_ultimo_cambio: 2026-10-28
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
depende_de: [docs/DESIGN.md, poc/stack.env]
lo_usan: [README.md, docs/HOST-YOUR-OWN.md, docs/JOIN.md]
docs: [docs/POC-STATUS.md]
scripts: [poc/stack.env, poc/edge-mesh, tests/test_join_refusal.py]
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

## Host operating system: what a joiner needs

A joining machine must be **privileged Linux**. `edge-mesh join` checks what it can before it starts anything, and refuses with a message
that says what to do:

| Needs | Why | Without it | Tested by |
| :--- | :--- | :--- | :--- |
| **root** (`sudo`, or `wsl -u root`) | Nebula creates a network device; k3s mounts, writes iptables and routes | exit 6: "refusing to join: run as root" | `tests/test_join_refusal.py` (every pull request, CI) |
| **`/dev/net/tun`** | Nebula's tunnel device. A VM, privileged Docker and WSL2 have it; a plain Docker container does not | exit 4: "refusing to join: no /dev/net/tun (needs a privileged Linux: VM, privileged Docker or WSL2)"; nothing is started | `tests/test_join_refusal.py`: root in a plain container gets exit 4, and the same container given the device passes that check (positive control) |
| **outbound UDP 4242** to the lighthouse | Nebula's handshake | exit 5, naming the port (a relay does not help: it is reached the same way) | `make poc-join` J16 |
| a kernel and mount table the **kubelet** accepts (cgroups v2, mounts, iptables) | the k3s agent runs containerd and the kubelet | exit 7: "the node did not register within 90 s: the kubelet did not start on this machine", with the kubelet's last error | seen on WSL2 with Docker Desktop's integration (below); before 2026-10-28 `edge-mesh join` printed `joined` there although no Node ever appeared |
| with Cilium (step 2): BPF, the WireGuard module, a shared `/sys/fs/bpf` | the Cilium agent | the node stays NotReady (matrix row 13 in DESIGN.md 14.1) | `make poc-cilium` / `poc-vm` |

Nothing inbound is needed on a joiner, and no particular NAT: behind a symmetric NAT the host turns on the relay
(`lighthouse-vps.sh sign --relay`, `make poc-nat`).

### Host kind against step (issue #14)

PASS = passed on that host kind (where and when: the "Proven" table of [POC-STATUS.md](POC-STATUS.md)); FAIL = tried, does not work (the reason); not possible
= cannot work by construction; not run = nobody tried it yet.

| Host kind | Refused without the privileges | Join, node Ready (step 1) | Study lab on it | Revoke cuts it | Cilium + WireGuard (step 2) | Placement by trust (step 4) | Behind symmetric NAT, through the relay |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **VM** (Incus, Ubuntu 24.04 guest, kernel 6.8; the maintainer's lab host) | not applicable: a VM has root and the device | PASS (`poc-join`, many runs) | PASS (J09-J11) | PASS (J17, `poc-revoke`) | PASS (`poc-vm`, 24) | PASS (`poc-placement`, 30) | PASS (`poc-nat`, 25, 2026-10-28) |
| **Privileged Docker** (k3s image, on WSL2 or Linux) | PASS: a plain container is refused (exit 4, CI) | PASS (`poc-join`, Docker flow; an earlier version of the suite) | PASS (same run) | PASS (`poc-revoke`, x2) | PASS (`poc-cilium`, 23; `/sys/fs/bpf` needs a manual shared mount) | PASS (1 run) | not run: `poc-nat` builds its two NATs with Incus |
| **WSL2 itself as the node** (a distro, kernel 6.18) | not applicable: `wsl -u root` and the device exist | **PASS** in a distro without Docker Desktop's integration (2026-10-28: node Ready about 4 s after the join, a pod ran on it, `kubectl exec` reached it, revoke cut it); **FAIL** in a distro with it: the kubelet refuses the mount table (Docker Desktop mounts its resources folder with an unescaped space in the options: `system validation failed - wrong number of fields (expected 6, got 7)`), now exit 7 with that message | not run | PASS | not run | PASS (mixed run of issue #25: a pod shared with `public` ran on it) | PASS: the WSL2 node sat behind the Windows NAT and the control plane behind a symmetric NAT (and, in the mixed run, behind the lab's double NAT); the tunnel went through the relay |
| **Cloud VM** (AWS, Ubuntu 24.04, another network; t3.small amd64 and t4g.small arm64) | not applicable | PASS (2026-10-28, twice: amd64 in the admission cycle against the public lighthouse, node Ready about 1 min after the join; arm64 in the mixed run of issue #25) | not run | PASS (reap and blocklist, then revoke: it no longer reaches the control plane) | not run | PASS (mixed run: a pod shared with `friends` ran on the arm64 node; a private pod naming it was refused) | PASS (mixed run: the control plane sat behind the lab's double NAT and the tunnel went through the relay; in the admission run hole punching gave a direct one) |
| **kind** (Cilium and SPIRE steps only) | not applicable | not possible: kind nodes are not joiners, Nebula is not in the path | not run | not possible | PASS (`poc-kind`) | not run | not possible |
| **macOS** (Docker Desktop) | not run | **not run**: no Mac was available. Natively not possible (k3s and Nebula's node side need Linux); privileged Docker inside Docker Desktop's Linux VM might work like the Docker row, untested | not run | not run | not run | not run | not run |
| **Bare-metal Linux** | not run | not run (nothing different from the VM row is expected; unverified) | not run | not run | not run | not run | not run |

**WSL2 as a node, what to know.** Every WSL2 distro and Docker Desktop share **one network namespace**: the iptables chains a node adds
(kube-proxy, flannel, k3s's network policy) are seen by all of them, and they stay after the node leaves until removed
(`iptables-save | grep -iv -e KUBE- -e CNI- -e flannel | iptables-restore`, the same for `ip6tables`; this was done and checked against a
snapshot after each run). Use a distro of its own without Docker Desktop's integration (`wsl --import` of the Ubuntu WSL image) and run
`edge-mesh join` as root there. **One node per Windows machine:** a second distro joining at the same time cannot create its own `nebula1`
("device or resource busy", the namespace is shared) and its agent disturbs the first node (it went NotReady in the 2026-10-28 run). WSL2 as the **host** (control plane) is only the Docker flow today: `mesh host init` brings the control plane
up in Docker or Incus, not on the distro itself.

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
| PyYAML (`python3-yaml`) | only the golden suite's check G8.1 (`poc/participants.py` parses YAML); without it G8.1 prints why and SKIPs | the machine that runs `make poc-golden` |
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
| node to node | UDP 4242 (random source ports, hole punching) | direct Nebula tunnels; behind symmetric NAT on both sides the lighthouse relays (`lighthouse-vps.sh sign --relay`; `make poc-nat`, DESIGN.md 15.1) |

**Inside the mesh (the narrowed Nebula firewall in `issue.sh`, `FW=narrow`)**

| Allowed inbound | Used by |
| :--- | :--- |
| ICMP | health, `ping` |
| **TCP 6443** (any host) | the k3s API and supervisor |
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
