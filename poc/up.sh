#!/usr/bin/env bash
# Bring up the PoC: Nebula CA + lighthouse, a k3s control plane on its Nebula IP, then the two outside nodes
# joining with "one bundle + one command" (edge-mesh join). Idempotent: down.sh first if you want a clean slate.
set -eu
. "$(dirname "$0")/lib.sh"

fetch_nebula() {
  [ -x "$CACHE/nebula" ] && return 0
  curl -sL "https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/nebula-linux-$ARCH.tar.gz" | tar xz -C "$CACHE"
}

node() { # node <name> <docker-ip>
  local n="$1" ip="$2"
  docker rm -f "$n" >/dev/null 2>&1 || true
  docker run -d --name "$n" --hostname "$n" --privileged --network "$DOCKER_NET" --ip "$ip" \
    --tmpfs /run --tmpfs /var/run \
    -v "$CACHE:/opt/nebula:ro" -v "$POC/edge-mesh:/usr/local/bin/edge-mesh:ro" \
    -v "$STATE/bundles/$n:/bundle" --entrypoint sleep "$K3S_IMAGE" infinity >/dev/null
  # cgroup v2 "no internal processes": what the k3s image entrypoint does; we replace the entrypoint, so do it here
  docker exec "$n" sh -c 'mkdir -p /sys/fs/cgroup/init && xargs -rn1 </sys/fs/cgroup/cgroup.procs >/sys/fs/cgroup/init/cgroup.procs; sed -e "s/ / +/g" -e "s/^/+/" </sys/fs/cgroup/cgroup.controllers >/sys/fs/cgroup/cgroup.subtree_control'
  # Cilium mounts /sys/fs/bpf into its pods; in a container it is not a mount, and kubelet refuses a non-shared one
  # ("path /sys/fs/bpf is mounted on /sys but it is not a shared mount"). A real host/VM has it from systemd.
  [ "${CNI:-flannel}" = cilium ] && docker exec "$n" sh -c 'mountpoint -q /sys/fs/bpf || mount -t bpf bpf /sys/fs/bpf; mount --make-rshared /'
  return 0
}

# CNI=flannel (step 1: k3s default over the Nebula interface) | cilium (step 2: tunnel + WireGuard, see cilium.sh)
CNI="${CNI:-flannel}"; export TAINT="${TAINT:-no}"
if [ "$CNI" = flannel ]; then CNI_FLAGS="--flannel-iface nebula1"; else CNI_FLAGS="--flannel-backend=none --disable-network-policy"; fi
fetch_nebula
docker network inspect "$DOCKER_NET" >/dev/null 2>&1 || docker network create --subnet "$DOCKER_SUBNET" "$DOCKER_NET" >/dev/null

# 1. lighthouse and control plane (the maintainer's side: Nebula first, k3s on the Nebula IP only)
# With LH_EXTERNAL the lighthouse is a public machine installed by lighthouse-vps.sh: it is not created here.
MACHINES="lh cp"; [ -z "${LH_EXTERNAL:-}" ] || MACHINES="cp"
[ -n "${LH_EXTERNAL:-}" ] || "$POC/issue.sh" lh "$LH_NEB" lighthouse 24h
"$POC/issue.sh" cp "$CP_NEB" own 24h
[ -n "${LH_EXTERNAL:-}" ] || node lh "$LH_DOCKER_IP"
node cp 172.31.0.3
for n in $MACHINES; do
  docker exec "$n" sh -c 'mkdir -p /etc/nebula && cp /bundle/* /etc/nebula/ && nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &'
done
# Host TCP over the Cilium node-to-node WireGuard: the default MSS comes from nebula1 (NEBULA_MTU 1300 -> 1248), but cilium_wg0 is 1055, and oversized
# (GSO) segments are dropped silently with no ICMP, so TCP never learns. Cilium's own agent handshake (mutual auth, TCP 4250) stalls this way.
# An advertised MSS on the mesh route keeps host segments under the WireGuard device MTU. Only with Cilium (flannel needs nothing).
wait_for 40 docker exec cp ping -c1 -W1 "$LH_NEB" || { echo "the control plane cannot reach the lighthouse $LH_NEB at $LH_DOCKER_IP:${LH_PORT:-4242}/udp (external lighthouse installed? port open?)"; exit 4; }
[ "$CNI" = cilium ] && docker exec cp ip route replace "${LH_NEB%.*}.0/24" dev nebula1 scope link advmss "${HOST_MSS:-860}"
if [ "$CNI" = cilium ]; then
  docker exec cp sh -c "mkdir -p /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d && printf \"[plugins.'io.containerd.cri.v1.runtime'.cni]\n  conf_dir = '/etc/cni/net.d'\n  bin_dirs = ['/opt/cni/bin', '/var/lib/rancher/k3s/data/cni']\n\" >/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/10-cni.toml"
fi
# the start command is kept in the machine, so `mesh host refresh cp` can start the server again after a Nebula restart (lib.sh cp_k3s_start)
echo "exec k3s server --node-ip $CP_NEB --advertise-address $CP_NEB --bind-address $CP_NEB --tls-san $CP_NEB \
  --node-name cp $CNI_FLAGS --disable=traefik,servicelb,metrics-server \
  --node-taint node-role.kubernetes.io/control-plane:NoSchedule --write-kubeconfig-mode 644 >>/var/log/k3s.log 2>&1" |
  docker exec -i cp sh -c 'mkdir -p /etc/emesh && cat >/etc/emesh/k3s-server.sh'
docker exec -d cp sh /etc/emesh/k3s-server.sh
wait_for 120 docker exec cp kubectl get --raw /readyz
docker exec cp sed "s#127.0.0.1:6443#$CP_NEB:6443#" /etc/rancher/k3s/k3s.yaml >"$STATE/kubeconfig"
wait_for 90 docker exec cp kubectl get node cp   # the API answers before the node object exists
k label node cp node-restriction.kubernetes.io/trust=own --overwrite >/dev/null
# from here on, a node's trust (labels node-restriction.kubernetes.io/*, the trust taint) changes only with the host's admin credential (node-trust-freeze.yaml)
k apply -f - <"$POC/node-trust-freeze.yaml" >/dev/null

[ "${HOST_ONLY:-0}" = 1 ] && { echo "host up"; exit 0; }
# 2. outside nodes: issue the bundle (cert + config + join token), then ONE command on the node
"$POC/issue.sh" n1 "$N1_NEB" friends 24h
"$POC/issue.sh" n2 "$N2_NEB" public 1h
node n1 172.31.0.4; node n2 172.31.0.5
docker exec n1 edge-mesh join /bundle --accept-golden-rule
docker exec n2 edge-mesh join /bundle --accept-golden-rule
wait_for 120 sh -c "docker exec cp kubectl get node n1 n2 >/dev/null 2>&1"
# the issuing side labels the node (a kubelet cannot set labels under node-restriction.kubernetes.io/)
k label node n1 node-restriction.kubernetes.io/trust=friends --overwrite >/dev/null
k label node n2 node-restriction.kubernetes.io/trust=public --overwrite >/dev/null
echo up
