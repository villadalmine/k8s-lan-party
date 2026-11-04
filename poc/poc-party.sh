#!/usr/bin/env bash
# make poc-party: LAN party mode, part A of issue #75 (DESIGN.md section 14.2), end to end on Incus VMs. Three players get a place from the
# pool; alice and bob join, carol works from her kubeconfig only (her shareable pods run on alice's node). Each namespace runs a server on the
# party's port (8080), another on a port the party does not declare (9090) and a client. Without consent nothing crosses; with one side's
# consent nothing crosses; with both, the tick opens 8080 both ways and nothing else, and carol reaches neither of them nor they her. The
# players cannot touch the party's NetworkPolicy, their namespace labels or each other's namespace, and their private pods stay home. leave,
# the host's off switch, the host's end, a kick and a lease end each close the party; a reconcile run twice changes nothing.
# Needs a Linux host with Incus. PASS/FAIL per step; identical across runs. POOL_LEASE (default 1500 s) must cover PY02-PY15. KEEP=1 leaves the VMs.
set -u
export RUNTIME=incus MAX_VMS=4 CNI="${CNI:-flannel}" TAINT=no   # TAINT pinned as poc-pool (issue #59); CNI=cilium runs it on Cilium
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-party.log"; : >"$LOG"
MESH="$POC/mesh"
LEASE="${POOL_LEASE:-1500}"
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^slots: [0-9]*/slots: 3/" -e "s/^idle_kick_seconds: [0-9]*/idle_kick_seconds: 0/" \
  -e "s#^party_ports: .*#party_ports: [8080/TCP]#" "$POC/pool.yaml" >"$POOL_CONFIG"
ADM="$STATE/admission"
unset ADMISSION_NOTIFY_CMD ADMISSION_NOTIFY_IP

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
row() { local want="$1" msg="$2" o rc; shift 2; o=$("$@" 2>&1); rc=$?; echo "$o"; echo "exit $rc"; [ "$rc" -eq "$want" ] && echo "$o" | grep -qF -- "$msg"; }
tick() { "$MESH" host auto --once; }
lease_of() { python3 -c 'import json,sys; print(next(l[sys.argv[3]] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == sys.argv[2]))' "$ADM/state.json" "$1" "$2"; }
sleep_until() { local s=$(( $1 - $(date +%s) )); [ "$s" -le 0 ] || sleep "$s"; }
P() { # P <player> <edge-mesh arguments...>: the player's own command on their machine; carol has none, she uses her kubeconfig from cp
  local who="$1"; shift
  if [ "$who" = carol ]; then incus exec emesh-cp -- env KUBECONFIG=/root/carol.kubeconfig edge-mesh "$@"; else incus exec "emesh-$who" -- edge-mesh "$@"; fi; }
K() { local who="$1"; shift; P "$who" kubectl "$@"; }
at() { echo "$1.play-$2.svc.cluster.local:$3"; }   # at <service> <player> <port>
can() { K "$1" exec cli -- /agnhost connect "$2" --timeout=3s; }
cannot() { local o; o=$(K "$1" exec cli -- /agnhost connect "$2" --timeout=3s 2>&1) && return 1; echo "$o"; echo "$o" | grep -Eq 'REFUSED|TIMEOUT'; }
opens() { wait_for 60 can "$@"; }
closes() { wait_for 60 cannot "$@"; }
consent() { "$MESH" party "$1" "$2" --as "$3"; }   # consent invite|accept <peer> <player>
consents_of() { k get configmaps -n "play-$1" -l edge-mesh/party-consent -o name 2>/dev/null; }
party_objects() { # every object a party leaves: its NetworkPolicies, the namespace labels, the consents
  k get networkpolicies -A -l edge-mesh/party -o name; k get configmaps -A -l edge-mesh/party-consent -o name
  k get namespaces -o json | python3 -c 'import json,sys; [print(n["metadata"]["name"], k) for n in json.load(sys.stdin)["items"] for k in (n["metadata"].get("labels") or {}) if k.startswith("party.edge-mesh/")]'; }
no_party() { [ -z "$(party_objects 2>/dev/null)" ]; }
pod_yaml() { # pod_yaml <name> <agnhost args> [extra spec lines] [extra labels]: passes Pod Security "restricted"; unlabelled = private
  printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: %s, labels: {app: %s%s}}\nspec:\n  automountServiceAccountToken: false\n' "$1" "$1" "${4:-}"
  printf '  securityContext: {runAsNonRoot: true, runAsUser: 65534, seccompProfile: {type: RuntimeDefault}}\n%s' "${3:-}"
  printf '  containers:\n  - {name: c, image: %s, args: [%s], securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}}\n' "$AGNHOST_IMAGE" "$2"; }

admitted() { "$MESH" host request alice --ip 198.51.100.10 && "$MESH" host request bob --ip 198.51.100.20 &&
  "$MESH" host request carol --ip 198.51.100.30 && "$MESH" host auto on && out_has "approved 3 | slots 3/3 used" tick; }
joined() { "$MESH" play alice && "$MESH" play bob && wait_for 240 ready alice && wait_for 240 ready bob &&
  incus file push -q "$STATE/bundles/carol/kubeconfig" emesh-cp/root/carol.kubeconfig; }
servers() { # srv (8080, the party's port) and alt (9090, not declared) with their Services and a client, in each namespace
  local who home="" lab="" aff
  aff=$'  affinity: {nodeAffinity: {requiredDuringSchedulingIgnoredDuringExecution: {nodeSelectorTerms: [{matchFields: [{key: metadata.name, operator: In, values: [alice]}]}]}}}\n'
  for who in alice bob carol; do
    [ "$who" != carol ] || { home="$aff"; lab=", edge-mesh/sensitivity: shareable"; }
    { pod_yaml srv "netexec, --http-port=8080" "$home" "$lab"; echo ---; pod_yaml alt "netexec, --http-port=9090" "$home" "$lab"; echo ---
      pod_yaml cli "pause" "$home" "$lab"; } | P "$who" kubectl apply -f - || return 1
    K "$who" expose pod srv --port 8080 && K "$who" expose pod alt --port 9090 || return 1
  done
  for who in alice bob carol; do K "$who" wait --for=condition=Ready pod/srv pod/alt pod/cli --timeout=240s || return 1; done
  for who in alice bob carol; do opens "$who" "$(at srv "$who" 8080)" && opens "$who" "$(at alt "$who" 9090)" || return 1; done; }
closed_both_ways() { closes alice "$(at srv bob 8080)" && closes bob "$(at srv alice 8080)"; }
one_side() { out_has "your consent is recorded" consent invite bob alice && tick && cannot alice "$(at srv bob 8080)" &&
  cannot bob "$(at srv alice 8080)" && out_has "waiting for bob to accept" "$MESH" party list --as alice &&
  [ -z "$(k get networkpolicies -A -l edge-mesh/party -o name)" ]; }
both_sides() { consent accept alice bob && out_has "party: alice -> bob: active: ports 8080/TCP" tick &&
  opens alice "$(at srv bob 8080)" && opens bob "$(at srv alice 8080)" && out_has "active: ports 8080/TCP" "$MESH" party list --as bob; }
declared_port_only() { cannot alice "$(at alt bob 9090)" && cannot bob "$(at alt alice 9090)" && can bob "$(at alt bob 9090)"; }
third_out() { # carol reaches neither, neither reaches her; her consent alone (alice never agreed) opens nothing
  cannot carol "$(at srv alice 8080)" && cannot carol "$(at srv bob 8080)" && cannot alice "$(at srv carol 8080)" &&
  cannot bob "$(at srv carol 8080)" || return 1
  P carol party invite alice && tick && out_has "waiting for alice to accept" P carol party list &&
    cannot carol "$(at srv alice 8080)" && cannot alice "$(at srv carol 8080)" && P carol party leave && tick; }
rules_hold() { # no RBAC on the other namespace, on NetworkPolicies or on namespace labels; private pods stay on their owner's node
  row 13 "forbidden (403)" K alice get pods -n play-bob && row 13 "forbidden (403)" K alice get networkpolicies &&
  row 13 "forbidden (403)" K alice delete networkpolicy party-bob && row 13 "forbidden (403)" K alice label namespace play-alice party.edge-mesh/carol=yes &&
  row 13 "forbidden (403)" K alice label namespace play-bob party.edge-mesh/carol=yes --overwrite || return 1
  [ "$(K alice get pod cli -o jsonpath='{.spec.nodeName}')" = alice ] && [ "$(K bob get pod cli -o jsonpath='{.spec.nodeName}')" = bob ]; }
rv() { k get networkpolicies -A -l edge-mesh/party -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}={.metadata.resourceVersion} {end}'; }
idempotent() { local a b; a=$(rv); b=$(party_objects | sort); [ -n "$a" ] && out_has "party: unchanged" "$MESH" host party reconcile &&
  out_has "party: unchanged" "$MESH" host party reconcile && tick && [ "$a" = "$(rv)" ] && [ "$b" = "$(party_objects | sort)" ]; }
leave_closes() { consent leave alice bob && out_has "party: alice -> bob: closed: bob left the party" tick && closed_both_ways &&
  [ -z "$(consents_of alice)" ] && no_party; }
reopen() { consent invite bob alice && consent accept alice bob && tick && opens alice "$(at srv bob 8080)" && opens bob "$(at srv alice 8080)"; }
switch_off() { sed -i 's/^parties: 1/parties: 0/' "$POOL_CONFIG"; }
switch_on() { sed -i 's/^parties: 0/parties: 1/' "$POOL_CONFIG"; }
off_closes() { reopen && switch_off && tick && closed_both_ways && out_has "off: the host has turned parties off" "$MESH" host party list &&
  [ -z "$(k get networkpolicies -A -l edge-mesh/party -o name)" ] && switch_on && tick && opens alice "$(at srv bob 8080)"; }
end_closes() { out_has "party alice+bob: ended by the host" "$MESH" host party end alice bob && closed_both_ways && no_party; }
kick_closes() { reopen && "$MESH" host reap --name bob && [ -z "$(consents_of alice)" ] && no_party; }
lease_closes() { # alice and carol: active; at the lease end the reconcile alone closes it, before the tick removes the namespaces
  consent invite carol alice && P carol party accept alice && tick && opens alice "$(at srv carol 8080)" && opens carol "$(at srv alice 8080)" || return 1
  sleep_until $(( $(lease_of alice end) + 2 ))
  out_has "closed: a lease ended" "$MESH" host party reconcile && no_party && k get namespace play-alice play-carol; }
all_gone() { out_has "expelled 2" tick && no_party && out_has "party: unchanged" "$MESH" host party reconcile; }

step PY01 "mesh host init: lighthouse and control plane" "$MESH" host init
step PY02 "alice, bob and carol ask; one tick admits the three" admitted
step PY03 "alice and bob join and are Ready; carol uses her kubeconfig from the control plane" joined
step PY04 "each namespace: a server on 8080 (the party's port), one on 9090, a client; each client reaches its own two" servers
step PY05 "no consent: alice and bob do not reach each other" closed_both_ways
step PY06 "alice invites bob: one consent opens nothing; she sees 'waiting for bob to accept'; no party NetworkPolicy" one_side
step PY07 "bob accepts: the tick opens the party; alice and bob reach each other on 8080, both ways" both_sides
step PY08 "only the declared port: 9090 stays closed between them (it answers inside its own namespace)" declared_port_only
step PY09 "carol, a third player, reaches neither and neither reaches her; her consent alone opens nothing" third_out
step PY10 "the golden rules hold: no access to bob's namespace, NetworkPolicies or namespace labels; private pods stay home" rules_hold
step PY11 "idempotent: reconcile twice says unchanged, a tick changes nothing, the policies keep their resourceVersion" idempotent
step PY12 "bob leaves: the tick closes the party both ways and deletes alice's consent" leave_closes
step PY13 "parties: 0 in pool.yaml closes an open party and keeps the consents (off); parties: 1 reopens it" off_closes
step PY14 "the host ends the party: both consents gone, closed both ways" end_closes
step PY15 "a kick (reap --name bob) closes the party at once: alice's consent, the policy and the labels are gone" kick_closes
step PY16 "alice and carol play; at the lease end the reconcile closes it before the namespaces go" lease_closes
step PY17 "the tick expels both; no party object is left; reconcile: unchanged" all_gone

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
