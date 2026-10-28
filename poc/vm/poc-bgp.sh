#!/usr/bin/env bash
# make poc-bgp: issue #13, the maintainer's endpoints on own nodes only (DESIGN.md section 6). On Incus VMs: lighthouse, cp (own), n1 (friends)
# and n2 (public), the remote two tainted at registration; Cilium with the BGP control plane; a test router (FRR, an Incus container on the
# underlay: it plays the edge router, the real one is never touched); Envoy Gateway and cert-manager pinned to own nodes; a Gateway whose
# certificate cert-manager issues by itself from a local CA. Every step prints PASS/FAIL, identical across runs. KEEP=1 leaves it up.
set -u
export RUNTIME=incus CNI=cilium TAINT=yes
. "$(dirname "$0")/../lib.sh"
nx() { local n="$1"; shift; incus exec "emesh-$n" -- "$@"; }
tcpd() { local n="$1"; shift; incus exec "emesh-$n" -- timeout 30 tcpdump "$@"; }
. "$POC/checks-cilium.sh"
R() { incus exec emesh-rtr -- "$@"; }
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
OWN_IP="$CP_UNDERLAY"; N1_IP="$UNDERLAY_NET.4"; N2_IP="$UNDERLAY_NET.5"

# cp plays the maintainer's own machine: schedulable, as in poc-placement (the gateway runs where the trust label says, no tolerations)
up_all() { "$POC/vm/up.sh" && k taint node cp node-role.kubernetes.io/control-plane:NoSchedule- >/dev/null; }
cilium_bgp() { BGP=yes "$POC/cilium.sh"; }
healthy() { all_ready && agents_ok; }
router_up() { "$POC/gateway.sh" router && R vtysh -c 'show running-config' | grep -q "neighbor $OWN_IP remote-as $OWN_ASN" \
  && [ "$(R vtysh -c 'show running-config' | grep -c 'remote-as')" = 1 ]; }
bgp_up() { # apply, then read the selectors back from the API: the BGP config selects trust=own nodes, the pool selects Services, not nodes
  "$POC/gateway.sh" bgp || return 1
  [ "$(k get ciliumbgpclusterconfig own-nodes-only -o jsonpath='{.spec.nodeSelector.matchLabels.node-restriction\.kubernetes\.io/trust}')" = own ] \
    && [ "$(k get ciliumloadbalancerippool gateway -o jsonpath='{.spec.serviceSelector.matchLabels.edge-mesh/expose}')" = gateway ]
}
router_session() { R vtysh -c "show bgp neighbors $1" 2>/dev/null | grep -q 'BGP state = Established'; }
bgp_peers() { cexec "$1" cilium-dbg shell -- bgp/peers; }   # columns: Instance Peer "Session State" Uptime Family Received Accepted Advertised
own_session() { router_session "$OWN_IP" && bgp_peers cp | tee -a "$LOG" | grep -qE '^own +router +established'; }
remote_silent() { # only cp has a BGP node config; n1 and n2 run no BGP instance and peer with nobody; the router sees no session from them
  local names; names=$(k get ciliumbgpnodeconfigs -o jsonpath='{.items[*].metadata.name}'); echo "bgp node configs: $names"; [ "$names" = cp ] || return 1
  local n out; for n in n1 n2; do # the agent answers (header present) and lists no peer at all
    out=$(bgp_peers "$n") || return 1; echo "$n: $out"; echo "$out" | head -1 | grep -q 'Session State' && [ "$(echo "$out" | grep -c .)" = 1 ] || return 1; done
  ! router_session "$N1_IP" && ! router_session "$N2_IP"
}
no_self_promote() { # a remote node's own kubelet credential cannot put it under the selector (NodeRestriction)
  local n out rc; for n in n1 n2; do
    out=$(nx "$n" k3s kubectl --kubeconfig /var/lib/rancher/k3s/agent/kubelet.kubeconfig label node "$n" node-restriction.kubernetes.io/trust=own --overwrite 2>&1); rc=$?
    echo "$n: rc=$rc $out"; [ "$rc" -ne 0 ] || return 1
  done
  [ "$(k get node n1 -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = friends ] \
    && [ "$(k get node n2 -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = public ]
}
agent_cannot() { # the Cilium agent's token, read from n1's disk by its owner (root there; as_agent_of in checks-cilium.sh): it may not write any
  # BGP object, the pool, a Node or a Service (so not the expose label either); then two real attempts, refused
  as_agent_of n1 auth whoami | grep -q 'system:serviceaccount:kube-system:cilium' || return 1
  local r v ans; for r in ciliumbgpclusterconfigs ciliumbgppeerconfigs ciliumbgpadvertisements ciliumbgpnodeconfigs ciliumbgpnodeconfigoverrides \
      ciliumloadbalancerippools nodes services; do
    for v in create update patch delete; do ans=$(as_agent_of n1 auth can-i "$v" "$r" -A); echo "$v $r: $ans"; [ "$ans" = no ] || return 1; done
  done
  local out; out=$(as_agent_of n1 patch ciliumbgpclusterconfig own-nodes-only --type merge -p '{"spec":{"nodeSelector":{"matchLabels":{"kubernetes.io/hostname":"n1"}}}}')
  echo "$out"; echo "$out" | grep -q Forbidden || return 1
  out=$(as_agent_of n1 label node n1 node-restriction.kubernetes.io/trust=own --overwrite); echo "$out"; echo "$out" | grep -q Forbidden
}
rogue_refused() { # root on n1 opens a BGP connection to the router: refused as "not configured", no session, no route through n1
  nx n1 timeout 5 bash -c "exec 3<>/dev/tcp/$ROUTER_IP/179; sleep 2" >/dev/null 2>&1
  sleep 2; R grep -q "$N1_IP.*not configured\|connection.*$N1_IP.*not configured\|$N1_IP is not configured" /var/log/frr/bgpd.log && ! router_session "$N1_IP"
}
gw_install() { "$POC/gateway.sh" install; }
cert_ready() { # cert-manager made the Certificate from the Gateway (nobody wrote it) and it is Ready; the Secret holds a key pair
  [ "$(k -n gateway get certificate gateway-tls -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ] \
    && [ "$(k -n gateway get certificate gateway-tls -o jsonpath='{.metadata.ownerReferences[0].kind}')" = Gateway ] \
    && [ -n "$(k -n gateway get secret gateway-tls -o jsonpath='{.data.tls\.key}')" ]
}
gw_svc() { k -n envoy-gateway-system get svc -l gateway.envoyproxy.io/owning-gateway-name=lan-party -o jsonpath='{.items[0].metadata.name}'; }
lb_ip() { k -n envoy-gateway-system get svc "$(gw_svc)" -o jsonpath='{.status.loadBalancer.ingress[0].ip}'; }
in_pool() { python3 -c 'import ipaddress,sys; sys.exit(0 if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2]) else 1)' "$1" "$LB_POOL_CIDR"; }
gw_programmed() {
  local ip; ip=$(lb_ip); echo "gateway address: $ip"; [ -n "$ip" ] && in_pool "$ip" \
    && [ "$(k -n gateway get gateway lan-party -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}')" = True ]
}
route_via_own() { # the router's route to the gateway address goes to the own node, and that is its only path
  local ip; ip=$(lb_ip); R vtysh -c "show ip bgp $ip/32" | tee -a "$LOG" | grep -q "$OWN_IP" \
    && R ip route show "$ip" | tee -a "$LOG" | grep -q "via $OWN_IP" && [ "$(R vtysh -c "show ip bgp $ip/32" | grep -c 'from ')" = 1 ]
}
https_ok() { # from outside the cluster (the router): HTTPS by name to the announced address, verified against the local CA
  local ip; ip=$(lb_ip)
  k -n cert-manager get secret lan-party-ca -o jsonpath='{.data.ca\.crt}' | base64 -d | R sh -c 'cat >/root/ca.crt'
  R curl -sS --max-time 10 --cacert /root/ca.crt --resolve "$GW_HOST:443:$ip" "https://$GW_HOST/" | grep -qx "${GW_BODY:-lan-party-gateway-ok}" \
    && R sh -c "echo | timeout 10 openssl s_client -connect $ip:443 -servername $GW_HOST -CAfile /root/ca.crt 2>/dev/null | grep -q 'Verify return code: 0 (ok)'"
}
all_on_own() { # every pod of the gateway, its backend, Envoy Gateway and cert-manager runs on a node labelled own
  local ns p n bad=0; for ns in gateway envoy-gateway-system cert-manager; do
    for p in $(k -n "$ns" get pod --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}={.spec.nodeName}{" "}{end}'); do
      n=${p#*=}; echo "$ns/${p%=*} on $n"
      [ "$(k get node "$n" -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = own ] || bad=1
    done
  done
  [ "$bad" = 0 ] && [ "$(k -n envoy-gateway-system get pod -l gateway.envoyproxy.io/owning-gateway-name=lan-party --no-headers | wc -l)" -ge 1 ]
}
no_pool_for() { [ "$(k -n default get svc "$1" -o jsonpath='{.status.conditions[?(@.type=="cilium.io/IPAMRequestSatisfied")].reason}')" = no_pool ]; }
unlabelled_lb() { # a LoadBalancer Service without the expose label: no address from the pool, nothing new on the router
  k -n default apply -f - >/dev/null <<'YAML' || return 1
apiVersion: v1
kind: Service
metadata: {name: not-exposed}
spec:
  type: LoadBalancer
  selector: {app: none}
  ports: [{port: 80}]
YAML
  wait_for 60 no_pool_for not-exposed || return 1   # Cilium's LB IPAM says so on the Service: no pool matches it
  local ip; ip=$(k -n default get svc not-exposed -o jsonpath='{.status.loadBalancer.ingress[0].ip}'); echo "not-exposed address: '$ip'"
  [ -z "$ip" ] && [ "$(R vtysh -c 'show ip bgp' | grep -c '/32')" = 1 ]
}
no_remote_routes() { # everything the router learned: prefixes inside the pool only, next hop the own node only
  local t; t=$(R vtysh -c 'show ip bgp'); echo "$t"
  echo "$t" | grep -qwF "$N1_IP" && return 1; echo "$t" | grep -qwF "$N2_IP" && return 1
  local rows p hop; rows=$(echo "$t" | awk '/^ *[*]/ {print $2, $3}'); [ -n "$rows" ] || return 1
  while read -r p hop; do echo "route $p via $hop"; [ "$hop" = "$OWN_IP" ] && in_pool "${p%/*}" || return 1; done <<<"$rows"
}

step G01 "4 VMs: lighthouse, cp (own), n1 (friends), n2 (public); Nebula + k3s without flannel" up_all
step G02 "n1 and n2 carry their trust taint from registration" remote_tainted
step G03 "Cilium $CILIUM_VERSION with the BGP control plane on (helm --wait)" cilium_bgp
step G04 "all three nodes Ready, Cilium healthy on every node" wait_for 300 healthy
step G05 "the test router (FRR) on the underlay lists one neighbour: the own node" router_up
step G06 "BGP config selects trust=own nodes; the LoadBalancer pool selects Services by label (it has no node selector)" bgp_up
step G07 "the own node has its BGP session Established, seen from the router and from Cilium" wait_for 120 own_session
step G08 "a remote node never speaks BGP: no BGP node config, no peer, no session for n1 or n2" remote_silent
step G09 "NEGATIVE: a remote node cannot put itself under the selector with its kubelet credential (NodeRestriction)" no_self_promote
step G10 "NEGATIVE: the Cilium agent token on a remote node cannot change the BGP config, the pool, the advertisement, a Node or a Service label" agent_cannot
step G11 "NEGATIVE: root on a remote node opening BGP to the router gets no session ('not configured')" rogue_refused
step G12 "Envoy Gateway $ENVOY_GATEWAY_VERSION and cert-manager $CERT_MANAGER_VERSION on own nodes, a local CA, the Gateway (helm --wait)" gw_install
step G13 "TLS issued automatically: cert-manager made the listener's Certificate from the Gateway, Ready" wait_for 180 cert_ready
step G14 "the gateway's Service got an address from the pool, the Gateway is Programmed" wait_for 180 gw_programmed
step G15 "the router learned that address through the own node only" wait_for 120 route_via_own
step G16 "from the router (outside the cluster): HTTPS by name answers and the certificate verifies against the local CA" wait_for 120 https_ok
step G17 "every pod of the gateway, its backend, Envoy Gateway and cert-manager runs on an own node" all_on_own
step G18 "NEGATIVE: a LoadBalancer Service without the expose label gets no address and nothing is announced for it" unlabelled_lb
step G19 "the router holds no route through a remote node and no prefix outside the pool" no_remote_routes
cp "$LOG" /tmp/poc-bgp-last.log 2>/dev/null
[ "${KEEP:-0}" = 1 ] || "$POC/vm/down.sh" >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
