#!/usr/bin/env bash
# make poc-pool: the cluster side of the free pool and the participant's own access (issues #9 and #11, DESIGN.md sections 8 and 14) end to
# end on Incus VMs. Three people ask for two places; one tick approves two and the third waits. Each approved player gets the namespace
# play-<name> (Pod Security "restricted", ResourceQuota, LimitRange, NetworkPolicy, the participant guard) and a kubeconfig of their own,
# a ServiceAccount token that lives exactly the lease, kept apart from the node credential. alice and bob join; alice works in her
# namespace: her unlabelled (private) pods stay on her own node, a shareable one may go to bob's, her pods cannot reach his. Every row of
# the error matrix that belongs to the user is triggered for real and must give its message and exit code (6 Nebula down, 7 another CA,
# 8 the token ended, 9 another namespace, 11 the quota). bob runs nothing: the idle kick expels him and his place goes to carol, whose
# token dies the moment her lease is ended by hand. A second request from
# alice's address waits while her lease is active and through the cooldown; the rate limit and X-Forwarded-For hold. At the end of alice's
# lease one tick removes her node, her namespace and her token, her certificate is blocklisted and expired, and her place is reused.
# Needs a Linux host with Incus. PASS/FAIL per step; identical across runs. Test values: POOL_LEASE (default 900 s; a token lives at least
# 10 minutes), POOL_IDLE (240), POOL_COOLDOWN (60); the other limits are the real ones of pool.yaml. KEEP=1 leaves the VMs up.
set -u
export RUNTIME=incus MAX_VMS=4 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-pool.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"
LEASE="${POOL_LEASE:-900}"; IDLE="${POOL_IDLE:-240}"; COOLDOWN="${POOL_COOLDOWN:-60}"
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^cooldown_seconds: [0-9]*/cooldown_seconds: $COOLDOWN/" \
  -e "s/^idle_kick_seconds: [0-9]*/idle_kick_seconds: $IDLE/" "$POC/pool.yaml" >"$POOL_CONFIG"
ADM="$STATE/admission"
unset ADMISSION_NOTIFY_CMD ADMISSION_NOTIFY_IP

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
exits() { local want="$1"; shift; "$@"; [ $? -eq "$want" ]; }
row() { # row <exit code> <text> <command...>: the command exits with that code and says that text (the error matrix, DESIGN.md 14.1)
  local want="$1" msg="$2" o rc; shift 2; o=$("$@" 2>&1); rc=$?; echo "$o"; echo "exit $rc"; [ "$rc" -eq "$want" ] && echo "$o" | grep -qF -- "$msg"; }
tick() { "$MESH" host auto --once; }
lease_of() { python3 -c 'import json,sys; print(next(l[sys.argv[3]] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == sys.argv[2]))' "$ADM/state.json" "$1" "$2"; }
sleep_until() { local s=$(( $1 - $(date +%s) )); [ "$s" -le 0 ] || sleep "$s"; }
digest() { cat "$ADM/state.json" "$ADM/auto.json" "$ADM/events.jsonl" 2>/dev/null | sha256sum; }
ns_gone() { [ -z "$(k get namespace "play-$1" --ignore-not-found -o name 2>/dev/null)" ]; }
ns_there() { [ "$(k get namespace "play-$1" -o name 2>/dev/null)" = "namespace/play-$1" ]; }
play_ready() { "$MESH" play "$1" && wait_for 240 ready "$1"; }
U() { incus exec emesh-alice -- edge-mesh kubectl "$@"; }   # alice, on her own machine, with the kubeconfig `edge-mesh join` installed
B() { incus exec emesh-bob -- edge-mesh kubectl "$@"; }     # bob, the same on his
pod_yaml() { # pod_yaml <name> [extra spec lines] [extra labels] [agnhost args]: a pod that passes Pod Security "restricted" (non-root,
  # no privilege, RuntimeDefault). No sensitivity label: private, the default. With agnhost args it runs agnhost instead of pause.
  local img="$PAUSE_IMAGE" args=""; [ -z "${4:-}" ] || { img="$AGNHOST_IMAGE"; args=", args: [$4]"; }
  printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: %s, labels: {app: %s%s}}\nspec:\n  automountServiceAccountToken: false\n' "$1" "$1" "${3:-}"
  printf '  securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: RuntimeDefault}}\n%s' "${2:-}"
  printf '  containers:\n  - {name: c, image: %s%s, securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}}\n' "$img" "$args"
}
apply_as_alice() { incus exec emesh-alice -- edge-mesh kubectl apply -f -; }
token_exp() { sed -n 's/^ *token: //p' "$STATE/bundles/$1/kubeconfig" | python3 -c '
import base64, json, sys
p = sys.stdin.read().strip().split(".")[1]; print(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))["exp"])'; }

objects_made() { # play-alice and play-bob: labelled, Pod Security restricted, the quota (no NodePort, no LoadBalancer), the limits, the guard
  local n
  for n in alice bob; do
    [ "$(k get namespace "play-$n" -o jsonpath='{.metadata.labels.edge-mesh/participant} {.metadata.labels.pod-security\.kubernetes\.io/enforce}')" = "$n restricted" ] || return 1
    [ "$(k get resourcequota pool -n "play-$n" -o jsonpath='{.spec.hard.services\.nodeports} {.spec.hard.services\.loadbalancers} {.spec.hard.pods} {.spec.hard.count/deployments\.apps}')" = "0 0 10 5" ] || return 1
    k get limitrange pool -n "play-$n" -o name && k get rolebinding participant -n "play-$n" -o name || return 1
  done
  k get validatingadmissionpolicybinding edge-mesh-participant-guard -o name
}
credentials_apart() { # alice's kubeconfig: 0600, the API at the control plane's Nebula IP, the cluster's CA, a token and nothing of the node's
  local kc="$STATE/bundles/alice/kubeconfig" je="$STATE/bundles/alice/join.env" tok
  [ "$(stat -c %a "$kc")" = 600 ] && grep -qx "    server: https://$CP_NEB:6443" "$kc" || return 1
  [ "$(sed -n 's/^ *certificate-authority-data: //p' "$kc" | base64 -d | sha256sum)" = "$(cpx cat /var/lib/rancher/k3s/server/tls/server-ca.crt | sha256sum)" ] || return 1
  tok=$(sed -n 's/^ *token: //p' "$kc"); [ -n "$tok" ] || return 1
  ! grep -Eq 'client-certificate|client-key|K10|NODE_PASSWORD' "$kc" && ! grep -qF "$tok" "$je" &&
    ! grep -qF "$(sed -n 's/^K3S_TOKEN=//p' "$je")" "$kc" && ! grep -qF "$(sed -n 's/^NODE_PASSWORD=//p' "$je")" "$kc"
}
token_is_the_lease() { # the token's subject is play-alice's ServiceAccount and it expires when the lease does (TokenRequest, exactly the lease)
  sed -n 's/^ *token: //p' "$STATE/bundles/alice/kubeconfig" | python3 -c '
import base64, json, sys
p = sys.stdin.read().strip().split(".")[1]; c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
print(c["sub"], c["exp"] - c["iat"], c["exp"] - int(sys.argv[2]))
sys.exit(0 if c["sub"] == "system:serviceaccount:play-alice:user" and abs(c["exp"] - c["iat"] - int(sys.argv[1])) <= 10
         and abs(c["exp"] - int(sys.argv[2])) <= 10 else 1)' "$LEASE" "$(lease_of alice end)"; }
joined_with_kubeconfig() { play_ready alice && [ "$(incus exec emesh-alice -- stat -c %a /etc/edge-mesh/kubeconfig)" = 600 ] &&
  incus file push -q "$STATE/bundles/alice/kubeconfig" emesh-cp/root/alice.kubeconfig; }
who_is_who() { # the user is play-alice's ServiceAccount, not a node; the node's credential cannot act in her namespace; her kubeconfig also
  # works from another mesh machine (cp, the positive control of row 8)
  [ "$(U auth whoami -o jsonpath='{.status.userInfo.username}')" = system:serviceaccount:play-alice:user ] || return 1
  local o; o=$(incus exec emesh-alice -- k3s kubectl --kubeconfig /var/lib/rancher/k3s/agent/kubelet.kubeconfig create configmap x -n play-alice 2>&1) && return 1
  echo "$o"; echo "$o" | grep -q 'User "system:node:alice" cannot create resource "configmaps"' || return 1
  incus exec emesh-cp -- env KUBECONFIG=/root/alice.kubeconfig edge-mesh kubectl get configmaps >/dev/null
}
works_in_her_namespace() { # a pod with no sensitivity label (private, the default: pinned to her own node, it gets the LimitRange
  # defaults), a ClusterIP Service and a ConfigMap
  pod_yaml web | apply_as_alice && U create configmap notes --from-literal=a=b && U create service clusterip web --tcp=80:80 &&
  [ "$(U get pod web -o jsonpath='{.spec.containers[0].resources.limits.cpu} {.spec.containers[0].resources.requests.cpu} {.spec.nodeSelector.kubernetes\.io/hostname}')" = "200m 100m alice" ] &&
  wait_for 180 sh -c "incus exec emesh-alice -- edge-mesh kubectl get pod web -o jsonpath='{.spec.nodeName} {.status.phase}' | grep -qx 'alice Running'"; }
private_stays_home() { # golden rule 2 for players: a private pod aimed at bob's node is refused; a shareable one (its owner opted in,
  # golden rule 1: bob can see it) may run there; it goes again so that bob stays idle
  pod_yaml away $'  nodeSelector: {kubernetes.io/hostname: bob}\n' >"$STATE/away.yaml"
  row 1 "a private pod (not labelled edge-mesh/sensitivity=shareable) runs only on your own node" \
    sh -c "incus exec emesh-alice -- edge-mesh kubectl apply -f - <'$STATE/away.yaml'" || return 1
  pod_yaml lent $'  nodeSelector: {kubernetes.io/hostname: bob}\n' ', edge-mesh/sensitivity: shareable' | apply_as_alice &&
    wait_for 180 sh -c "incus exec emesh-alice -- edge-mesh kubectl get pod lent -o jsonpath='{.spec.nodeName} {.status.phase}' | grep -qx 'bob Running'" &&
    U delete pod lent --wait=true; }
isolated() { # the NetworkPolicy: alice's pods reach each other by name (the DNS works), not bob's; the test pods go again
  { pod_yaml srv "" "" "netexec, --http-port=8080" | apply_as_alice && pod_yaml cli "" "" "pause" | apply_as_alice && U expose pod srv --port 8080 &&
    pod_yaml srv "" "" "netexec, --http-port=8080" | incus exec emesh-bob -- edge-mesh kubectl apply -f - && B expose pod srv --port 8080 &&
    U wait --for=condition=Ready pod/srv pod/cli --timeout=180s && B wait --for=condition=Ready pod/srv --timeout=180s; } || return 1
  U exec cli -- /agnhost connect srv:8080 --timeout=5s || return 1   # positive control: the same namespace
  local o; o=$(U exec cli -- /agnhost connect srv.play-bob.svc.cluster.local:8080 --timeout=5s 2>&1) && return 1
  echo "$o"; echo "$o" | grep -Eq 'REFUSED|TIMEOUT' || return 1
  U delete pod srv cli --wait=true && U delete service srv && B delete pod srv --wait=true && B delete service srv; }
outside_refused() { # row 9: another player's namespace, the default one
  row 13 "edge-mesh: forbidden (403): you may only work in your namespace play-alice" U get pods -n play-bob &&
  row 13 "forbidden (403)" U create configmap x -n play-bob && row 13 "forbidden (403)" U create configmap x -n default; }
cluster_refused() { # no secret anywhere (cluster-wide, kube-system, her own), no node
  row 13 'cannot list resource "secrets" in API group "" at the cluster scope' U get secrets -A &&
  row 13 "forbidden (403)" U get secrets -n kube-system && row 13 "forbidden (403)" U get secrets && row 13 'cannot list resource "nodes"' U get nodes; }
own_limits_hold() { # she cannot lift her own quota, bind herself more rights, mint tokens or relabel her namespace
  row 13 "forbidden (403)" U delete resourcequota pool && row 13 "forbidden (403)" U create rolebinding more --clusterrole=admin --serviceaccount=play-alice:user &&
  row 13 "forbidden (403)" U create token user && row 13 "forbidden (403)" U label namespace play-alice pod-security.kubernetes.io/enforce=privileged --overwrite; }
over_quota() { # row 11: two containers of 500m on top of web's 200m exceed the 1000m of CPU
  local sc='securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}'
  cat >"$STATE/big.yaml" <<EOF
apiVersion: v1
kind: Pod
metadata: {name: big}
spec:
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: RuntimeDefault}}
  containers:
  - {name: one, image: $PAUSE_IMAGE, resources: {limits: {cpu: 500m}}, $sc}
  - {name: two, image: $PAUSE_IMAGE, resources: {limits: {cpu: 500m}}, $sc}
EOF
  row 14 "edge-mesh: exceeded quota in play-alice: lower the requests or delete something" sh -c "incus exec emesh-alice -- edge-mesh kubectl apply -f - <'$STATE/big.yaml'" &&
    ! U get pod big; }
policies_refuse() { # Pod Security (privileged), the participant guard (NodePort, externalIPs, a pod pinned to the control plane): kubectl's message
  local t="$STATE/deny.yaml"
  pod_yaml root | sed 's#securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: \[ALL\]}}#securityContext: {privileged: true}#' >"$t"
  row 1 'violates PodSecurity "restricted:latest"' sh -c "incus exec emesh-alice -- edge-mesh kubectl apply -f - <'$t'" || return 1
  row 1 "a participant's Service must be ClusterIP" U create service nodeport np --tcp=80:80 || return 1
  printf 'apiVersion: v1\nkind: Service\nmetadata: {name: ext}\nspec:\n  ports: [{port: 80}]\n  externalIPs: [%s]\n' "$CP_NEB" >"$t"
  row 1 "a participant's Service may not set externalIPs" sh -c "incus exec emesh-alice -- edge-mesh kubectl apply -f - <'$t'" || return 1
  pod_yaml pinned $'  nodeName: cp\n' >"$t"
  row 1 "a participant's pod may not set spec.nodeName" sh -c "incus exec emesh-alice -- edge-mesh kubectl apply -f - <'$t'"
}
other_ca() { # row 7: the same kubeconfig with another CA (k3s's client CA): the API's certificate does not verify
  python3 - "$STATE/bundles/alice/kubeconfig" "$STATE/wrong-ca.kubeconfig" "$(cpx cat /var/lib/rancher/k3s/server/tls/client-ca.crt | base64 -w0)" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
open(sys.argv[2], "w").write(re.sub(r"certificate-authority-data: \S+", "certificate-authority-data: " + sys.argv[3], s))
PY
  incus file push -q "$STATE/wrong-ca.kubeconfig" emesh-alice/root/wrong-ca.kubeconfig &&
  row 11 "edge-mesh: the API's certificate is not the one your bundle pins (x509)" incus exec emesh-alice -- env KUBECONFIG=/root/wrong-ca.kubeconfig edge-mesh kubectl get pods; }
nebula_down() { # row 6: Nebula stops on alice's machine; then it starts again and her node and kubeconfig work again
  incus exec emesh-alice -- sh -c 'kill $(pidof nebula)'; wait_for 15 sh -c '! incus exec emesh-alice -- ip -4 addr show dev nebula1 | grep -q inet'
  row 10 "edge-mesh: Nebula is down (nebula1 has no address), so the cluster cannot be reached; run: sudo edge-mesh join" U get pods; local r=$?
  incus exec emesh-alice -- sh -c '(nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &)'
  wait_for 120 U get pods && wait_for 180 ready alice && [ "$r" = 0 ]; }
deferred_while_active() { out_has "(active-lease), position 2 of 2 [added]" "$MESH" host request alice-2 --ip 198.51.100.10; }
rate_limited() { # identity 198.51.100.77: five requests in the window (a repeat counts), the sixth refused and not listed; then taken off the list
  local i; for i in 1 2 3 4 5; do "$MESH" host request eve --ip 198.51.100.77 >/dev/null || return 1; done
  row 6 "eve-2: refused: too many requests from this identity (5 in 600 s)" "$MESH" host request eve-2 --ip 198.51.100.77 &&
  ! "$MESH" host waitlist | grep -q eve-2 && "$MESH" host reject eve; }
xff() { # the header is believed only from the trusted proxy (127.0.0.1 in pool.yaml); from anyone else the socket address is the identity
  "$MESH" host request mallory --peer 203.0.113.50 --xff 198.51.100.66 && "$MESH" host request trent --peer 127.0.0.1 --xff 198.51.100.60 || return 1
  local w; w=$("$MESH" host waitlist); echo "$w"
  echo "$w" | grep -Eq '^[0-9]+ +mallory +[a-z]+ +[^ ]+ +203\.0\.113\.50' && echo "$w" | grep -Eq '^[0-9]+ +trent +[a-z]+ +[^ ]+ +198\.51\.100\.60' &&
    ! echo "$w" | grep -q 198.51.100.66 && "$MESH" host reject mallory && "$MESH" host reject trent; }
idle_then_reuse() { # bob joined but runs nothing: once the idle time has passed one tick expels him (alice, Ready with a pod, stays) and
  # carol takes the place
  sleep_until $(( $(lease_of alice start) + IDLE + 2 ))
  out_has "tick: auto on | expelled 1 | approved 1 | slots 2/2 used | 1 in the list (0 eligible now)" tick || return 1
  python3 - "$ADM/events.jsonl" <<'PY' || return 1
import json, sys
ev = [json.loads(l) for l in open(sys.argv[1])]
ex = [(e["name"], e.get("how")) for e in ev if e["type"] == "expelled"]
order = [(e["type"], e["name"]) for e in ev]
sys.exit(0 if ex == [("bob", "idle")] and order.index(("expelled", "bob")) < order.index(("approved", "carol")) else 1)
PY
  ns_gone bob && ! machine_running bob && ns_there carol && "$MESH" host leases | grep -q '^carol ' && "$MESH" host leases | grep -q '^alice ' &&
    incus file push -q "$STATE/bundles/carol/kubeconfig" emesh-cp/root/carol.kubeconfig &&
    incus exec emesh-cp -- env KUBECONFIG=/root/carol.kubeconfig edge-mesh kubectl get pods; }   # positive control for PL21
token_dies_at_kick() { # carol's lease ended by hand: her token stops working at once, although its own expiry is still far away
  out_has "reaped carol: kicked, place freed" "$MESH" host reap --name carol && ns_gone carol || return 1
  local exp; exp=$(token_exp carol); echo "exp in $(( exp - $(date +%s) )) s"; [ "$exp" -gt $(( $(date +%s) + 300 )) ] || return 1
  row 12 "edge-mesh: 401 Unauthorized: your access ended with your lease" incus exec emesh-cp -- env KUBECONFIG=/root/carol.kubeconfig edge-mesh kubectl get pods; }
held_while_active() { # a place is free, but alice-2 (alice's address) is not admitted while alice's lease runs
  row 6 "alice-2: not eligible until" "$MESH" host approve alice-2 || return 1
  out_has "tick: auto on | expelled 0 | approved 0 | slots 1/2 used | 1 in the list (0 eligible now)" tick || return 1
  local a; a=$(digest); out_has "expelled 0 | approved 0" tick && [ "$a" = "$(digest)" ]; }   # the idle bookkeeping writes only changes
lease_ends() { # one tick at the end: alice's node, namespace and token gone, her certificate blocklisted and expired, her bundle out of the outbox
  sleep_until $(( $(lease_of alice end) + 2 ))
  out_has "tick: auto on | expelled 1 | approved 0 | slots 0/2 used | 1 in the list (0 eligible now)" tick || return 1
  local fp; fp=$("$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])')
  ! k get node alice && ns_gone alice && ! machine_running alice && grep -qx "$fp" "$STATE/blocklist" && [ ! -e "$ADM/outbox/alice.tar.gz" ] &&
    "$CACHE/nebula-cert" print -json -path "$STATE/bundles/alice/host.crt" | python3 -c '
import sys, json, datetime as d
n = json.load(sys.stdin)[0]["details"]["notAfter"]
sys.exit(0 if d.datetime.fromisoformat(n.replace("Z", "+00:00")) <= d.datetime.now(d.timezone.utc) else 1)'; }
token_ended() { row 12 "edge-mesh: 401 Unauthorized: your access ended with your lease" incus exec emesh-cp -- env KUBECONFIG=/root/alice.kubeconfig edge-mesh kubectl get pods; }
cooldown_then_reuse() { # the cooldown holds alice-2 back for its length; then one tick gives her the freed place and a namespace of her own
  out_has "approved 0 | slots 0/2 used | 1 in the list (0 eligible now)" tick || return 1
  local until; until=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pool"]["cooldown"]["198.51.100.10"])' "$ADM/state.json")
  sleep_until $(( until + 2 ))
  out_has "tick: auto on | expelled 0 | approved 1 | slots 1/2 used | 0 in the list" tick && ns_there alice-2; }
all_gone() { out_has "reaped 1" "$MESH" host reap --all && ns_gone alice-2 && [ -z "$(k get namespace -o name | grep '^namespace/play-')" ] &&
  [ -z "$(k get serviceaccount -A --no-headers | awk '$1 ~ /^play-/')" ]; }

step PL01 "mesh host init: lighthouse and control plane" "$MESH" host init
step PL02 "alice, bob and carol ask; auto on" sh -c "'$MESH' host request alice --ip 198.51.100.10 && '$MESH' host request bob --ip 198.51.100.20 &&
  '$MESH' host request carol --ip 198.51.100.30 | grep -q 'position 3 of 3' && '$MESH' host auto on"
step PL03 "one tick approves alice and bob; carol, over capacity, waits in the FIFO list" out_has "tick: auto on | expelled 0 | approved 2 | slots 2/2 used | 1 in the list (1 eligible now)" tick
step PL04 "each approved player has play-<name>: labelled, Pod Security restricted, quota (no NodePort/LB), limits, RBAC, the participant guard" objects_made
step PL05 "alice's kubeconfig: 0600, the API on the Nebula IP, the cluster CA pinned, a token; no node credential in it, no user token in join.env" credentials_apart
step PL06 "the token is play-alice's ServiceAccount and expires exactly with the lease ($LEASE s)" token_is_the_lease
step PL07 "alice joins and is Ready; edge-mesh installed her kubeconfig (0600)" joined_with_kubeconfig
step PL07b "bob joins with his bundle and is Ready" play_ready bob
step PL08 "alice is a ServiceAccount, not a node; the node's credential cannot act in her namespace; her kubeconfig works over the mesh" who_is_who
step PL09 "alice runs an unlabelled pod: private, pinned to her own node, LimitRange defaults; a ClusterIP Service and a ConfigMap" works_in_her_namespace
step PL09b "golden rule 2: her private pod aimed at bob's node is refused; a shareable one may run there (rule 1)" private_stays_home
step PL10 "row 9: another player's namespace or default: exit 13, 'forbidden (403): you may only work in your namespace'" outside_refused
step PL11 "no secret (cluster-wide, kube-system, her own) and no node: exit 13" cluster_refused
step PL12 "she cannot delete her quota, bind herself more rights, mint a token or relabel her namespace: exit 13" own_limits_hold
step PL13 "row 11: a pod over the CPU quota: exit 14, 'exceeded quota'" over_quota
step PL13b "NetworkPolicy: alice's pods reach each other by name, not bob's pods" isolated
step PL14 "a privileged pod, a NodePort, externalIPs and a pod pinned to the control plane are refused with the policy's message" policies_refuse
step PL15 "row 7: her kubeconfig with another CA: exit 11, 'x509'" other_ca
step PL16 "row 6: Nebula stopped on her machine: exit 10, 'Nebula is down'; started again, she works again" nebula_down
step PL17 "a second request from alice's address while her lease is active is deferred (active-lease)" deferred_while_active
step PL18 "rate limit: the sixth request of one identity in 10 minutes is refused (exit 6) and not listed" rate_limited
step PL19 "X-Forwarded-For is believed from the trusted proxy only; from anyone else the socket address counts" xff
step PL20 "idle kick: bob runs nothing; after $IDLE s one tick expels him (his namespace too), keeps alice, and carol takes the place" idle_then_reuse
step PL21 "carol's lease ended by hand: her token stops working at once (401, exit 12), long before its own expiry" token_dies_at_kick
step PL21b "a place is free, but alice-2 is held (approve exit 6, the tick admits nobody); a quiet tick changes nothing" held_while_active
step PL22 "alice's lease ends: one tick removes her node and namespace, her certificate is blocklisted and expired, the outbox is empty" lease_ends
step PL23 "row 8: her kubeconfig after the lease: exit 12, '401 Unauthorized: your access ended with your lease'" token_ended
step PL24 "the cooldown holds alice-2; then one tick gives her the freed place and play-alice-2" cooldown_then_reuse
step PL25 "reap --all: no participant namespace or ServiceAccount is left" all_gone

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
