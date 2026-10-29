#!/usr/bin/env bash
# The maintainer side of a join: sign a Nebula cert with the CA, create a short-lived k3s join token, write the bundle.
#   issue.sh <name> <nebula-ip> <trust> <cert-duration>      -> .state/bundles/<name>/
#   (a player's bundle also gets a node password: issue.sh stores its hash in kube-system/<name>.node-password.k3s; poc/node_password.py)
#   CONFIG_ONLY=1 issue.sh <name> x x x                      rewrite only config.yml of an existing bundle (same certificate): `mesh host refresh`
#   TOKEN_TTL=5m issue.sh ...                                how long the k3s join token lives: 1 to 8 minutes, default 8m (lib.sh TOKEN_TTL_MAX:
#                                                            it must die before k3s may drop the node password of a name nobody took). A Kubernetes
#                                                            bootstrap token is NOT one-time: it authenticates any machine until it expires or is
#                                                            deleted. The host deletes it once the node registered (`mesh host burn`: reap, players, revoke).
# PoC fixture: the CA key sits in .state/ca (a real deployment keeps it offline or as a protected secret: DESIGN.md decision 5).
set -eu
. "$(dirname "$0")/lib.sh"
name="$1"; ip="$2"; trust="$3"; dur="$4"
if [ -z "${CONFIG_ONLY:-}" ] && [ "$name" != lh ] && [ "$name" != cp ]; then
  ttl_s=$(ttl_seconds "${TOKEN_TTL:-8m}") || { echo "TOKEN_TTL=${TOKEN_TTL:-}: a join token lives 1 to 8 minutes (90s, 5m, 8m); nothing was issued"; exit 2; }
fi
B="$STATE/bundles/$name"; mkdir -p "$B" "$STATE/ca"
NC="$CACHE/nebula-cert"
if [ -z "${CONFIG_ONLY:-}" ]; then
# Two CAs (DESIGN.md: free pool versus persistent participants). Every host trusts both (ca.crt is the two PEMs together), but:
#  - ca/       the PERSISTENT CA: offline in a real deployment, signs by hand when a signed PR is merged. It may not sign `public`.
#  - ca-pool/  the POOL CA: online (an automatic issuer holds its key), short-lived certificates only. It is created WITH A GROUP
#              CONSTRAINT (`-groups public`), so even a stolen pool key can mint nothing but `public` certificates.
# Removing one CA from the bundle cuts off everything it signed and leaves the other untouched (poc-ca.sh proves it).
mkdir -p "$STATE/ca-pool"
[ -f "$STATE/ca/ca.crt" ] || (cd "$STATE/ca" && "$NC" ca -name "edge-mesh-poc-persistent" -groups "own,control-plane,friends,lighthouse" -duration 8760h >/dev/null)
[ -f "$STATE/ca-pool/ca.crt" ] || (cd "$STATE/ca-pool" && "$NC" ca -name "edge-mesh-poc-pool" -groups "public" -duration 8760h >/dev/null)
groups="$trust"; [ "$trust" = own ] && groups="own,control-plane"
if [ "$trust" = public ]; then CAD="$STATE/ca-pool"; else CAD="$STATE/ca"; fi
# IN_PUB=<file>: sign a public key the machine generated itself (nebula-cert keygen there). The private key never leaves that machine, so the
# bundle has a certificate and no host.key (used for a lighthouse on a public server: lighthouse-vps.sh).
if [ -n "${IN_PUB:-}" ]; then KEYARGS=(-in-pub "$IN_PUB" -out-crt host.crt); else KEYARGS=(-out-crt host.crt -out-key host.key); fi
(cd "$B" && rm -f host.crt host.key && "$NC" sign -ca-crt "$CAD/ca.crt" -ca-key "$CAD/ca.key" -name "$name" \
  -ip "$ip/24" -groups "$groups" -duration "$dur" "${KEYARGS[@]}" >/dev/null)
cat "$STATE/ca/ca.crt" "$STATE/ca-pool/ca.crt" >"$B/ca.crt"
else [ -f "$B/host.crt" ] || { echo "refresh: no such bundle: $name"; exit 3; }; fi
lh_hosts=""; [ "$name" = lh ] && am_lh=true || am_lh=false
[ "$am_lh" = false ] && lh_hosts="\"$LH_NEB\""
# FW=narrow (default): only what the design needs between hosts; FW=open: any/any (the first PoC runs).
#  ICMP, the Kubernetes API (6443), the kubelet (10250, only from the control plane), Cilium WireGuard (51871),
#  and the flannel VXLAN port (8472) only when flannel is the CNI. 4240 (Cilium health) is not needed: node
#  encryption carries it inside WireGuard.
if [ "${FW:-narrow}" = open ]; then INBOUND='[{port: any, proto: any, host: any}]'
else
  INBOUND='[{port: any, proto: icmp, host: any}, {port: 6443, proto: tcp, host: any}, {port: 10250, proto: tcp, groups: [control-plane]}, {port: 51871, proto: udp, host: any}'
  [ "${CNI:-flannel}" = flannel ] && INBOUND="$INBOUND, {port: 8472, proto: udp, host: any}"
  INBOUND="$INBOUND]"
fi
# every new certificate starts with the revocations made so far (.state/blocklist, one fingerprint per line), so a host admitted after a revoke also refuses it
# A joiner NOT on the lighthouse's network (a PC on the LAN, a friend): the control plane reports the address it can be reached at (ADVERTISE=host:port,
# a forwarded port) and the joiner's bundle points at the lighthouse's reachable address (LH_DOCKER_IP, LH_PORT). Not covered by an automated check.
ADV=""; [ -n "${ADVERTISE:-}" ] && [ "$name" = cp ] && ADV="  advertise_addrs: [\"$ADVERTISE\"]"
# Nebula's own metrics (Prometheus text format), loopback only, on the lighthouse and the control plane: peers, handshakes, drops, certificate TTL.
# Read by a local Prometheus scrape (whatever monitoring the host runs); nothing listens outside the machine.
STATS=""; case "$name" in lh|cp) STATS="stats: {type: prometheus, listen: 127.0.0.1:${STATS_PORT:-9110}, path: /metrics, namespace: nebula, interval: 15s}" ;; esac
# LH_RELAY=yes: the lighthouse is also a Nebula relay (`lighthouse-vps.sh sign --relay` remembers it in .state/lighthouse.env). Two hosts that
# cannot reach each other directly (symmetric NAT on both sides) then talk through it: every other host names the lighthouse as its relay,
# and `relay.use_relays` stays at its default (true). The relay forwards Nebula packets it cannot read (end to end encrypted) and the
# firewall of each end still decides what gets in; but every relayed byte crosses the relay host (DESIGN.md sections 15 and 16).
RELAY=""; if [ "${LH_RELAY:-no}" = yes ]; then if [ "$name" = lh ]; then RELAY="relay: {am_relay: true}"; else RELAY="relay: {relays: [\"$LH_NEB\"]}"; fi; fi
# no blank line of its own: a game that never turned the relay on keeps a byte-identical config (no spurious change at the first refresh)
nl='
'; [ -z "$RELAY" ] || ADV="${ADV:+$ADV$nl}$RELAY"
BL=""; if [ -s "$STATE/blocklist" ]; then while read -r f; do [ -n "$f" ] && BL="${BL:+$BL, }\"$f\""; done <"$STATE/blocklist"; fi
cat >"$B/config.yml" <<EOF
pki:
  ca: /etc/nebula/ca.crt
  cert: /etc/nebula/host.crt
  key: /etc/nebula/host.key
  disconnect_invalid: true
  blocklist: [$BL]
static_host_map:
  "$LH_NEB": ["$LH_DOCKER_IP:${LH_PORT:-4242}"]
lighthouse:
  am_lighthouse: $am_lh
  hosts: [$lh_hosts]
$ADV
listen: {host: 0.0.0.0, port: 4242}
punchy: {punch: true}
tun: {dev: nebula1, mtu: $NEBULA_MTU}
logging: {level: info}
$STATS
firewall:
  outbound: [{port: any, proto: any, host: any}]
  inbound: $INBOUND
EOF
if [ -z "${CONFIG_ONLY:-}" ] && [ "$name" != lh ] && [ "$name" != cp ]; then
  tok="$(cpx k3s token create --ttl "${ttl_s}s")"
  # The node password of this name, made here (poc/node_password.py): k3s accepts a registration under this name only with this password,
  # so another visitor with a valid token of his own cannot take the name, even at its Nebula IP. Only the bundle carries the password.
  npw="$(python3 "$POC/node_password.py" new)"
  printf '%s' "$npw" | python3 "$POC/node_password.py" secret "$name" | k create -f - >/dev/null
  cat >"$B/join.env" <<EOF
K3S_URL=https://$CP_NEB:6443
K3S_TOKEN=$tok
NODE_PASSWORD=$npw
NODE_NAME=$name
NODE_IP=$ip
TRUST=$trust
LH_NEB=$LH_NEB
TAINT=${TAINT:-no}
CNI=${CNI:-flannel}
EOF
  chmod 600 "$B/join.env"
fi
echo "issued $name"
