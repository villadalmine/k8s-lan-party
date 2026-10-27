#!/usr/bin/env bash
# Cilium: tunnel mode (VXLAN) over the Nebula IPs, WireGuard always on (pod-to-pod and node-to-node), MTU set once.
# MUTUAL=yes adds mutual authentication (SPIRE) on top: a separate run, because SPIRE is the one piece that can fail on its own.
# SPIRE server: non-root, PVC datastore, pinned to an own node (spire-args.sh). History: the chart's hostPath socket dir is root-owned and the
# image runs as uid 1000; the first PoC worked around it with runAsUser=0 and no datastore (docs/POC-STATUS.md).
# CNI paths: the chart defaults (/etc/cni/net.d, /opt/cni/bin) are what k3s's containerd uses once flannel is off; up.sh and
# edge-mesh add a containerd drop-in so the k3s bundled plugins (loopback) are found too. See docs/TROUBLESHOOTING.md (Cilium pods stay in CreateContainerError) for why.
set -eu
. "$(dirname "$0")/lib.sh"
SPIRE_FLAGS=()
[ "${MUTUAL:-no}" = yes ] && { . "$POC/spire-args.sh"; SPIRE_FLAGS=("${SPIRE_ARGS[@]}"); }
helm_cp repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm_cp upgrade --install cilium cilium --repo https://helm.cilium.io --version "$CILIUM_VERSION" -n kube-system \
  --set routingMode=tunnel --set tunnelProtocol=vxlan \
  --set encryption.enabled=true --set encryption.type=wireguard --set encryption.nodeEncryption=true \
  --set MTU="$POD_MTU" \
  --set kubeProxyReplacement=false \
  --set k8sServiceHost="$CP_NEB" --set k8sServicePort=6443 \
  --set ipam.operator.clusterPoolIPv4PodCIDRList="10.42.0.0/16" \
  --set operator.replicas=1 \
  "${SPIRE_FLAGS[@]}" \
  --wait --timeout "${HELM_TIMEOUT:-10m}"
