#!/usr/bin/env bash
# make poc-placement: phase C, the golden rule. Trust-based placement with Kyverno (DESIGN.md section 4): private workloads only on the
# owner's (`own`) nodes, shareable workloads may go to shared nodes if their owner opted in, and a private pod on someone else's node is
# REJECTED. Nodes: cp = own (schedulable for this test: the maintainer's machine), n1 = friends, n2 = public, both tainted at registration.
# Same runtimes as poc-join (RUNTIME=docker|incus). Output: one PASS/FAIL per step, identical across runs.
set -u
export CNI=flannel TAINT=yes
. "$(dirname "$0")/lib.sh"
"$POC/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"; : >"$LOG"
NS=lab
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
bring_up() { "$UP" && dx cp ping -c1 -W2 "$LH_NEB"; }
tainted() { # tainted <node> <level>
  [ "$(k get node "$1" -o jsonpath="{.spec.taints[?(@.key=='trust')].value}")" = "$2" ] \
    && [ "$(k get node "$1" -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = "$2" ]
}
own_cp() { k taint node cp node-role.kubernetes.io/control-plane:NoSchedule- >/dev/null 2>&1; k create namespace "$NS" >/dev/null 2>&1; true; }

# Kyverno 1.19: only the admission controller (and the background one the chart insists on) on the own node; no reports/cleanup (RAM)
KSEL='{"node-restriction.kubernetes.io/trust":"own"}'
KTOL='[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"}]'
kyverno_up() {
  local a=(--set reportsController.enabled=false --set cleanupController.enabled=false)
  local c; for c in admissionController backgroundController; do
    a+=(--set "$c.replicas=1" --set-json "$c.nodeSelector=$KSEL" --set-json "$c.tolerations=$KTOL")
  done
  helm_cp upgrade --install kyverno kyverno --repo https://kyverno.github.io/kyverno/ --version "$KYVERNO_CHART" -n kyverno --create-namespace \
    "${a[@]}" --wait --timeout 8m
}
kyverno_on_own() { local n; for n in $(k -n kyverno get pods -o jsonpath='{.items[*].spec.nodeName}'); do [ "$n" = cp ] || return 1; done; [ -n "$n" ]; }
policies_up() {
  k apply -f - <"$POC/placement/trust-placement.yaml" && k apply -f - <"$POC/placement/node-trust-vap.yaml" || return 1
  wait_for 120 cpol_ready
}
cpol_ready() { [ "$(k get cpol edge-mesh-placement -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ]; }
nodes_joined() { k get node n1 n2; }

# pod <name> <sensitivity|-> <share-with|-> [extra spec lines, already indented by 2]
pod() {
  local labels=""
  [ "$2" != - ] && labels="$labels
    edge-mesh/sensitivity: $2"
  [ "$3" != - ] && labels="$labels
    edge-mesh/share-with: $3"
  [ -z "$labels" ] && labels=" {}" || labels="$labels"
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $NS
  labels:$labels
spec:
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 1
  containers:
  - name: c
    image: busybox:1.36
    command: ["sleep","3600"]
    resources: {limits: {cpu: 50m, memory: 32Mi}}
${4:-}
YAML
}
# Kyverno admission is eventually consistent for a few seconds after the policy is Ready: retry the apply until it answers
reject() { # reject <message fragment>: the manifest on stdin must be refused, and the message must name the rule
  local out; out=$(cat >"$STATE/m.yaml"; k apply -f - <"$STATE/m.yaml" 2>&1); local rc=$?
  echo "$out"; [ $rc -ne 0 ] && echo "$out" | grep -q "$1"
}
accept() { k apply -f - >/dev/null; }
on() { [ "$(k -n "$NS" get pod "$1" -o jsonpath='{.spec.nodeName}' 2>/dev/null)" = "$2" ] && [ "$(k -n "$NS" get pod "$1" -o jsonpath='{.status.phase}')" = Running ]; }
stays_pending() { sleep 20; [ "$(k -n "$NS" get pod "$1" -o jsonpath='{.status.phase}')" = Pending ] && k -n "$NS" get pod "$1" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}' | grep -q Unschedulable; }
absent() { ! k -n "$NS" get pod "$1" >/dev/null 2>&1; }

# ---- private ----
t_private_default() { pod p-default - - | accept && wait_for 90 on p-default cp && [ "$(k -n $NS get pod p-default -o jsonpath='{.spec.nodeSelector.node-restriction\.kubernetes\.io/trust}')" = own ]; }
t_private_nodename_friends() { pod p-foreign1 private - '  nodeName: n1' | reject "may only run on"; }
t_private_nodename_public() { pod p-foreign2 - - '  nodeName: n2' | reject "may only run on"; }
t_private_wants_friends() { pod p-wants private - '  nodeSelector: {node-restriction.kubernetes.io/trust: friends}' | accept && wait_for 90 on p-wants cp; }
t_private_toleration_key() { pod p-tol private - '  tolerations: [{key: trust, operator: Equal, value: friends, effect: NoSchedule}]' | reject "cannot tolerate"; }
t_private_toleration_all() { pod p-tolall - - '  tolerations: [{operator: Exists}]' | reject "cannot tolerate"; }
t_label_value() { pod p-badlabel maybe - | reject "must be"; }
t_deployment() {
  k apply -f - <<YAML >/dev/null
apiVersion: apps/v1
kind: Deployment
metadata: {name: dep, namespace: $NS}
spec:
  replicas: 3
  selector: {matchLabels: {app: dep}}
  template:
    metadata: {labels: {app: dep}}
    spec:
      terminationGracePeriodSeconds: 1
      containers: [{name: c, image: busybox:1.36, command: ["sleep","3600"]}]
YAML
  wait_for 120 dep_ready || return 1
  [ "$(k -n $NS get pods -l app=dep -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | tr '\n' ' ')" = "cp " ]
}
dep_ready() { [ "$(k -n $NS get deploy dep -o jsonpath='{.status.readyReplicas}')" = 3 ]; }
# ---- shareable ----
t_share_friends() { pod s-friends shareable friends '  nodeSelector: {node-restriction.kubernetes.io/trust: friends}' | accept && wait_for 120 on s-friends n1; }
t_share_public() { pod s-public shareable public '  nodeSelector: {node-restriction.kubernetes.io/trust: public}' | accept && wait_for 120 on s-public n2; }
t_share_friends_not_public() { pod s-fnp shareable friends '  nodeSelector: {node-restriction.kubernetes.io/trust: public}' | accept && stays_pending s-fnp; }
t_share_no_optin() { pod s-noopt shareable - '  nodeSelector: {node-restriction.kubernetes.io/trust: friends}' | accept && stays_pending s-noopt; }
t_share_hostpath() { pod s-hp shareable friends '  volumes: [{name: h, hostPath: {path: /etc}}]' | reject "hostPath"; }
t_share_privileged() { pod s-priv shareable friends '' | sed 's#^    resources:#    securityContext: {privileged: true}\n    resources:#' | reject "privileged"; }
t_share_secret() { pod s-sec shareable friends '' | sed 's#^    resources:#    env: [{name: X, valueFrom: {secretKeyRef: {name: s, key: k}}}]\n    resources:#' | reject "Secrets"; }
t_share_hostnet() { pod s-hn shareable friends '  hostNetwork: true' | reject "host network"; }
t_share_nolimits() { pod s-nl shareable friends '' | sed '/resources:/d' | reject "limits"; }
t_share_token() { pod s-tok shareable friends '' | sed 's#automountServiceAccountToken: false#automountServiceAccountToken: true#' | reject "Secrets"; }
# ---- the node side ----
t_label_without_taint() { ! k label node cp node-restriction.kubernetes.io/trust=friends --overwrite 2>"$STATE/vap.err" && grep -q "must carry the taint" "$STATE/vap.err"; }
t_taint_removal() { ! k taint node n1 trust=friends:NoSchedule- 2>"$STATE/vap2.err" && grep -q "must carry the taint" "$STATE/vap2.err" && tainted n1 friends; }
t_node_cannot_upgrade() { # the node's OWN credential (the kubelet's) tries to raise its trust label: NodeRestriction refuses
  local out; out=$(dx n1 sh -c 'if command -v kubectl >/dev/null; then K=kubectl; else K="k3s kubectl"; fi; $K --kubeconfig /var/lib/rancher/k3s/agent/kubelet.kubeconfig label node n1 node-restriction.kubernetes.io/trust=own --overwrite' 2>&1); local rc=$?
  echo "$out"; [ $rc -ne 0 ] && echo "$out" | grep -qiE "forbidden|cannot set|not allowed" && tainted n1 friends
}
t_cp_still_own() { [ "$(k get node cp -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}')" = own ]; }

step L01 "cluster up: cp (own), n1 and n2 joined with the trust taint" bring_up
step L02 "n1 and n2 registered, Ready" wait_for 180 nodes_joined
wait_for 120 ready n1; wait_for 120 ready n2
step L03a "n1: label and taint = friends" tainted n1 friends
step L03b "n2: label and taint = public" tainted n2 public
step L04 "cp is the own worker for the test (control-plane taint removed)" own_cp
step L05 "Kyverno $KYVERNO_VERSION installed on the own node (helm --wait)" kyverno_up
step L06 "Kyverno pods run on cp (own)" kyverno_on_own
step L07 "placement policy and node-trust admission policy applied, policy Ready" policies_up
sleep 10
step L10 "private (no label) pod: admitted, forced to nodeSelector trust=own, runs on cp" t_private_default
step L11 "private pod asking for a friends node by nodeSelector ends up on cp, not on the friends node" t_private_wants_friends
step L12 "private Deployment (3 replicas, no label): every pod on cp" t_deployment
step L20 "NEGATIVE: private pod with nodeName on a friends node is REJECTED" t_private_nodename_friends
step L21 "NEGATIVE: unlabelled pod (private by default) with nodeName on a public node is REJECTED" t_private_nodename_public
step L22 "NEGATIVE: private pod tolerating the trust taint is REJECTED" t_private_toleration_key
step L23 "NEGATIVE: unlabelled pod with an Exists toleration (tolerates everything) is REJECTED" t_private_toleration_all
step L24 "NEGATIVE: an unknown sensitivity value is REJECTED" t_label_value
step L30 "shareable pod opted in to friends runs on the friends node (n1)" t_share_friends
step L31 "shareable pod opted in to public runs on the public node (n2)" t_share_public
step L32 "shareable pod opted in to friends only does NOT run on the public node (stays Pending)" t_share_friends_not_public
step L33 "shareable pod without opt-in does NOT run on a shared node (stays Pending)" t_share_no_optin
step L40 "NEGATIVE: shareable pod with hostPath is REJECTED" t_share_hostpath
step L41 "NEGATIVE: shareable privileged pod is REJECTED" t_share_privileged
step L42 "NEGATIVE: shareable pod with secretKeyRef is REJECTED" t_share_secret
step L43 "NEGATIVE: shareable pod on the host network is REJECTED" t_share_hostnet
step L44 "NEGATIVE: shareable pod without resource limits is REJECTED" t_share_nolimits
step L45 "NEGATIVE: shareable pod that mounts the service account token is REJECTED" t_share_token
step L50 "NEGATIVE: labelling cp as friends without the taint is REJECTED (node-side policy)" t_label_without_taint
step L51 "NEGATIVE: removing the trust taint from the friends node is REJECTED" t_taint_removal
step L52 "NEGATIVE: a node cannot raise its own trust label with its kubelet credential (NodeRestriction)" t_node_cannot_upgrade
step L53 "cp is still own after the rejected relabel" t_cp_still_own

echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
