#!/usr/bin/env bash
# Bring up the PoC on real VMs (Incus): a lighthouse, a control plane and two nodes, each its own kernel (Ubuntu 24.04), joined with
# the same `edge-mesh join` the Docker flow uses. Run it on a Linux host that has Incus (and sudo-less `incus`). Idempotent: down.sh first.
# Limits (the Dell this was written on): 1 vCPU/1 GiB for the lighthouse, 2 vCPU/3 GiB for the other three (4 VMs, 9 GiB).
set -eu
export RUNTIME=incus
. "$(dirname "$0")/../lib.sh"
. "$POC/vm/lib-vm.sh"

CNI="${CNI:-flannel}"; export CNI TAINT="${TAINT:-no}"
fetch
incus network show "$VMNET" >/dev/null 2>&1 || incus network create "$VMNET" ipv4.address=$UNDERLAY_NET.1/24 ipv4.nat=true ipv6.address=none >/dev/null
# With LH_EXTERNAL the lighthouse is a public machine installed by lighthouse-vps.sh: no lh VM, no lh bundle here.
MACHINES="lh cp"; [ -z "${LH_EXTERNAL:-}" ] || MACHINES="cp"
[ -n "${LH_EXTERNAL:-}" ] || vm_up lh 1 1GiB "${VMIP[lh]}"
vm_up cp 2 3GiB "${VMIP[cp]}"
[ "${PERSIST:-0}" != 1 ] || incus config set emesh-cp boot.autostart=true
[ "${HOST_ONLY:-0}" = 1 ] || { vm_up n1 2 3GiB "${VMIP[n1]}"; vm_up n2 2 3GiB "${VMIP[n2]}"; }
[ -n "${LH_EXTERNAL:-}" ] || "$POC/issue.sh" lh "$LH_NEB" lighthouse 24h
# a throw-away control plane lives a day; a persistent one (PERSIST=1) a season, renewed before it ends (docs/HOST-YOUR-OWN.md)
CP_CERT="${CP_CERT_DURATION:-24h}"; [ "${PERSIST:-0}" != 1 ] || CP_CERT="${CP_CERT_DURATION:-4320h}"
"$POC/issue.sh" cp "$CP_NEB" own "$CP_CERT"
for n in $MACHINES; do push_bundle $n; start_nebula $n; done
# Host TCP over the Cilium node-to-node WireGuard: the default MSS comes from nebula1 (NEBULA_MTU 1300 -> 1248), but cilium_wg0 is 1055, and oversized
# (GSO) segments are dropped silently with no ICMP, so TCP never learns. Cilium's own agent handshake (mutual auth, TCP 4250) stalls this way.
# An advertised MSS on the mesh route keeps host segments under the WireGuard device MTU. Only with Cilium (flannel needs nothing).
wait_for 60 incus exec emesh-cp -- ping -c1 -W1 "$LH_NEB" || { echo "the control plane cannot reach the lighthouse $LH_NEB at $LH_DOCKER_IP:${LH_PORT:-4242}/udp (external lighthouse installed? port open?)"; exit 4; }
[ "$CNI" = cilium ] && incus exec emesh-cp -- ip route replace "${LH_NEB%.*}.0/24" dev nebula1 scope link advmss "${HOST_MSS:-860}"
[ "$CNI" = cilium ] && incus exec emesh-cp -- sh -c "mkdir -p /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d && printf \"[plugins.'io.containerd.cri.v1.runtime'.cni]\n  conf_dir = '/etc/cni/net.d'\n  bin_dirs = ['/opt/cni/bin', '/var/lib/rancher/k3s/data/cni']\n\" >/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/10-cni.toml"
cni_flags="--flannel-iface nebula1"; [ "$CNI" = cilium ] && cni_flags="--flannel-backend=none --disable-network-policy"
start_k3s_server "--node-ip $CP_NEB --advertise-address $CP_NEB --bind-address $CP_NEB --tls-san $CP_NEB \
  --node-name cp $cni_flags --disable=traefik,servicelb,metrics-server \
  --node-taint node-role.kubernetes.io/control-plane:NoSchedule --write-kubeconfig-mode 644"
wait_for 180 incus exec emesh-cp -- k3s kubectl get --raw /readyz
incus exec emesh-cp -- sh -c "sed 's#127.0.0.1:6443#$CP_NEB:6443#' /etc/rancher/k3s/k3s.yaml >/root/kubeconfig"
incus file push -q "$CACHE/helm" emesh-cp/usr/local/bin/helm; incus exec emesh-cp -- chmod +x /usr/local/bin/helm
incus exec emesh-cp -- ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl
incus file push -q -r "$POC/labs" emesh-cp/
wait_for 90 incus exec emesh-cp -- k3s kubectl get node cp
k label node cp node-restriction.kubernetes.io/trust=own --overwrite >/dev/null
[ "${HOST_ONLY:-0}" = 1 ] && { echo "host up"; exit 0; }
for p in "n1 $N1_NEB friends" "n2 $N2_NEB public"; do
  set -- $p; "$POC/issue.sh" "$1" "$2" "$3" 24h; push_bundle "$1"
  incus exec "emesh-$1" -- edge-mesh join /bundle --accept-golden-rule
done
wait_for 180 sh -c "incus exec emesh-cp -- k3s kubectl get node n1 n2 >/dev/null 2>&1"
k label node n1 node-restriction.kubernetes.io/trust=friends --overwrite >/dev/null
k label node n2 node-restriction.kubernetes.io/trust=public --overwrite >/dev/null
echo up
