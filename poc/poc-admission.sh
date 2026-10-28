#!/usr/bin/env bash
# make poc-admission: admission with manual approval (option A, docs/ADMISSION.md) end to end on Incus VMs. People ask for a place, the host
# approves them one at a time inside the limits of pool.yaml (2 places), the approved ones join with their bundle, `reap` burns the join token of a
# registered node (a k3s bootstrap token is not one-time) and expels a lease that ended, and its place goes to the next one in the list. Needs a
# Linux host with Incus. PASS/FAIL per step; identical across runs. A second machine with a copy of bob's bundle (bob2) that does not cooperate
# (a plain `k3s agent`, its LAN address plus an IPv6 one, a name of its own) tries to register inside the window before the burn: the node guard
# refuses it; the same machine then tries to take the name of vic, a guest invited but not joined yet, at vic's Nebula IP: k3s refuses it
# (vic's node password is in vic's bundle only), and vic still joins; and a name whose node password k3s dropped leaves the guard at the
# next reap, so the copy cannot take that one either. The kick at the end of bob's lease cuts the copy's tunnel.
# The lease is shortened for the test (ADM_LEASE seconds, default 1800: five VMs have to boot inside the first lease); the other limits are the
# real ones. KEEP=1 leaves the VMs up afterwards.
set -u
export RUNTIME=incus MAX_VMS=5 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-admission.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"
LEASE="${ADM_LEASE:-1800}"
export POOL_CONFIG="$STATE/pool-test.yaml"
sed "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" "$POC/pool.yaml" >"$POOL_CONFIG"
ADM="$STATE/admission/state.json"

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
exits() { local want="$1"; shift; "$@"; [ $? -eq "$want" ]; }
in_list() { "$MESH" host waitlist | awk 'NR>1' | wc -l; }
play_ready() { "$MESH" play "$1" && wait_for 240 ready "$1"; }
bad_name_refused() { exits 2 "$MESH" host request 'Eve;id' --ip 198.51.100.30 && ! "$MESH" host waitlist | grep -q Eve; }
same_ip_not_added() { out_has "already in the list as alice" "$MESH" host request alice-two --ip 198.51.100.10 && [ "$(in_list)" -eq 3 ]; }
lease_end() { python3 -c 'import json,sys; print(next(l["end"] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == sys.argv[2]))' "$ADM" "$1"; }
cert_lives_the_lease() { "$CACHE/nebula-cert" print -json -path "$STATE/bundles/$1/host.crt" | python3 -c '
import sys, json, datetime as d
n = json.load(sys.stdin)[0]["details"]
a = d.datetime.fromisoformat(n["notBefore"].replace("Z", "+00:00")); b = d.datetime.fromisoformat(n["notAfter"].replace("Z", "+00:00"))
sys.exit(0 if abs((b - a).total_seconds() - int(sys.argv[1])) <= 10 else 1)' "$LEASE"; }
token_line() { local id; id=$(token_id "$1"); [ -n "$id" ] && cpx k3s token list 2>/dev/null | grep -E "(^|[^a-z0-9])$id([^a-z0-9]|$)"; }
token_8m() { # the token of THIS player's bundle (its id from join.env) expires 5 to 8.5 minutes from now: the default TOKEN_TTL of 8m
  token_line "$1" | python3 -c '
import re, sys, datetime as d
now = d.datetime.now(d.timezone.utc)
ts = [d.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)", sys.stdin.read())]
sys.exit(0 if len(ts) == 1 and 300 <= (ts[0] - now).total_seconds() <= 510 else 1)'; }
burned_by_reap() { reap_none && ! token_line alice; }
token_reuse_refused() { # zed has its own certificate but presents alice's join token, which reap burned: the join is refused and zed never registers
  local used; used=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/alice/join.env")
  "$MESH" host invite zed --trust public --ttl 1h || return 1
  sed -i "s#^K3S_TOKEN=.*#K3S_TOKEN=$used#" "$STATE/bundles/zed/join.env"
  "$MESH" play zed; [ $? -eq 7 ] || return 1   # exit 7: the control plane rejected the token
  sleep 20; ! k get node zed
}
zed_gone() { "$MESH" host kick zed && ! machine_running zed && ! token_line zed; }
two_full() { exits 5 "$MESH" host approve; }
leases_show_both() { local o; o=$("$MESH" host leases); echo "$o"; echo "$o" | grep -q '^alice ' && echo "$o" | grep -q '^bob ' &&
  "$MESH" host admission-status | grep -q 'slots 2/2 used'; }
wait_alice_end() { local s=$(( $(lease_end alice) - $(date +%s) + 2 )); [ "$s" -le 0 ] || sleep "$s"; }
reap_alice() { wait_alice_end; out_has "reaped 1" "$MESH" host reap && ! "$MESH" host leases | grep -q '^alice '; }
alice_out() { local fp; fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])')
  ! k get node alice && grep -qx "$fp" "$STATE/blocklist" && ! machine_running alice && ready bob; }
reap_none() { local o; o=$("$MESH" host reap); echo "$o"; [ "$o" = "reaped 0" ]; }
api_from_bob2() { incus exec emesh-bob2 -- curl -sk -m5 -o /dev/null -w '%{http_code}' "https://$CP_NEB:6443/readyz" 2>/dev/null; }
bob2_reaches() { [ "$(api_from_bob2)" != 000 ]; }
bob2_cut() { [ "$(api_from_bob2)" = 000 ]; }
copy_refused() { # bob2: a second VM with a copy of bob's bundle (certificate, key, token) that does not use edge-mesh join: Nebula with bob's
  # certificate, then a plain k3s agent under its own name, with its LAN address and an IPv6 address as InternalIP (the copy picks them)
  local tok; tok=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/bob/join.env")
  vm_up bob2 2 3GiB "$UNDERLAY_NET.199" || return 1
  incus exec emesh-bob2 -- date -s "@$(date +%s)" >/dev/null
  tar cf - -C "$STATE/bundles/bob" . | incus exec emesh-bob2 -- tar xf - -C /bundle
  incus exec emesh-bob2 -- sh -c 'mkdir -p /etc/nebula && cp /bundle/ca.crt /bundle/host.crt /bundle/host.key /bundle/config.yml /etc/nebula/ &&
    (nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &) && ip -6 addr add fd00:99::199/64 dev enp5s0'
  wait_for 60 bob2_reaches || return 1   # positive control: the copy is inside the mesh and talks to the API
  printf 'nohup k3s agent --server https://%s:6443 --token %s --node-name bob2 --node-ip %s,fd00:99::199 >/var/log/k3s-copy.log 2>&1 &\n' \
    "$CP_NEB" "$tok" "$UNDERLAY_NET.199" >"$STATE/copy-bob2.sh"
  incus file push -q "$STATE/copy-bob2.sh" emesh-bob2/root/copy.sh && incus exec emesh-bob2 -- sh /root/copy.sh
  wait_for 240 incus exec emesh-bob2 -- grep -q "edge-mesh node guard" /var/log/k3s-copy.log || return 1   # the guard answered it
  sleep 20; ! k get node bob2
}
squat_refused() { # the same machine, with bob's bundle and bob's still unburned token, registers as vic (issued, not joined) at vic's Nebula IP
  local tok vip; tok=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/bob/join.env")
  "$MESH" host invite vic --trust friends --ttl 2h || return 1
  vip=$(sed -n 's/^NODE_IP=//p' "$STATE/bundles/vic/join.env")
  incus exec emesh-bob2 -- pkill -f "k3s agen[t]"; sleep 3
  incus exec emesh-bob2 -- sh -c "rm -rf /var/lib/rancher/k3s/agent /etc/rancher/node; ip link add vic0 type dummy; ip addr add $vip/32 dev vic0; ip link set vic0 up"
  printf 'nohup k3s agent --server https://%s:6443 --token %s --node-name vic --node-ip %s >/var/log/k3s-squat.log 2>&1 &\n' "$CP_NEB" "$tok" "$vip" \
    >"$STATE/squat.sh"
  incus file push -q "$STATE/squat.sh" emesh-bob2/root/squat.sh && incus exec emesh-bob2 -- sh /root/squat.sh
  wait_for 240 incus exec emesh-bob2 -- grep -qi "node password rejected" /var/log/k3s-squat.log || return 1   # k3s answered it
  sleep 20; ! k get node vic
}
dropped_name_refused() { # wes is issued; k3s drops its node password as it does for a name nobody took in 10 minutes (here by hand, now):
  # the next reap takes wes off the node guard, so the copy cannot take the name, though wes's token and bob's token still work
  local tok wip; tok=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/bob/join.env")
  "$MESH" host invite wes --trust public --ttl 1h || return 1
  wip=$(sed -n 's/^NODE_IP=//p' "$STATE/bundles/wes/join.env")
  k -n kube-system delete secret wes.node-password.k3s || return 1
  "$MESH" host reap >/dev/null || return 1
  k -n kube-system get configmap edge-mesh-nodes -o jsonpath='{.data.wes}' | grep -q . && return 1   # off the guard
  incus exec emesh-bob2 -- pkill -f "k3s agen[t]"; sleep 3
  incus exec emesh-bob2 -- sh -c "rm -rf /var/lib/rancher/k3s/agent /etc/rancher/node; ip link add wes0 type dummy; ip addr add $wip/32 dev wes0; ip link set wes0 up"
  printf 'nohup k3s agent --server https://%s:6443 --token %s --node-name wes --node-ip %s >/var/log/k3s-wes.log 2>&1 &\n' "$CP_NEB" "$tok" "$wip" \
    >"$STATE/wes.sh"
  incus file push -q "$STATE/wes.sh" emesh-bob2/root/wes.sh && incus exec emesh-bob2 -- sh /root/wes.sh
  wait_for 240 incus exec emesh-bob2 -- grep -q "edge-mesh node guard" /var/log/k3s-wes.log || return 1   # the guard answered it
  sleep 20; k get node wes && return 1
  "$MESH" host revoke wes >/dev/null
  # the copy goes quiet before vic and bob join (two machines with one Nebula certificate fight over its address); it comes back later
  incus stop -f emesh-bob2 || true   # its checks are above; a stop that reports a transport error still stops it (A30 starts it again)
}
vic_joins() { play_ready vic && "$MESH" host kick vic && ! k get node vic; }
none_left() { out_has "reaped 2" "$MESH" host reap --all && ! k get node bob && ! k get node bob2 && ! k get node carol &&
  [ "$(k get nodes -o name)" = "node/cp" ] && "$MESH" host admission-status | grep -q 'slots 0/2 used'; }
copy_cut() { # the copy, started again with bob's certificate after his lease was kicked, cannot get back to the API
  incus start emesh-bob2 && wait_for 240 incus exec emesh-bob2 -- true || return 1
  incus exec emesh-bob2 -- date -s "@$(date +%s)" >/dev/null
  incus exec emesh-bob2 -- sh -c '(nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &)'
  sleep 30; ! wait_for 30 bob2_reaches
}

step A01 "mesh host init: lighthouse and control plane" "$MESH" host init
step A02 "alice asks for a place: position 1 of 1" out_has "alice: waiting, position 1 of 1" "$MESH" host request alice --ip 198.51.100.10 --note "first"
step A03 "bob asks: position 2 of 2" out_has "bob: waiting, position 2 of 2" "$MESH" host request bob --ip 198.51.100.20
step A04 "carol asks from IPv6: position 3 of 3" out_has "carol: waiting, position 3 of 3" "$MESH" host request carol --ip 2001:db8:c::1
step A05 "a name that is not [a-z][a-z0-9-] is refused (exit 2) and not listed" bad_name_refused
step A06 "a second request from alice's IP under another name is not added" same_ip_not_added
step A07 "approve with no name takes the first in line: alice" out_has "approved alice (public)" "$MESH" host approve
step A08 "alice's certificate lives exactly the lease ($LEASE s)" cert_lives_the_lease alice
step A09 "alice's own join token (its id in her bundle) expires in about 8 minutes, the most a token may live" token_8m alice
step A10 "alice joins with her bundle and is Ready" play_ready alice
step A11 "reap (the timer's command) burns alice's join token now that her node registered" burned_by_reap
step A12 "a second machine (zed, own certificate) presenting alice's token is refused (exit 7) and never registers" token_reuse_refused
step A13 "zed is kicked: machine gone, its own token burned" zed_gone
step A14 "approve again: bob, the next in line" out_has "approved bob (public)" "$MESH" host approve
step A15 "a copy of bob's bundle (bob2: plain k3s agent, LAN and IPv6 addresses, own name) reaches the API but the node guard refuses its Node" copy_refused
step A16 "the copy registers as vic (invited, not joined) at vic's Nebula IP with bob's token: k3s refuses the name (vic's node password)" squat_refused
step A17 "an issued name whose node password k3s dropped (wes) leaves the guard at the next reap: the copy cannot take it either" dropped_name_refused
step A18 "vic, the owner of the name, still joins with her bundle and is Ready (then kicked)" vic_joins
step A19 "bob joins and is Ready" play_ready bob
step A20 "both places taken: approve refuses (exit 5), carol keeps waiting" two_full
step A21 "leases lists alice and bob; admission-status says slots 2/2 used" leases_show_both
step A22 "reap before any lease ends: reaped 0, nobody touched" reap_none
step A23 "alice's lease ends: reap expels her (reaped 1)" reap_alice
step A24 "alice is out (node gone, fingerprint on the blocklist, machine gone); bob is still Ready" alice_out
step A25 "reap again: reaped 0 (idempotent)" reap_none
step A26 "alice asks again under a new name: deferred by the cooldown" out_has "alice-2: deferred until" "$MESH" host request alice-2 --ip 198.51.100.10
step A27 "the freed place goes to the next one in line: carol" out_has "approved carol (public)" "$MESH" host approve
step A28 "carol joins and is Ready" play_ready carol
step A29 "reap --all (the abuse switch): reaped 2, no Node left but the control plane, slots 0/2" none_left
step A30 "the copy of bob's bundle, started again after the kick, cannot reach the API: bob's certificate is blocklisted" copy_cut
step A31 "reap again: reaped 0" reap_none

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
