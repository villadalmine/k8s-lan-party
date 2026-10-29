#!/usr/bin/env bash
# Shared helpers for the PoC. Output is deterministic: only fixed PASS/FAIL lines (details go to poc/.state/poc.log).
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# EMESH_STATE: the state directory (CA keys, bundles, blocklist, lighthouse.env). A second game on the same machine needs its own,
# or the first `down.sh` of a PoC suite deletes its CA (docs/HOST-YOUR-OWN.md, "A second game").
STATE="${EMESH_STATE:-$POC/.state}"
CACHE="${EMESH_CACHE:-$POC/.cache}"   # pinned binaries (EMESH_CACHE: tests point it at stubs)
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
# The join token of a player: K10<ca-hash>::<id>.<secret> in its join.env. A bootstrap token is not one-time (it authenticates any number of
# machines until it expires or is deleted), so the issuing side deletes it once the node registered: `mesh host burn` (reap, players), revoke.sh.
token_id() { local t; t=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/$1/join.env" 2>/dev/null); t=${t##*::}; t=${t%%.*}; echo "$t"; }
token_burn() { # token_burn <player>: delete its join token on the control plane; 0 if it was there and is gone, 1 if there was none
  local id; id=$(token_id "$1"); [ -n "$id" ] || return 1
  cpx k3s token list 2>/dev/null | grep -Eq "(^|[^a-z0-9])$id([^a-z0-9]|$)" || return 1
  cpx k3s token delete "$id" >/dev/null 2>&1
}
live_bundles() { # name=nebula-ip of every bundle whose certificate is not on the blocklist (cp at CP_NEB; lh is no Node). A fingerprint that
  # cannot be read counts as live: the sweep then keeps that Node rather than delete a player's node by mistake.
  local b n fp ip
  for b in "$STATE"/bundles/*/; do
    n=$(basename "$b"); [ -f "$b/host.crt" ] && [ "$n" != lh ] || continue
    fp=$("$CACHE/nebula-cert" print -json -path "$b/host.crt" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])' 2>/dev/null) || fp=""
    [ -n "$fp" ] && grep -qx "$fp" "$STATE/blocklist" 2>/dev/null && continue
    if [ "$n" = cp ]; then ip=$CP_NEB; else ip=$(sed -n 's/^NODE_IP=//p' "$b/join.env" 2>/dev/null); fi
    echo "$n=$ip"
  done
}
# The life of an issued name that has no Node yet. k3s garbage-collects a node password that no Node took once it is 10 minutes old
# (MinOrphanSecretAge, checked every minute); after that anybody with a valid token of his own could register the name with his own password.
# So the name may be admitted only while (1) its join token works, and a join token lives at most TOKEN_TTL_MAX, 8 minutes, enforced here,
# and (2) its node password secret still exists. guard_names checks both and every reap rewrites the guard: an unused name leaves the guard
# when its token dies (at most 8 minutes plus one reap interval), before k3s may drop its password.
TOKEN_TTL_MAX=480
ttl_seconds() { # ttl_seconds <duration: 90s, 5m, 8m>: the seconds, or status 2 if it is not one, is under a minute or over TOKEN_TTL_MAX
  local v="$1" n u
  case "$v" in "" | *[!0-9smh]*) return 2 ;; esac
  n=${v%[smh]}; u=${v#"$n"}
  case "$n" in "" | *[!0-9]*) return 2 ;; esac
  case "$u" in m) n=$((n * 60)) ;; h) n=$((n * 3600)) ;; esac
  [ "$n" -ge 60 ] && [ "$n" -le "$TOKEN_TTL_MAX" ] || return 2
  echo "$n"
}
guard_names() { # name=nebula-ip of the names the node guard admits: a live bundle that has a Node already, or (no Node yet) whose join token
  # still works AND whose node password secret still exists (see above). Status 2 when the API does not answer.
  local nodes toks pws l n id
  nodes=$(k get nodes -o name 2>/dev/null) || return 2
  toks=$(cpx k3s token list 2>/dev/null) || return 2
  pws=$(k get secrets -n kube-system -o name 2>/dev/null) || return 2
  for l in $(live_bundles); do
    n=${l%%=*}
    if printf '%s\n' "$nodes" | grep -qx "node/$n"; then echo "$l"; continue; fi
    id=$(token_id "$n")
    if [ -n "$id" ] && printf '%s\n' "$toks" | grep -Eq "(^|[^a-z0-9])$id([^a-z0-9]|$)" &&
      printf '%s\n' "$pws" | grep -qx "secret/$n.node-password.k3s"; then echo "$l"; fi
  done
}
with_guard_lock() { # with_guard_lock <command...>: one writer of the node guard at a time (a hand invite or revoke against the reap timer)
  local l="$STATE/guard.lock" i=0 rc pid
  if [ -z "${EMESH_NO_FLOCK:-}" ] && command -v flock >/dev/null 2>&1; then (flock 9 && "$@") 9>"$l"; return; fi
  # no flock (macOS): a symlink as the lock. `ln -s` creates it atomically WITH its content, "<holder pid>:<epoch>", so a waiter never sees a
  # lock without its owner (a directory plus a pid file had that window, and the CI runner hit it). A lock is stale, and taken over, only when
  # it is over a minute old AND its holder is gone (killed): a killed command does not leave every later revoke failing, and a live holder
  # is never taken over, whatever `kill -0` says about it in its first minute.
  local me now held at
  until me="$BASHPID:$(date +%s)"; ln -s "$me" "$l.pid" 2>/dev/null; do
    held=$(readlink "$l.pid" 2>/dev/null || true); pid=${held%%:*}; at=${held#*:}; now=$(date +%s)
    case "$at" in "" | *[!0-9]*) at=$now ;; esac
    # the takeover itself is serialized (mkdir $l.takeover): of two waiters that both saw the same stale lock, the second finds a new owner
    # in the link, not the stale one, and leaves it alone
    if [ -n "$pid" ] && [ $((now - at)) -gt 60 ] && ! kill -0 "$pid" 2>/dev/null && mkdir "$l.takeover" 2>/dev/null; then
      if [ "$(readlink "$l.pid" 2>/dev/null)" = "$held" ]; then rm -f "$l.pid"; fi
      rmdir "$l.takeover"; continue
    fi
    i=$((i + 1)); [ "$i" -lt "${GUARD_LOCK_TRIES:-240}" ] || { echo "the node guard lock $l.pid is held by pid ${pid:-?}"; return 1; }
    sleep 0.5
  done
  "$@"; rc=$?; rm -f "$l.pid"; return "$rc"
}
node_guard_write() {
  local names l args=""
  names=$(guard_names) || return 2
  for l in $names; do args="$args --from-literal=$l"; done
  # shellcheck disable=SC2086
  k create configmap edge-mesh-nodes -n kube-system $args --dry-run=client -o yaml | k apply -f - >/dev/null &&
    k apply -f - <"$POC/node-guard.yaml" >/dev/null && date +%s >"$STATE/guard.refreshed"
}
guard_refresh_age() { # seconds since the node guard was last written (a large number if never)
  local t; t=$(cat "$STATE/guard.refreshed" 2>/dev/null || echo 0); echo $(( $(date +%s) - t ))
}
node_guard() { # write the guard's list (guard_names) and apply poc/node-guard.yaml: only those names, at those addresses, may register or
  # update a Node. Idempotent, under a lock; the list first, because the policy refuses every kubelet while the list is missing.
  with_guard_lock node_guard_write
}
stray_nodes() { # stray_nodes <revoked-nebula-ip>: "<node><TAB><why>" for every Node that must go (poc/nodes_sweep.py: keyed on the bundle
  # names the host issued, not on the addresses a kubelet reports). The API not answering is an error (status 2), never "nothing left".
  local json; json=$(k get nodes -o json 2>/dev/null) || return 2
  # shellcheck disable=SC2046
  printf '%s' "$json" | python3 "$POC/nodes_sweep.py" --gone-ip "$1" $(live_bundles) || return 2
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

# --- conformance of poc/node-trust-freeze.yaml (issue #13, review of PR #28): a node's trust is set by the issuing side only. Every joined machine
# also holds k3s's controller credential (k3scontroller.kubeconfig, user system:k3s-controller), whose ClusterRole may `patch nodes/status` on EVERY Node
# (and only read everything else it has). Standard tooling cannot drive that exact credential through the status subresource: kubectl reads before it
# patches (`get nodes/status`, which the role lacks). So the status path is exercised with a stand-in identity that holds a role like it plus the read,
# and the real credential is tried on the Node itself. The policy only tells the host (system:masters) from everybody else.
node_kc() { # node_kc <node> <kubectl args...>: kubectl on that joined machine with the controller credential k3s leaves there
  local n="$1"; shift
  dx "$n" sh -c 'if command -v kubectl >/dev/null; then K=kubectl; else K="k3s kubectl"; fi; exec $K --kubeconfig /var/lib/rancher/k3s/agent/k3scontroller.kubeconfig "$@"' sh "$@" 2>&1
}
trust_of() { k get node "$1" -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}'; }
freeze_probe_role() { # a stand-in for the controller's role on node status, plus the read kubectl needs; removed again by the caller
  k apply -f - >/dev/null <<'YAML'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: edge-mesh-freeze-probe}
rules:
- {apiGroups: [""], resources: [nodes, nodes/status], verbs: [get, patch]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: edge-mesh-freeze-probe}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: edge-mesh-freeze-probe}
subjects: [{apiGroup: rbac.authorization.k8s.io, kind: User, name: edge-mesh-freeze-probe}]
YAML
}
probe_can() { [ "$(k auth can-i patch nodes/status --as=edge-mesh-freeze-probe)" = yes ]; }
as_probe() { k --as=edge-mesh-freeze-probe "$@" 2>&1; }
trust_frozen() { # trust_frozen <joined-node> <its-level>
  local n="$1" lvl="$2" out rc msg='edge-mesh node trust freeze' args
  # 1. the real controller credential, on the joined machine: it is that identity, and it cannot change a trust label on a Node (RBAC or the policy)
  out=$(node_kc "$n" auth whoami); echo "$out"; echo "$out" | grep -q 'system:k3s-controller' || return 1
  for args in "label node $n node-restriction.kubernetes.io/trust=own --overwrite" "label node cp node-restriction.kubernetes.io/trust- "; do
    # shellcheck disable=SC2086
    out=$(node_kc "$n" $args); rc=$?; echo "[$args] rc=$rc: $out"; [ "$rc" -ne 0 ] || return 1
  done
  # 2. an identity that CAN patch node status (the controller's reach), through the subresource: the policy refuses a trust label change, own or
  # another node's, and lets a status-only patch through; the host (system:masters) is not refused by it
  freeze_probe_role || return 1; wait_for 30 probe_can || return 1
  local lab='{"metadata":{"labels":{"node-restriction.kubernetes.io/trust":"own"}}}' unl='{"metadata":{"labels":{"node-restriction.kubernetes.io/trust":null}}}'
  out=$(as_probe patch node "$n" --subresource=status --type=merge -p "$lab"); rc=$?; echo "[status: $n -> own] rc=$rc: $out"
  [ "$rc" -ne 0 ] && echo "$out" | grep -q "$msg" || return 1
  out=$(as_probe patch node cp --subresource=status --type=merge -p "$unl"); rc=$?; echo "[status: cp -> no trust label] rc=$rc: $out"
  [ "$rc" -ne 0 ] && echo "$out" | grep -q "$msg" || return 1
  out=$(as_probe patch node "$n" --type=merge -p "$lab"); rc=$?; echo "[node: $n -> own] rc=$rc: $out"; [ "$rc" -ne 0 ] || return 1
  out=$(as_probe patch node "$n" --subresource=status --type=merge -p '{"status":{"conditions":[{"type":"EdgeMeshProbe","status":"True"}]}}'); rc=$?
  echo "[status-only] rc=$rc: $out"; [ "$rc" -eq 0 ] || return 1
  [ "$(trust_of "$n")" = "$lvl" ] && [ "$(trust_of cp)" = own ] || return 1
  # 3. positive control: the host can change a label under the prefix (so the refusals are the policy's), and puts it back
  k label node "$n" node-restriction.kubernetes.io/trust-probe=x >/dev/null && k label node "$n" node-restriction.kubernetes.io/trust-probe- >/dev/null || return 1
  k delete clusterrolebinding/edge-mesh-freeze-probe clusterrole/edge-mesh-freeze-probe >/dev/null
}
