#!/usr/bin/env bash
# edge-mesh revoke <node>: the hard revocation, in order.
#  1. the certificate fingerprint goes on the Nebula blocklist of every host, which reloads (SIGHUP);
#  2. the node object is deleted (the blocklist is what keeps it from coming back);
#  3. the join token of the player is deleted (if it still existed: a bootstrap token is not one-time, it works until deleted or expired);
#     and so is the ServiceAccount behind the player's kubeconfig (play-<name>/user): that token stops working at once;
#  4. every Node not named after a live bundle is deleted too (a second machine started with a copy of the bundle before the burn picks
#     its own name and addresses; the sweep keys on the names the host issued: poc/nodes_sweep.py).
# Idempotent: a second run finds the fingerprint already listed, the node already gone, and exits 0. Unknown name: exit 3. A Node still
# left to sweep 30 s later (REVOKE_WAIT), an API that does not answer, or no NODE_IP in the bundle: exit 4 (`mesh host reap` then keeps
# the lease and tries again on its next run).
set -eu
. "$(dirname "$0")/lib.sh"
n="${1:-}"
[ -n "$n" ] && [ -f "$STATE/bundles/$n/host.crt" ] || { echo "revoke: no such player: '${n}' (see: mesh host players)"; exit 3; }
fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/$n/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])')
touch "$STATE/blocklist"; grep -qx "$fp" "$STATE/blocklist" || echo "$fp" >>"$STATE/blocklist"
for cfg in "$STATE"/bundles/*/config.yml; do
  [ -f "$cfg" ] || continue
  h=$(basename "$(dirname "$cfg")")
  python3 - "$cfg" "$fp" <<'PY'
import re, sys
p, fp = sys.argv[1], sys.argv[2]
s = open(p).read()
m = re.search(r"blocklist: \[(.*)\]", s)
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"%s"' % fp not in items:
    items.append('"%s"' % fp)
open(p, "w").write(s[:m.start()] + "blocklist: [" + ", ".join(items) + "]" + s[m.end():])
PY
  nebula_reload "$h" >/dev/null 2>&1 || true
done
# An external lighthouse (lighthouse-vps.sh) holds its own copy of the blocklist: push it when this machine can reach the server.
if [ -n "${LH_RSH:-}" ]; then
  "$POC/lighthouse-vps.sh" sync --ssh "$LH_RSH" >/dev/null 2>&1 || echo "warning: the external lighthouse was not updated; run: poc/lighthouse-vps.sh sync --ssh \"$LH_RSH\""
elif [ -n "${LH_EXTERNAL:-}" ]; then
  echo "note: push the new blocklist to the external lighthouse where its ssh key is: poc/lighthouse-vps.sh deploy --ssh \"<ssh command>\" <copy of .state/bundles/lh>"
fi
k delete node "$n" --wait=false --ignore-not-found >/dev/null 2>&1 || true   # the sweep below confirms it (or exits 4)
token_burn "$n" || true
k delete serviceaccount user -n "play-$n" --ignore-not-found >/dev/null 2>&1 || true   # the player's kubeconfig token dies with its ServiceAccount
# The node guard (poc/node-guard.yaml) drops this name from the names a kubelet may register: no copy can come back under it.
guard=ok; node_guard || guard=failed
# A second machine started with a copy of this bundle before its token was burned registers a Node whose name and addresses it picks
# itself. The sweep keys on what it cannot pick: every Node not named after a live bundle goes (and a live-named one that reports this
# bundle's address, or not its own). Success only once nothing is left to sweep; otherwise exit 4 (run it again): an API that does not
# answer, or a NODE_IP that cannot be read, never reads as "nothing left".
ip=$(sed -n 's/^NODE_IP=//p' "$STATE/bundles/$n/join.env" 2>/dev/null || true)
[ -n "$ip" ] || { echo "revoke $n: no NODE_IP in its join.env: the Nodes registered with this bundle cannot be swept; nothing is reported as done"; exit 4; }
i=0; wait_s=${REVOKE_WAIT:-30}
while :; do
  left=$(stray_nodes "$ip") || left="(the API did not answer)"
  [ -n "$left" ] || break
  i=$((i+1)); [ "$i" -le "$wait_s" ] || { echo "revoke $n: Nodes still registered: $(printf '%s\n' "$left" | cut -f1 | tr '\n' ' ')(run it again)"; exit 4; }
  printf '%s\n' "$left" | while IFS="$(printf '\t')" read -r o why; do
    case "$o" in ""|"(the API did not answer)") continue ;; esac
    k delete node "$o" --wait=false --ignore-not-found >/dev/null 2>&1 || true
    [ "$o" = "$n" ] || echo "node $o deleted: $why"
  done
  sleep 1
done
[ "$guard" = ok ] || { echo "revoke $n: the node guard was not updated (the API did not take it): run it again"; exit 4; }
echo "revoked $n: fingerprint on the blocklist, node deleted, join token gone"
