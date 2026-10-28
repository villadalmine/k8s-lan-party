#!/usr/bin/env bash
# make poc-vm: step 2 on real VMs (Incus, Ubuntu 24.04, own kernel). The same checks as poc-cilium.sh (checks-cilium.sh),
# but the nodes are VMs: closer to how people join (a VM or a host), and the kernel is a distro one, not the WSL2 kernel.
# Run on a Linux host with Incus. KEEP=1 leaves the VMs up afterwards.
set -u
export RUNTIME=incus CNI=cilium TAINT=yes   # remote nodes tainted at registration, as a real join does
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
step V04b "n1 and n2 carry their trust taint from registration (friends, public)" remote_tainted
step V05 "Cilium agent healthy on every node" wait_for 180 agents_ok
step V06 "cilium-dbg encrypt status: WireGuard on every node, every other node a peer, its own key the one it published" wait_for 120 wg_on
step V06b "the WireGuard peers in the kernel of every node are exactly the other nodes' published keys" wg_peers_match
step V06c "NEGATIVE: the Cilium agent token on a remote node cannot rewrite another node's WireGuard key (its own CiliumNode it can)" node_guard_holds
step V06d "the k3s-in-Docker finding (Cilium in CreateContainerError) cannot come back: the containerd CNI drop-in is on every node" cni_dropin_everywhere
test_pods >>"$LOG" 2>&1
step V07 "test pods Running on different nodes (web on n1, cli on n2)" wait_for 240 pods_ready
step V08 "pod on n2 reaches pod on n1 across nodes" wait_for 60 cross_node
step V09 "DNS inside a pod resolves cluster services" wait_for 120 dns_ok
step V10 "on the wire: the clear text exists inside the node (cilium_wg0, VXLAN payload) and never on the Nebula interface" on_the_wire
step V11 "MTU set once: Nebula $NEBULA_MTU on every node, the pod MTU ($POD_MTU) derived from it on every pod" mtu_chain
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
step V98 "the SPIRE agent leaves $AUTH_DENY_NODE; two twin pods (one identity, new) on $AUTH_OK_NODE and on $AUTH_DENY_NODE" deny_setup
step V99 "the twins share one identity, not one that authenticated before" wait_for 60 twins_same_identity
step V100 "the twin on a node with an identity is authenticated and allowed by the policy" wait_for 120 auth_ok_twin
step V101 "NEGATIVE: the twin on the node without an identity is dropped: no answer, drops 'Authentication required' counted" auth_denied_twin
step V102 "the SPIRE agent back on $AUTH_DENY_NODE: the same pod is let in (the drop was authentication, nothing else)" auth_restored
cp "$LOG" /tmp/poc-vm-last.log 2>/dev/null
[ "${KEEP:-0}" = 1 ] || "$POC/vm/down.sh" >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
