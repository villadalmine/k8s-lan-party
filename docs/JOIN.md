---
tipo: runbook
id: edge-mesh-lab-join
version: 0.2.0
fecha_alta: 2026-10-21
fecha_ultimo_cambio: 2026-10-21
categoria: plataforma
dueno_agente: casa-orquestador
pieza: edge-mesh-lab
meta: ../meta.yml
depende_de: [docs/REQUIREMENTS.md, docs/HOST-YOUR-OWN.md, poc/edge-mesh]
lo_usan: [README.md, docs/DEMO.md]
docs: [docs/TROUBLESHOOTING.md, docs/GOLDEN-RULES.md]
scripts: [poc/edge-mesh, poc/lib.sh, poc/vm/lib-vm.sh]
runbooks: []
tags: [join, participante, bundle]
---

# Join a game

You received a **bundle** from a host. One bundle, one command. Read the golden rule first, because the command makes you accept it:

> What runs on someone else's machine can be seen by its owner. Nothing encrypts a pod from the owner of the node it runs on. Keep
> private things on your own machine. ([GOLDEN-RULES.md](GOLDEN-RULES.md))

**Tested by following this guide literally, 2026-10-21:** a fresh Ubuntu 24.04 VM (Incus, kernel 6.8, nothing from the PoC scripts installed) joined a control plane
with only the commands below, showed `joined: ... (trust=friends)`, reached the lighthouse and the control plane over Nebula and appeared Ready on the host; an
expired bundle was refused with a clear message. Privileged Docker on WSL2 was run earlier with the PoC's own flow. **Not tested:** native WSL2, macOS, bare metal.

**What you will have at the end:** your machine is a node of the host's cluster, on an encrypted private network; only workloads that their owners marked
shareable (and that opted in to your trust level) can land on it. **Time and cost:** about 10 minutes (downloads included), free.

## What you need

- A **privileged Linux**: a VM, privileged Docker, or WSL2 (see [REQUIREMENTS.md](REQUIREMENTS.md); 2 vCPU, 3 GiB RAM, 12 GiB disk).
- **Outbound UDP 4242** to the host's lighthouse. Nothing needs to be opened inbound on your side (Nebula punches holes).
- The bundle directory: `ca.crt host.crt host.key config.yml join.env`. It holds **your private key**; keep it private.
- On the machine, as root (versions pinned in `poc/stack.env`); these are the exact commands that were run:

```bash
apt-get update && apt-get install -y curl ca-certificates iproute2 iputils-ping
mkdir -p /opt/nebula && curl -sL https://github.com/slackhq/nebula/releases/download/v1.11.2/nebula-linux-amd64.tar.gz | tar xz -C /opt/nebula
curl -sLo /usr/local/bin/k3s "https://github.com/k3s-io/k3s/releases/download/v1.37.1%2Bk3s1/k3s" && chmod +x /usr/local/bin/k3s
cp poc/edge-mesh /usr/local/bin/edge-mesh && chmod +x /usr/local/bin/edge-mesh      # the script from this repository
/opt/nebula/nebula -version && k3s --version | head -1                               # 1.11.2 and v1.37.1+k3s1
```

- A correct clock (NTP). A clock a second behind makes your new certificate "not yet valid".

## Join

```bash
edge-mesh join /path/to/bundle --accept-golden-rule        # as root, [dell] [docker]
```

What it does, in order: shows the golden rule and refuses without `--accept-golden-rule`; checks `/dev/net/tun`; copies the files to
`/etc/nebula`; starts Nebula and waits (30 s) for the lighthouse; starts the k3s agent with `--node-ip <your Nebula IP>`, using the join
token over the Nebula tunnel (the k3s token travels over TLS with the CA pinned). Output: `joined: <name> (trust=<level>)`.

Exit codes you can meet:

| Code | Meaning | Fix |
| :--- | :--- | :--- |
| 2 | wrong usage or no bundle directory | `edge-mesh join <bundle-dir> --accept-golden-rule` |
| 3 | golden rule not accepted | add `--accept-golden-rule` after reading it |
| 4 | no `/dev/net/tun` | use a VM, `docker run --privileged`, or WSL2 |
| 5 | cannot reach the lighthouse | UDP 4242 to the host must be allowed; ask the host for the relay option (not built yet) |
| 6 | not running as root | run it with `sudo` |
| 7 | the control plane rejected the token, or the node did not register in 90 s | the token works until a node registered with it (the host then burns it) or 8 minutes pass: ask the host for a new invite (under a new name) |

### In a VM [dell]

The PoC does this for each player (`poc/vm/lib-vm.sh`): create the VM, install `tcpdump wireguard-tools iproute2 iputils-ping curl`, copy
`nebula`, `k3s`, `edge-mesh`, set the clock, put the bundle in `/bundle`, then `edge-mesh join /bundle --accept-golden-rule`.

### In Docker [docker]

A privileged container with the k3s image, `/dev/net/tun` available, and the three files mounted (`poc/lib.sh`, function `new_container`).
For a quick local try use the host's own simulation instead: `./mesh play <name>` from `poc/` ([HOST-YOUR-OWN.md](HOST-YOUR-OWN.md) section 2).
With Cilium, `/sys/fs/bpf` must be a shared mount inside the container (`mount -t bpf bpf /sys/fs/bpf; mount --make-rshared /`);
see TROUBLESHOOTING.md.

### In WSL2 [not tested]

Native WSL2 as a node was not run. The WSL2 *host* works for the Docker flow above.

## Verify (from your machine)

```bash
ip addr show nebula1                  # your Nebula IP (10.77.0.x)
ping -c2 10.77.0.1                    # the lighthouse
ping -c2 10.77.0.2                    # the control plane
tail -n5 /var/log/nebula.log /var/log/k3s-agent.log
```

## Verify (the host sees you)

```bash
./mesh host players                   # your node, READY=True, certificate expiry
kubectl get nodes -o wide             # InternalIP is your Nebula IP
kubectl get node <you> --show-labels  # node-restriction.kubernetes.io/trust=friends|public, set by the host
```

If the host runs trust-based placement, your node has the taint `trust=<level>:NoSchedule`: only **shareable** workloads whose owner opted in
to your level land on it. A private pod aimed at your node is refused at admission.

## Leaving

Ask the host to `kick` you (they blocklist your certificate and remove the node). To clean your own machine afterwards, stop Nebula and k3s and
delete `/etc/nebula` and `/var/lib/rancher` **[not tested as a script]**; in a VM or container, delete the machine.

If your bundle expires, your tunnel closes by itself (Nebula `disconnect_invalid`); ask for a new bundle.

## Problems

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) lists every problem the PoC hit, with the command that diagnoses it.
