#!/usr/bin/env bash
# make poc-weakness: what breaks with ONE control plane and unreliable links, measured and published as data.
# PASS/FAIL asserts that each weakness behaves the way the design says; the numbers go to .state/weakness.txt.
set -u
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"; mkdir -p "$POC/results"; OUT="$POC/results/weakness.txt"; : >"$OUT"
NETSHOOT=nicolaka/netshoot:v0.13

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
not_ready() { ! ready "$1"; }
pod_on() { # pod_on <name> <node> <command> [tolerationSeconds]
  local tol=""; [ -n "${4:-}" ] && tol="tolerations: [{key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: $4}, {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: $4}]"
  k apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $1}
spec:
  nodeName: $2
  $tol
  containers: [{name: c, image: busybox:1.36, command: ["sh","-c","$3"]}]
EOF
}
running() { [ "$(k get pod "$1" -o jsonpath='{.status.phase}' 2>/dev/null)" = Running ]; }
secs() { date +%s; }
tcn() { if [ "$RUNTIME" = incus ]; then dx "$1" tc "${@:2}"; else docker run --rm --cap-add NET_ADMIN --net=container:"$1" "$NETSHOOT" tc "${@:2}"; fi; }

bring_up() { "$UP"; }
step W00 "a control plane and two outside nodes are up" bring_up
wait_for 180 ready n1; wait_for 180 ready n2
pod_on p1 n1 'mkdir -p /www; dd if=/dev/zero of=/www/big bs=1M count=5 2>/dev/null; echo ok >/www/i; httpd -f -p 8080 -h /www' >>"$LOG" 2>&1
pod_on p2 n2 'sleep 3600' >>"$LOG" 2>&1
wait_for 120 running p1; wait_for 120 running p2
P1IP=$(k get pod p1 -o jsonpath='{.status.podIP}')

fetch() { local t0 t1; t0=$(secs); k exec p2 -- wget -qO /dev/null -T60 "http://$P1IP:8080/big" || return 1; t1=$(secs); echo "$((t1 - t0))"; }
baseline() { local s; s=$(fetch) || return 1; echo "baseline 5 MB pod-to-pod: ${s} s" >>"$OUT"; dx n1 ping -c5 -q "$N2_NEB" | tail -1 | sed 's/^/baseline nebula ping: /' >>"$OUT"; }
step W01 "baseline measured: 5 MB transfer and Nebula latency" baseline

lossy() { # intercontinental-like link on n2: 150 ms +-20 ms, 2 % loss
  tcn n2 qdisc add dev $UNDERLAY_IF root netem delay 150ms 20ms loss 2% || return 1
  local s; s=$(fetch) || { tcn n2 qdisc del dev $UNDERLAY_IF root; return 1; }
  echo "150 ms + 2 % loss: 5 MB pod-to-pod ${s} s" >>"$OUT"
  dx n1 ping -c5 -q "$N2_NEB" | tail -1 | sed 's/^/150 ms + 2 % loss, nebula ping: /' >>"$OUT"
  tcn n2 qdisc del dev $UNDERLAY_IF root
}
step W02 "a 150 ms, 2 % loss link: the transfer still completes (numbers recorded)" lossy

flap() { # n2 loses its Nebula tunnel: time until the node is NotReady
  local t0 t1; t0=$(secs)
  dx n2 sh -c 'kill $(pidof nebula)'
  wait_for 200 not_ready n2 || return 1
  t1=$(secs); echo "node drop -> NotReady after $((t1 - t0)) s (kubelet status interval 10 s, controller grace 40 s defaults)" >>"$OUT"
  [ $((t1 - t0)) -ge 20 ] && [ $((t1 - t0)) -le 120 ]
}
step W03 "a node that drops goes NotReady after about 40-50 s (flapping window)" flap

evict_default() { # the default tolerations of a pod are 300 s for not-ready and unreachable
  k get pod p2 -o jsonpath='{.spec.tolerations[?(@.key=="node.kubernetes.io/unreachable")].tolerationSeconds}' | grep -qx 300
}
step W04 "the default eviction delay of a pod is 300 s" evict_default

pod_leaving() { [ -n "$(k get pod tuned -o jsonpath='{.metadata.deletionTimestamp}' 2>&1)" ]; }
evict_diag() { # what to read when W05 fails, written to the results file (down.sh removes the state afterwards)
  { echo "diagnostics ($1):"
    echo "  node n2: $(k get node n2 -o jsonpath='Ready={.status.conditions[?(@.type=="Ready")].status} taints={.spec.taints}' 2>&1)"
    echo "  pod tuned: $(k get pod tuned -o jsonpath='phase={.status.phase} deletionTimestamp={.metadata.deletionTimestamp} node={.spec.nodeName} tolerations={.spec.tolerations}' 2>&1)"
    k get events --sort-by=.lastTimestamp 2>&1 | tail -6 | sed 's/^/  event: /'
  } >>"$OUT"
}
recover() { # the tunnel comes back: the node has to be Ready again with nobody touching it. When the tunnel drops, nebula1 loses its address and the
  # embedded flannel panics; the join command supervises the agent so it starts again (without it the node stayed NotReady for ever: measured 2026-10-26)
  local t0 t1; t0=$(secs)
  dx n2 sh -c 'nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &'
  wait_for 150 ready n2 || { evict_diag "n2 was not Ready again within 150 s after the tunnel came back"; return 1; }
  t1=$(secs); echo "tunnel back -> node Ready again after $((t1 - t0)) s, without help (the join command supervises the agent)" >>"$OUT"
}
step W03b "when the tunnel comes back the node is Ready again by itself" recover

evict_tuned() { # a pod tolerating 20 s is evicted far sooner than the 300 s default
  local t0 t1
  pod_on tuned n2 'sleep 3600' 20 >>"$LOG" 2>&1
  wait_for 120 running tuned || { evict_diag "tuned was not Running within 120 s"; return 1; }
  t0=$(secs); dx n2 sh -c 'kill $(pidof nebula)'
  wait_for 240 pod_leaving || { evict_diag "no deletionTimestamp within 240 s"; return 1; }
  t1=$(secs); echo "tolerationSeconds=20 pod evicted $((t1 - t0)) s after the drop (the default 300 s toleration would add about five minutes on top of the NotReady delay)" >>"$OUT"
  [ $((t1 - t0)) -lt 200 ] || { evict_diag "evicted after $((t1 - t0)) s, over the 200 s bound"; return 1; }
}
step W05 "with tolerationSeconds tuned, the pod leaves a dead node in minutes, not 5+" evict_tuned
dx n2 sh -c 'nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &' >/dev/null 2>&1

cp_down() { # the single point of failure: stop the control plane; running pods keep running, kubectl fails clearly
  wait_for 120 ready n2 || true
  dx cp sh -c 'kill $(pidof k3s)' >/dev/null 2>&1 || dx cp sh -c 'kill $(pgrep -f "k3s server")' >/dev/null 2>&1
  sleep 8
  k get pods >"$STATE/cpdown.txt" 2>&1 && return 1
  echo "control plane down, kubectl says: $(tail -1 "$STATE/cpdown.txt" | cut -c1-150)" >>"$OUT"
  k exec p2 -- true 2>/dev/null && return 1
  # the data plane keeps working: the pod on n1 still answers a request from n2's host network namespace through the CNI
  dx n2 sh -c "wget -qO- -T5 http://$P1IP:8080/i" | grep -q ok
  echo "control plane down: the running pod still answers over the pod network; nothing can be managed" >>"$OUT"
}
step W06 "control plane down: running pods keep answering, kubectl fails with a clear error" cp_down

"$POC/down.sh" >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
