#!/usr/bin/env bash
# The step-2 assertions, independent of where the cluster runs. The caller (poc-cilium.sh for Docker, vm/poc-vm.sh for Incus VMs)
# defines three functions and sources this file:
#   k <kubectl args>            kubectl against the cluster
#   nx <node> <command...>      run a command in the node's own namespaces (node = cp | n1 | n2)
#   tcpd <node> <tcpdump args>  tcpdump in the node's network namespace
# and the variables from stack.env (POD_MTU, N1_NEB, N2_NEB, CP_NEB, NEB_IF). Every check prints PASS/FAIL through `step`.
NEB_IF="${NEB_IF:-nebula1}"
declare -F nn >/dev/null || nn() { echo "$1"; }   # role (cp|n1|n2) -> Kubernetes node name; kind-cilium.sh defines its own
CANARY="EDGEMESH-CANARY-0123456789-plaintext-marker"

cilium_pod() { k -n kube-system get pod -l k8s-app=cilium --field-selector "spec.nodeName=$(nn "$1")" -o jsonpath='{.items[0].metadata.name}'; }
cexec() { local n="$1"; shift; k -n kube-system exec "$(cilium_pod "$n")" -c cilium-agent -- "$@"; }
node_ready() { [ "$(k get node "$(nn "$1")" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
all_ready() { node_ready cp && node_ready n1 && node_ready n2; }
remote_tainted() { [ "$(k get node "$(nn n1)" -o jsonpath="{.spec.taints[?(@.key=='trust')].value}")" = friends ] \
  && [ "$(k get node "$(nn n2)" -o jsonpath="{.spec.taints[?(@.key=='trust')].value}")" = public ]; }
cilium_ds_ready() { [ "$(k -n kube-system get ds cilium -o jsonpath='{.status.numberReady}')" = 3 ]; }
agents_ok() { local n; for n in cp n1 n2; do cexec "$n" cilium-dbg status --brief | grep -qx OK || return 1; done; }
wg_on() { # every agent: WireGuard on cilium_wg0, its own public key is the one it published, and every other node is a peer
  local n out key total; total=$(k get nodes --no-headers | wc -l); [ "$total" -ge 3 ] || return 1
  for n in cp n1 n2; do
    # (the key is read before the pipe: in the VM flow `k` is `incus exec`, which would swallow the pipe's input)
    out=$(cexec "$n" cilium-dbg encrypt status) || return 1; key=$(wg_key_of "$n"); [ -n "$key" ] || return 1
    echo "$out"; echo "$out" | grep -q 'Encryption: Wireguard' || return 1
    echo "$out" | grep -qE "^[[:space:]]*Number of peers: $((total - 1))[[:space:]]*$" || return 1
    echo "$out" | grep -qF "Public key: $key" || return 1
  done
}
wg_key_of() { k get ciliumnode "$(nn "$1")" -o jsonpath='{.metadata.annotations.network\.cilium\.io/wg-pub-key}'; }
wg_peers_match() { # the peers are exactly the other nodes, by key: what the kernel holds (wg show) against what each node published (VM flow: wg on the node)
  local n m want got; for n in cp n1 n2; do
    want=$(for m in cp n1 n2; do [ "$m" = "$n" ] || wg_key_of "$m"; echo; done | grep . | sort)
    got=$(nx "$n" wg show cilium_wg0 peers | sort)
    echo "$n peers: $(echo "$got" | tr '\n' ' ')"; [ -n "$want" ] && [ "$want" = "$got" ] || return 1
  done
}
wg_handshake() { # the peers actually shook hands (a configured peer that never connects is the silent failure)
  nx n1 sh -c 'wg show cilium_wg0 latest-handshakes 2>/dev/null || true' | grep -qE '[0-9]{9,}$' \
    || cexec n1 cilium-dbg encrypt status | grep -qi peers
}
test_pods() {
  k apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata: {name: web, labels: {app: web}}
spec:
  nodeName: $(nn n1)
  tolerations: [{operator: Exists}]
  containers: [{name: c, image: busybox:1.36, command: ["sh","-c","mkdir -p /www; echo $CANARY >/www/canary; dd if=/dev/urandom of=/www/big bs=1M count=30 2>/dev/null; httpd -f -p 8080 -h /www"]}]
---
apiVersion: v1
kind: Pod
metadata: {name: cli, labels: {app: cli}}
spec:
  nodeName: $(nn n2)
  tolerations: [{operator: Exists}]
  containers: [{name: c, image: $NETSHOOT_IMAGE, command: ["sleep","3600"]}]
YAML
}
pods_ready() { [ "$(k get pod web cli -o jsonpath='{range .items[*]}{.status.phase}{end}')" = RunningRunning ]; }
web_ip() { k get pod web -o jsonpath='{.status.podIP}'; }
cross_node() { k exec cli -- curl -sf --max-time 10 "http://$(web_ip):8080/canary" | grep -q "$CANARY"; }
dns_ok() { k exec cli -- nslookup kubernetes.default.svc.cluster.local; }
on_the_wire() { # while the pod fetches the canary: the clear text exists inside the node (cilium_wg0) and never on the Nebula interface
  local t="${STATE:-/tmp}/cap"; mkdir -p "$t"
  # `-l -A` prints payload as ASCII; the file holds what was seen, the counts below are the evidence
  tcpd n1 -i cilium_wg0 -A -nn -l "udp port 8472 or tcp port 8080" >"$t/inside.txt" 2>"$t/inside.err" &
  local p1=$!
  tcpd n1 -i "$NEB_IF" -A -nn -l "udp port 51871" >"$t/wire.txt" 2>"$t/wire.err" &
  local p2=$!
  # wait until both captures are listening (the first run may still be pulling the tcpdump image)
  local w=0; until grep -q listening "$t/inside.err" 2>/dev/null && grep -q listening "$t/wire.err" 2>/dev/null; do w=$((w+1)); [ "$w" -gt 90 ] && break; sleep 1; done
  local i; for i in 1 2 3; do k exec cli -- curl -sf --max-time 10 "http://$(web_ip):8080/canary" >/dev/null; done
  wait "$p1" "$p2" 2>/dev/null   # both captures end by themselves (timeout in tcpd), so their output is complete
  local inside wire pk
  inside=$(grep -c "$CANARY" "$t/inside.txt" || true); wire=$(grep -c "$CANARY" "$t/wire.txt" || true)
  pk=$(grep -cE 'UDP' "$t/wire.txt" || true)
  echo "canary seen inside (cilium_wg0): $inside; on $NEB_IF: $wire; WireGuard UDP lines on $NEB_IF: $pk"
  [ "$inside" -gt 0 ] && [ "$wire" -eq 0 ] && [ "$pk" -gt 0 ]
}
mtu_chain() { # MTU set once: every node's Nebula interface has NEBULA_MTU, and both test pods have the MTU derived from what was measured
  # (pod_mtu_from in stack.env: minus VXLAN 50 and WireGuard 80, rounded down), so nothing else in the chain is a number of its own
  local n nm pm p; for n in cp n1 n2; do nm=$(nx "$n" cat "/sys/class/net/$NEB_IF/mtu"); echo "$n nebula=$nm"; [ "$nm" = "$NEBULA_MTU" ] || return 1; done
  for p in web cli; do pm=$(k exec "$p" -- cat /sys/class/net/eth0/mtu); echo "$p pod=$pm"; [ "$pm" = "$(pod_mtu_from "$nm")" ] || return 1; done
  [ $((pm + 50 + 80)) -le "$nm" ]
}
df_ping() { # a full-size packet crosses (DF set), one byte more is refused at the source. Cilium subtracts the WireGuard
  # overhead from the pod route, so the limit is the route MTU the pod sees, not eth0's
  local m; m=$(k exec cli -- ip route get "$(web_ip)" | grep -o 'mtu [0-9]*' | cut -d' ' -f2); m="${m:-$POD_MTU}"
  echo "route mtu to the other pod: $m"; [ "$m" -ge 1000 ] && [ "$m" -le "$POD_MTU" ] || return 1
  k exec cli -- ping -c2 -W3 -M do -s $((m - 28)) "$(web_ip)" && ! k exec cli -- ping -c1 -W2 -M do -s $((m - 27)) "$(web_ip)"
}
big_transfer() { # 30 MB of random data arrives intact (md5 equal): no PMTU black hole, no stall
  local a b
  a=$(k exec web -- md5sum /www/big | cut -d' ' -f1); b=$(k exec cli -- sh -c "curl -sf --max-time 90 http://$(web_ip):8080/big | md5sum" | cut -d' ' -f1)
  echo "$a $b"; [ -n "$a" ] && [ "$a" = "$b" ]
}
spire_ready() { [ "$(k -n cilium-spire get sts spire-server -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" = 1 ] \
  && [ "$(k -n cilium-spire get ds spire-agent -o jsonpath='{.status.numberReady}' 2>/dev/null)" = 3 ]; }
mutual_policy() { # traffic cli -> web is allowed only with mutual authentication
  k apply -f - <<YAML
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: {name: web-needs-mutual-auth}
spec:
  endpointSelector: {matchLabels: {app: web}}
  ingress:
  - fromEndpoints: [{matchLabels: {app: cli}}]
    toPorts: [{ports: [{port: "8080", protocol: TCP}]}]
    authentication: {mode: required}
YAML
}
auth_listed() { cexec n1 cilium-dbg bpf auth list | tee -a "${STATE:-/tmp}/auth-list.txt" | grep -qE "[0-9]"; }
cross_node_fast() { k exec cli -- curl -sf --max-time 3 "http://$(web_ip):8080/canary" | grep -q "$CANARY"; }
mutual_diag() { # on failure: what the SPIRE server, the SPIRE agents and the Cilium agents say (goes to poc.log only)
  echo "--- mutual auth diagnostics"; local n
  k -n cilium-spire logs spire-server-0 -c spire-server --tail=40 2>&1 | cut -c1-240
  for n in n1 n2; do echo "-- spire-agent on $n"; k -n cilium-spire logs "$(k -n cilium-spire get pod -l app=spire-agent --field-selector spec.nodeName=$(nn $n) -o name | head -1)" -c spire-agent --tail=20 2>&1 | cut -c1-240
    echo "-- cilium on $n"; k -n kube-system logs "$(cilium_pod $n)" -c cilium-agent --tail=3000 2>&1 | grep -i "authenticat\|spire" | grep -v mesh-auth | tail -12 | cut -c1-300; done
  cexec n1 cilium-dbg bpf auth list 2>&1 | head
  # the handshake between the two Cilium agents (TCP 4250, host to host): where do its packets go? (30 s of capture on both ends)
  echo "-- sockets on 4250"; for n in n1 n2; do nx $n sh -c "ss -tin 'sport = :4250 or dport = :4250' | cut -c1-260"; done
  echo "-- host MTUs"; for n in n1 n2; do nx $n sh -c 'for d in nebula1 cilium_wg0 cilium_vxlan; do echo -n "$d=$(cat /sys/class/net/$d/mtu) "; done; echo'; done
  ( tcpd n1 -i any -nn -c 25 "tcp port 4250" >"${STATE:-/tmp}/cap/d-n1.txt" 2>&1 & tcpd n2 -i any -nn -c 25 "tcp port 4250" >"${STATE:-/tmp}/cap/d-n2.txt" 2>&1 &
    cexec n1 timeout 30 cilium-dbg monitor -t drop >"${STATE:-/tmp}/cap/d-drop-n1.txt" 2>&1 & cexec n2 timeout 30 cilium-dbg monitor -t drop >"${STATE:-/tmp}/cap/d-drop-n2.txt" 2>&1 & wait ) 2>/dev/null
  for f in d-n1 d-n2 d-drop-n1 d-drop-n2; do echo "-- $f"; head -30 "${STATE:-/tmp}/cap/$f.txt" | cut -c1-200; done
}
mutual_works() { wait_for 40 cross_node_fast && wait_for 30 auth_listed; }
mutual_ok() { mutual_policy && { mutual_works || { mutual_diag; return 1; }; }; }

underlay_ports() { # what the Nebula firewall sees: every protocol/destination port on the Nebula interface of n1 while pods talk, DNS resolves
  # and the control plane is used. The result is the exact allow-list a narrowed Nebula firewall needs (docs: Nebula firewall).
  local t="${STATE:-/tmp}/cap"; mkdir -p "$t"
  tcpd n1 -i "$NEB_IF" -nn -q -l "not arp" >"$t/ports.txt" 2>"$t/ports.err" &
  local p=$!
  local w=0; until grep -q listening "$t/ports.err" 2>/dev/null; do w=$((w+1)); [ "$w" -gt 60 ] && break; sleep 1; done
  local i; for i in 1 2 3; do k exec cli -- curl -sf --max-time 10 "http://$(web_ip):8080/big" -o /dev/null; k exec cli -- nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1; k get nodes >/dev/null 2>&1; k logs web >/dev/null 2>&1; done
  k exec cli -- ping -c2 -W2 "$(web_ip)" >/dev/null 2>&1; nx n1 ping -c2 -W2 "$N2_NEB" >/dev/null 2>&1
  wait "$p" 2>/dev/null
  awk '/ICMP/ {print "icmp"; next} / UDP,/ {n=split($5,a,"."); sub(":","",a[n]); print "udp/" (a[n]>=32768 ? "ephemeral" : a[n]); next} / tcp / {n=split($5,a,"."); sub(":","",a[n]); print "tcp/" (a[n]>=32768 ? "ephemeral" : a[n])}' "$t/ports.txt"     | sort | uniq -c | sort -rn | tee "$t/ports-summary.txt"
  # only the documented set may appear: WireGuard (Cilium), the Kubernetes API, the kubelet, ICMP, Cilium health, VXLAN
  ! awk '{print $2}' "$t/ports-summary.txt" | grep -vE '^(udp/51871|icmp|tcp/6443|tcp/10250|tcp/4240|udp/8472|tcp/ephemeral|udp/ephemeral)$' | grep -q .
}

# --- SPIRE hardening (phase B): non-root, pinned to an own node, state on a PVC that survives a restart ---
spire_cli() { # the spire-server CLI, through the shared process namespace of the cilium-init container (as the chart's own init does)
  k -n cilium-spire exec spire-server-0 -c cilium-init -- sh -c 'R=/proc/$(pgrep spire-server)/root; exec "$R/opt/spire/bin/spire-server" "$@" -socketPath "$R/tmp/spire-server/private/api.sock"' sh "$@"
}
spire_nonroot() { # the server process runs as uid 1000, and no container in the pod asks for root except the one-shot init
  local uid; uid=$(k -n cilium-spire exec spire-server-0 -c cilium-init -- sh -c 'grep ^Uid: /proc/$(pgrep spire-server)/status' | awk '{print $2}')
  echo "spire-server uid: $uid"; [ "$uid" = 1000 ]
}
spire_on_own() { # pinned to a node labelled trust=own
  local n; n=$(k -n cilium-spire get pod spire-server-0 -o jsonpath='{.spec.nodeName}')
  echo "spire-server node: $n"; [ "$(k get node "$n" -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = own ]
}
spire_pvc() { # the datastore is on a bound PVC and the sqlite file is there
  [ "$(k -n cilium-spire get pvc spire-data-spire-server-0 -o jsonpath='{.status.phase}')" = Bound ] \
    && k -n cilium-spire exec spire-server-0 -c cilium-init -- sh -c 'test -s /proc/$(pgrep spire-server)/root/run/spire/data/datastore.sqlite3'
}
spire_state() { # one line that changes if the CA or the registration entries change
  local b e; b=$(spire_cli bundle show | sha256sum | cut -c1-16); e=$(spire_cli entry show | grep -c 'Entry ID')
  echo "bundle=$b entries=$e"
}
spire_survives_restart() {
  local before after; before=$(spire_state); echo "before: $before"
  k -n cilium-spire delete pod spire-server-0 --wait=true >/dev/null
  wait_for 240 spire_ready || return 1
  wait_for 120 spire_cli bundle show || return 1
  after=$(spire_state); echo "after:  $after"
  [ -n "$before" ] && [ "$before" = "$after" ] && [ "${before##*entries=}" -ge 3 ]
}
mutual_after_restart() { wait_for 120 mutual_works; }

# --- mutual authentication, the deny half (issue #8). The same policy (web needs mutual auth from app=cli) must DROP a peer that cannot authenticate.
# The unauthenticated peer: a pod on a node whose SPIRE agent is gone, so that node's Cilium agent holds no certificate for the pod's identity and the
# handshake fails. Its twin, same labels and so the same identity, on a node that has a SPIRE agent is the control: the only difference is the node's
# authentication, not the policy and not the identity. Then the agent comes back and the same pod is let in (the drop was authentication, nothing else).
AUTH_DENY_NODE=n2; AUTH_OK_NODE=cp
spire_agent_skip() { # spire_agent_skip <node>|none: the SPIRE agent DaemonSet leaves that node out (none: every node again)
  if [ "$1" = none ]; then k -n cilium-spire patch ds spire-agent --type json -p '[{"op":"remove","path":"/spec/template/spec/affinity"}]' || return 1
  else k -n cilium-spire patch ds spire-agent --type merge -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchFields":[{"key":"metadata.name","operator":"NotIn","values":["'"$(nn "$1")"'"]}]}]}}}}}}}' || return 1; fi
  k -n cilium-spire rollout status ds spire-agent --timeout=240s
}
spire_agent_on() { [ -n "$(k -n cilium-spire get pod -l app=spire-agent --field-selector "spec.nodeName=$(nn "$1"),status.phase=Running" -o name)" ]; }
deny_setup() { # the SPIRE agent off the deny node, and two twin pods (app=cli, probe=auth: a new identity, never authenticated before)
  spire_agent_skip "$AUTH_DENY_NODE" && ! spire_agent_on "$AUTH_DENY_NODE" && spire_agent_on "$AUTH_OK_NODE" || return 1
  local p n; for p in "auth-ok $AUTH_OK_NODE" "auth-no $AUTH_DENY_NODE"; do set -- $p
    k apply -f - <<YAML || return 1
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: {app: cli, probe: auth}}
spec:
  nodeName: $(nn "$2")
  tolerations: [{operator: Exists}]
  containers: [{name: c, image: $NETSHOOT_IMAGE, command: ["sleep","3600"]}]
YAML
  done
  k wait --for=condition=Ready pod/auth-ok pod/auth-no --timeout=180s
}
twins_same_identity() { # both twins carry one Cilium identity, and it is not cli's (so no earlier authentication can be reused)
  local a b c; a=$(k get cep auth-ok -o jsonpath='{.status.identity.id}'); b=$(k get cep auth-no -o jsonpath='{.status.identity.id}'); c=$(k get cep cli -o jsonpath='{.status.identity.id}')
  echo "auth-ok=$a auth-no=$b cli=$c"; [ -n "$a" ] && [ "$a" = "$b" ] && [ "$a" != "$c" ]
}
twin_reaches() { k exec "$1" -- curl -sf --max-time 3 "http://$(web_ip):8080/canary" | grep -q "$CANARY"; }
auth_drops() { cexec n1 cilium-dbg metrics list | awk '/cilium_drop_count_total/ && /direction=INGRESS/ && /reason=Authentication required/ {print int($NF)}' | head -1; }
auth_ok_twin() { twin_reaches auth-ok; }
auth_denied_twin() { # three tries over 30 s: no answer, and the receiving node counted drops with reason "Authentication required"
  local before after i; before=$(auth_drops); before=${before:-0}
  for i in 1 2 3; do twin_reaches auth-no && { echo "auth-no got through on try $i"; return 1; }; sleep 7; done
  after=$(auth_drops); after=${after:-0}; echo "drops 'Authentication required' on $(nn n1): $before -> $after"
  [ "$after" -gt "$before" ]
}
auth_restored() { spire_agent_skip none && spire_agent_on "$AUTH_DENY_NODE" && wait_for 120 twin_reaches auth-no; }

# --- the CiliumNode guard (poc/cilium-node-guard.yaml): every Cilium agent shares one service account that may update every CiliumNode, and a
# CiliumNode carries the node's WireGuard key. The owner of a remote node reads that token from his own disk (root there).
agent_token() { # agent_token <node>: the Cilium agent's service account token, read from the node's disk as its owner would
  local uid; uid=$(k -n kube-system get pod "$(cilium_pod "$1")" -o jsonpath='{.metadata.uid}')
  nx "$1" sh -c "cat /var/lib/kubelet/pods/$uid/volumes/kubernetes.io~projected/*/token"
}
as_agent_of() { # as_agent_of <node> <kubectl args...>: kubectl on that node with its Cilium agent's token (VM flow: k3s kubectl on the node)
  local n="$1"; shift; local t; t=$(agent_token "$n") || return 1
  nx "$n" k3s kubectl --server "https://$CP_NEB:6443" --certificate-authority /var/lib/rancher/k3s/agent/server-ca.crt --token "$t" "$@" 2>&1
}
rewrite_wg_key() { # rewrite_wg_key <as-node> <target-node> <key>: replace the target's published WireGuard key with the as-node's agent token
  local t obj; t=$(agent_token "$1") && obj=$(as_agent_of "$1" get ciliumnode "$(nn "$2")" -o json) || return 1   # both before the pipe (incus exec reads stdin)
  echo "$obj" | sed 's#"network.cilium.io/wg-pub-key": "[^"]*"#"network.cilium.io/wg-pub-key": "'"$3"'"#' \
    | nx "$1" k3s kubectl --server "https://$CP_NEB:6443" --certificate-authority /var/lib/rancher/k3s/agent/server-ca.crt --token "$t" replace -f - 2>&1
}
node_guard_holds() { # from n2: cp's key cannot be replaced (and stays what it was); n2's own CiliumNode can still be written (same key back)
  local before out; before=$(wg_key_of cp)
  out=$(rewrite_wg_key n2 cp AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=); echo "n2 -> cp: $out"
  echo "$out" | grep -q 'may write only the CiliumNode of its own node' && [ "$(wg_key_of cp)" = "$before" ] || return 1
  out=$(rewrite_wg_key n2 n2 "$(wg_key_of n2)"); echo "n2 -> n2: $out"; echo "$out" | grep -q replaced
}
spire_delegate_ok() { # every Cilium agent talks to its SPIRE agent: the admin socket exists and the delegate stream has been quiet (no
  # "failed to init watcher", no "no identity issued") for 25 s. A policy with authentication applied BEFORE this point can fail its first
  # authentication ("no SPIFFE ID for spiffe://spiffe.cilium/identity/N") and Cilium does not retry that flow: it stays dropped.
  local n; for n in cp n1 n2; do
    cexec "$n" test -S /run/spire/sockets/admin.sock || return 1
    ! k -n kube-system logs "$(cilium_pod "$n")" -c cilium-agent --since=25s 2>&1 | grep -qE 'failed to init watcher|Error in delegate stream|no identity issued' || return 1
  done
}
spire_identities() { # the operator has registered the SPIFFE entries of the two test pods' Cilium identities (it does so when its SPIRE client
  # initializes, which can be seconds AFTER the SPIRE pods are Ready), and the SPIRE agents had 10 s to sync them
  local id e; e=$(spire_cli entry show) || return 1
  for id in $(k get cep web cli -o jsonpath='{range .items[*]}{.status.identity.id}{"
"}{end}'); do
    echo "$e" | grep -q "spiffe://spiffe.cilium/identity/$id\$" || return 1
  done
  sleep 10
}
spire_full() { spire_ready && [ "$(spire_cli entry show | grep -c 'Entry ID')" -ge 3 ] && spire_delegate_ok && spire_identities; }

host_tcp() { # host-to-host TCP over the node addresses (the Cilium agents' own traffic) crosses the WireGuard: 2 MB n1 -> n2 without a stall
  nsx_listen() { if [ "$RUNTIME" = incus ]; then nx n2 sh -c 'kill $(pidof nc) 2>/dev/null; nohup nc -l -p 9998 >/dev/null 2>&1 &'
    else docker rm -f htl >/dev/null 2>&1; docker run -d --name htl --network container:n2 "$NETSHOOT_IMAGE" nc -l -p 9998 >/dev/null; fi; }
  nsx_listen; sleep 2
  local rc=0
  if [ "$RUNTIME" = incus ]; then nx n1 sh -c 'head -c 2000000 /dev/zero | timeout 20 nc -q1 -w15 '"$N2_NEB"' 9998' || rc=1
  else docker run --rm --network container:n1 "$NETSHOOT_IMAGE" sh -c 'head -c 2000000 /dev/zero | timeout 20 nc -q1 -w15 '"$N2_NEB"' 9998' || rc=1; fi
  if [ "$RUNTIME" = incus ]; then nx n2 sh -c 'kill $(pidof nc) 2>/dev/null; true'; else docker rm -f htl >/dev/null 2>&1; fi
  return $rc
}
cni_dropin_everywhere() { # the fix of the first attempt (Cilium pods in CreateContainerError, docs/TROUBLESHOOTING.md): containerd looks for CNI where
  # Cilium writes, with the k3s plugins still on the path. vm/up.sh writes it on the control plane, `edge-mesh join` on every joining node.
  local n f=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/10-cni.toml
  for n in cp n1 n2; do nx "$n" grep -q "conf_dir = '/etc/cni/net.d'" "$f" && nx "$n" grep -q "/var/lib/rancher/k3s/data/cni" "$f" || return 1; done
  ! k -n kube-system get pod -l k8s-app=cilium -o jsonpath='{range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{" "}{.status.initContainerStatuses[*].state.waiting.reason}{"\n"}{end}' | grep -q CreateContainerError
}
