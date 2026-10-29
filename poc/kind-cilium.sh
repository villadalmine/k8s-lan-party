#!/usr/bin/env bash
# make poc-kind: Cilium on kind (default CNI off) with tunnel mode + WireGuard (+ SPIRE as a last step), and the same assertions as step 2.
# Shows Cilium itself works on this machine's kernel; Nebula is NOT in the path here (kind's own Docker network is the underlay),
# so the capture is on the node's eth0 and the MTU chain starts from 1500 minus nothing. Needs kind, helm, kubectl, Docker.
set -u
. "$(dirname "$0")/lib.sh"
KIND_NODE_IMAGE="${KIND_NODE_IMAGE:-kindest/node:v1.37.0}"
NEB_IF=eth0   # POD_MTU comes from stack.env (derived from NEBULA_MTU): the same value as on the mesh
nn() { case "$1" in cp) echo emesh-control-plane;; n1) echo emesh-worker;; n2) echo emesh-worker2;; esac; }
k() { kubectl --context kind-emesh "$@"; }
nx() { local n="$1"; shift; docker exec "$(nn "$n")" "$@"; }
tcpd() { local n="$1"; shift; docker run --rm --network "container:$(nn "$n")" --cap-add NET_ADMIN --cap-add NET_RAW "$NETSHOOT_IMAGE" timeout 30 tcpdump "$@"; }
. "$POC/checks-cilium.sh"
mkdir -p "$STATE"; : >"$LOG"
cfg() { cat >"$STATE/kind.yaml" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking: {disableDefaultCNI: true, podSubnet: 10.42.0.0/16}
nodes: [{role: control-plane}, {role: worker}, {role: worker}]
YAML
}
create() { kind delete cluster --name emesh >/dev/null 2>&1; cfg && kind create cluster --name emesh --image "$KIND_NODE_IMAGE" --config "$STATE/kind.yaml" --wait 0s; }
install() { helm upgrade --install cilium cilium --repo https://helm.cilium.io --version "$CILIUM_VERSION" -n kube-system --kube-context kind-emesh \
  --set routingMode=tunnel --set tunnelProtocol=vxlan --set encryption.enabled=true --set encryption.type=wireguard --set encryption.nodeEncryption=true \
  --set MTU="$POD_MTU" --set kubeProxyReplacement=false --set ipam.mode=kubernetes --set operator.replicas=1 \
  --wait --timeout 8m; }
step K01 "kind cluster, default CNI off, 1 control plane + 2 workers" create
step K02 "Cilium $CILIUM_VERSION installed (tunnel mode, WireGuard) with helm --wait" install
step K03 "all three nodes Ready" wait_for 300 all_ready
step K04 "Cilium agent healthy on every node" wait_for 180 agents_ok
step K05 "WireGuard on, two peers on every node" wait_for 120 wg_on
docker pull -q "$NETSHOOT_IMAGE" >/dev/null 2>&1; test_pods >>"$LOG" 2>&1
step K06 "test pods Running on different nodes" wait_for 240 pods_ready
step K07 "pod on n2 reaches pod on n1 across nodes" wait_for 60 cross_node
step K08 "DNS inside a pod resolves cluster services" wait_for 120 dns_ok
step K09 "on the wire: the clear text exists inside the node (cilium_wg0, VXLAN payload) and never on the underlay interface" on_the_wire
pod_mtu() { [ "$(k exec cli -- cat /sys/class/net/eth0/mtu)" = "$POD_MTU" ]; }
step K10 "MTU: pod eth0 is $POD_MTU" pod_mtu
step K11 "MTU: a full-size DF packet crosses, one byte more is refused" df_ping
step K12 "30 MB pod-to-pod over WireGuard arrives intact" big_transfer
mutual_up() { . "$POC/spire-args.sh"; helm upgrade cilium cilium --repo https://helm.cilium.io --version "$CILIUM_VERSION" -n kube-system --kube-context kind-emesh --reuse-values "${SPIRE_ARGS[@]}" --wait --timeout 4m; }
k label node emesh-control-plane node-restriction.kubernetes.io/trust=own --overwrite >>"$LOG" 2>&1
step K90 "mutual authentication: SPIRE installed on top (helm --wait)" mutual_up
step K91 "SPIRE server and agents ready, the 3 registration entries created" wait_for 240 spire_full
step K92 "policy with authentication.mode=required: traffic allowed, auth entry present" mutual_ok
step K93 "SPIRE server runs as uid 1000, not root" spire_nonroot
step K94 "SPIRE server pinned to an own node" spire_on_own
step K95 "SPIRE datastore on a bound PVC" wait_for 30 spire_pvc
step K96 "SPIRE state survives a server restart (same CA bundle, same entries)" spire_survives_restart
step K97 "mutual authentication still works after the restart" mutual_after_restart
[ "${KEEP:-0}" = 1 ] || kind delete cluster --name emesh >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
