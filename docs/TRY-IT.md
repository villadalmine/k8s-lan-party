---
tipo: runbook
id: k8s-lan-party-try-it
version: 0.1.0
fecha_alta: 2026-10-28
fecha_ultimo_cambio: 2026-10-28
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
depende_de: [docs/HOST-YOUR-OWN.md, docs/JOIN.md, docs/REQUIREMENTS.md]
lo_usan: [README.md]
docs: [docs/POC-STATUS.md, docs/REQUIREMENTS.md, docs/TROUBLESHOOTING.md]
scripts: [poc/mesh, poc/kc, poc/edge-mesh, poc/install-placement.sh, poc/lighthouse-vps.sh, poc/kind-cilium.sh]
runbooks: [docs/HOST-YOUR-OWN.md, docs/JOIN.md]
tags: [try-it, recipes, incus, docker, kind, wsl2, cloud, mixed]
---

# Try it your way

People have different machines. Pick the row that matches yours: every recipe below was run end to end, word for word, on the date given
(host a game, a player joins, the node is Ready, a pod lands by trust, revoke, tear down). Which host kind passes which step, and why the
others do not, is the matrix in [REQUIREMENTS.md](REQUIREMENTS.md) ("Host kind against step"); the runs are recorded in
[POC-STATUS.md](POC-STATUS.md) ("Recipes of TRY-IT.md").

| You have | Recipe | Host (control plane + lighthouse) | Players | Last run |
| :--- | :--- | :--- | :--- | :--- |
| a Linux machine with Incus | [1. Incus VMs](#1-incus-vms-a-linux-host) | VMs | VMs | 2026-10-28, PASS |
| a Linux machine (or a VM, or WSL2) with Docker | [2. Docker](#2-docker-k3s-in-privileged-containers) | privileged containers | privileged containers | 2026-10-28, PASS in a Docker VM (recipe steps); teardown fixed after it, see recipe 2 |
| kind, to try the encrypted pod network only | [3. kind](#3-kind-the-pod-network-only) | kind nodes | none (no Nebula) | earlier version PASS (3 runs, WSL2); **not re-run on 2026-10-28** |
| Windows | [4. WSL2 as a player](#4-wsl2-on-windows-as-a-player) (as a host: recipe 2 inside WSL2, **not run**) | any of the others | a WSL2 distro | 2026-10-28, PASS (as a player) |
| a cloud account | [5. A cloud VM](#5-a-cloud-vm-lighthouse-or-player) | its public lighthouse (and relay) | cloud VMs (amd64, arm64) | 2026-10-28, PASS |
| several of these | [6. Mixed](#6-mixed-a-control-plane-on-one-platform-players-on-two-others) | an Incus VM at home + a lighthouse on a cloud VM | a cloud arm64 VM and a WSL2 distro | 2026-10-28, PASS |

Common to all: get the code, and run everything from `poc/`. Versions are pinned in `poc/stack.env`. Read the golden rule first: what runs on
someone else's machine can be seen by its owner ([GOLDEN-RULES.md](GOLDEN-RULES.md)).

```bash
git clone https://github.com/villadalmine/k8s-lan-party.git && cd k8s-lan-party/poc
```

The same five steps appear in recipes 1 and 2 (and in 6 with a remote lighthouse). The three pods of step 4 are the golden rule in action:
`private-app` has no label, so it is private and may only run on your own node; `shared-app` opts in to `friends` nodes; `bad-app` tries to put a
private pod on a friend's node with `nodeName` and is refused at admission.

```bash
# 4. three pods (paste as is)
./kc apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: private-app, namespace: demo}
spec:
  containers: [{name: c, image: busybox:1.36, command: ["sleep","3600"]}]
---
apiVersion: v1
kind: Pod
metadata:
  name: shared-app
  namespace: demo
  labels: {edge-mesh/sensitivity: shareable, edge-mesh/share-with: friends}
spec:
  automountServiceAccountToken: false
  nodeSelector: {node-restriction.kubernetes.io/trust: friends}
  containers: [{name: c, image: busybox:1.36, command: ["sleep","3600"], resources: {limits: {cpu: 50m, memory: 32Mi}}}]
EOF
./kc apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: bad-app, namespace: demo}
spec:
  nodeName: alice
  containers: [{name: c, image: busybox:1.36, command: ["sleep","3600"]}]
EOF
./kc -n demo wait --for=condition=Ready pod/private-app pod/shared-app --timeout=180s
./kc -n demo get pods -o wide
```

## 1. Incus VMs (a Linux host)

**Prerequisites:** Linux with Incus 6.x, `incus` usable without `sudo`; about 10 GiB of free RAM and 40 GiB of disk (three VMs of 12 GiB);
`bash`, `curl`, `python3`; internet for the images and the pinned binaries.

```bash
export RUNTIME=incus TAINT=yes
./mesh host init                                  # CA, lighthouse VM, control plane VM (about 4 min)
./mesh host invite alice --trust friends
./mesh play alice                                 # a VM joins with `edge-mesh join`
./kc label node alice node-restriction.kubernetes.io/trust=friends
./install-placement.sh                            # Kyverno and the two policies (a few minutes)
./kc taint node cp node-role.kubernetes.io/control-plane:NoSchedule-
./kc create namespace demo
# step 4: the three pods (above)
./mesh host revoke alice
./kc get nodes
```

**Expected:** `host up`; `joined: alice (trust=friends)`; `placement installed: ...`; `bad-app` refused with
`private-cannot-name-a-foreign-node: 'A private workload may only run on `own` nodes ...'`; then

```
NAME          READY   STATUS    ...   NODE
private-app   1/1     Running   ...   cp
shared-app    1/1     Running   ...   alice
```

`revoked alice: fingerprint on the blocklist, node deleted, join token gone`, and `./kc get nodes` lists only `cp`.

**Tear down:** `./mesh host down` (`VM flow removed`): every `emesh-*` VM of the Incus project, the bridge and the state.

**What it cannot do:** the lighthouse is a VM on a private bridge of your machine, so nobody outside it can join. For that, put the lighthouse
on a public server (recipe 5 or 6). Running a second game next to this one: HOST-YOUR-OWN.md, "A second game".

## 2. Docker (k3s in privileged containers)

**Prerequisites:** Linux (a VM is fine) or WSL2 with Docker 29.x and `--privileged` allowed; about 6 GiB of free RAM; `bash`, `curl`,
`python3`. The container names (`lh`, `cp`, `alice`) and the network `emesh` are fixed: one game per Docker host.

```bash
export RUNTIME=docker TAINT=yes
# then exactly the commands of recipe 1, from `./mesh host init` to `./kc get nodes`
```

**Expected:** the same lines as recipe 1.

**Tear down:** `./mesh host down` (removes the containers, the network and the state). The run of 2026-10-28 found that `down.sh` left the player
container (`alice`) and the network behind; it now removes every container on the game's network. That fix is **not yet re-run end to end**:
after `down`, check `docker ps -a` and `docker network ls` and remove what is left by hand.

**What it cannot do:** the players are containers on the same machine; machines outside cannot reach a Docker bridge, so the same
"public lighthouse" rule applies. In WSL2 this recipe is how WSL2 hosts a game; a player from another machine needs a public lighthouse
(recipe 5) and, behind the Windows NAT, its relay. Cilium (step 2) needs a shared `/sys/fs/bpf` in the containers
([TROUBLESHOOTING.md](TROUBLESHOOTING.md)).

## 3. kind (the pod network only)

kind is a throwaway Kubernetes in Docker. Here it shows that Cilium in tunnel mode with WireGuard and SPIRE mutual auth works on your
kernel, **without** Nebula.

**Prerequisites:** Docker, `kind` 0.33.0, `kubectl`, `helm` 4.3.1; about 6 GiB of free RAM.

```bash
make -C .. poc-kind                               # creates the kind cluster `emesh`, installs Cilium, runs the checks
kind delete cluster --name emesh                  # tear down
```

**Expected:** one `PASS` line per check and `RESULT PASS`. (Not re-run on 2026-10-28; the last runs are in POC-STATUS.md.)

**What it cannot do:** no lighthouse, no join, no players: kind nodes are not machines someone joins with a bundle. Use it to check your
kernel before the Cilium step; use recipes 1 or 2 for the game.

## 4. WSL2 on Windows as a player

The full recipe, with the two things to know first, is [JOIN.md](JOIN.md) "In WSL2". In short, from PowerShell and then as root in a
distro of its own:

```powershell
curl.exe -LO https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz   # check it against SHA256SUMS there
wsl --import k8s-lan-party $HOME\wsl\k8s-lan-party .\ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz --version 2
wsl -d k8s-lan-party -u root
```

```bash
# inside the distro: the binaries of JOIN.md "What you need", then the bundle the host sent you
edge-mesh join /path/to/bundle --accept-golden-rule
```

**Expected:** `joined: <name> (trust=<level>)`; on the host, the node is Ready within seconds and its kernel reads `...-microsoft-standard-WSL2`.

**Tear down:** stop the agent and Nebula by PID, remove the iptables chains of the shared network namespace, `wsl --unregister k8s-lan-party`
(JOIN.md "In WSL2", "Leaving").

**What it cannot do:** run in a distro with Docker Desktop's integration (the kubelet cannot start; exit 7 says so); two WSL2 players on one
Windows machine (they share one network namespace). The host must have a lighthouse your PC can reach: a public one (recipe 5), or one on your
LAN with its UDP port forwarded.

## 5. A cloud VM (lighthouse or player)

A cloud account is the easy way to have a public address. The provider owns the hardware, so golden rule 1 applies to it; the trust path
(your CA, your control plane) stays yours. **The AWS example with Terraform** (free-tier instance types, security groups, a hard lifetime and a
teardown that checks nothing is left billing) is issue #24 (`examples/aws/`, `docs/DEMO-AWS.md`); use it when it is merged. Until then, any
Ubuntu 24.04 VM with a public address works like this.

**Prerequisites:** a VM for the lighthouse with **UDP 4242 open inbound** and SSH from your address only; player VMs need nothing inbound
(amd64 or arm64; a free-tier `t3.small` and `t4g.small` were used). On your machine, the CA side: recipe 1 or 2.

```bash
# on the machine with your CA (poc/), the lighthouse's key is made on the server and never leaves it; --relay for players behind symmetric NAT
./lighthouse-vps.sh install <public-ip> --relay --ssh "ssh -i <key> ubuntu@<public-ip> sudo"
./lighthouse-vps.sh verify --ssh "ssh -i <key> ubuntu@<public-ip> sudo"     # ... ok: relay on ...
RUNTIME=incus TAINT=yes ./mesh host init           # or RUNTIME=docker: the control plane at home, the lighthouse in the cloud
TAINT=yes ./mesh host invite bob --trust friends   # send poc/.state/bundles/bob to bob's VM, privately
```

On a player VM (as root): the commands of [JOIN.md](JOIN.md) "What you need", with `nebula-linux-arm64.tar.gz` and `k3s-arm64` on arm64, then
`edge-mesh join <bundle> --accept-golden-rule`. Then label, placement and the three pods as in recipe 1 (with `bob` for `alice`).

**Expected:** `verify` prints only `ok:` lines; the player prints `joined: bob (trust=friends)` and is Ready on the host.

**Tear down:** `./mesh host revoke bob`, `./mesh host down`, `./lighthouse-vps.sh remove --ssh ...`, then delete the VMs in the provider and
check nothing is left (instances, volumes, addresses).

**What it cannot do:** keep running for free: a public lighthouse is a machine you pay for, and with the relay on, every relayed byte
crosses it twice (about 2.35 bytes of its traffic per byte, DESIGN.md 15.1).

## 6. Mixed: a control plane on one platform, players on two others

The run of 2026-10-28: the control plane in an Incus VM at home (behind two NATs), the lighthouse and relay on a cloud VM (amd64), one
player on a cloud VM (arm64, another network) and one in a WSL2 distro on a Windows PC. It is recipes 1, 5 and 4 together:

```bash
# the CA machine (an Incus host at home), in poc/
./lighthouse-vps.sh install <public-ip> --relay --ssh "ssh -i <key> ubuntu@<public-ip> sudo"
RUNTIME=incus TAINT=yes ./mesh host init
RUNTIME=incus ./install-placement.sh
RUNTIME=incus ./kc taint node cp node-role.kubernetes.io/control-plane:NoSchedule-
RUNTIME=incus ./kc create namespace demo
RUNTIME=incus TAINT=yes ./mesh host invite arm1 --trust friends    # -> the arm64 cloud VM: edge-mesh join (recipe 5)
RUNTIME=incus TAINT=yes ./mesh host invite wsl3 --trust public     # -> the WSL2 distro: edge-mesh join (recipe 4)
RUNTIME=incus ./kc label node arm1 node-restriction.kubernetes.io/trust=friends
RUNTIME=incus ./kc label node wsl3 node-restriction.kubernetes.io/trust=public
```

Then the pods: `private-app` as in step 4, `shared-app` with `share-with: friends`, a third one like it with `share-with: public` and
`nodeSelector: {node-restriction.kubernetes.io/trust: public}`, and `bad-app` with `nodeName: arm1`.

**Seen on 2026-10-28:**

```
NAME   STATUS   ROLES           VERSION        INTERNAL-IP   KERNEL-VERSION
arm1   Ready    <none>          v1.37.1+k3s1   10.77.0.5     7.0.0-1014-aws (arm64)
cp     Ready    control-plane   v1.37.1+k3s1   10.77.0.2     6.8.0-147-generic (amd64)
wsl3   Ready    <none>          v1.37.1+k3s1   10.77.0.6     6.18.54.1-microsoft-standard-WSL2 (amd64)

NAME             READY   STATUS    NODE
private-app      1/1     Running   cp
shared-friends   1/1     Running   arm1      (uname -m in it: aarch64)
shared-public    1/1     Running   wsl3      (uname -m in it: x86_64)
```

`bad-app` was refused (`private-cannot-name-a-foreign-node`). Both players reached the control plane **through the relay** (their Nebula log:
`Handshake message received ... (relayed) certName=cp`): the control plane sat behind two NATs. `mesh host revoke arm1` and `wsl3`: neither
reached the control plane afterwards.

**Tear down:** `./mesh host revoke <each>`, `./mesh host down`; destroy the cloud VMs and check nothing is billing; in WSL2 the "Leaving"
steps of JOIN.md. Done that day: nothing left.

**What it cannot do (yet):** the players were added by hand (`mesh host invite`); the public request page and the signed-PR lane are other
issues (#11, #12).
