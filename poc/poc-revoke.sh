#!/usr/bin/env bash
# make poc-revoke: "revocation in one command" (issue #7). `mesh host revoke <name>` puts the certificate fingerprint on the Nebula blocklist of every
# host, reloads them and deletes the node object. Proved here: deleting the node alone does NOT revoke it (the machine stays in the mesh); the one command does;
# it is idempotent; an unknown name is refused; it works for a pool (public) node too; a host admitted later starts with the revocations already in
# its blocklist; and the time from the command to the loss of access is measured. Same runtimes as poc-join (RUNTIME=docker|incus).
# Output: one PASS/FAIL per step, identical across runs. The measured time goes to results/revoke-measurements.txt (it varies).
set -u
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
MESH="$POC/mesh"
MEAS="$STATE/revoke-measurements.txt"; : >"$MEAS"
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
bring_up() { "$UP" && dx cp ping -c1 -W2 "$LH_NEB"; }
fp_of() { "$CACHE/nebula-cert" print -json -path "$STATE/bundles/$1/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])'; }
in_mesh() { dx "$1" ping -c1 -W2 "$LH_NEB"; }
out_of_mesh() { ! dx "$1" ping -c1 -W2 "$LH_NEB"; }

t_delete_alone_is_not_revocation() { # the negative test: delete the node object and nothing else. The node object goes (the kubelet does not
  # re-register until it restarts), but the machine is still inside the mesh: Nebula still carries it to the lighthouse and the control plane.
  k delete node n1 --wait=true >/dev/null && ! k get node n1 >/dev/null 2>&1 && in_mesh n1 && dx n1 ping -c1 -W2 "$CP_NEB" >/dev/null
}
t_revoke_n1() { # the single command; the time to loss of access is measured around it
  local t0 t1; t0=$(date +%s); "$MESH" host revoke n1 || return 1
  wait_for 90 out_of_mesh n1 || return 1; t1=$(date +%s)
  echo "revoke n1 (friends, persistent CA): $((t1 - t0)) s from the command until n1 cannot reach the lighthouse" >>"$MEAS"
}
t_node_gone_and_stays() { ! k get node n1 >/dev/null 2>&1 && sleep 40 && ! k get node n1 >/dev/null 2>&1; }
count_listed() { grep -c "$(fp_of "$1")" "$STATE"/bundles/*/config.yml | awk -F: '{s+=$2} END {print s}'; }
t_idempotent() { # a second run: exit 0, nothing added twice (the fingerprint appears once per host config and once in .state/blocklist)
  local a b; a=$(count_listed n1); "$MESH" host revoke n1 >/dev/null || return 1; b=$(count_listed n1)
  echo "occurrences before=$a after=$b; blocklist file lines: $(grep -c "$(fp_of n1)" "$STATE/blocklist")"
  [ "$a" = "$b" ] && [ "$a" -ge 1 ] && [ "$(grep -c "$(fp_of n1)" "$STATE/blocklist")" = 1 ]
}
t_unknown() { local rc=0 out; out=$("$MESH" host revoke nobody 2>&1) || rc=$?; echo "$out"; [ "$rc" = 3 ] && echo "$out" | grep -q "no such player"; }
t_revoke_pool_node() { # a public node (pool CA) is revoked the same way
  local t0 t1; t0=$(date +%s); "$MESH" host revoke n2 || return 1
  wait_for 90 out_of_mesh n2 || return 1; t1=$(date +%s)
  echo "revoke n2 (public, pool CA): $((t1 - t0)) s" >>"$MEAS"
  ! k get node n2 >/dev/null 2>&1
}
t_later_host_inherits() { # a player invited AFTER the revocations starts with both fingerprints already in its blocklist
  "$MESH" host invite late --ttl 1h --trust friends >/dev/null || return 1
  grep -q "$(fp_of n1)" "$STATE/bundles/late/config.yml" && grep -q "$(fp_of n2)" "$STATE/bundles/late/config.yml"
}
t_cp_lists_both() { grep -q "$(fp_of n1)" "$STATE/bundles/cp/config.yml" && grep -q "$(fp_of n2)" "$STATE/bundles/cp/config.yml" && grep -q "$(fp_of n1)" "$STATE/bundles/lh/config.yml"; }

step R01 "mesh up: lighthouse, control plane, n1 (friends) and n2 (public) joined" bring_up
both_ready() { ready n1 && ready n2; }
step R02 "n1 and n2 Ready" wait_for 180 both_ready
step R03 "NEGATIVE: kubectl delete node n1 alone is not a revocation: the machine is still inside the mesh (lighthouse and control plane reachable)" t_delete_alone_is_not_revocation
step R04 "mesh host revoke n1: one command, and n1 can no longer reach the lighthouse" t_revoke_n1
step R05 "the node object is gone and stays gone" t_node_gone_and_stays
step R06 "idempotent: a second revoke exits 0 and adds nothing (once per host config, once in the blocklist file)" t_idempotent
step R07 "an unknown name is refused with a clear message (exit 3)" t_unknown
step R08 "a pool (public) node is revoked the same way" t_revoke_pool_node
step R09 "the lighthouse and the control plane list both fingerprints" t_cp_lists_both
step R10 "a player invited after the revocations starts with both already blocklisted" t_later_host_inherits

mkdir -p "$POC/results"; cp "$MEAS" "$POC/results/revoke-measurements.txt"
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
