#!/usr/bin/env bash
# make poc-golden: the golden path conformance suite (issue #23). One deterministic check per golden rule (docs/GOLDEN-RULES.md), run against
# the game this machine hosts (the state in .state, or EMESH_STATE): the maintainer's or a stranger's. It builds nothing and tears nothing
# down; it needs the game opened the golden way (CNI=cilium TAINT=yes, ./cilium.sh, ./install-placement.sh, at least one friends node).
#   ./poc-golden.sh                          one PASS / FAIL / SKIP / DEP line per check, then RESULT
#   GOLDEN_ENV="lab host (Incus)" ./poc-golden.sh   also writes this run's column of the table in docs/POC-STATUS.md (poc/golden.py)
# What it leaves behind: one revoked throwaway player (gold<digits>, an invite that never joins: a name is used once), like any kick.
# DEP = the check belongs to an issue not built yet (it is listed, not counted as a pass). SKIP = the game lacks what the check needs (no
# public node, say). Details of every check go to .state/golden.log; the table rows to .state/golden.tsv.
set -u
. "$(dirname "$0")/game-env.sh"
. "$(dirname "$0")/lib.sh"
LOG="$STATE/golden.log"; : >"$LOG"
RES="$STATE/golden.tsv"; : >"$RES"
MESH="$POC/mesh"
NS=golden
GOLD="gold$(date +%s | tail -c 7)"   # a fresh name every run: a name can be used once, even after a revoke
CANARY="EDGEMESH-GOLDEN-CANARY-0123456789"
SENTENCE="what runs on someone else's machine can be seen by its owner"
NEB9="$STATE/golden-nebula"   # the throwaway player's Nebula, run on THIS machine, without a tun device
NEB9_PID=""

check() { # check <rule> <id> <description> <command...>: exit 0 PASS, 3 SKIP, anything else FAIL
  local rule="$1" id="$2" desc="$3" rc st; shift 3
  log "CHECK $id: $desc"
  "$@" >>"$LOG" 2>&1; rc=$?
  case "$rc" in 0) st=PASS ;; 3) st=SKIP ;; *) st=FAIL; FAILS=$((FAILS+1)) ;; esac
  printf '%-4s %-6s %s\n' "$st" "$id" "$desc"
  printf '%s\t%s\t%s\t%s\n' "$rule" "$id" "$st" "$desc" >>"$RES"
}
depends() { # depends <rule> <id> <description> <issue>: a check that needs an issue not built yet
  printf '%-4s %-6s %s (depends on %s)\n' DEP "$2" "$3" "$4"
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "DEP $4" "$3" >>"$RES"
}

# ---- the game ----
trust_of() { k get node "$1" -o jsonpath='{.metadata.labels.node-restriction\.kubernetes\.io/trust}' 2>/dev/null; }
ready_nodes() { k get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | awk '$2=="True"{print $1}'; }
first_of() { local n; for n in $(ready_nodes); do [ "$(trust_of "$n")" = "$1" ] && { echo "$n"; return; }; done; }
FRIENDS=$(first_of friends); PUBLIC=$(first_of public)
log "friends node: ${FRIENDS:-none}; public node: ${PUBLIC:-none}; throwaway player: $GOLD"
need() { [ -n "$1" ] || { echo "the game has no Ready node of that trust level"; return 3; }; }
cp_curl() { # curl from inside the control plane's network namespace (the only place the API listens)
  if [ "$RUNTIME" = incus ]; then dx cp curl "$@"; else docker run --rm --network container:cp "$TOOLS_IMAGE" curl "$@"; fi
}
cp_tcpdump() { # cp_tcpdump <seconds> <tcpdump args...>: a capture in the control plane machine
  local s="$1"; shift
  if [ "$RUNTIME" = incus ]; then dx cp timeout "$s" tcpdump "$@"
  else docker run --rm --network container:cp --cap-add NET_ADMIN --cap-add NET_RAW "$NETSHOOT_IMAGE" timeout "$s" tcpdump "$@"; fi
}
fp_of() { "$CACHE/nebula-cert" print -json -path "$STATE/bundles/$1/host.crt" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])'; }
cleanup() {
  [ -z "$NEB9_PID" ] || kill "$NEB9_PID" 2>/dev/null; rm -rf "$NEB9"
  # forced: a test pod on a node that went NotReady (its certificate expired) would keep the namespace Terminating for ever
  k -n "$NS" delete pod --all --force --grace-period=0 >/dev/null 2>&1
  k delete namespace "$NS" --wait=false --ignore-not-found >/dev/null 2>&1
}
trap cleanup EXIT

# ---- rule 1: said at join time and in the docs ----
g1_join_says_it() { # the join refuses without --accept-golden-rule (exit 3) and prints the rule first
  local d out rc; d=$(mktemp -d); out=$(sh "$POC/edge-mesh" join "$d" 2>&1); rc=$?; rmdir "$d"
  echo "$out"; [ "$rc" = 3 ] && echo "$out" | grep -qi "$SENTENCE"
}
g1_docs_say_it() { local f; for f in docs/GOLDEN-RULES.md docs/JOIN.md README.md; do grep -qi "$SENTENCE" "$POC/../$f" || { echo "missing in $f"; return 1; }; done; }

# ---- rule 2: placement by trust, enforced ----
g2_policies() {
  [ "$(k get cpol edge-mesh-placement -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = True ] &&
    k get validatingadmissionpolicybinding edge-mesh-node-trust >/dev/null
}
g2_nodes_labelled() { # every node has a trust level; every friends/public node carries the matching taint; cp is own
  local n t taint ok=0
  for n in $(k get nodes -o jsonpath='{.items[*].metadata.name}'); do
    t=$(trust_of "$n"); taint=$(k get node "$n" -o jsonpath="{.spec.taints[?(@.key=='trust')].value}")
    echo "$n trust=${t:-none} taint=${taint:-none}"
    case "$t" in own) ;; friends|public) [ "$taint" = "$t" ] || ok=1 ;; *) ok=1 ;; esac
  done
  [ "$(trust_of cp)" = own ] && [ "$ok" = 0 ]
}
g2_audit() { # no pod outside the system namespaces that is private (unlabelled or private) runs on a node that is not own
  local bad n map=""
  k get pods -A -o json >"$STATE/golden-pods.json" || return 1
  for n in $(k get nodes -o jsonpath='{.items[*].metadata.name}'); do map="$map $n=$(trust_of "$n")"; done
  bad=$(python3 "$POC/golden.py" audit "$STATE/golden-pods.json" "$map") || return 1
  echo "${bad:-no private pod outside an own node}"; [ -z "$bad" ]
}
pod() { # pod <name> <sensitivity|-> <share-with|-> <extra spec lines> [command]
  local labels=""
  [ "$2" = - ] || labels="edge-mesh/sensitivity: $2"
  [ "$3" = - ] || labels="$labels, edge-mesh/share-with: $3"
  labels="{$labels}"
  cat <<YAML
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $NS, labels: $labels}
spec:
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 1
$4
  containers: [{name: c, image: busybox:1.36, command: ["sh","-c","${5:-sleep 3600}"], resources: {limits: {cpu: 100m, memory: 64Mi}}}]
YAML
}
rejected() { # rejected <node>: an unlabelled (private) pod naming a node that is not own is refused, with a message that names the rule
  need "$1" || return 3
  local out rc; out=$(pod "p-$RANDOM" - - "  nodeName: $1" | k apply -f - 2>&1); rc=$?
  echo "$out"; [ "$rc" != 0 ] && echo "$out" | grep -q 'may only run on `own` nodes' && echo "$out" | grep -q 'golden rule 1'
}
on_node() { [ "$(k -n "$NS" get pod "$1" -o jsonpath='{.status.phase}')" = Running ] && [ "$(k -n "$NS" get pod "$1" -o jsonpath='{.spec.nodeName}')" = "$2" ]; }
g2_private_forced_own() { # an unlabelled pod is mutated to nodeSelector trust=own and runs on an own node (the control plane here)
  pod web - - '  nodeName: cp
  tolerations: [{key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule}]' \
    "mkdir -p /www; echo $CANARY >/www/canary; httpd -f -p 8080 -h /www" | k apply -f - || return 1
  [ "$(k -n "$NS" get pod web -o jsonpath='{.spec.nodeSelector.node-restriction\.kubernetes\.io/trust}')" = own ] && wait_for 180 on_node web cp
}
g2_shareable_runs() { # a shareable pod opted in to friends runs on the friends node
  need "$FRIENDS" || return 3
  pod cli shareable friends "  nodeName: $FRIENDS" | k apply -f - && wait_for 180 on_node cli "$FRIENDS"
}

# ---- rule 3: one job per layer; transit encryption always on ----
cilium_pods() { k -n kube-system get pod -l k8s-app=cilium -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\n"}{end}'; }
g3_cilium_everywhere() { # Cilium is the CNI and its agent is Ready on every node
  local nodes ready; nodes=$(k get nodes --no-headers | wc -l)
  ready=$(k -n kube-system get ds cilium -o jsonpath='{.status.numberReady}' 2>/dev/null)
  echo "nodes=$nodes cilium ready=${ready:-none}"; [ "${ready:-0}" = "$nodes" ]
}
g3_wireguard_peers() { # every agent: WireGuard on, and a peer for every other node
  local n p node out want; n=$(k get nodes --no-headers | wc -l); want=$((n - 1))
  [ "$n" -ge 2 ] || { echo "one node only: no peers to check"; return 3; }
  cilium_pods | while read -r p node; do
    out=$(k -n kube-system exec "$p" -c cilium-agent -- cilium-dbg encrypt status) || exit 1
    echo "$node: $(echo "$out" | tr -s ' \t\n' ' ')"
    echo "$out" | grep -q 'Encryption: Wireguard' && echo "$out" | grep -q "Number of peers: $want" || exit 1
  done
}
g3_capture() { # while the friends node fetches the canary from the control plane: the clear text is on cilium_wg0 (the capture works), and
  # neither on nebula1 (only WireGuard UDP 51871 there) nor on the underlay interface (only Nebula's own UDP there)
  need "$FRIENDS" || return 3
  on_node web cp && on_node cli "$FRIENDS" || { echo "test pods not running"; return 1; }
  local t="$STATE/golden-cap" ip i; mkdir -p "$t"; ip=$(k -n "$NS" get pod web -o jsonpath='{.status.podIP}')
  [ "$RUNTIME" = incus ] || docker pull -q "$NETSHOOT_IMAGE" >/dev/null
  cp_tcpdump 25 -i cilium_wg0 -A -nn -l "udp port 8472 or tcp port 8080" >"$t/wg.txt" 2>"$t/wg.err" &
  local p1=$!
  cp_tcpdump 25 -i nebula1 -A -nn -l >"$t/neb.txt" 2>"$t/neb.err" &
  local p2=$!
  cp_tcpdump 25 -i "$UNDERLAY_IF" -A -nn -l "udp" >"$t/under.txt" 2>"$t/under.err" &
  local p3=$!
  i=0; until grep -q listening "$t/wg.err" && grep -q listening "$t/neb.err" && grep -q listening "$t/under.err"; do i=$((i+1)); [ "$i" -gt 20 ] && break; sleep 1; done
  for i in 1 2 3; do k -n "$NS" exec cli -- wget -qO- -T 8 "http://$ip:8080/canary" >/dev/null; done
  wait "$p1" "$p2" "$p3" 2>/dev/null
  local wg neb under wgu nebu
  wg=$(grep -c "$CANARY" "$t/wg.txt"); neb=$(grep -c "$CANARY" "$t/neb.txt"); under=$(grep -c "$CANARY" "$t/under.txt")
  wgu=$(grep -c '51871.*UDP' "$t/neb.txt"); nebu=$(grep -c 'UDP' "$t/under.txt")
  echo "canary: cilium_wg0=$wg nebula1=$neb $UNDERLAY_IF=$under; WireGuard lines on nebula1=$wgu; UDP lines on $UNDERLAY_IF=$nebu"
  [ "$wg" -gt 0 ] && [ "$neb" = 0 ] && [ "$under" = 0 ] && [ "$wgu" -gt 0 ] && [ "$nebu" -gt 0 ]
}

# ---- rule 4: tokens per node, burned; credentials encrypted in transit ----
g4_no_shared_token() { # every player has its own join token; none is the server's shared token
  local srv b ids n=0; srv=$(cpx cat /var/lib/rancher/k3s/server/token) || return 1
  ids=""
  for b in "$STATE"/bundles/*/join.env; do
    [ -f "$b" ] || continue; n=$((n+1))
    grep -q "^K3S_TOKEN=.*::server:" "$b" && { echo "server token in $b"; return 1; }
    grep -qF "${srv##*:}" "$b" && { echo "server secret in $b"; return 1; }
    ids="$ids $(sed -n 's/^K3S_TOKEN=.*::\([a-z0-9]*\)\..*/\1/p' "$b")"
  done
  echo "join tokens: $n bundles, $(echo "$ids" | tr ' ' '\n' | grep -c .) ids, $(echo "$ids" | tr ' ' '\n' | grep . | sort -u | wc -l) distinct"
  [ "$n" -ge 1 ] && [ "$(echo "$ids" | tr ' ' '\n' | grep -c .)" = "$(echo "$ids" | tr ' ' '\n' | grep . | sort -u | wc -l)" ]
}
g4_burned() { # after `mesh host burn` no registered player's join token is left on the control plane
  "$MESH" host burn || return 1
  local list b p id left=0; list=$(cpx k3s token list)
  for b in "$STATE"/bundles/*/; do p=$(basename "$b"); case "$p" in lh|cp) continue ;; esac
    k get node "$p" >/dev/null 2>&1 || continue
    id=$(token_id "$p"); [ -n "$id" ] && echo "$list" | grep -q "$id" && { echo "token of $p still there"; left=1; }
  done
  [ "$left" = 0 ]
}
bearer_code() { local t; t=$(sed -n 's/^K3S_TOKEN=.*:://p' "$STATE/bundles/$GOLD/join.env"); cp_curl -sk -m5 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $t" "https://$CP_NEB:6443/api/v1/namespaces/kube-system/secrets"; }
g4_fresh_invite() { # a new invite: a token that lives 8 minutes at most, pinned to the cluster CA, sent to the API on the Nebula IP, and that works
  "$MESH" host invite "$GOLD" --ttl 1h --trust friends >/dev/null || return 1
  local id exp left ca code K3S_URL K3S_TOKEN; id=$(token_id "$GOLD")
  K3S_URL=$(sed -n 's/^K3S_URL=//p' "$STATE/bundles/$GOLD/join.env"); K3S_TOKEN=$(sed -n 's/^K3S_TOKEN=//p' "$STATE/bundles/$GOLD/join.env")
  exp=$(k -n kube-system get secret "bootstrap-token-$id" -o jsonpath='{.data.expiration}' | base64 -d)
  left=$(python3 -c 'import sys,datetime as d;print(int((d.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00"))-d.datetime.now(d.timezone.utc)).total_seconds()))' "$exp")
  ca=$(cpx sha256sum /var/lib/rancher/k3s/server/tls/server-ca.crt | cut -d' ' -f1)
  code=$(bearer_code)
  echo "url=$K3S_URL ttl_left=${left}s ca_pinned=$([ "${K3S_TOKEN:3:64}" = "$ca" ] && echo yes || echo no) bearer=$code"
  [ "$K3S_URL" = "https://$CP_NEB:6443" ] && [ "$left" -le 480 ] && [ "$left" -gt 0 ] && [ "${K3S_TOKEN:3:64}" = "$ca" ] && [ "$code" = 403 ]
}

# ---- rule 5: revocation is one command, and hard ----
# The throwaway certificate is tried from the machine that runs this suite, by a Nebula WITHOUT a tun device (tun disabled): no root, no
# network device, no route, nothing of the game's own machines touched. It still handshakes with the lighthouse, which is the proof: a
# certificate the lighthouse accepts gets "Handshake message received" from it; a blocklisted one gets "Close tunnel received" and then only
# "Handshake timed out". (A second Nebula with a tun device inside the control plane took the mesh's /24 route and cut the game: found 2026-10-28.)
neb9_up() {
  local b="$STATE/bundles/$GOLD" f
  rm -rf "$NEB9"; mkdir -p "$NEB9"; chmod 700 "$NEB9"
  for f in ca.crt host.crt host.key; do cp "$b/$f" "$NEB9/" || return 1; done
  sed -e "s#/etc/nebula/#$NEB9/#" -e 's#^tun: .*#tun: {disabled: true}#' -e 's#port: 4242}#port: 0}#' "$b/config.yml" >"$NEB9/config.yml"
  grep -qx 'tun: {disabled: true}' "$NEB9/config.yml" || { echo "could not disable the tun device in the copy of the config"; return 1; }
  "$CACHE/nebula" -config "$NEB9/config.yml" </dev/null >"$NEB9/log" 2>&1 &
  NEB9_PID=$!
}
lh_said() { # lh_said <message> [from line]: the throwaway Nebula's log has <message> about the lighthouse after that line
  tail -n +"${2:-1}" "$NEB9/log" | grep "msg=\"$1" | grep -q "vpnAddrs=\[$LH_NEB\]"
}
g5_before() { # positive control: before the revoke, the throwaway certificate is accepted by the lighthouse
  [ -f "$STATE/bundles/$GOLD/host.key" ] || return 1
  neb9_up && wait_for 40 lh_said "Handshake message received"
}
lh_lists() { # lh_lists <fingerprint>: the running lighthouse has it on its blocklist
  if [ -n "${LH_RSH:-}" ]; then echo "grep -c '$1' /etc/nebula/config.yml" | $LH_RSH bash -s | grep -qv '^0$'
  elif [ -n "${LH_EXTERNAL:-}" ]; then echo "external lighthouse without LH_RSH: cannot look"; return 1
  else dx lh grep -q "$1" /etc/nebula/config.yml; fi
}
REVOKED_AT=1
g5_revoke() { # one command: exit 0; the fingerprint on the blocklist file, in every bundle config, in the running control plane and lighthouse
  REVOKED_AT=$(($(wc -l <"$NEB9/log" 2>/dev/null || echo 0) + 1))
  "$MESH" host revoke "$GOLD" || return 1
  local fp; fp=$(fp_of "$GOLD")
  grep -qx "$fp" "$STATE/blocklist" && grep -q "$fp" "$STATE/bundles/cp/config.yml" && dx cp grep -q "$fp" /etc/nebula/config.yml && lh_lists "$fp"
}
g5_cut() { # after the revoke the lighthouse drops the tunnel and refuses every new handshake: timeouts, and no handshake answered after the first
  wait_for 60 sh -c "[ \$(tail -n +$REVOKED_AT '$NEB9/log' | grep 'msg=\"Handshake timed out' | grep -c 'vpnAddrs=\[$LH_NEB\]') -ge 2 ]" || { tail -n +"$REVOKED_AT" "$NEB9/log" | cut -c1-200; return 1; }
  local first; first=$(tail -n +"$REVOKED_AT" "$NEB9/log" | grep -n 'msg="Handshake timed out' | grep "vpnAddrs=\[$LH_NEB\]" | head -1 | cut -d: -f1)
  tail -n +"$REVOKED_AT" "$NEB9/log" | tail -n +"$first" | cut -c1-200
  ! lh_said "Handshake message received" $((REVOKED_AT + first - 1))
}
g5_token_dead() { # the join token stops authenticating (the API server caches a token for about 10 s)
  local i c; for i in $(seq 12); do c=$(bearer_code); [ "$c" = 401 ] && return 0; echo "bearer still $c"; sleep 5; done; return 1
}
g5_idempotent() {
  local fp before; fp=$(fp_of "$GOLD"); before=$(grep -c "$fp" "$STATE/blocklist")
  "$MESH" host revoke "$GOLD" >/dev/null && [ "$(grep -c "$fp" "$STATE/blocklist")" = "$before" ] && [ "$before" = 1 ] || return 1
  local rc=0; "$MESH" host revoke nobody-here >/dev/null 2>&1 || rc=$?; [ "$rc" = 3 ]
}

# ---- rule 6: the API is never public ----
g6_listens_on_nebula_only() { # every socket listening on 6443 in the control plane is bound to its Nebula IP or to loopback (k3s's own local
  # listener, which nothing outside the machine reaches); none to an underlay address or to every address
  local all out; all=$(cpx cat /proc/net/tcp /proc/net/tcp6 | python3 "$POC/golden.py" listeners 6443) || return 1
  out=$(echo "$all" | grep -vxE '127\.[0-9.]+|::1' | sort -u | tr '\n' ' ')
  echo "6443 listens on: $(echo "$all" | tr '\n' ' ')"; [ "$out" = "$CP_NEB " ]
}
g6_not_outside() { # from this machine, outside the mesh, the control plane's underlay address does not answer on 6443; the Nebula IP does
  local outside inside; outside=$(curl -sk -m4 -o /dev/null -w '%{http_code}' "https://$CP_UNDERLAY:6443/readyz"); inside=$(cp_curl -sk -m4 -o /dev/null -w '%{http_code}' "https://$CP_NEB:6443/readyz")
  echo "underlay $CP_UNDERLAY: $outside; nebula $CP_NEB from inside: $inside"; [ "$outside" = 000 ] && [ "$inside" != 000 ]
}

# ---- rule 8 (the pool half) and rule 9 ----
cert() { python3 "$POC/golden.py" cert "$CACHE/nebula-cert" "$1"; }   # fingerprint issuer seconds-of-life
g8_pool_lease() { # every public certificate is issued by the pool CA and lives at most the lease of pool.yaml
  local lease b n=0 pool fp iss life; lease=$(sed -n 's/^lease_seconds: *\([0-9]*\).*/\1/p' "${POOL_CONFIG:-$POC/pool.yaml}")
  read -r pool _ <<<"$(cert "$STATE/ca-pool/ca.crt")"
  for b in "$STATE"/bundles/*/; do
    grep -qs '^TRUST=public' "$b/join.env" || continue; n=$((n+1))
    read -r fp iss life <<<"$(cert "$b/host.crt")"
    [ "$iss" = "$pool" ] || { echo "$(basename "$b"): not issued by the pool CA"; return 1; }
    [ "$life" -le $((lease + 5)) ] || { echo "$(basename "$b"): lives ${life}s, longer than the lease"; return 1; }
  done
  echo "public bundles: $n, all from the pool CA, none longer than ${lease}s"; [ "$n" -ge 1 ] || return 3
}
g9_our_ca() { # every certificate in the game was issued by one of our two CAs, every bundle trusts exactly those two, and the lighthouse is ours
  local all b own pool fp iss n=0; all=$(cat "$STATE/ca/ca.crt" "$STATE/ca-pool/ca.crt")
  read -r own _ <<<"$(cert "$STATE/ca/ca.crt")"; read -r pool _ <<<"$(cert "$STATE/ca-pool/ca.crt")"
  for b in "$STATE"/bundles/*/; do
    [ -f "$b/host.crt" ] || continue; n=$((n+1))
    [ "$(cat "$b/ca.crt")" = "$all" ] || { echo "$(basename "$b"): another CA bundle"; return 1; }
    read -r fp iss _ <<<"$(cert "$b/host.crt")"
    [ "$iss" = "$own" ] || [ "$iss" = "$pool" ] || { echo "$(basename "$b"): issued by $iss, not by our CAs"; return 1; }
    grep -qF "\"$LH_NEB\": [\"$LH_DOCKER_IP:" "$b/config.yml" || { echo "$(basename "$b"): another lighthouse"; return 1; }
  done
  echo "certificates: $n, all from our CAs, all pointing at the lighthouse $LH_DOCKER_IP"
}

step_g10() { # rule 10: every rule 1-9 had at least one check that ran and passed in this run
  local r missing=""; for r in 1 2 3 4 5 6 8 9; do awk -F'\t' -v r="$r" '$1==r && $3=="PASS"' "$RES" | grep -q . || missing="$missing $r"; done
  echo "rules without a passing check:${missing:- none} (rule 7 is not built: #13)"; [ -z "$missing" ]
}

ns_gone() { ! k get namespace "$NS" >/dev/null 2>&1; }
k -n "$NS" delete pod --all --force --grace-period=0 >/dev/null 2>&1   # a run that was killed before its cleanup
wait_for 180 ns_gone; k create namespace "$NS" >>"$LOG" 2>&1
check 1 G1.1 "the join refuses without --accept-golden-rule (exit 3) and prints rule 1 first" g1_join_says_it
check 1 G1.2 "rule 1 is said in GOLDEN-RULES.md, JOIN.md and the README" g1_docs_say_it
check 2 G2.1 "the Kyverno placement policy is Ready and the node trust policy is bound" g2_policies
check 2 G2.2 "every node has a trust level, every friends/public node carries the matching taint, cp is own" g2_nodes_labelled
check 2 G2.3 "audit: no private pod runs on a node that is not own" g2_audit
check 2 G2.4 "a private pod naming a friends node is REJECTED, and the message names the rule" rejected "$FRIENDS"
check 2 G2.5 "a private pod naming a public node is REJECTED, and the message names the rule" rejected "$PUBLIC"
check 2 G2.6 "an unlabelled pod is forced to trust=own and runs on an own node" g2_private_forced_own
check 2 G2.7 "a shareable pod opted in to friends runs on the friends node" g2_shareable_runs
depends 2 G2.8 "trust is relative to the owner: a user's private pods run only on that user's own nodes" "#9"
check 3 G3.1 "Cilium is the CNI: its agent is Ready on every node" g3_cilium_everywhere
check 3 G3.2 "WireGuard is on in every agent, with a peer for every other node" g3_wireguard_peers
check 3 G3.3 "a capture on the control plane: the canary is in clear only on cilium_wg0, never on nebula1 or the underlay" g3_capture
depends 3 G3.4 "mutual authentication by policy: an unauthenticated peer is dropped" "#8"
check 4 G4.1 "every player has its own join token; no bundle carries the shared server token" g4_no_shared_token
check 4 G4.2 "after mesh host burn no registered player's join token is left" g4_burned
check 4 G4.3 "a fresh invite: token lives 8 min at most, pinned to the cluster CA, for the API on the Nebula IP, and it authenticates" g4_fresh_invite
check 5 G5.1 "positive control: before the revoke, the lighthouse accepts the throwaway certificate" g5_before
check 5 G5.2 "mesh host revoke: one command; the fingerprint is on the blocklist of the control plane and the lighthouse" g5_revoke
check 5 G5.3 "the lighthouse drops the revoked certificate and refuses every new handshake" g5_cut
check 5 G5.4 "the revoked player's join token no longer authenticates" g5_token_dead
check 5 G5.5 "revoke is idempotent, and an unknown name is refused (exit 3)" g5_idempotent
check 6 G6.1 "the API listens on the control plane's Nebula IP only" g6_listens_on_nebula_only
check 6 G6.2 "outside the mesh the API does not answer; inside it does" g6_not_outside
depends 7 G7.1 "the maintainer's endpoints (AI gateway, LoadBalancer with BGP) run only on own nodes" "#13"
depends 8 G8.1 "persistent participants join only by a signed PR" "#12"
check 8 G8.2 "every public certificate comes from the pool CA and lives at most the lease (pool.yaml)" g8_pool_lease
check 9 G9.1 "every certificate chains to our two CAs and every bundle points at our lighthouse" g9_our_ca
check 10 G10.1 "every rule 1-9 that is built has a passing check in this run" step_g10

echo "----"
if [ -n "${GOLDEN_ENV:-}" ]; then python3 "$POC/golden.py" table --doc "$POC/../docs/POC-STATUS.md" --env "$GOLDEN_ENV" --results "$RES" >>"$LOG" 2>&1 || echo "note: the table in docs/POC-STATUS.md was not updated (see $LOG)"; fi
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
