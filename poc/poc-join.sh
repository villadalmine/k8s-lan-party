#!/usr/bin/env bash
# make poc-join: step 1, "it joins". A local CA + lighthouse + control plane, two outside nodes joining with one bundle
# and one command, the default CNI (flannel over the Nebula interface), and a real study lab (CKA 1.1) running on them.
# Output: one PASS/FAIL line per step, identical across runs. Numbers go to .state/measurements.txt (they vary).
set -u
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"; : >"$STATE/measurements.txt"

tools() { if [ "$RUNTIME" = incus ]; then incus exec emesh-cp -- env KUBECONFIG=/root/kubeconfig I_UNDERSTAND_THIS_IS_A_DISPOSABLE_VM=yes "$@"; return; fi
  docker run --rm --network container:cp -v "$STATE/kubeconfig:/kube:ro" -e KUBECONFIG=/kube \
  -e I_UNDERSTAND_THIS_IS_A_DISPOSABLE_VM=yes -v "$POC/labs:/labs:ro" "$TOOLS_IMAGE" "$@"; }
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
pod_on() { # pod_on <name> <node> <command>
  k apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $1, labels: {app: poc}}
spec:
  nodeName: $2
  tolerations: [{operator: Exists}]
  containers: [{name: c, image: busybox:1.36, command: ["sh","-c","$3"]}]
EOF
}
bring_up() { "$UP" && dx cp ping -c1 -W2 "$LH_NEB"; }
nodeip_is() { [ "$(k get node "$1" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')" = "$2" ]; }
mtu_ok() { # Nebula 1300, flannel VXLAN takes 50 more: pods get 1250; a near-full-size packet crosses the tunnel
  [ "$(dx n1 cat /sys/class/net/nebula1/mtu)" = 1300 ] && [ "$(dx n1 cat /sys/class/net/flannel.1/mtu)" = 1250 ] \
    && dx n1 ping -c1 -W2 -s 1200 "$N2_NEB"
}
dns_ready() { [ "$(k -n kube-system get deploy coredns -o jsonpath='{.status.readyReplicas}')" = 1 ]; }
bytes() { dx "$1" cat "/sys/class/net/$2/statistics/$3_bytes"; }
snap() { for n in n1 n2 lh; do echo "$1 $n nebula1 rx=$(bytes $n nebula1 rx) tx=$(bytes $n nebula1 tx) $UNDERLAY_IF rx=$(bytes $n $UNDERLAY_IF rx) tx=$(bytes $n $UNDERLAY_IF tx)"; done >>"$STATE/measurements.txt"; }
lab() { tools bash /labs/cka-1.1/break_fix.sh "$1"; }
lab_break() { lab break; }
lab_not_yet() { lab verify 2>&1 | grep -q 'Todavía no'; }
lab_solved() { lab verify 2>&1 | grep -q 'Laboratorio resuelto'; }
lab_pod_running() { [ "$(k get pod -n cka-1-1-storage-lab cka-lab-writer -o jsonpath='{.status.phase}')" = Running ]; }
lab_on_outside() { local n; n=$(k get pod -n cka-1-1-storage-lab cka-lab-writer -o jsonpath='{.spec.nodeName}'); [ "$n" = n1 ] || [ "$n" = n2 ]; }
p_ready() { [ "$(k get pod p1 p2 -o jsonpath='{range .items[*]}{.status.phase}{end}')" = RunningRunning ]; }

step J01 "Nebula: CA, lighthouse and control plane handshake" bring_up
step J02 "n1 joined with one bundle and one command, node Ready" wait_for 180 ready n1
step J03 "n2 joined with one bundle and one command, node Ready" wait_for 180 ready n2
step J04 "node InternalIP is the Nebula IP" nodeip_is n1 "$N1_NEB"
step J05 "n1 reaches n2 over Nebula" dx n1 ping -c2 -W2 "$N2_NEB"
pod_on p1 n1 'mkdir -p /www; echo ok >/www/i; httpd -f -p 8080 -h /www' >>"$LOG" 2>&1
pod_on p2 n2 'sleep 3600' >>"$LOG" 2>&1
wait_for 150 p_ready
P1IP=$(k get pod p1 -o jsonpath='{.status.podIP}')
step J06 "a pod on n2 reaches a pod on n1 across nodes" k exec p2 -- wget -qO- -T5 "http://$P1IP:8080/i"
dns_resolves() { k exec p2 -- nslookup kubernetes.default.svc.cluster.local; }
wait_for 120 dns_ready
step J07 "DNS inside a pod resolves cluster services" wait_for 90 dns_resolves
step J08 "MTU: Nebula 1300, pod network 1250, a 1200-byte packet crosses the tunnel" mtu_ok

# measurements: idle 30 s, then a 50 MB pod-to-pod transfer; the lighthouse must stay tiny (no relay in use)
snap idle-start; sleep 30; snap idle-end
k exec p1 -- sh -c 'dd if=/dev/zero of=/www/big bs=1M count=50 2>/dev/null' >>"$LOG" 2>&1
snap transfer-start
T0=$(date +%s); k exec p2 -- wget -qO /dev/null -T90 "http://$P1IP:8080/big" >>"$LOG" 2>&1; T1=$(date +%s)
snap transfer-end
echo "50 MB pod-to-pod over nebula: $((T1 - T0)) s" >>"$STATE/measurements.txt"

step J09 "study lab cka/1.1: break leaves the lab broken" lab_break
step J10 "study lab cka/1.1: verify says NOT solved yet, as it must" lab_not_yet
k delete storageclass cka-lab-broken-sc >>"$LOG" 2>&1
printf 'apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata:\n  name: cka-lab-broken-sc\nprovisioner: rancher.io/local-path\nreclaimPolicy: Delete\nvolumeBindingMode: WaitForFirstConsumer\n' | k apply -f - >>"$LOG" 2>&1
wait_for 180 lab_pod_running
step J11 "study lab cka/1.1: after the reference fix, verify says solved" lab_solved
step J12 "the lab pod ran on a joined outside node, not on the control plane" lab_on_outside

step J13 "narrowed Nebula firewall: ICMP and 6443 pass, an unlisted TCP port between hosts is refused" fw_blocks

tok_reuse() { # the one-time token is deleted once n1 is Ready; a second machine presenting it is refused
  local used tid
  used=$(. "$STATE/bundles/n1/join.env"; echo "$K3S_TOKEN"); tid=${used##*::}; tid=${tid%%.*}
  dx cp k3s token delete "$tid" >/dev/null 2>&1 || true
  "$POC/issue.sh" n3 10.77.0.9 friends 24h >/dev/null
  sed -i "s#^K3S_TOKEN=.*#K3S_TOKEN=$used#" "$STATE/bundles/n3/join.env"
  new_container n3 172.31.0.6
  dx n3 edge-mesh join /bundle --accept-golden-rule; [ $? = 7 ] || return 1   # exit 7: the token was refused
  sleep 25
  dx n3 grep -qiE "unauthorized|invalid|token|forbidden|failed" /var/log/k3s-agent.log && ! k get node n3
}
step J14 "token already used and deleted: a second machine is refused" tok_reuse
machine_rm n3

expired_cert() { # a certificate past its expiry is rejected (a skewed clock looks the same: check NTP)
  local t="$STATE/exp"; rm -rf "$t"; mkdir -p "$t"
  (cd "$t" && "$CACHE/nebula-cert" sign -ca-crt "$STATE/ca/ca.crt" -ca-key "$STATE/ca/ca.key" -name x -ip 10.77.0.50/24 -duration 2s >/dev/null)
  sleep 4
  sed "s#/etc/nebula/host.crt#$t/x.crt#; s#/etc/nebula/host.key#$t/x.key#; s#/etc/nebula/ca.crt#$STATE/ca/ca.crt#" "$STATE/bundles/lh/config.yml" >"$t/config.yml"
  ! "$CACHE/nebula" -test -config "$t/config.yml"
}
step J15 "an expired Nebula certificate is rejected" expired_cert

udp_blocked() { # the lighthouse cannot be reached (firewall): the join fails with exit 5 and names the port
  local out rc
  "$POC/issue.sh" n4 10.77.0.8 friends 24h >/dev/null
  new_container n4 172.31.0.7
  dx n4 ip route add blackhole "$LH_DOCKER_IP/32"
  out=$(dx n4 edge-mesh join /bundle --accept-golden-rule 2>&1); rc=$?
  machine_rm n4
  echo "$out"; [ "$rc" = 5 ] && echo "$out" | grep -q "UDP 4242"
}
step J16 "lighthouse unreachable (UDP blocked): the join fails clearly and names the port" udp_blocked

gone_from_nebula() { ! dx n2 ping -c1 -W2 "$LH_NEB"; }
revoked() {
  "$POC/revoke.sh" n2 || return 1
  wait_for 60 gone_from_nebula || return 1
  ! k get node n2 || return 1
  sleep 20
  ! k get node n2
}
step J17 "revoke in one command: node deleted, cannot reach the lighthouse, does not come back" revoked

mkdir -p "$POC/results"; cp "$STATE/measurements.txt" "$POC/results/join-measurements.txt"
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
