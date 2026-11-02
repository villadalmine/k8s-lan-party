#!/usr/bin/env bash
# Cilium: tunnel mode (VXLAN) over the Nebula IPs, WireGuard always on (pod-to-pod and node-to-node), MTU set once.
# MUTUAL=yes adds mutual authentication (SPIRE) on top: a separate run, because SPIRE is the one piece that can fail on its own.
# BGP=yes turns on the BGP control plane (issue #13, poc/gateway.sh): it only adds the CRDs and the reconciler; who announces is decided by the
# CiliumBGPClusterConfig's node selector (poc/gateway/bgp.yaml), so a node outside it never speaks BGP.
# SPIRE server: non-root, PVC datastore, pinned to an own node (spire-args.sh). History: the chart's hostPath socket dir is root-owned and the
# image runs as uid 1000; the first PoC worked around it with runAsUser=0 and no datastore (docs/POC-STATUS.md).
# CNI paths: the chart defaults (/etc/cni/net.d, /opt/cni/bin) are what k3s's containerd uses once flannel is off; up.sh and
# edge-mesh add a containerd drop-in so the k3s bundled plugins (loopback) are found too. See docs/TROUBLESHOOTING.md (Cilium pods stay in CreateContainerError) for why.
set -eu
. "$(dirname "$0")/lib.sh"
EXTRA_FLAGS=()
[ "${MUTUAL:-no}" = yes ] && { . "$POC/spire-args.sh"; EXTRA_FLAGS=("${SPIRE_ARGS[@]}"); }
[ "${BGP:-no}" = yes ] && EXTRA_FLAGS+=(--set bgpControlPlane.enabled=true)
helm_cp repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm_cp upgrade --install cilium cilium --repo https://helm.cilium.io --version "$CILIUM_VERSION" -n kube-system \
  --set routingMode=tunnel --set tunnelProtocol=vxlan \
  --set encryption.enabled=true --set encryption.type=wireguard --set encryption.nodeEncryption=true \
  --set MTU="$POD_MTU" \
  --set kubeProxyReplacement=false \
  --set k8sServiceHost="$CP_NEB" --set k8sServicePort=6443 \
  --set ipam.operator.clusterPoolIPv4PodCIDRList="10.42.0.0/16" \
  --set operator.replicas=1 \
  --set identityManagementMode=operator \
  "${EXTRA_FLAGS[@]}" \
  --wait --timeout "${HELM_TIMEOUT:-10m}"
# every agent shares one service account, and its token sits on every node's disk. poc/cilium-node-guard.yaml narrows what a write by it may claim:
# only its own node's CiliumNode, never the pod CIDRs (issue #8), no CiliumIdentity at all (the operator creates them: identityManagementMode
# above, beta in Cilium), and only CiliumEndpoints of its own node's address and pod CIDRs (issue #29)
k apply -f - <"$POC/cilium-node-guard.yaml" >/dev/null
