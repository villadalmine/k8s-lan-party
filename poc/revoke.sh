#!/usr/bin/env bash
# edge-mesh revoke <node>: the hard revocation, in order.
#  1. the certificate fingerprint goes on the Nebula blocklist of every host, which reloads (SIGHUP);
#  2. the node object is deleted (the blocklist is what keeps it from coming back);
#  3. the one-time join token is already gone (deleted when the node became Ready).
# Idempotent: a second run finds the fingerprint already listed, the node already gone, and exits 0. Unknown name: exit 3.
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
k delete node "$n" --wait=false --ignore-not-found >/dev/null
echo "revoked $n: fingerprint on the blocklist, node deleted"
