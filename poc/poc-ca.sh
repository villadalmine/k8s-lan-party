#!/usr/bin/env bash
# make poc-ca: phase B3, "two CAs": a persistent CA (offline in real life, signs when a signed PR is merged) and a pool CA (online, free
# pool of public nodes). Every host trusts both; the pool CA is constrained to the group `public`, so a stolen pool key cannot mint
# friends/own certificates; and pulling the pool CA out of the trust bundle cuts off the pool without touching the persistent nodes.
# Same runtimes as poc-join (RUNTIME=docker|incus). Output: one PASS/FAIL per step, identical across runs.
set -u
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
NC="$CACHE/nebula-cert"
fp() { "$NC" print -json -path "$1" | python3 -c 'import sys,json;d=json.load(sys.stdin)[0];print(d["fingerprint"])'; }
issuer() { "$NC" print -json -path "$1" | python3 -c 'import sys,json;d=json.load(sys.stdin)[0];print(d["details"]["issuer"])'; }
ca_groups() { "$NC" print -json -path "$1" | python3 -c 'import sys,json;d=json.load(sys.stdin)[0];print(",".join(sorted(d["details"]["groups"])))'; }
push_ca() { # push_ca <host>: the host's trust bundle (ca.crt) as the issuing side now has it, then reload Nebula
  if [ "$RUNTIME" = incus ]; then incus file push -q "$STATE/bundles/$1/ca.crt" "emesh-$(vmname "$1")/etc/nebula/ca.crt"
  else docker exec "$1" cp /bundle/ca.crt /etc/nebula/ca.crt; fi
  dx "$1" sh -c 'kill -HUP $(pidof nebula)'
}
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
bring_up() { "$UP" && dx cp ping -c1 -W2 "$LH_NEB"; }
two_cas_on_host() { [ "$(dx n1 grep -c 'BEGIN NEBULA CERTIFICATE' /etc/nebula/ca.crt)" = 2 ] && [ "$(dx n2 grep -c 'BEGIN NEBULA CERTIFICATE' /etc/nebula/ca.crt)" = 2 ]; }
issued_by() { # friends node by the persistent CA, public node by the pool CA
  [ "$(issuer "$STATE/bundles/n1/host.crt")" = "$(fp "$STATE/ca/ca.crt")" ] && [ "$(issuer "$STATE/bundles/n2/host.crt")" = "$(fp "$STATE/ca-pool/ca.crt")" ] \
    && [ "$(issuer "$STATE/bundles/n1/host.crt")" != "$(issuer "$STATE/bundles/n2/host.crt")" ]
}
constrained() { [ "$(ca_groups "$STATE/ca-pool/ca.crt")" = public ] && [ "$(ca_groups "$STATE/ca/ca.crt")" = "control-plane,friends,lighthouse,own" ]; }
sign_with() { # sign_with <ca-dir> <groups>: try to mint a certificate
  local t="$STATE/forge"; rm -rf "$t"; mkdir -p "$t"
  (cd "$t" && "$NC" sign -ca-crt "$1/ca.crt" -ca-key "$1/ca.key" -name forged -ip 10.77.0.99/24 -groups "$2" -duration 1h >/dev/null 2>&1)
}
pool_cannot_forge() { ! sign_with "$STATE/ca-pool" own && ! sign_with "$STATE/ca-pool" friends && ! sign_with "$STATE/ca-pool" "public,own" && sign_with "$STATE/ca-pool" public; }
persist_cannot_public() { ! sign_with "$STATE/ca" public && sign_with "$STATE/ca" friends; }
n1_n2() { dx n1 ping -c2 -W2 "$N2_NEB"; }
n2_gone() { ! dx n1 ping -c1 -W2 "$N2_NEB"; }
contain() { # pull the pool CA from the trust bundle of lh, cp and n1 (n2 is the pool node and keeps whatever it has)
  local h; for h in lh cp n1; do cp "$STATE/ca/ca.crt" "$STATE/bundles/$h/ca.crt"; push_ca "$h" || return 1; done
}
n1_cp() { dx n1 ping -c2 -W2 "$CP_NEB"; }

step B01 "two CAs: lighthouse, control plane, a friends node (persistent CA) and a public node (pool CA) join" bring_up
step B02 "n1 joined and Ready (persistent CA)" wait_for 180 ready n1
step B03 "n2 joined and Ready (pool CA)" wait_for 180 ready n2
step B04 "every host trusts both CAs (the bundle holds two)" two_cas_on_host
step B05 "n1 certificate issued by the persistent CA, n2 by the pool CA (different issuers)" issued_by
step B06 "the pool CA is constrained to group public; the persistent CA cannot sign public" constrained
step B07 "NEGATIVE: with the pool CA key in hand, a certificate for own, friends or public+own cannot be minted" pool_cannot_forge
step B08 "NEGATIVE: the persistent CA cannot mint a public certificate (the two paths do not overlap)" persist_cannot_public
step B09 "certificates from both CAs interoperate: n1 reaches n2 over Nebula" wait_for 30 n1_n2
step B10 "pool CA removed from the trust bundle: the public node is cut off" contain
step B11 "the pool node n2 is unreachable from n1" wait_for 90 n2_gone
step B12 "the persistent nodes are untouched: n1 still reaches the control plane and stays Ready" n1_cp
step B13 "n1 node still Ready" ready n1

echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
