#!/usr/bin/env bash
# make poc-lighthouse: a lighthouse on a separate public server (here a plain Ubuntu VM that plays the server), the control plane and a
# player behind it. Proves lighthouse-vps.sh phase by phase, that every phase is idempotent, that the server's private key never leaves it,
# that a player joins through the external lighthouse, and that `mesh host revoke` reaches the server. Needs a Linux host with Incus.
# PASS/FAIL per step; identical across runs. KEEP=1 leaves the VMs up afterwards.
set -u
export RUNTIME=incus MAX_VMS=6 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
LOG="$CACHE/poc-lighthouse.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"; LHS="$POC/lighthouse-vps.sh"
SERVER=emesh-vps; SERVER_IP=$UNDERLAY_NET.2; RSH="incus exec $SERVER --"; OUT="$STATE/lh-out"

server_up() { # a plain Ubuntu machine with a public-looking address: no nebula, no k3s, nothing of ours
  incus network show "$VMNET" >/dev/null 2>&1 || incus network create "$VMNET" ipv4.address=$UNDERLAY_NET.1/24 ipv4.nat=true ipv6.address=none >/dev/null
  incus init "$IMG" "$SERVER" --vm -c limits.cpu=1 -c limits.memory=1GiB -d root,size=12GiB --network "$VMNET" >/dev/null
  incus config device set "$SERVER" eth0 ipv4.address="$SERVER_IP" >/dev/null
  incus start "$SERVER"
  wait_for 240 incus exec "$SERVER" -- true
  wait_for 120 incus exec "$SERVER" -- ping -c1 -W1 $UNDERLAY_NET.1
  incus exec "$SERVER" -- date -s "@$(date +%s)" >/dev/null   # a real server has NTP; a fresh VM can be seconds behind, and Nebula refuses a certificate from the future
  incus exec "$SERVER" -- sh -c 'cloud-init status --wait >/dev/null 2>&1 || true; command -v curl >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null; }'
}
unchanged() { local o; o=$("$@" 2>&1) || return 1; echo "$o"; ! echo "$o" | grep -q '^changed:'; }   # idempotence: a second run changes nothing
no_private_key_at_home() { [ ! -e "$STATE/bundles/lh/host.key" ] && ! grep -rl 'NEBULA X25519 PRIVATE KEY' "$STATE/bundles/lh" >/dev/null 2>&1; }
cert_is_lighthouse() { "$CACHE/nebula-cert" print -json -path "$STATE/bundles/lh/host.crt" | python3 -c '
import sys, json
d = json.load(sys.stdin)[0]["details"]
sys.exit(0 if "lighthouse" in d["groups"] and any(i.startswith("10.77.0.1/") for i in (d.get("networks") or d.get("ips") or [])) else 1)'; }
nodes_point_at_server() { grep -q "\"$SERVER_IP:4242\"" "$STATE/bundles/cp/config.yml"; }
only_cp_and_server() { [ "$(incus list emesh- -c n -f csv | sort | tr '\n' ' ')" = "emesh-cp emesh-vps " ]; }
cp_pings_lighthouse() { incus exec emesh-cp -- ping -c1 -W2 "$LH_NEB"; }
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
fp_on_server() { local fp; fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])')
  incus exec "$SERVER" -- grep -q "$fp" /etc/nebula/config.yml; }
stats_answer() { incus exec "$SERVER" -- sh -c 'curl -s 127.0.0.1:9110/metrics | grep -q "^nebula_hostmap_main_hosts"'; }
cp_stats_answer() { incus exec emesh-cp -- sh -c 'curl -s 127.0.0.1:9110/metrics | grep -q "^nebula_hostmap_main_hosts"'; }
cp_healthy() { k get --raw /readyz >/dev/null && ready cp; }
nebula_pid() { incus exec emesh-cp -- pidof nebula; }
refresh_restart() { # a real change that needs a restart (another metrics port), and back: each time the API and the node come back, the time is logged
  local t0 port
  for port in 9111 9110; do
    t0=$(date +%s)
    STATS_PORT=$port "$MESH" host refresh cp | grep -q 'nebula restarted' || return 1
    wait_for 180 cp_healthy || return 1
    wait_for 60 incus exec emesh-cp -- sh -c "curl -s 127.0.0.1:$port/metrics | grep -q ^nebula_hostmap_main_hosts" || return 1   # published every 15 s
    log "refresh with a restart (stats port $port): API ready and node Ready $(( $(date +%s) - t0 )) s after the command started"
  done
}
refresh_reload() { # a change read on SIGHUP (an advertised address) and back: nebula keeps its pid, the API never goes away
  local pid; pid=$(nebula_pid) || return 1
  ADVERTISE="$UNDERLAY_NET.3:4242" "$MESH" host refresh cp | grep -q 'nebula reloaded' || return 1
  grep -q advertise_addrs "$STATE/bundles/cp/config.yml" || return 1
  "$MESH" host refresh cp | grep -q 'nebula reloaded' || return 1
  [ "$(nebula_pid)" = "$pid" ] && cp_healthy
}
server_clean() { ! incus exec "$SERVER" -- test -e /etc/nebula && ! incus exec "$SERVER" -- systemctl is-active -q nebula; }

sign_lh() { "$LHS" sign "$SERVER_IP" "$OUT/lh.pub"; }
deploy_lh() { "$LHS" deploy --ssh "$RSH"; }
cp_up() { HOST_ONLY=1 "$UP"; }
alice_invited() { "$MESH" host invite alice --ttl 2h; }
alice_revoked() { "$MESH" host revoke alice; }

step P01 "a plain Ubuntu VM plays the public server" server_up
step P02 "prepare: nebula $NEBULA_VERSION installed (sha256 checked), the key made on the server, lh.pub written" "$LHS" prepare "$SERVER_IP" --ssh "$RSH" --out "$OUT"
step P03 "prepare again: nothing changed" unchanged "$LHS" prepare "$SERVER_IP" --ssh "$RSH" --out "$OUT"
step P04 "sign: a lighthouse certificate from the CA" sign_lh
step P05 "the certificate is group lighthouse on 10.77.0.1 and the bundle holds no private key" bash -c "$(declare -f cert_is_lighthouse no_private_key_at_home); STATE='$STATE'; CACHE='$CACHE'; cert_is_lighthouse && no_private_key_at_home"
step P06 "sign again: unchanged (same key, more than 30 days left)" bash -c "'$LHS' sign '$SERVER_IP' '$OUT/lh.pub' | grep -q unchanged"
step P07 "deploy: nebula runs on the server" deploy_lh
step P08 "deploy again: nothing changed" unchanged "$LHS" deploy --ssh "$RSH"
step P09 "verify: service, udp/4242, key mode, CA chain, certificate days, am_lighthouse" "$LHS" verify --ssh "$RSH"
step P09b "Nebula publishes its metrics on the loopback of the server (peers, certificate TTL)" wait_for 60 stats_answer
step P10 "mesh host init with the external lighthouse: only the control plane is a PoC machine" cp_up
step P11 "every bundle points at the server's address" nodes_point_at_server
step P12 "the control plane reaches the lighthouse over Nebula" cp_pings_lighthouse
step P12b "the control plane publishes its Nebula metrics too" wait_for 60 cp_stats_answer
step P12c "mesh host refresh cp: nothing to change" bash -c "'$MESH' host refresh cp | grep -q unchanged"
step P12d "mesh host refresh cp with a change that needs a restart (metrics port), and back: the API and the node come back" refresh_restart
step P12e "mesh host refresh cp with a change read on reload (advertised address), and back: same nebula process, API up" refresh_reload
step P13 "mesh host invite alice" alice_invited
step P14 "alice joins through the external lighthouse (mesh play alice)" "$MESH" play alice
step P15 "alice's node becomes Ready" wait_for 240 ready alice
step P16 "mesh host revoke alice pushes the blocklist to the server" alice_revoked
step P17 "the server's config now lists alice's fingerprint" fp_on_server
step P18 "remove: the server is clean again" bash -c "'$LHS' remove --ssh '$RSH' && $(declare -f server_clean); SERVER='$SERVER'; server_clean"

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
