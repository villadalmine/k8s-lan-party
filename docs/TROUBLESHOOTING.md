---
tipo: doc
id: k8s-lan-party-troubleshooting
version: 0.2.0
fecha_alta: 2026-10-21
fecha_ultimo_cambio: 2026-10-28
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
depende_de: [docs/REQUIREMENTS.md, docs/POC-STATUS.md]
lo_usan: [README.md, docs/HOST-YOUR-OWN.md, docs/JOIN.md]
docs: [docs/DESIGN.md]
scripts: [poc/cilium.sh, poc/spire-args.sh, poc/checks-cilium.sh, poc/issue.sh, poc/gateway.sh, poc/cilium-node-guard.yaml, poc/node-trust-freeze.yaml]
runbooks: []
tags: [problemas, diagnostico, spire, cilium, nebula, kyverno]
---

# Troubleshooting

Every problem below was hit for real while building the PoC. Each has the **symptom**, the **cause**, the **fix** and the **command that diagnoses
it**. `poc/.state/poc.log` has the detail of the last run of any PoC script (`/tmp/emesh-poc-host.log` for `poc-host`).

## Nebula and the join

### The join fails with exit code 5: "cannot reach the lighthouse"
- **Cause:** UDP 4242 to the lighthouse is blocked (corporate or hotel network), or the bundle points at an address you cannot reach.
- **Diagnose:** `tail /var/log/nebula.log`; `grep -A1 static_host_map /etc/nebula/config.yml`.
- **Fix:** allow outbound UDP 4242. The host's relay does not help here: it is reached over the same UDP port. (The relay is for the
  other case: both ends behind symmetric NAT, which gives no exit code at all, see below.)

### Joined, the lighthouse answers, but the control plane does not (both ends behind symmetric NAT)
- **Cause:** a symmetric NAT gives a new public port for every destination, so hole punching cannot work when both ends have one (some carrier
  NAT, some corporate routers). Each end reaches the lighthouse; they never reach each other. `make poc-nat` reproduces it (N15).
- **Diagnose:** on the joiner, `ping 10.77.0.1` works and `ping 10.77.0.2` does not; `grep "Handshake timed out" /var/log/nebula.log`
  names the control plane.
- **Fix (the host):** turn the relay on: `poc/lighthouse-vps.sh sign <public-host> <lh.pub> --relay`, then `deploy --ssh ...` and
  `poc/mesh host refresh cp`; bundles issued after that name the relay. With it, `grep relayed /var/log/nebula.log` shows the tunnel through
  it. Every relayed byte crosses the lighthouse's server twice (DESIGN.md 15.1).

### `edge-mesh join` exits 7: "the kubelet did not start on this machine" (WSL2 with Docker Desktop)
- **Cause:** the kubelet parses the mount table and refuses a line with a space in its options. Docker Desktop's WSL integration mounts its
  resources folder (`C:\Program Files\Docker\...`) into every distro it integrates with, and that line has one. Measured 2026-10-28 (WSL2 kernel 6.18,
  k3s v1.37.1): `Failed to start ContainerManager ... system validation failed - wrong number of fields (expected 6, got 7)`.
- **Diagnose:** `awk 'NF != 6' /proc/mounts` prints the line.
- **Fix:** join from a distro without Docker Desktop's integration (JOIN.md, "In WSL2"). Before 2026-10-28 the join printed `joined` here
  although no Node ever appeared; it now waits for the Node and says why it is not there.

### A fresh certificate is "not yet valid" / the handshake fails right after the join
- **Cause:** the node's clock is behind the machine that signed the certificate (seen with Incus VMs about 1 s behind).
- **Diagnose:** `date -u` on both sides; `nebula-cert print -path host.crt` (`notBefore`).
- **Fix:** sync the clock (NTP). The VM flow does `date -s @<host time>` before pushing a bundle (`poc/vm/lib-vm.sh`, `push_bundle`).
- **Also seen as** `Could not load client cert ... nebula certificate for this host is expired` in the first start of a new server (a clock behind makes the certificate look not valid yet). With `lighthouse-vps.sh` the systemd unit restarts after 5 s and `deploy` waits for it to settle; a real server has NTP, so this only shows on a fresh VM.

### The node joined, then lost the mesh after the certificate's lifetime
- **Cause:** by design. `pki.disconnect_invalid: true` closes tunnels whose certificate expired. Pool slots are 1 h.
- **Fix:** the host issues a new bundle. An expired certificate is rejected (`J15`).

### "certificate is in the block list"
- **Cause:** the node was revoked (`mesh host kick` / `revoke.sh`). Deleting the Kubernetes node alone would not keep it out; the blocklist does.
- **Diagnose:** `grep blocklist /etc/nebula/config.yml`.

### Reload after editing the blocklist does nothing
- **Cause:** the k3s image has no `pkill`; `SIGHUP` was never sent.
- **Fix:** `kill -HUP $(pidof nebula)` (`revoke.sh` does).

### Nebula works, the pods cannot talk
- **Cause:** the Nebula range overlaps the pod network. k3s uses `10.42.0.0/16`.
- **Fix:** the PoC uses `10.77.0.0/24`. Do not choose `10.42.x.x`.

### Nodes are NotReady, `kubectl logs/exec` fails, pods on other nodes are unreachable after narrowing the firewall
- **Cause:** a port outside the allow-list. The list is ICMP, TCP 6443, TCP 10250 (from group `control-plane`), UDP 51871, and UDP 8472 only with flannel.
- **Diagnose:** run the capture the suite uses (`underlay_ports` in `poc/checks-cilium.sh`: tcpdump on `nebula1` while pods talk) and compare. A
  *dropped* packet never appears on `nebula1`; look at the sender's side. `FW=open` (any/any) as a bisection tool, never to keep.
- **Fix:** add the port in `issue.sh` (`INBOUND`). Not needed with Cilium and node encryption: TCP 4240 and TCP 4250.

### The reachable API is also reachable on the underlay
- **Check:** `H02` asserts the API answers on the Nebula IP and not on the Docker/VM address. k3s must run with `--bind-address`, `--advertise-address`
  and `--tls-san` set to the Nebula IP.

## k3s and the container runtimes

### Cilium pods stay in `CreateContainerError` (k3s with flannel off)
- **Cause:** k3s's containerd looks for CNI config/plugins in its own directories while Cilium writes to `/etc/cni/net.d` and `/opt/cni/bin`.
- **Fix:** a containerd drop-in `config-v3.toml.d/10-cni.toml` with `conf_dir = '/etc/cni/net.d'` and `bin_dirs = ['/opt/cni/bin', '/var/lib/rancher/k3s/data/cni']`
  (the k3s bundled `loopback` must stay on the path). `up.sh`, `vm/up.sh` and `edge-mesh` write it when `CNI=cilium`.
- **Diagnose:** `kubectl -n kube-system describe pod <cilium pod>`; `ls /etc/cni/net.d`.
- **Flannel vs Cilium:** the server needs `--flannel-backend=none --disable-network-policy` for Cilium; the **agent has no such flag** (it follows the server).

### Cilium fails in Docker: "path /sys/fs/bpf is mounted on /sys but it is not a shared mount"
- **Cause:** in a container `/sys/fs/bpf` is not a mount. A VM or real host gets it from systemd.
- **Fix:** `mount -t bpf bpf /sys/fs/bpf; mount --make-rshared /` in the node container (`up.sh` does it when `CNI=cilium`).

### kubelet fails with "cannot enter cgroupv2 ... invalid state" (k3s in Docker)
- **Cause:** the PoC replaces the image entrypoint, so the processes are not moved out of the cgroup root.
- **Fix:** do the same move the image's entrypoint does (`up.sh`, `lib.sh` `new_container`).

### The tunnel dropped and came back, but the node stays `NotReady` for ever (k3s agent gone, flannel panic)
- **Symptom:** `kubectl get nodes` shows the node `NotReady`, taints `node.kubernetes.io/unreachable`; Nebula is up and the lighthouse answers `ping`, yet nothing recovers.
  `/var/log/k3s-agent.log` ends with `panic: runtime error: index out of range [0] with length 0` in `flannel ... vxlan.(*network).reCreateVxlan` (flannel v0.28.4 inside k3s v1.37.1).
- **Cause:** when Nebula stops, `nebula1` loses its address; the embedded flannel (the default CNI here, `--flannel-iface nebula1`) panics when it rebuilds the VXLAN device and takes the whole
  k3s agent down. Started with a bare `nohup`, nobody starts it again.
- **Fix (in the join command since 2026-10-26):** `edge-mesh join` runs the agent in a restart loop (5 s), like a Deployment would. A restart needs no valid token: the agent reuses the
  credentials it stored at the first join (tested with a bogus token; the invite token lives 8 minutes at most and is burned once the node registers). Measured: the node is Ready again 20 s after the tunnel returns (`W03b`). If you start
  the agent by hand, run it under `systemd` with `Restart=always`. Cilium instead of flannel does not have this panic.
- **Diagnose:** `tail -20 /var/log/k3s-agent.log` on the node; `pidof k3s` empty means the agent is dead.

### Blocking the lighthouse with `iptables` in `DOCKER-USER` has no effect
- **Cause:** it does not filter traffic between containers on the same bridge. The test uses a blackhole route in the joining container.

### `nc` not found when testing the firewall
- **Cause:** the k3s image has none. The test uses the netshoot image in the node's namespace (`nsx` in `poc/lib.sh`).

## Cilium, WireGuard, SPIRE

### `spire-server` starts and stops in the same second, never opens :8080, exit 137 (liveness kills it)
- **Symptom:** logs `Starting Server APIs` then `Stopping Server APIs`, no error; the spire-agents stay in `Init`. Same on three kernels, with or without a PVC.
- **Cause:** the chart mounts a hostPath socket directory (`/var/run/spire-server/sockets`) that Kubernetes creates `root:root 0755`, and the
  image runs as uid 1000, so the server cannot create `api.sock`.
- **Diagnose:** `kubectl -n cilium-spire exec spire-server-0 -c cilium-init -- sh -c 'grep ^Uid: /proc/$(pgrep spire-server)/status'`; on the node `ls -ld /var/run/spire-server/sockets`.
- **Fix:** a one-shot root initContainer that `chown`s the directory (and the data volume) to 1000 (`poc/spire-args.sh`). The first PoC ran the server as root
  instead (`runAsUser=0`); that is no longer needed.

### The SPIRE registration entries are never created ("entries=0"), mutual auth never works, no error
- **Cause:** the chart's `cilium-init` sidecar reaches the server through `/proc/<pid>/root`. A different uid, root included without `SYS_PTRACE`, is
  refused (`Permission denied`). With the server at 1000 and the sidecar at root, nothing registers.
- **Diagnose:** `kubectl -n cilium-spire logs spire-server-0 -c cilium-init` (it loops "Waiting for spire-server to start"); `spire_cli entry show` in `checks-cilium.sh`.
- **Fix:** run the **whole pod** as uid 1000 (`podSecurityContext.runAsUser`), keeping only the initContainer at root (`poc/spire-args.sh`).

### Mutual auth: traffic stays dropped after applying the `authentication.mode: required` policy (the sticky failure)
- **Symptom:** `curl` between the two pods times out for ever. `cilium-dbg monitor -t drop` shows `Authentication required`; `cilium-dbg bpf auth list` is empty;
  `cilium_datapath_signals_handled_total{signal=auth_required}` grows but no `Failed to authenticate request` is ever logged. `ss -tin` on a node shows one
  connection to the other node's **TCP 4250** with `Send-Q` stuck (for example 1483), `bytes_acked:1` and a retransmit timer in the minutes.
- **Root cause (found 2026-10-21):** the handshake between two Cilium agents is host-to-host TCP on port 4250 and it goes through the node-to-node WireGuard
  (`cilium_wg0`, MTU about 1055). The host stack computes its MSS from `nebula1` (MTU 1300, MSS 1248), so full-size or GSO segments are larger than `cilium_wg0`
  can carry; they are dropped silently, no ICMP "fragmentation needed" comes back (a GSO packet never gets one), and TCP never learns. The first small packets
  (SYN, a ClientHello) pass, the TLS certificates (about 1.5 KB) do not. The auth manager keeps that key "pending" for ever, so every later request is skipped
  without a log line. Whether a given run stalls depends on what packet sizes happened earlier on that node pair, hence the "random" failures: 5 of 7 Dell runs
  with the PVC before the fix (a PVC is not the cause; it only changed the timing).
- **Diagnose:** `kubectl -n kube-system exec <cilium pod> -c cilium-agent -- cilium-dbg metrics list | grep -iE 'signal|Authentication'`; on both nodes
  `ss -tin 'dport = :4250 or sport = :4250'`; `tcpdump -i any -nn 'tcp port 4250'` (retransmissions of a 1248-byte segment, small segments acked with SACK).
  A host-to-host bulk transfer shows the same thing: `head -c 2000000 /dev/zero | nc <other node's Nebula IP> <port>` stalls.
- **Fix:** advertise a small MSS on the mesh route on every node (the PoC does it in `edge-mesh join`, `up.sh` and `vm/up.sh` when `CNI=cilium`):
  `ip route replace 10.77.0.0/24 dev nebula1 scope link advmss 860`. Segments are then 912 bytes on the wire, below the WireGuard device MTU. It does not touch UDP,
  so the WireGuard traffic itself keeps the 1300 Nebula MTU. A stuck connection from before the fix needs the Cilium agents restarted
  (`kubectl -n kube-system rollout restart ds/cilium`).
- **Also keep:** apply authentication policies only after the registration entries exist and the delegate stream has been quiet for 25 s (`spire_full`
  in `checks-cilium.sh`); an authentication that fails before SPIRE has the entries is not retried.
- Mutual auth is beta in this Cilium: keep it opt-in per policy.

### Cilium agents unhealthy or WireGuard shows no peers
- **Diagnose:** `kubectl -n kube-system exec <cilium pod> -c cilium-agent -- cilium-dbg status --brief` and `cilium-dbg encrypt status` (expect `Wireguard`,
  `Number of peers:` the number of nodes minus one, and its `Public key` equal to the node's `network.cilium.io/wg-pub-key` annotation on its `CiliumNode`).
  On the node, `wg show cilium_wg0 peers` must list exactly the other nodes' published keys (`V06b`).
- **Check:** UDP 51871 allowed in the Nebula firewall; MTU chain (Nebula 1300 > pod 1150 + 130). A full-size DF ping across nodes is `V12`.

### Large transfers stall but small pings work
- **Cause:** MTU: Nebula 1300, VXLAN 50, WireGuard up to 80. The pod MTU is derived once from `NEBULA_MTU` in `poc/stack.env` (1150); never set
  a second number, and do not leave Cilium's default 1500.

### A pod on a joined node is dropped by a policy with `authentication.mode: required` ("Authentication required")
- **Symptom:** `cilium-dbg metrics list` on the receiving node shows `cilium_drop_count_total ... reason=Authentication required` growing, and its agent
  logs `Failed to authenticate request ... remote error: tls: internal error` for the pod's node.
- **Cause:** that node has no SPIRE agent, so its Cilium agent holds no certificate for the pod's identity. This is the deny half working (`V101`). If the
  node should have one: the agent needs a toleration for every taint the node carries; joined nodes carry `trust=<level>:NoSchedule`, which the
  chart's defaults do not cover (`spire-args.sh` adds it). `kubectl -n cilium-spire get pod -l app=spire-agent -o wide` must list every node.

### Another node's WireGuard key or address changed in its `CiliumNode`
- **Cause:** every Cilium agent runs under one service account that may update every `CiliumNode`; its token is on each node's disk. A node owner can
  use it to rewrite another node's key.
- **Fix:** `poc/cilium-node-guard.yaml` (applied by `cilium.sh`) admits an agent's write only to the `CiliumNode` of the node its token was issued
  on. Check it is there: `kubectl get validatingadmissionpolicybinding cilium-agent-own-ciliumnode`. `V06c` tests it.

### Envoy Gateway's chart fails: "Installing experimental CRDs on top of standard channel CRDs is prohibited"
- **Cause:** k3s 1.37 installs the Gateway API CRDs (standard channel) and Gateway API's `safe-upgrades` admission policy; Envoy Gateway's main chart
  carries the experimental channel.
- **Fix:** what `gateway.sh install` does: Envoy Gateway's own CRDs from `gateway-crds-helm` (rendered, `kubectl apply --server-side`; as a Helm release
  they exceed the 1 MiB release Secret), then the main chart with `--skip-crds`.

### A joined machine relabelled a node (`own` appeared where it should not, or vanished from cp)
- **Cause:** the k3s controller credential every joined machine holds (`k3scontroller.kubeconfig`) may `patch nodes/status` on every Node, and a status
  patch can carry labels; NodeRestriction only watches kubelets.
- **Fix:** `poc/node-trust-freeze.yaml` (`up.sh` applies it): only `system:masters` changes the `node-restriction.kubernetes.io/` labels or the `trust`
  taint. Check: `kubectl get validatingadmissionpolicybinding edge-mesh-node-trust-frozen`; `G09b` and `L52b` test it.

### The router shows no session with the own node, or a route through a joined node
- **Diagnose:** `kubectl get ciliumbgpnodeconfigs` lists only `own` nodes; on an own node `cilium-dbg shell -- bgp/peers` shows the router
  `established`; on the router (FRR) `show bgp summary` and `show ip bgp`.
- **Remember:** the router must list only your own nodes as neighbours; a connection from any other address is logged as
  `not configured and not valid for dynamic` and gets no session (`G11`).

## Kyverno and placement

### No pod can be created; `kubectl run` hangs or says the webhook is unavailable
- **Cause:** the policy has `failurePolicy: Fail` and Kyverno runs one admission replica pinned to the control plane. If it is down, nothing is admitted
  (outside the excluded system namespaces).
- **Diagnose:** `kubectl -n kyverno get pods -o wide`; `kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations | grep kyverno`.
- **Fix:** bring the admission controller back. HA (3 replicas) and a break-glass exception were not tested. Remove the policy with `kubectl delete cpol edge-mesh-placement` if you must.

### A private pod is rejected with "failed to check deny conditions ... permission denied"
- **Cause:** Kyverno's admission controller may not read Nodes (the rule looks the node's trust label up for `spec.nodeName`).
- **Fix:** the `ClusterRole edge-mesh-kyverno-read-nodes` (label `rbac.kyverno.io/aggregate-to-admission-controller`) in `trust-placement.yaml`. Allow a few seconds for it to aggregate.

### A shareable pod is rejected although it sets `automountServiceAccountToken: false`
- **Cause:** a JMESPath `x || \`true\`` treats `false` as empty. The rule compares `to_string(...)` with `"false"`.

### A shareable pod without limits is admitted
- **Cause:** `!resources.limits.cpu` in a JMESPath filter did not match. Use `resources.limits.cpu == \`null\``.

### A label on a node is refused: "A friends or public node must carry the taint"
- **Cause:** by design (`node-trust-vap.yaml`): label and taint travel together; taint removal is refused too.
- **Fix:** join with `TAINT=yes` (the node registers with the taint), then label it.

### My own pod with `tolerations: [{operator: Exists}]` (a common debugging habit) is refused
- **Cause:** by design for private pods: it would tolerate the shared taints. Label it `shareable` with `share-with`, or run it in `kube-system`/`kyverno`.

## Running the PoC

### `docker: command not found` from `mesh` on a host without Docker
- **Cause:** every tool in `poc/` defaults to `RUNTIME=docker`.
- **Fix:** `export RUNTIME=incus` once per shell (HOST-YOUR-OWN.md section 0). An invite that fails midway is rolled back, so you can simply run it again.

### `a player called alice already exists`
- **Cause:** a name is used once, even after `kick` or `revoke` (its certificate stays on the blocklist).
- **Fix:** invite under a new name.

### `VM limit reached (MAX_VMS=4)`
- **Cause:** the VM flow keeps at most 4 machines (lighthouse, control plane and two players on a small host).
- **Fix:** `./mesh host kick <name>` frees one, or raise `MAX_VMS` if the host has the RAM.

### `edge-mesh join` exits 7: the control plane rejected the join token
- **Cause:** the token expired (8 minutes at most, `TOKEN_TTL`), or a node already registered with it and the host burned it (`mesh host burn`, run by `reap`, `players` and `revoke`).
- **Fix:** ask the host for a new invite under a new name. Diagnose with `tail /var/log/k3s-agent.log` (`failed to retrieve configuration from server: not authorized`).

### `edge-mesh join` exits 7: "refused this node name with this bundle's node password"
- **Cause:** the host makes a node password for every bundle name (k3s secret `kube-system/<name>.node-password.k3s`); the agent presented another one:
  `/etc/rancher/node/password` on the machine is not the bundle's `NODE_PASSWORD`, or a node already registered under this name with another password.
- **Fix:** ask the host for a new invite under a new name. Diagnose with `grep -i 'password rejected' /var/log/k3s-agent.log`.

### `edge-mesh join` exits 6: not root
- **Fix:** run it as root (`sudo`). Nebula creates a network device and k3s mounts filesystems.

### Every step prints "command not found" and the suite still says `RESULT PASS`
- **Cause:** CRLF line endings in the scripts (a Windows checkout or an editor). The suite counts failed steps, and no step ran.
- **Fix:** `sed -i 's/\r$//' poc/*.sh poc/vm/*.sh poc/mesh poc/edge-mesh`; the repo's `.gitattributes` forces LF. Do not run a sed over the cached binaries in `poc/.cache`.

### `kind delete cluster` hangs under WSL; background runs vanish
- **Cause:** the Windows `kind.exe` found through the PATH interop hangs; and a process started with `nohup` inside a one-off `wsl -- ...` call is killed when that call returns.
- **Fix:** install the Linux `kind` (`~/bin/kind`, put first in `PATH`) and run the script in the foreground of the `wsl` call.

### Two PoC runs at once break each other
- **Cause:** one state directory and one set of container/VM names. Run one script at a time; `down.sh` removes everything. A game you keep next to the
  tests needs its own `EMESH_STATE` and Incus project (HOST-YOUR-OWN.md, "A second game next to the PoC tests").

### After `mesh host refresh cp` the API answers "connection refused"
- **Cause:** restarting Nebula takes the address off `nebula1` for a moment, and the k3s server's embedded flannel exits ("failed to find IPv4 address for
  interface nebula1"). Measured 2026-10-27 on a control plane started with `nohup`: nothing started it again.
- **Fix:** `mesh host refresh` now reloads Nebula (SIGHUP) unless `stats` or `listen` changed; when it must restart, on `cp` it stops k3s first and starts it
  again (systemd unit, or `/etc/emesh/k3s-server.sh`) once `nebula1` has its address, then waits for `/readyz` (P12d). A control plane created before this
  change has no `/etc/emesh/k3s-server.sh`: start k3s there by hand.
- **Diagnose:** `tail /var/log/k3s.log` on cp; `./kc get --raw /readyz`.

### The Dell run is slow or stops
- Limits kept: at most 4 VMs, `/` below 85 %, pause above 85 degrees C on the CPU package. `incus list emesh-` shows what is left; `RUNTIME=incus ./vm/down.sh` removes it.
