#!/usr/bin/env bash
# make poc-nat (issue #15): a player behind a symmetric NAT reaches a control plane behind ANOTHER symmetric NAT through a Nebula relay on the
# lighthouse, and joins. Needs a Linux host with Incus. A simulated internet: one public network with the lighthouse (a plain Ubuntu VM plays
# the public server, installed with lighthouse-vps.sh like a real one), and two private LANs behind two routers (Incus containers, nftables
# masquerade). The control plane lives behind router A, the player behind router B. In order:
#   1. port-preserving NAT on both sides (what most home routers do): hole punching gives a DIRECT tunnel with no relay configured; measured;
#   2. symmetric NAT (masquerade fully-random: a new source port per destination) on both sides: no direct tunnel, and no connection at all
#      without a relay (the negative test);
#   3. `lighthouse-vps.sh sign --relay`: the same player reaches the control plane through the relay; the relay really carries the bytes (the
#      lighthouse's counters grow by at least twice the payload) and does not bypass the firewall of the far end; measured;
#   4. a real player joins with `edge-mesh join` through the relay: node Ready, the API reaches its kubelet; `mesh host revoke` cuts it,
#      relay included.
# PASS/FAIL per step, identical across runs; the numbers go to .state/nat-measurements.txt and poc/results/nat-measurements.txt.
# The simulated internet does not route private addresses (the routers drop traffic to the other LAN and the lighthouse blackholes both LANs),
# as the real one does not. Change the addresses or bridge names with NAT_PUB, NAT_A, NAT_B (first three octets) and NET_PUB, NET_A, NET_B.
# KEEP=1 leaves everything up afterwards; `poc/poc-nat.sh down` removes it.
set -u
export RUNTIME=incus MAX_VMS=6 CNI=flannel
NAT_PUB="${NAT_PUB:-10.78.20}"; NAT_A="${NAT_A:-10.78.21}"; NAT_B="${NAT_B:-10.78.22}"
NET_PUB="${NET_PUB:-emnatpub}"; NET_A="${NET_A:-emnata}"; NET_B="${NET_B:-emnatb}"
export UNDERLAY_NET="$NAT_PUB"   # lib.sh: the lighthouse's underlay address is $NAT_PUB.2
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
RIMG="${ROUTER_IMAGE:-images:ubuntu/24.04}"
SERVER=emesh-vps; SERVER_IP=$NAT_PUB.2; RSH="incus exec $SERVER --"; OUT="$STATE/lh-out"; LHS="$POC/lighthouse-vps.sh"; MESH="$POC/mesh"
PAYLOAD_MIB=50

nat_down() {
  for n in $(incus list emesh- -c n -f csv 2>/dev/null); do incus delete -f "$n" >/dev/null 2>&1; done
  for net in "$NET_A" "$NET_B" "$NET_PUB"; do incus network delete "$net" >/dev/null 2>&1; done
  rm -rf "$STATE"
}
if [ "${1:-}" = down ]; then nat_down; echo "poc-nat removed"; exit 0; fi
nat_down
mkdir -p "$STATE"; MEAS="$STATE/nat-measurements.txt"; : >"$MEAS"
LOG="$CACHE/poc-nat.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down; .state does not)

# --- the simulated internet ---------------------------------------------------------------------------------------------------------------
lan_net() { # lan_net <bridge> <prefix>: a private LAN; DHCP hands out its router (.254) as the gateway, the host does no NAT for it
  incus network create "$1" ipv4.address="$2.1/24" ipv4.nat=false ipv6.address=none \
    ipv4.dhcp.gateway="$2.254" ipv4.dhcp.ranges="$2.100-$2.199" >/dev/null
}
nat_rules() { # nat_rules <router> <lan prefix> <easy|symmetric>: replace the router's own nftables table and forget every mapping.
  # wan_in drops unsolicited packets to the router itself, as a home router does. Without it (found on the first run) the far end's first
  # handshake, arriving before our own punch, became a conntrack entry of the router that held the public port, so our punch went out on
  # another port and hole punching failed even with port-preserving NAT. A dropped packet's conntrack entry is never confirmed.
  local flag=""; [ "$3" = symmetric ] && flag=fully-random
  incus exec "$1" -- sh -c "nft delete table ip emnat 2>/dev/null; nft -f - <<EOF || exit 1
table ip emnat {
  chain snat_out { type nat hook postrouting priority srcnat; policy accept; oifname \"eth0\" ip saddr $2.0/24 masquerade $flag; }
  chain no_lan { type filter hook forward priority filter; policy accept; iifname \"eth1\" ip daddr { $NAT_A.0/24, $NAT_B.0/24 } drop; }
  chain wan_in { type filter hook input priority filter; policy accept; iifname \"eth0\" ct state new drop; }
}
EOF
conntrack -F >/dev/null 2>&1; true" || return 1
  # what runs is what was asked: the masquerade rule, fully-random or not
  local rule; rule=$(incus exec "$1" -- nft list chain ip emnat snat_out | grep masquerade) || return 1
  if [ -n "$flag" ]; then echo "$rule" | grep -q fully-random; else ! echo "$rule" | grep -q random; fi
}
router_up() { # router_up <name> <lan bridge> <lan prefix> <public address>
  incus init "$RIMG" "$1" --network "$NET_PUB" -c limits.cpu=1 -c limits.memory=512MiB >/dev/null &&
    incus config device set "$1" eth0 ipv4.address="$4" >/dev/null &&
    incus config device add "$1" eth1 nic network="$2" name=eth1 >/dev/null && incus start "$1" || return 1
  wait_for 120 incus exec "$1" -- sh -c "ip -4 route | grep -q '^default'" || return 1
  incus exec "$1" -- sh -c "ip addr add $3.254/24 dev eth1; ip link set eth1 up; sysctl -qw net.ipv4.ip_forward=1
    for i in 1 2 3 4 5; do apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables conntrack >/dev/null && break; sleep 5; done
    command -v nft && command -v conntrack" || return 1
  nat_rules "$1" "$3" easy
}
internet_up() {
  incus network create "$NET_PUB" ipv4.address="$NAT_PUB.1/24" ipv4.nat=true ipv6.address=none >/dev/null || return 1
  lan_net "$NET_A" "$NAT_A" && lan_net "$NET_B" "$NAT_B" || return 1
  router_up emesh-nat-a "$NET_A" "$NAT_A" "$NAT_PUB.11" && router_up emesh-nat-b "$NET_B" "$NAT_B" "$NAT_PUB.12"
}
nat_mode() { nat_rules emesh-nat-a "$NAT_A" "$1" && nat_rules emesh-nat-b "$NAT_B" "$1"; }
server_up() { # a plain Ubuntu VM with a public address: no nebula, no k3s, nothing of ours (as in poc-lighthouse.sh)
  incus init "$IMG" "$SERVER" --vm -c limits.cpu=1 -c limits.memory=1GiB -d root,size=12GiB --network "$NET_PUB" >/dev/null || return 1
  incus config device set "$SERVER" eth0 ipv4.address="$SERVER_IP" >/dev/null; incus start "$SERVER" || return 1
  wait_for 240 incus exec "$SERVER" -- true || return 1
  wait_for 120 incus exec "$SERVER" -- ping -c1 -W1 "$NAT_PUB.1" || return 1
  incus exec "$SERVER" -- date -s "@$(date +%s)" >/dev/null
  # the internet does not route private addresses: nothing on the public side may reach a LAN except through its router
  incus exec "$SERVER" -- sh -c "ip route replace blackhole $NAT_A.0/24; ip route replace blackhole $NAT_B.0/24
    cloud-init status --wait >/dev/null 2>&1 || true; command -v curl >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null; }"
}
player_vm() { UNDERLAY_NET=$NAT_B VMNET=$NET_B vm_up alice 2 3GiB "$NAT_B.4" && incus exec emesh-alice -- sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iperf3 >/dev/null'; }
cp_up() { UNDERLAY_NET=$NAT_A VMNET=$NET_A HOST_ONLY=1 "$POC/vm/up.sh" && incus exec emesh-cp -- sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iperf3 >/dev/null'; }

# --- what the NAT does to a flow: one source port, two destinations on the public server ---------------------------------------------------
mapped_ports() { # mapped_ports <vm>: the source ports the public server sees for two datagrams sent from port 5555 to its ports 7001 and 7002
  incus exec "$SERVER" -- sh -c 'rm -f /tmp/ports; nohup python3 -c "
import socket
s = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
for x, p in zip(s, (7001, 7002)): x.bind((\"0.0.0.0\", p)); x.settimeout(15)
print(*[x.recvfrom(64)[1][1] for x in s])" >/tmp/ports 2>&1 &' || return 1
  sleep 1
  incus exec "$1" -- python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('0.0.0.0', 5555))
for p in (7001, 7002): s.sendto(b'x', ('$SERVER_IP', p))" || return 1
  local i; for i in $(seq 15); do incus exec "$SERVER" -- grep -Eq '^[0-9]+ [0-9]+$' /tmp/ports 2>/dev/null && break; sleep 1; done
  incus exec "$SERVER" -- cat /tmp/ports
}
same_mapping() { local p; p=$(mapped_ports "$1") || return 1; echo "$1: $p"; set -- $p; [ "$#" = 2 ] && [ "$1" = "$2" ]; }
new_port_per_destination() { local p; p=$(mapped_ports "$1") || return 1; echo "$1: $p"; set -- $p; [ "$#" = 2 ] && [ "$1" != "$2" ]; }

# --- Nebula between the player and the control plane --------------------------------------------------------------------------------------
probe_reaches_cp() { incus exec emesh-alice -- ping -c1 -W2 "$CP_NEB"; }
probe_on() { # the player VM runs Nebula with the bundle "probe" (no k3s yet): bare Nebula for the path checks and the measurements
  push_bundle probe alice && start_nebula alice && wait_for 30 nebula_has_ip probe && wait_for 30 incus exec emesh-alice -- ping -c1 -W2 "$LH_NEB"; }
probe_restart() { nebula_restart probe && wait_for 30 incus exec emesh-alice -- ping -c1 -W2 "$LH_NEB"; }
never_reaches() { # never_reaches <seconds>: the player does not reach the control plane once in that time
  local i; for i in $(seq "$1"); do incus exec emesh-alice -- ping -c1 -W1 "$CP_NEB" >/dev/null 2>&1 && { echo "reached the control plane after $i s"; return 1; }; sleep 1; done; }
lh_bytes() { incus exec "$SERVER" -- sh -c 'echo $(( $(cat /sys/class/net/enp5s0/statistics/rx_bytes) + $(cat /sys/class/net/enp5s0/statistics/tx_bytes) ))'; }
lh_cpu() { # CPU ticks (1/100 s) the lighthouse's nebula has used: user + system
  incus exec "$SERVER" -- sh -c 'p=$(systemctl show -p MainPID --value nebula); cut -d" " -f14,15 /proc/$p/stat | tr " " +' | python3 -c 'import sys; print(sum(int(x) for x in sys.stdin.read().strip().split("+") if x))'; }
measure() { # measure <label> <min-ratio|max-ratio> <percent>: ping and a 50 MiB iperf3 each way between cp and the player over Nebula (port 10250:
  # the narrowed firewall admits it from the control plane only, which is where iperf3 connects from); the lighthouse's bytes in + out during
  # the transfers, against the payload: at least <percent> % of it (it went through the relay) or at most (it did not)
  local label="$1" kind="$2" pct="$3" b0 b1 lhb pay rtt fwd rev c0 c1
  incus exec emesh-alice -- sh -c 'kill $(pidof iperf3) 2>/dev/null; nohup iperf3 -s -p 10250 >/dev/null 2>&1 &'
  wait_for 10 incus exec emesh-cp -- sh -c "nc -z -w2 $N_PROBE 10250" || return 1
  rtt=$(incus exec emesh-cp -- ping -c 20 -i 0.2 -q "$N_PROBE" | sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p')
  b0=$(lh_bytes); c0=$(lh_cpu)
  fwd=$(incus exec emesh-cp -- iperf3 -c "$N_PROBE" -p 10250 -n ${PAYLOAD_MIB}M -J | python3 -c 'import sys,json;print(round(json.load(sys.stdin)["end"]["sum_received"]["bits_per_second"]/1e6,1))') || return 1
  rev=$(incus exec emesh-cp -- iperf3 -c "$N_PROBE" -p 10250 -n ${PAYLOAD_MIB}M -R -J | python3 -c 'import sys,json;print(round(json.load(sys.stdin)["end"]["sum_received"]["bits_per_second"]/1e6,1))') || return 1
  b1=$(lh_bytes); c1=$(lh_cpu); lhb=$((b1 - b0)); pay=$((2 * PAYLOAD_MIB * 1048576))
  incus exec emesh-alice -- sh -c 'kill $(pidof iperf3) 2>/dev/null; true'
  echo "$label: Nebula ping cp -> player avg ${rtt:-?} ms; iperf3 ${PAYLOAD_MIB} MiB cp -> player ${fwd} Mbit/s, player -> cp ${rev} Mbit/s; lighthouse eth0 in+out during the two transfers: $lhb bytes for $pay bytes of payload ($((lhb * 100 / pay)) %); lighthouse nebula CPU $(( (c1 - c0) * 10 )) ms" | tee -a "$MEAS"
  if [ "$kind" = min ]; then [ "$((lhb * 100 / pay))" -ge "$pct" ]; else [ "$((lhb * 100 / pay))" -le "$pct" ]; fi
}
firewall_holds() { # through the relay, the far end's Nebula firewall still decides: 6443 answers, an unlisted port (9999) does not
  incus exec emesh-cp -- sh -c 'kill $(pidof nc) 2>/dev/null; nohup nc -lk -p 9999 >/dev/null 2>&1 &'
  wait_for 10 incus exec emesh-cp -- nc -z -w1 127.0.0.1 9999 || return 1
  incus exec emesh-alice -- nc -z -w3 "$CP_NEB" 6443 || { incus exec emesh-cp -- sh -c 'kill $(pidof nc)'; return 1; }
  local r=0; incus exec emesh-alice -- nc -z -w3 "$CP_NEB" 9999 && r=1
  incus exec emesh-cp -- sh -c 'kill $(pidof nc) 2>/dev/null; true'; [ "$r" = 0 ]
}
relay_on() { "$LHS" sign "$SERVER_IP" "$OUT/lh.pub" --relay | grep -q 'changed: relay on' && "$LHS" deploy --ssh "$RSH" | grep -q 'restarted'; }
relay_verified() { "$LHS" verify --ssh "$RSH" | grep -q '^ok:   relay on'; }
unchanged() { local o; o=$("$@" 2>&1) || return 1; echo "$o"; ! echo "$o" | grep -q '^changed:'; }
relay_idempotent() { relay_verified && unchanged "$LHS" sign "$SERVER_IP" "$OUT/lh.pub" --relay && unchanged "$LHS" deploy --ssh "$RSH"; }
refresh_cp() { "$MESH" host refresh cp | grep -q 'nebula restarted' && wait_for 180 k get --raw /readyz && grep -q "relays: \[\"$LH_NEB\"\]" "$STATE/bundles/cp/config.yml"; }
refresh_probe() { "$MESH" host refresh probe | grep -q "nebula restarted" && wait_for 60 probe_reaches_cp; }
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
alice_joins() { # the bare probe stops; alice gets a fresh invite (an 8-minute token) and joins on the same machine with the one command
  incus exec emesh-alice -- sh -c 'kill $(pidof nebula) 2>/dev/null; rm -rf /etc/nebula /bundle/*; true'; rm -f "$STATE/alias/probe"
  "$MESH" host invite alice --trust public >/dev/null && push_bundle alice && incus exec emesh-alice -- edge-mesh join /bundle --accept-golden-rule
}
kubelet_through_relay() { [ "$(k get --raw /api/v1/nodes/alice/proxy/healthz 2>/dev/null)" = ok ]; }
alice_cut() { # revoked: the blocklist reaches the lighthouse (it relays no more for alice) and the control plane
  "$MESH" host revoke alice >/dev/null || return 1
  local i; for i in $(seq 30); do incus exec emesh-alice -- ping -c1 -W1 "$CP_NEB" >/dev/null 2>&1 || break; sleep 1; done
  sleep 5; ! incus exec emesh-alice -- ping -c3 -W2 "$CP_NEB" >/dev/null 2>&1 && ! k get node alice >/dev/null 2>&1 || return 1
  # the relay half, isolated: the lighthouse itself holds her fingerprint (the control plane's blocklist alone would cut her too), and she does
  # not reach the lighthouse any more, so there is no relay for her to use
  local fp; fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])') || return 1
  incus exec "$SERVER" -- grep -q "$fp" /etc/nebula/config.yml || return 1
  ! incus exec emesh-alice -- ping -c3 -W2 "$LH_NEB" >/dev/null 2>&1
}
revoke_fails_when_sync_fails() { # the host cannot reach the lighthouse: the revocation of the relay is not done, and the command says so (exit 5)
  local env="$STATE/lighthouse.env" keep rc out; keep=$(cat "$env"); out=$(sed -i 's/^LH_RSH=.*/LH_RSH=false/' "$env" && "$MESH" host revoke alice 2>&1); rc=$?
  printf '%s
' "$keep" >"$env"; echo "$out"; [ "$rc" = 5 ] && echo "$out" | grep -q 'external lighthouse'
}
N_PROBE=""   # the probe's Nebula address, read from its bundle once it is issued
probe_invited() { "$MESH" host invite probe --trust public >/dev/null && N_PROBE=$(sed -n 's/^NODE_IP=//p' "$STATE/bundles/probe/join.env") && [ -n "$N_PROBE" ] &&
  mkdir -p "$STATE/alias" && echo alice >"$STATE/alias/probe"; }

step N01 "the simulated internet: a public network, two private LANs, two routers (nftables masquerade)" internet_up
step N02 "a plain Ubuntu VM on the public network plays the lighthouse's server" server_up
step N03 "lighthouse-vps.sh install, no relay: nebula on the server, its key made there" "$LHS" install "$SERVER_IP" --ssh "$RSH" --out "$OUT"
step N04 "mesh host init: the control plane behind router A reaches the lighthouse" cp_up
step N05 "the player's machine behind router B" player_vm
step N06 "port-preserving NAT: router A keeps one source port for two destinations" same_mapping emesh-cp
step N07 "port-preserving NAT: router B keeps one source port for two destinations" same_mapping emesh-alice
probe_up() { probe_invited && probe_on; }
step N08 "a player bundle (probe, trust public, no k3s yet); its Nebula reaches the lighthouse from behind router B" probe_up
step N09 "port-preserving NAT on both sides, no relay configured: hole punching gives a direct tunnel to the control plane" wait_for 30 probe_reaches_cp
step N10 "direct path measured: the lighthouse carried under 5 % of the payload" measure "direct (port-preserving NAT on both sides)" max 5
step N11 "both routers switched to symmetric NAT (fully-random), every mapping forgotten" nat_mode symmetric
step N12 "symmetric NAT: router A gives a new source port per destination" new_port_per_destination emesh-cp
step N13 "symmetric NAT: router B gives a new source port per destination" new_port_per_destination emesh-alice
step N14 "the player's Nebula restarted behind symmetric NAT still reaches the lighthouse" probe_restart
step N15 "NEGATIVE: symmetric NAT on both sides and no relay: the player never reaches the control plane (40 s)" never_reaches 40
step N16 "lighthouse-vps.sh sign --relay, deploy: the lighthouse restarts as a relay too" relay_on
step N17 "verify says the relay is on; sign --relay and deploy again change nothing" relay_idempotent
step N18 "mesh host refresh cp: the control plane names the relay, Nebula restarted, the API back" refresh_cp
step N19 "mesh host refresh probe: the player names the relay and reaches the control plane through it" refresh_probe
step N20 "the relay carries the traffic: the lighthouse moved at least twice the payload (in and out); measured" measure "relay (symmetric NAT on both sides)" min 190
step N21 "through the relay the control plane's firewall still decides: 6443 answers, port 9999 does not" firewall_holds
step N22 "a player (alice, trust public) joins with edge-mesh join from behind symmetric NAT, through the relay" alice_joins
step N23 "alice's node becomes Ready" wait_for 240 ready alice
step N24 "the API reaches alice's kubelet (logs, exec) through the relay" wait_for 60 kubelet_through_relay
step N25 "mesh host revoke alice: the lighthouse holds her fingerprint and she cannot reach it (no relay for her), she cannot reach the control plane, her Node is gone" alice_cut
step N26 "NEGATIVE: when the sync to the lighthouse fails, revoke ends with exit 5 and says the lighthouse still lacks the blocklist" revoke_fails_when_sync_fails

if [ "$FAILS" -eq 0 ]; then mkdir -p "$POC/results"; cp "$MEAS" "$POC/results/nat-measurements.txt"; fi
if [ "${KEEP:-0}" != 1 ]; then nat_down; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
