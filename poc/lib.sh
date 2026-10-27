#!/usr/bin/env bash
# Shared helpers for the PoC. Output is deterministic: only fixed PASS/FAIL lines (details go to poc/.state/poc.log).
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# EMESH_STATE: the state directory (CA keys, bundles, blocklist, lighthouse.env). A second game on the same machine needs its own,
# or the first `down.sh` of a PoC suite deletes its CA (docs/HOST-YOUR-OWN.md, "A second game").
STATE="${EMESH_STATE:-$POC/.state}"
CACHE="$POC/.cache"
LOG="$STATE/poc.log"
# shellcheck disable=SC1091
. "$POC/stack.env"
mkdir -p "$STATE" "$CACHE"
FAILS=0

log() { echo "[$(date +%H:%M:%S)] $*" >>"$LOG"; }
step() { # step <id> <description> <command...>: PASS when the command succeeds
  local id="$1" desc="$2"; shift 2
  log "STEP $id: $desc"
  if "$@" >>"$LOG" 2>&1; then printf 'PASS %-6s %s\n' "$id" "$desc"; else printf 'FAIL %-6s %s\n' "$id" "$desc"; FAILS=$((FAILS+1)); fi
}
# RUNTIME=docker (default: containers) | incus (the VM flow, poc/vm/): where the cluster nodes live
RUNTIME="${RUNTIME:-docker}"
if [ "$RUNTIME" = incus ]; then LH_DOCKER_IP=$UNDERLAY_NET.2; UNDERLAY_IF=enp5s0; CP_UNDERLAY=$UNDERLAY_NET.3; UP="$POC/vm/up.sh"
else UNDERLAY_IF=eth0; CP_UNDERLAY=172.31.0.3; UP="$POC/up.sh"; fi
# LH_EXTERNAL=<host>: the lighthouse is a separate public machine (poc/lighthouse-vps.sh install), not one of the PoC machines; every
# bundle points at it. `lighthouse-vps.sh install` remembers the choice in .state/lighthouse.env, so later commands need no variable.
[ -f "$STATE/lighthouse.env" ] && . "$STATE/lighthouse.env"
[ -z "${LH_EXTERNAL:-}" ] || LH_DOCKER_IP="$LH_EXTERNAL"
vmname() { local a="$STATE/alias/$1"; if [ -f "$a" ]; then cat "$a"; else echo "$1"; fi; }   # a spare VM can play another player
dx() { local c="$1"; shift; if [ "$RUNTIME" = incus ]; then incus exec "emesh-$(vmname "$c")" -- "$@"; else docker exec "$c" "$@"; fi; }
nebula_reload() { # nebula_reload <host>: apply the (blocklist) config the issuing side just changed
  if [ "$RUNTIME" = incus ]; then
    incus file push -q "$STATE/bundles/$1/config.yml" "emesh-$(vmname "$1")/etc/nebula/config.yml" && dx "$1" sh -c 'kill -HUP $(pidof nebula)'
  else docker exec "$1" sh -c 'cp /bundle/config.yml /etc/nebula/config.yml; kill -HUP $(pidof nebula)'; fi
}
# The control plane's k3s server: stopped before Nebula restarts (when nebula1 loses its address the embedded flannel exits and a bare
# nohup server stays down), started again once nebula1 has its address. systemd unit when PERSIST, else /etc/emesh/k3s-server.sh (up.sh, vm/lib-vm.sh).
K3S_PIDS='pids=$(pgrep -f "[k]3s server" 2>/dev/null || pidof k3s 2>/dev/null)'
cp_k3s_managed() { dx cp sh -c 'systemctl is-enabled -q k3s 2>/dev/null || test -f /etc/emesh/k3s-server.sh'; }
cp_k3s_stop() {
  dx cp sh -c "if systemctl is-enabled -q k3s 2>/dev/null; then systemctl stop k3s; else $K3S_PIDS; [ -z \"\$pids\" ] || kill \$pids
    for i in \$(seq 30); do $K3S_PIDS; [ -z \"\$pids\" ] && break; sleep 1; done; fi"
}
cp_k3s_start() {
  if [ "$RUNTIME" = incus ]; then
    dx cp sh -c 'if systemctl is-enabled -q k3s 2>/dev/null; then systemctl start k3s; else nohup sh /etc/emesh/k3s-server.sh >/dev/null 2>&1 & fi'
  else docker exec -d cp sh /etc/emesh/k3s-server.sh; fi
}
nebula_has_ip() { dx "$1" sh -c 'ip -4 addr show dev nebula1 2>/dev/null | grep -q inet'; }
nebula_restart() { # nebula_restart <host>: push the bundle's config and restart nebula (systemd unit when PERSIST, else the nohup process)
  local cpk=0; [ "$1" = cp ] && cp_k3s_managed && cpk=1
  [ "$cpk" = 1 ] && cp_k3s_stop
  if [ "$RUNTIME" = incus ]; then
    incus file push -q "$STATE/bundles/$1/config.yml" "emesh-$(vmname "$1")/etc/nebula/config.yml"
    dx "$1" sh -c 'if systemctl is-enabled -q nebula 2>/dev/null; then systemctl restart nebula; else kill $(pidof nebula); sleep 1; nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 & fi'
  else docker exec "$1" sh -c 'cp /bundle/config.yml /etc/nebula/config.yml; kill $(pidof nebula); sleep 1; nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &'; fi
  wait_for 30 nebula_has_ip "$1" || { echo "nebula1 on $1 has no address 30 s after the restart: see /var/log/nebula.log there"; return 1; }
  if [ "$cpk" = 1 ]; then cp_k3s_start; wait_for 180 k get --raw /readyz || { echo "the API of cp did not answer /readyz within 180 s"; return 1; }; fi
  if [ "$1" = cp ] && [ "$cpk" = 0 ]; then echo "note: this control plane predates /etc/emesh/k3s-server.sh: if its API stays down, start k3s by hand"; fi
  return 0
}
machine_rm() { # a player machine goes away
  if [ "$RUNTIME" = incus ]; then
    if [ -f "$STATE/alias/$1" ]; then rm -f "$STATE/alias/$1"; else incus delete -f "emesh-$1" >/dev/null 2>&1 || true; fi
  else docker rm -f "$1" >/dev/null 2>&1 || true; fi
}
machine_running() { # machine_running <name>
  if [ "$RUNTIME" = incus ]; then [ -f "$STATE/alias/$1" ] || incus info "emesh-$1" >/dev/null 2>&1
  else [ -n "$(docker ps -q -f "name=^$1$")" ]; fi
}
any_machines() { if [ "$RUNTIME" = incus ]; then [ -n "$(incus list emesh- -c n -f csv)" ]; else [ -n "$(docker ps -aq -f 'name=^(lh|cp|n1|n2|alice)$')" ]; fi; }
cpx() { if [ "$RUNTIME" = incus ]; then incus exec emesh-cp -- "$@"; else docker exec -i cp "$@"; fi; }
k() { if [ "$RUNTIME" = incus ]; then incus exec emesh-cp -- k3s kubectl "$@"; else docker exec -i cp kubectl "$@"; fi; }
helm_cp() { # helm inside cp's network namespace: the API is reachable only on the Nebula IP
  if [ "$RUNTIME" = incus ]; then incus exec emesh-cp -- env KUBECONFIG=/root/kubeconfig helm "$@"
  else docker run --rm --network container:cp -v "$STATE/kubeconfig:/kube:ro" -e KUBECONFIG=/kube "$HELM_IMAGE" "$@"; fi
}
wait_for() { # wait_for <seconds> <command...>
  local n="$1"; shift; local i=0
  until "$@" >/dev/null 2>&1; do i=$((i+1)); [ "$i" -ge "$n" ] && return 1; sleep 1; done
}

new_container() { # new_container <name> <docker-ip>
  if [ "$RUNTIME" = incus ]; then . "$POC/vm/lib-vm.sh"; vm_new "$1"; return; fi
  docker rm -f "$1" >/dev/null 2>&1
  docker run -d --name "$1" --privileged --network "$DOCKER_NET" --ip "$2" --tmpfs /run --tmpfs /var/run \
    -v "$CACHE:/opt/nebula:ro" -v "$POC/edge-mesh:/usr/local/bin/edge-mesh:ro" -v "$STATE/bundles/$1:/bundle" \
    --entrypoint sleep "$K3S_IMAGE" infinity >/dev/null
  docker exec "$1" sh -c 'mkdir -p /sys/fs/cgroup/init && xargs -rn1 </sys/fs/cgroup/cgroup.procs >/sys/fs/cgroup/init/cgroup.procs; sed -e "s/ / +/g" -e "s/^/+/" </sys/fs/cgroup/cgroup.controllers >/sys/fs/cgroup/cgroup.subtree_control'
}

# Nebula firewall (narrowed): a TCP port outside the allow-list is refused between hosts, an allowed one is not.
# n2 listens on 9999 (checked on n2 itself, which bypasses the firewall); n1 must not reach it over Nebula. Positive control:
# n1 reaches the Kubernetes API (6443) on cp over Nebula, and n1 can ping n2 (ICMP).
# `nc` comes from the netshoot image in the node's network namespace (Docker: the k3s image has none) or from the VM itself.
nsx() { local n="$1"; shift; if [ "$RUNTIME" = incus ]; then incus exec "emesh-$(vmname "$n")" -- "$@"
  else docker run --rm --network "container:$n" "$NETSHOOT_IMAGE" "$@"; fi; }
fw_listen() { if [ "$RUNTIME" = incus ]; then dx n2 sh -c 'kill $(pidof nc) 2>/dev/null; nohup nc -l -p 9999 >/dev/null 2>&1 &'
  else docker rm -f fwl >/dev/null 2>&1; docker run -d --name fwl --network container:n2 "$NETSHOOT_IMAGE" nc -l -p 9999 >/dev/null; fi; }
fw_unlisten() { if [ "$RUNTIME" = incus ]; then dx n2 sh -c 'kill $(pidof nc) 2>/dev/null; true'; else docker rm -f fwl >/dev/null 2>&1; fi; }
fw_blocks() {
  fw_listen
  wait_for 10 nsx n2 nc -z -w1 127.0.0.1 9999 || { fw_unlisten; return 1; }
  nsx n1 nc -z -w2 "$CP_NEB" 6443 || { fw_unlisten; return 1; }
  dx n1 ping -c1 -W2 "$N2_NEB" >/dev/null || { fw_unlisten; return 1; }
  local r=0; nsx n1 nc -z -w3 "$N2_NEB" 9999 && r=1
  fw_unlisten
  [ "$r" = 0 ]
}
