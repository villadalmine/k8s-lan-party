#!/usr/bin/env bash
# make poc-host: "host your own game". init, invite a player, the player joins, players lists them, kick removes
# them, down cleans everything. PASS/FAIL per step; identical across runs.
set -u
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
MESH="$POC/mesh"
LOG="/tmp/emesh-poc-host.log"; : >"$LOG"   # kept after the run for diagnosis

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
players_lists() { "$MESH" host players | grep -q "^alice .*True"; }
api_code() { # api_code <machine> <address>: the HTTP code of the API as seen from that machine (000 = no answer)
  if [ "$RUNTIME" = incus ]; then dx "$1" curl -sk -m4 -o /dev/null -w '%{http_code}' "https://$2:6443/readyz"
  else docker run --rm --network "container:$1" "$TOOLS_IMAGE" curl -sk -m4 -o /dev/null -w '%{http_code}' "https://$2:6443/readyz"; fi
}
api_only_on_nebula() { # the API answers on the Nebula IP and not on the control plane's own underlay address
  [ "$(api_code cp "$CP_NEB")" != 000 ] && [ "$(api_code cp "$CP_UNDERLAY")" = 000 ]
}
lighthouse_no_api() { # the lighthouse is in the mesh (it pings the control plane) but not a member: the API refuses it (issue.sh, members only)
  dx lh ping -c1 -W3 "$CP_NEB" >/dev/null && [ "$(api_code lh "$CP_NEB")" = 000 ]
}
cert_ttl_2h() { # the invite's certificate expires about 2 h out
  "$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c '
import sys, json, datetime as d
n = json.load(sys.stdin)[0]["details"]
a = d.datetime.fromisoformat(n["notBefore"].replace("Z", "+00:00")); b = d.datetime.fromisoformat(n["notAfter"].replace("Z", "+00:00"))
sys.exit(0 if 7190 <= (b - a).total_seconds() <= 7210 else 1)'
}
kicked() { ! k get node alice && ! machine_running alice; }
all_gone() { ! any_machines && [ ! -d "$STATE/ca" ]; }

step H01 "mesh host init: CA, lighthouse and control plane are up" "$MESH" host init
step H02 "the Kubernetes API answers on the Nebula IP and not on the Docker network" api_only_on_nebula
step H02b "the lighthouse reaches the control plane over Nebula but not its API (members only)" lighthouse_no_api
step H03 "mesh host invite alice --ttl 2h: a bundle with a 2 h certificate" bash -c "'$MESH' host invite alice --ttl 2h && [ -f '$STATE/bundles/alice/join.env' ]"
step H04 "the invite certificate expires in 2 h" cert_ttl_2h
step H05 "alice joins with one command (mesh play alice)" bash -c "'$MESH' play alice"
step H06 "alice's node becomes Ready" wait_for 180 ready alice
step H07 "mesh host players lists alice as Ready" players_lists
step H08 "mesh host kick alice: node deleted and machine out" bash -c "'$MESH' host kick alice"
step H09 "alice is gone and stays gone" kicked
step H10 "mesh host down cleans everything" bash -c "'$MESH' host down"
step H11 "nothing is left running" all_gone

echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
