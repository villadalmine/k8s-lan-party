#!/usr/bin/env bash
# make poc-admission-auto: the automatic pool (phase B1, docs/ADMISSION.md) end to end on Incus VMs, with no hand in the loop between the
# requests and the expulsion: three people ask, `mesh host auto on`, one tick (`mesh host auto --once`, what the host's timer runs) approves two
# and the third waits; both join with the bundle the tick left in the outbox; the lease ends and ONE tick expels them (a copy of bob's bundle on a
# second machine, bob2, tries before bob joins and is refused by the node guard; started again after the kick it cannot reach the API; no Node is left but the control plane) and only
# then promotes the third; a burned token is refused to another
# machine; with auto off the tick only expels; the daily cap refuses a third request of one identity; panic expels everyone; the notify hook
# received every event in order. Needs a Linux host with Incus. PASS/FAIL per step; identical across runs.
# Test values (the other limits are the real ones): ADM_LEASE seconds (default 1200, as poc-admission: three VMs boot inside the first lease), ADM_COOLDOWN
# (default 60, so the same identity's second lease fits in the run). KEEP=1 leaves the VMs up afterwards.
set -u
export RUNTIME=incus MAX_VMS=5 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-admission-auto.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"
LEASE="${ADM_LEASE:-1200}"
COOLDOWN="${ADM_COOLDOWN:-60}"
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^cooldown_seconds: [0-9]*/cooldown_seconds: $COOLDOWN/" "$POC/pool.yaml" >"$POOL_CONFIG"
ADM="$STATE/admission"
# The notify hook of the test: one line per call, "<type>|<summary>". The repository knows no ntfy and no bot; the host wires its own command.
HOOK_LOG="$STATE/hook.log"; : >"$HOOK_LOG"
printf '#!/bin/sh\nprintf "%%s|%%s\\n" "$1" "$2" >>"%s"\n' "$HOOK_LOG" >"$STATE/hook.sh"; chmod +x "$STATE/hook.sh"
export ADMISSION_NOTIFY_CMD="$STATE/hook.sh"
unset ADMISSION_NOTIFY_IP

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
exits() { local want="$1"; shift; "$@"; [ $? -eq "$want" ]; }
tick() { "$MESH" host auto --once; }
tick_has() { out_has "$1" tick; }
play_ready() { "$MESH" play "$1" && wait_for 240 ready "$1"; }
lease_end() { python3 -c 'import json,sys; print(next(l["end"] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == sys.argv[2]))' "$ADM/state.json" "$1"; }
sleep_until() { local s=$(( $1 - $(date +%s) )); [ "$s" -le 0 ] || sleep "$s"; }
digest() { cat "$ADM/state.json" "$ADM/auto.json" "$ADM/events.jsonl" 2>/dev/null | sha256sum; }
tick_noop() { # a tick, then a second one: the second expels nothing, approves nothing and leaves the state, the switch and the log byte-identical
  tick || return 1; local a; a=$(digest); out_has "expelled 0 | approved 0" tick || return 1; [ "$a" = "$(digest)" ]; }
bad_name_refused() { local n; n=$(wc -l <"$HOOK_LOG"); exits 2 "$MESH" host request 'eve;id' --ip 198.51.100.30 &&
  exits 2 "$MESH" host request '$(id)' --ip 198.51.100.31 && [ "$(wc -l <"$HOOK_LOG")" -eq "$n" ]; }
token_line() { local id; id=$(token_id "$1"); [ -n "$id" ] && cpx k3s token list 2>/dev/null | grep -E "(^|[^a-z0-9])$id([^a-z0-9]|$)"; }
cert_lives_the_lease() { "$CACHE/nebula-cert" print -json -path "$STATE/bundles/$1/host.crt" | python3 -c '
import sys, json, datetime as d
n = json.load(sys.stdin)[0]["details"]
a = d.datetime.fromisoformat(n["notBefore"].replace("Z", "+00:00")); b = d.datetime.fromisoformat(n["notAfter"].replace("Z", "+00:00"))
sys.exit(0 if abs((b - a).total_seconds() - int(sys.argv[1])) <= 10 else 1)' "$LEASE"; }
outbox_is_the_bundle() { # the outbox file is 0600, holds exactly the bundle the invite wrote, the same bytes again, and the join line is printed
  local t="$ADM/outbox/$1.tar.gz" x; x=$(mktemp -d)
  [ "$(stat -c %a "$t")" = 600 ] && [ "$(stat -c %a "$ADM/outbox")" = 700 ] && tar xzf "$t" -C "$x" && diff -r "$x/$1" "$STATE/bundles/$1" &&
    cert_lives_the_lease "$1" && out_has "join: tar xzf $1.tar.gz && sudo edge-mesh join ./$1 --accept-golden-rule" "$MESH" host bundle "$1" &&
    "$MESH" host bundle "$1" | grep -q '(unchanged)'
  local rc=$?; rm -rf "$x"; return $rc
}
api_from_bob2() { incus exec emesh-bob2 -- curl -sk -m5 -o /dev/null -w '%{http_code}' "https://$CP_NEB:6443/readyz" 2>/dev/null; }
bob2_reaches() { [ "$(api_from_bob2)" != 000 ]; }
bob2_cut() { [ "$(api_from_bob2)" = 000 ]; }
copy_refused() { # bob2: a second VM with a copy of bob's bundle (certificate, key, token) that does not use edge-mesh join: Nebula with bob's
  # certificate, then a plain k3s agent under its own name, with its LAN address and an IPv6 address as InternalIP (the same copy as poc-admission A16)
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
  sleep 20; k get node bob2 && return 1
  # the copy goes quiet before bob joins (two machines with one Nebula certificate fight over its address); it comes back after the kick
  incus stop -f emesh-bob2
}
tokens_burned() { out_has "expelled 0 | approved 0" tick && ! token_line alice && ! token_line bob; }
expiring_once() { # 5 minutes before the end a tick records `expiring` for both, and a later tick does not repeat it
  sleep_until $(( $(lease_end alice) - 300 + 2 )); tick && tick &&
  [ "$(grep -c '^expiring|alice:' "$HOOK_LOG")" -eq 1 ] && [ "$(grep -c '^expiring|bob:' "$HOOK_LOG")" -eq 1 ]; }
expel_then_promote() { # one tick at the end: alice and bob out, no Node left but the control plane, and only then carol in
  sleep_until $(( $(lease_end alice) + 2 )); out_has "expelled 2 | approved 1 | slots 1/2 used" tick || return 1
  [ "$(k get nodes -o name)" = "node/cp" ] && "$MESH" host leases | grep -q '^carol ' &&
  python3 - "$ADM/events.jsonl" <<'PY'
import json, sys
ev = [json.loads(l) for l in open(sys.argv[1])]
order = [(e["type"], e["name"]) for e in ev]
sys.exit(0 if order.index(("expelled", "bob")) < order.index(("approved", "carol")) else 1)
PY
}
alice_out() { local fp; fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])')
  grep -qx "$fp" "$STATE/blocklist" && ! machine_running alice && ! machine_running bob &&
  [ ! -e "$ADM/outbox/alice.tar.gz" ] && [ ! -e "$ADM/outbox/bob.tar.gz" ] && [ -f "$ADM/outbox/carol.tar.gz" ]; }
copy_cut() { # the copy, started again with bob's certificate after the tick kicked his lease, cannot get back to the API
  incus start emesh-bob2 && wait_for 240 incus exec emesh-bob2 -- true || return 1
  incus exec emesh-bob2 -- date -s "@$(date +%s)" >/dev/null
  incus exec emesh-bob2 -- sh -c '(nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &)'
  sleep 30; ! wait_for 30 bob2_reaches || return 1
  incus delete -f emesh-bob2 >/dev/null
}
token_reuse_refused() { # zed has its own certificate but presents alice's join token, which the tick burned: refused, never registers
  local used; used=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/alice/join.env")
  "$MESH" host invite zed --trust public --ttl 1h || return 1
  sed -i "s#^K3S_TOKEN=.*#K3S_TOKEN=$used#" "$STATE/bundles/zed/join.env"
  "$MESH" play zed; [ $? -eq 7 ] || return 1   # exit 7: the control plane rejected the token
  sleep 20; ! k get node zed
}
zed_gone() { "$MESH" host kick zed && ! machine_running zed && ! token_line zed; }
off_only_warns() { # auto off, 5 min before carol's end: `expiring` for carol, and no approval although alice-2 and dave are eligible and a place is free
  sleep_until $(( $(lease_end carol) - 300 + 2 )); out_has "tick: auto off | expelled 0 | approved 0 | slots 1/2 used | 2 in the list (2 eligible now)" tick &&
  grep -q '^expiring|carol:' "$HOOK_LOG"; }
off_only_expels() { sleep_until $(( $(lease_end carol) + 2 )); out_has "tick: auto off | expelled 1 | approved 0 | slots 0/2 used | 2 in the list" tick &&
  ! k get node carol && ! machine_running carol; }
panic_all() { out_has "panic: on | expelled 2 | auto off | slots 0" "$MESH" host panic && ! k get node alice-2 && ! machine_running alice-2 &&
  [ -z "$(ls -A "$ADM/outbox")" ] && "$MESH" host leases | awk 'NR>1' | grep -c . | grep -qx 0; }
panic_again() { local n; n=$(wc -l <"$HOOK_LOG"); out_has "panic: on (already) | expelled 0" "$MESH" host panic && [ "$(wc -l <"$HOOK_LOG")" -eq "$n" ]; }
panic_holds() { out_has "auto: off | panic: on | slots 0/0 used" "$MESH" host auto status && exits 7 "$MESH" host auto on && exits 7 "$MESH" host approve &&
  out_has "tick: auto off (panic) | expelled 0 | approved 0" tick; }
cleared() { out_has "panic: cleared" "$MESH" host panic --clear && out_has "tick: auto off | expelled 0 | approved 0" tick; }
hook_in_order() { # every event reached the hook, in this order, with no IP, key, token or path in the arguments
  ! grep -Eq '198\.51\.100|2001:db8|K10|PRIVATE|/' "$HOOK_LOG" || return 1
  python3 - "$HOOK_LOG" <<'PY'
import sys
want = """request alice|request bob|request carol|auto_on|approved alice|bundle_ready alice|approved bob|bundle_ready bob|expiring alice|expiring bob|
request alice-2|expelled alice|expelled bob|approved carol|bundle_ready carol|auto_off|request dave|expiring carol|expelled carol|auto_on|approved alice-2|
bundle_ready alice-2|approved dave|bundle_ready dave|refused alice-3|panic|expelled alice-2|expelled dave|panic_cleared"""
want = [w.strip() for w in want.replace("\n", "").split("|")]
named = {"request", "refused", "approved", "bundle_ready", "expiring", "expelled"}
got = []
for line in open(sys.argv[1]):
    kind, summary = line.rstrip("\n").split("|", 1)
    got.append(f"{kind} {summary.split(':')[0]}" if kind in named else kind)
print("\n".join(got))
sys.exit(0 if got == want else 1)
PY
}
on_approves_two() { "$MESH" host auto on && out_has "tick: auto on | expelled 0 | approved 2 | slots 2/2 used | 0 in the list" tick &&
  "$MESH" host leases | grep -q '^alice-2 ' && "$MESH" host leases | grep -q '^dave '; }
daily_cap_refused() { local o rc; o=$("$MESH" host request alice-3 --ip 198.51.100.10 2>&1); rc=$?; echo "$o"
  [ "$rc" -eq 6 ] && echo "$o" | grep -q "alice-3: refused: the daily cap (2 leases in 24 h)" && ! "$MESH" host waitlist | grep -q alice-3; }
metrics_match() { local m; m=$("$MESH" host admission-metrics); echo "$m"
  for want in 'emesh_admission_slots_total 2' 'emesh_admission_leases_active 0' 'emesh_admission_auto_enabled 0' 'emesh_admission_panic 0' \
    'emesh_admission_events_total{type="approved"} 5' 'emesh_admission_events_total{type="expelled"} 5' \
    'emesh_admission_events_total{type="refused"} 1' 'emesh_admission_events_total{type="kick_failed"} 0'; do
    echo "$m" | grep -qxF "$want" || return 1
  done
  ! echo "$m" | grep -Eq 'alice|bob|carol|dave|198\.51|2001:db8'; }

step AA01 "mesh host init: lighthouse and control plane" "$MESH" host init
step AA02 "auto is off by default" out_has "auto: off | panic: off" "$MESH" host auto status
step AA03 "alice asks for a place: position 1 of 1" out_has "alice: waiting, position 1 of 1" "$MESH" host request alice --ip 198.51.100.10
step AA04 "bob asks: position 2 of 2" out_has "bob: waiting, position 2 of 2" "$MESH" host request bob --ip 198.51.100.20
step AA05 "carol asks from IPv6: position 3 of 3" out_has "carol: waiting, position 3 of 3" "$MESH" host request carol --ip 2001:db8:c::1
step AA06 "names with ';' or '\$(...)' are refused (exit 2) and reach no hook" bad_name_refused
step AA07 "with auto off a tick approves nobody" tick_has "tick: auto off | expelled 0 | approved 0 | slots 0/2 used | 3 in the list"
step AA08 "mesh host auto on" out_has "auto: on" "$MESH" host auto on
step AA09 "one tick approves alice and bob; carol waits" tick_has "tick: auto on | expelled 0 | approved 2 | slots 2/2 used | 1 in the list (1 eligible now)"
step AA10 "a tick with nothing to do changes nothing (state, switch and log byte-identical)" tick_noop
step AA11 "the outbox holds alice's bundle: 0600, the same files, a certificate of exactly the lease ($LEASE s), the join line" outbox_is_the_bundle alice
step AA12 "alice joins with her bundle and is Ready" play_ready alice
step AA13 "a copy of bob's bundle (bob2: plain k3s agent, LAN and IPv6 addresses, own name) reaches the API but the node guard refuses its Node" copy_refused
step AA14 "bob joins with his bundle and is Ready" play_ready bob
step AA15 "the next tick burns alice's and bob's join tokens and changes nothing else" tokens_burned
step AA16 "5 minutes before the end a tick records 'expiring' for alice and bob, once" expiring_once
step AA17 "alice asks again as alice-2: deferred by her active lease" out_has "alice-2: deferred until" "$MESH" host request alice-2 --ip 198.51.100.10
step AA18 "the leases end: one tick expels alice and bob, leaves no Node but the control plane, and only then promotes carol" expel_then_promote
step AA19 "alice is out (fingerprint on the blocklist, machines gone); their outbox files are deleted, carol's is there" alice_out
step AA20 "the copy of bob's bundle, started again after the kick, cannot reach the API: bob's certificate is blocklisted" copy_cut
step AA21 "mesh host auto off" out_has "auto: off" "$MESH" host auto off
step AA22 "dave asks: position 2 of 2" out_has "dave: waiting, position 2 of 2" "$MESH" host request dave --ip 198.51.100.40
step AA23 "a second machine (zed, own certificate) presenting alice's burned token is refused (exit 7) and never registers" token_reuse_refused
step AA24 "zed is kicked: machine gone, its own token burned" zed_gone
step AA25 "carol, promoted by the tick, joins with her bundle and is Ready" play_ready carol
step AA26 "auto off: a tick with nothing to expel changes nothing, although alice-2 and dave are eligible" tick_noop
step AA27 "auto off: 5 minutes before carol's end the tick warns and approves nobody (2 eligible, a place free)" off_only_warns
step AA28 "auto off: carol's lease ends and the tick only expels her" off_only_expels
step AA29 "auto on: one tick approves alice-2 (her identity's second lease in 24 h) and dave" on_approves_two
step AA30 "alice-3 from the same IP: refused by the daily cap of 2 (exit 6), not listed" daily_cap_refused
step AA31 "alice-2 joins with her bundle and is Ready" play_ready alice-2
step AA32 "panic: auto off, slots 0, every lease expelled (alice-2 and dave), no node, empty outbox" panic_all
step AA33 "panic again: expelled 0, no new event (idempotent)" panic_again
step AA34 "while panic is on: status says so, auto on and approve are refused (exit 7), a tick approves nobody" panic_holds
step AA35 "panic --clear: auto stays off, a tick approves nobody" cleared
step AA36 "the notify hook received every event in order, without IP, key, token or path" hook_in_order
step AA37 "admission-metrics: the counters match, no name or identity" metrics_match

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
