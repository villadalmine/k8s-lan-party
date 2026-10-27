# Sourced by cilium.sh and kind-cilium.sh: the helm arguments for the SPIRE install (mutual authentication) in SPIRE_ARGS.
# Hardened (phase B): the server runs as uid 1000 (the image user), not root. The chart mounts a hostPath socket directory that
# Kubernetes creates root:root 0755, so an initContainer (root, only this step) hands it and the data volume to 1000. The datastore
# is a PVC (state survives a restart: same CA, same entries). The pod-level runAsUser=1000 matters twice: the chart's cilium-init sidecar reaches the
# server through /proc/<pid>/root, which a different uid (even root, without SYS_PTRACE) is refused: with the server at 1000 and the sidecar at root
# no registration entry is ever created and mutual auth silently never works. Same uid for the whole pod fixes it.
# The server is also pinned to an `own` node (DESIGN.md section 4.4).
SPIRE_NS=cilium-spire
SPIRE_PERSIST="${SPIRE_PERSIST:-yes}"   # yes: PVC datastore; no: ephemeral (the first PoC)
DATA_PATH=""; DATA_MOUNT=""
[ "$SPIRE_PERSIST" = yes ] && { DATA_PATH=" /run/spire/data"; DATA_MOUNT=',{"name":"spire-data","mountPath":"/run/spire/data"}'; }
SPIRE_INIT='[{"name":"chown-state","image":"docker.io/library/busybox:1.38.0","command":["sh","-c","chown -R 1000:1000 /tmp/spire-server/private'"$DATA_PATH"'"],"securityContext":{"runAsUser":0,"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"],"add":["CHOWN","DAC_OVERRIDE","FOWNER"]}},"volumeMounts":[{"name":"spire-server-socket","mountPath":"/tmp/spire-server/private"}'"$DATA_MOUNT"']}]'
SPIRE_SELECTOR='{"node-restriction.kubernetes.io/trust":"own"}'
SPIRE_TOL='[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"}]'
SPIRE_SC='{"runAsNonRoot":true,"runAsUser":1000,"runAsGroup":1000,"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}'
S=authentication.mutual.spire
SPIRE_ARGS=(--set authentication.enabled=true --set $S.enabled=true --set $S.install.enabled=true
  --set $S.install.server.dataStorage.enabled=$([ "$SPIRE_PERSIST" = yes ] && echo true || echo false) --set $S.install.server.dataStorage.size=1Gi
  --set-json "$S.install.server.podSecurityContext={\"runAsUser\":1000,\"runAsGroup\":1000,\"fsGroup\":1000}"
  --set-json "$S.install.server.securityContext=$SPIRE_SC"
  --set-json "$S.install.server.initContainers=$SPIRE_INIT"
  --set-json "$S.install.server.nodeSelector=$SPIRE_SELECTOR"
  --set-json "$S.install.server.tolerations=$SPIRE_TOL")
