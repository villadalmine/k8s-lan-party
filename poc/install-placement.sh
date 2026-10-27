#!/usr/bin/env bash
# install-placement.sh: install Kyverno (pinned in stack.env) on YOUR running control plane, pinned to the own node, and apply the trust-placement
# policies (poc/placement/). Needs a cluster whose control plane node carries node-restriction.kubernetes.io/trust=own (mesh host init does that).
# Idempotent. Remove with:  ./kc delete cpol edge-mesh-placement; ./kc delete validatingadmissionpolicybinding,validatingadmissionpolicy edge-mesh-node-trust
set -eu
. "$(dirname "$0")/lib.sh"
KSEL='{"node-restriction.kubernetes.io/trust":"own"}'
KTOL='[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"}]'
a=(--set reportsController.enabled=false --set cleanupController.enabled=false)
for c in admissionController backgroundController; do a+=(--set "$c.replicas=1" --set-json "$c.nodeSelector=$KSEL" --set-json "$c.tolerations=$KTOL"); done
helm_cp upgrade --install kyverno kyverno --repo https://kyverno.github.io/kyverno/ --version "$KYVERNO_CHART" -n kyverno --create-namespace "${a[@]}" --wait --timeout 8m
k apply -f - <"$POC/placement/trust-placement.yaml"
k apply -f - <"$POC/placement/node-trust-vap.yaml"
cpol_ready() { [ "$(k get cpol edge-mesh-placement -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ]; }
wait_for 120 cpol_ready
echo "placement installed: private workloads run only on own nodes (see docs/HOST-YOUR-OWN.md section 5)"
