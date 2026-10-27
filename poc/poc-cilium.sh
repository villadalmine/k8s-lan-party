#!/usr/bin/env bash
# make poc-cilium: step 2, "the pod network is encrypted". Same cluster as step 1 (Nebula + k3s in Docker) but flannel off and
# Cilium on: tunnel mode, WireGuard pod-to-pod and node-to-node, MTU set once, mutual authentication by policy (SPIRE).
# Needs Docker on a Linux kernel with BPF (WSL2, a Linux host; NOT Docker Desktop on macOS: its VM kernel is another story, not tested: docs/POC-STATUS.md).
# Output: one PASS/FAIL line per step, identical across runs.
set -u
. "$(dirname "$0")/lib.sh"
nx() { local n="$1"; shift; docker exec "$n" "$@"; }
tcpd() { local n="$1"; shift; docker run --rm --network "container:$n" --cap-add NET_ADMIN --cap-add NET_RAW "$NETSHOOT_IMAGE" timeout 30 tcpdump "$@"; }
. "$POC/checks-cilium.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"

bring_up() { CNI=cilium "$POC/up.sh"; }
install_cilium() { "$POC/cilium.sh"; }
step C01 "Nebula + k3s without flannel: control plane and two nodes up" bring_up
step C02 "Cilium $CILIUM_VERSION installed (tunnel mode, WireGuard) with helm --wait" install_cilium
step C03 "all three nodes Ready (the CNI is what makes them Ready)" wait_for 300 all_ready
step C04 "Cilium agent healthy on every node" wait_for 180 agents_ok
step C05 "WireGuard on, two peers on every node" wait_for 120 wg_on
test_pods >>"$LOG" 2>&1
step C06 "test pods Running on different nodes (web on n1, cli on n2)" wait_for 240 pods_ready
step C07 "pod on n2 reaches pod on n1 across nodes" wait_for 60 cross_node
step C08 "DNS inside a pod resolves cluster services" wait_for 120 dns_ok
docker pull -q "$NETSHOOT_IMAGE" >/dev/null 2>&1
step C09 "on the wire: the clear text exists inside the node (cilium_wg0, VXLAN payload) and never on the Nebula interface" on_the_wire
step C10 "MTU: Nebula 1300 >= pod 1150 + WireGuard overhead" mtu_chain
step C11 "MTU: a full-size DF packet crosses, one byte more is refused" df_ping
step C12 "30 MB pod-to-pod over WireGuard over Nebula arrives intact" big_transfer
step C13 "Nebula firewall allow-list: only the documented ports appear on the Nebula interface" underlay_ports
step C13b "host-to-host TCP (the Cilium agents' own traffic) crosses WireGuard without a stall: 2 MB between node addresses" host_tcp
step C14 "narrowed Nebula firewall: ICMP and 6443 pass, an unlisted TCP port between hosts is refused" fw_blocks
mutual_up() { MUTUAL=yes HELM_TIMEOUT=4m "$POC/cilium.sh"; }
step C90 "mutual authentication: SPIRE installed on top (helm --wait)" mutual_up
step C91 "SPIRE server and agents ready, the 3 registration entries created" wait_for 240 spire_full
step C92 "policy with authentication.mode=required: traffic allowed, auth entry present" mutual_ok
step C93 "SPIRE server runs as uid 1000, not root" spire_nonroot
step C94 "SPIRE server pinned to an own node" spire_on_own
step C95 "SPIRE datastore on a bound PVC" wait_for 30 spire_pvc
step C96 "SPIRE state survives a server restart (same CA bundle, same entries)" spire_survives_restart
step C97 "mutual authentication still works after the restart" mutual_after_restart
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
