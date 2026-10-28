#!/usr/bin/env bash
# make poc-vm: step 2 on real VMs (Incus, Ubuntu 24.04, own kernel). The same checks as poc-cilium.sh (checks-cilium.sh),
# but the nodes are VMs: closer to how people join (a VM or a host), and the kernel is a distro one, not the WSL2 kernel.
# Run on a Linux host with Incus. KEEP=1 leaves the VMs up afterwards.
set -u
export RUNTIME=incus CNI=cilium
. "$(dirname "$0")/../lib.sh"
nx() { local n="$1"; shift; incus exec "emesh-$n" -- "$@"; }
tcpd() { local n="$1"; shift; incus exec "emesh-$n" -- timeout 30 tcpdump "$@"; }
. "$POC/checks-cilium.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"

vm_up() { "$POC/vm/up.sh"; }
kernel_ok() { local v; v=$(nx n1 uname -r); echo "$v"; [ "${v%%.*}" -ge 6 ]; }
install_cilium() { "$POC/cilium.sh"; }
step V01 "4 VMs (lighthouse, control plane, 2 nodes): Nebula + k3s without flannel, nodes joined" vm_up
step V02 "the VMs run a distro kernel >= 6" kernel_ok
step V03 "Cilium $CILIUM_VERSION installed (tunnel mode, WireGuard) with helm --wait" install_cilium
step V04 "all three nodes Ready" wait_for 300 all_ready
step V05 "Cilium agent healthy on every node" wait_for 180 agents_ok
step V06 "WireGuard on, two peers on every node" wait_for 120 wg_on
test_pods >>"$LOG" 2>&1
step V07 "test pods Running on different nodes (web on n1, cli on n2)" wait_for 240 pods_ready
step V08 "pod on n2 reaches pod on n1 across nodes" wait_for 60 cross_node
step V09 "DNS inside a pod resolves cluster services" wait_for 120 dns_ok
step V10 "on the wire: the clear text exists inside the node (cilium_wg0, VXLAN payload) and never on the Nebula interface" on_the_wire
step V11 "MTU: Nebula 1300 >= pod 1150 + WireGuard overhead" mtu_chain
step V12 "MTU: a full-size DF packet crosses, one byte more is refused" df_ping
step V13 "30 MB pod-to-pod over WireGuard over Nebula arrives intact" big_transfer
step V13b "host-to-host TCP (the Cilium agents' own traffic) crosses WireGuard without a stall: 2 MB between node addresses" host_tcp
step V14 "Nebula firewall allow-list: only the documented ports appear on the Nebula interface" underlay_ports
step V15 "narrowed Nebula firewall: ICMP and 6443 pass, an unlisted TCP port between hosts is refused" fw_blocks
mutual_up() { MUTUAL=yes HELM_TIMEOUT=4m "$POC/cilium.sh"; }
step V90 "mutual authentication: SPIRE installed on top (helm --wait)" mutual_up
step V91 "SPIRE server and agents ready, the 3 registration entries created" wait_for 240 spire_full
step V92 "policy with authentication.mode=required: traffic allowed, auth entry present" mutual_ok
[ "${HOLD:-0}" = 1 ] && [ "$FAILS" -gt 0 ] && { cp "$LOG" /tmp/poc-vm-hold.log; echo "HOLD: V92 failed, VMs kept for diagnosis"; exit 1; }
step V93 "SPIRE server runs as uid 1000, not root" spire_nonroot
step V94 "SPIRE server pinned to an own node" spire_on_own
step V95 "SPIRE datastore on a bound PVC" wait_for 30 spire_pvc
step V96 "SPIRE state survives a server restart (same CA bundle, same entries)" spire_survives_restart
step V97 "mutual authentication still works after the restart" mutual_after_restart
cp "$LOG" /tmp/poc-vm-last.log 2>/dev/null
[ "${KEEP:-0}" = 1 ] || "$POC/vm/down.sh" >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
