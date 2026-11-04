#!/usr/bin/env bash
# The Doom pods of issue #75 part B, as YAML on stdout: pod.sh server|client <image> (the name build.sh printed). Both pass Pod Security
# "restricted" (non-root 65534, no capabilities, no service account token), fit the player's quota (a namespace with the server, a probe and a robot: 900m CPU and 768 MiB) and never pull the image:
# it is already on the node (poc/doom/build.sh and poc-doom.sh DM05); only the probe pod, a public image, may pull. Unlabelled = private: the pod runs only on its owner's node.
#   poc/doom/pod.sh server "$IMAGE" | edge-mesh kubectl apply -f -     # doom-server: server.sh on 10666/UDP (then: kubectl expose, see JOIN.md)
#   poc/doom/pod.sh client "$IMAGE" | edge-mesh kubectl apply -f -     # doom-client: idle; `kubectl exec doom-client -- client.sh <host> 10666 40 <name>`
#   poc/doom/pod.sh probe "$AGNHOST_IMAGE" | ...                       # doom-probe (only the suite): answers UDP on 10666 with its name, so
#                                                                       # `client.sh --probe` tells an open party from a closed one without joining the game
set -eu
case "${1:-}" in
  server) name=doom-server cmd=server.sh port=10666 pull=Never cpu=300m mem=256Mi ;;
  client) name="${3:-doom-client}" cmd="sleep, infinity" port= pull=Never cpu=400m mem=384Mi ;;
  probe) name=doom-probe cmd="/agnhost, netexec, --udp-port=10666, --http-port=8080" port=10666 pull=IfNotPresent cpu=200m mem=128Mi ;;   # a public image, pulled as poc-party's
  *) echo "usage: pod.sh server|client|probe <image> [name]" >&2; exit 2 ;;
esac
img="${2:?image}"
printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: %s, labels: {app: %s}}\nspec:\n  automountServiceAccountToken: false\n' "$name" "$name"
printf '  securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: RuntimeDefault}}\n  containers:\n'
printf '  - name: c\n    image: %s\n    imagePullPolicy: %s\n    command: [%s]\n' "$img" "$pull" "$cmd"
[ -z "$port" ] || printf '    ports: [{containerPort: %s, protocol: UDP}]\n' "$port"
printf '    resources: {requests: {cpu: 200m, memory: 128Mi}, limits: {cpu: %s, memory: %s, ephemeral-storage: 512Mi}}\n' "$cpu" "$mem"
printf '    securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}\n'
