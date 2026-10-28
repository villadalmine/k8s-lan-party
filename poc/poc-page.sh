#!/usr/bin/env bash
# make poc-page: the public page (phase B2, docs/ADMISSION.md, docs/PUBLIC-PAGE.md) end to end. A public server runs Caddy, the page and the
# lighthouse; the control plane (an Incus VM here) pulls from the page, outbound only; a visitor on another machine goes
#   page -> request -> wait -> claim -> edge-mesh join -> Ready -> lease end -> kicked
# with its own keys, while the page never holds a credential. A decoy request from the host takes the only place first, so the visitor
# really waits in the list. Needs a Linux host with Incus. PASS/FAIL per step; identical across runs.
#   default: two plain Ubuntu VMs play the public server (emesh-pub) and the visitor (emesh-vis), on the PoC's network
#   PUB_SSH="<ssh command to root on a public server>" PUB_ADDR=<its public address> VIS_SSH="<ssh command to root on the visitor>":
#            real machines on other networks (a cloud VM each, as in the maintainer's run); the suite leaves nothing on them (G25)
# Test values: ADM_LEASE seconds (default 420), one place (slots: 1). KEEP=1 leaves everything up.
set -u
export RUNTIME=incus MAX_VMS=4 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-page.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"; PG="$POC/page"; LHS="$POC/lighthouse-vps.sh"; PS="$PG/page-server.sh"
LEASE="${ADM_LEASE:-420}"
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^slots: [0-9]*/slots: 1/" -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^cooldown_seconds: [0-9]*/cooldown_seconds: 60/" \
  "$POC/pool.yaml" >"$POOL_CONFIG"
EXTERNAL=0; [ -n "${PUB_SSH:-}" ] && EXTERNAL=1
PUB_SSH="${PUB_SSH:-incus exec emesh-pub --}"; VIS_SSH="${VIS_SSH:-incus exec emesh-vis --}"
PUB_ADDR="${PUB_ADDR:-$UNDERLAY_NET.2}"
SITE="https://$PUB_ADDR"
pub() { $PUB_SSH bash -s; }
vis() { $VIS_SSH bash -s; }
CA="$STATE/page/caddy-root.crt"; TOKEN="$STATE/page/puller.token"
VK=/root/mykeys   # the visitor's keys, on the visitor

plain_vm() { # plain_vm <name> <ip>: an Ubuntu machine with nothing of ours (it plays a public server, or a visitor's own computer)
  incus network show "$VMNET" >/dev/null 2>&1 || incus network create "$VMNET" ipv4.address=$UNDERLAY_NET.1/24 ipv4.nat=true ipv6.address=none >/dev/null
  incus init "$IMG" "emesh-$1" --vm -c limits.cpu=2 -c limits.memory=2GiB -d root,size=12GiB --network "$VMNET" >/dev/null &&
  incus config device set "emesh-$1" eth0 ipv4.address="$2" >/dev/null && incus start "emesh-$1" &&
  wait_for 240 incus exec "emesh-$1" -- true && wait_for 120 incus exec "emesh-$1" -- ping -c1 -W1 "$UNDERLAY_NET.1" &&
  incus exec "emesh-$1" -- date -s "@$(date +%s)" >/dev/null &&
  incus exec "emesh-$1" -- sh -c 'cloud-init status --wait >/dev/null 2>&1 || true; command -v curl >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null; }'
}
servers_up() {
  if [ "$EXTERNAL" = 1 ]; then echo true | pub && echo true | vis; return; fi
  plain_vm pub "$PUB_ADDR" && plain_vm vis "$UNDERLAY_NET.150"
}
unchanged() { local o; o=$("$@" 2>&1) || { echo "$o"; return 1; }; echo "$o"; ! echo "$o" | grep -q '^changed:'; }
lh_install() { "$LHS" install "$PUB_ADDR" --ssh "$PUB_SSH" --out "$STATE/lh-out"; }
page_install() { "$PS" install --ssh "$PUB_SSH" --site "$SITE" --tls internal --with-caddy; }
cp_up() { HOST_ONLY=1 "$POC/vm/up.sh" && incus exec emesh-cp -- ping -c1 -W2 "$LH_NEB"; }
token_only_hash() { # the token stays on the host (0600); the server holds its SHA-256 and nothing that equals the token
  "$PS" puller-token --ssh "$PUB_SSH" && [ "$(stat -c %a "$TOKEN")" = 600 ] &&
  [ "$(echo 'cat /etc/k8s-lan-party-page/puller.sha256' | pub)" = "$(tr -d '\n' <"$TOKEN" | sha256sum | cut -d' ' -f1)" ] &&
  ! echo "grep -rqsF -- '$(cat "$TOKEN")' /etc /opt /var/lib /root /home" | pub; }
cacert() { "$PS" cacert --ssh "$PUB_SSH" >"$CA" && grep -q 'BEGIN CERTIFICATE' "$CA"; }
api() { curl -s --cacert "$CA" -m 20 "$@"; }
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1], ""))' "$1"; }
closed_before_pull() { local s; s=$(api "$SITE/api/v1/status") && [ "$(echo "$s" | jget open)" = False ] &&
  [ "$(api -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{}' "$SITE/api/v1/requests")" = 503 ]; }
pull() { python3 "$PG/puller.py" run --url "$SITE" --token-file "$TOKEN" --cacert "$CA" --mesh "$MESH" --state "$STATE"; }
cycle() { "$MESH" host auto --once >>"$LOG" 2>&1; pull >>"$LOG" 2>&1; }   # what the host's timers run every minute
cycle_until() { # cycle_until <seconds> <command...>: cycles every 15 s until the command succeeds
  local end=$(( $(date +%s) + $1 )); shift
  while :; do cycle; "$@" >/dev/null 2>&1 && return 0; [ "$(date +%s)" -lt "$end" ] || return 1; sleep 15; done; }
same_numbers() { # the page says what admission-status says: places used and total, length of the list, eligible now
  local line page; cycle; line=$("$MESH" host admission-status); page=$(api "$SITE/api/v1/status"); echo "$line"; echo "$page"
  python3 - "$line" "$page" <<'PY'
import json, re, sys
m = re.match(r"admission: slots (\d+)/(\d+) used \| (\d+) in the list \((\d+) eligible now\)", sys.argv[1])
p = json.loads(sys.argv[2])
sys.exit(0 if m and p["open"] and [int(x) for x in m.groups()] == [p["slots_used"], p["slots_total"], p["waiting"], p["eligible"]] else 1)
PY
}
api_not_from_server() { # over Nebula the server reaches the control plane (ping: the positive control) but not the Kubernetes API, and
  # the page's own process may open connections to the loopback only
  echo "ping -c1 -W3 $CP_NEB >/dev/null" | pub || return 1
  [ "$(echo "curl -sk -m6 -o /dev/null -w '%{http_code}' https://$CP_NEB:6443/readyz" | pub)" = 000 ] || return 1
  echo "systemctl show k8s-lan-party-page -p IPAddressDeny | grep -q '0.0.0.0/0'" | pub; }
host_keys() { [ -f "$STATE/page/decoy/host.key" ] || python3 "$PG/visit.py" keygen "$STATE/page/decoy" --nebula-cert "$CACHE/nebula-cert" >/dev/null; }
decoy_asks() { # the decoy asks with the curl calls of docs/PUBLIC-PAGE.md ("The same with curl")
  host_keys || return 1
  local d="$STATE/page/decoy" C CH BITS NONCE out
  C=$(api "$SITE/api/v1/challenge") || return 1
  CH=$(printf %s "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["challenge"])')
  BITS=$(printf %s "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["difficulty"])')
  NONCE=$(python3 "$PG/visit.py" pow "$CH" "$BITS")
  python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "nebula_public_key": open(sys.argv[4] + "/host.pub").read(),
  "seal_public_key": open(sys.argv[4] + "/seal.pub").read(), "challenge": sys.argv[2], "nonce": sys.argv[3]}))' decoy "$CH" "$NONCE" "$d" >"$d/req.json"
  out=$(api -H 'Content-Type: application/json' --data @"$d/req.json" "$SITE/api/v1/requests"); echo "$out" | sed 's/"claim_code": "[^"]*"/"claim_code": "..."/'
  echo "$out" | jget claim_code >"$d/claim-code"; [ -s "$d/claim-code" ] && grep -q . "$d/claim-code"; }
decoy_claims() { # collect with curl, open with visit.py open: the certificate is for the decoy's own key, no private key came with it
  local d="$STATE/page/decoy"
  api -H 'Content-Type: application/json' -d "{\"claim_code\": \"$(cat "$d/claim-code")\"}" "$SITE/api/v1/requests/claim" >"$d/claim.json" &&
  python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["sealed"], open(sys.argv[2], "w"))' "$d/claim.json" "$d/sealed.json" &&
  python3 "$PG/visit.py" open "$d" "$d/sealed.json" --nebula-cert "$CACHE/nebula-cert" | grep -q "^join: sudo edge-mesh join $d/decoy" &&
  ! grep -q 'PRIVATE KEY' "$d/claim.json" && cmp -s "$d/host.key" "$d/decoy/host.key"; }
decoy_state() { python3 "$PG/visit.py" status "$SITE" --keys "$STATE/page/decoy" --cacert "$CA"; }
no_secret_on_server() { # nothing on the server's disk that opens the mesh: no CA key, no join token in clear, no private key but the
  # lighthouse's own and Caddy's, no claim code
  local codes="" c
  for c in "$STATE/page/decoy/claim-code"; do [ -f "$c" ] && codes="$codes $(tr -d '-' <"$c")"; done
  pub <<EOF
set -e
! grep -rlsE 'K10[0-9a-f]{20}|NODE_PASSWORD=' /etc /opt /var/lib /root /home /tmp
! grep -rlsE 'PRIVATE KEY' /etc /opt /var/lib /root /home /tmp | grep -vE '^/etc/nebula/host.key$|^/var/lib/caddy/|^/etc/ssh/|^/etc/ssl/private/'
! find / -xdev -name 'ca.key' 2>/dev/null | grep -q .
for c in $codes; do ! grep -rqsF "\$c" /etc /opt /var/lib /root /home /tmp; done
EOF
}
vis_tools() { # the visitor installs what JOIN.md says, checking the published checksums, plus visit.py (from the repository)
  vis <<EOF || return 1
set -e
command -v openssl >/dev/null && command -v python3 >/dev/null && command -v curl >/dev/null
mkdir -p /opt/nebula /opt/k8s-lan-party; cd /tmp
curl -fsSLO https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/nebula-linux-amd64.tar.gz
curl -fsSLO https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/SHASUM256.txt
grep ' nebula-linux-amd64.tar.gz$' SHASUM256.txt | sha256sum -c - >/dev/null && tar xzf nebula-linux-amd64.tar.gz -C /opt/nebula
curl -fsSLo k3s "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION/+/%2B}/k3s"
curl -fsSLo k3s.sha "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION/+/%2B}/sha256sum-amd64.txt"
grep ' k3s$' k3s.sha | sha256sum -c - >/dev/null && install -m 755 k3s /usr/local/bin/k3s
EOF
  local f; for f in "$POC/edge-mesh:/usr/local/bin/edge-mesh" "$PG/visit.py:/opt/k8s-lan-party/visit.py" "$PG/seal.py:/opt/k8s-lan-party/seal.py" "$CA:/opt/k8s-lan-party/page-ca.crt"; do
    { echo "base64 -d >'${f#*:}' <<'B64'"; base64 -w0 "${f%%:*}"; echo; echo B64; echo "chmod 755 '${f#*:}'"; } | vis || return 1; done
}
V="python3 /opt/k8s-lan-party/visit.py"; VA="--keys $VK --cacert /opt/k8s-lan-party/page-ca.crt --nebula-cert /opt/nebula/nebula-cert"
vis_keys() { echo "$V keygen $VK --nebula-cert /opt/nebula/nebula-cert && stat -c %a $VK/host.key $VK/seal.key | sort -u" | vis | tail -1 | grep -qx 600; }
vis_asks() { echo "$V request $SITE visitor $VA" | vis | grep -q 'Claim code:'; }
vis_waits() { # position 1 of 1, and an estimate about when the decoy's lease ends
  local s end; s=$(echo "$V status $SITE $VA" | vis) && echo "$s" && echo "$s" | grep -q 'in the list, position 1 of 1' || return 1
  end=$(python3 -c 'import json,sys; print(next(l["end"] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == "decoy"))' "$STATE/admission/state.json")
  local eta; eta=$(echo "$s" | grep -o 'around [0-9T:-]*Z' | cut -d' ' -f2) && [ -n "$eta" ] &&
  python3 -c 'import sys,datetime as d; e=d.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")).timestamp(); sys.exit(0 if abs(e-int(sys.argv[2])) <= 180 else 1)' "$eta" "$end"; }
code_of_vis() { echo "cat $VK/claim-code" | vis; }
vis_curl() { echo "curl -s --cacert /opt/k8s-lan-party/page-ca.crt -m 20 $*" | vis; }
ATTACK='import json, ssl, sys, urllib.request, urllib.error
sys.path.insert(0, "/opt/k8s-lan-party")
import visit
url, keys, ca, mode = sys.argv[1:5]
ctx = ssl.create_default_context(cafile=ca)
def post(path, body, hdr={}):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **hdr})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
def ask(name, hdr={}):
    s, c = visit.call(url, "/api/v1/challenge", cacert=ca)
    n = visit.pow_solve(c["challenge"], c["difficulty"])
    body = {"name": name, "nebula_public_key": open(keys + "/host.pub").read(), "seal_public_key": open(keys + "/seal.pub").read(),
            "challenge": c["challenge"], "nonce": n}
    return body, post("/api/v1/requests", body, hdr)
if mode == "replay":   # the first use spends the challenge even though the name is refused; the second use is refused as spent
    body, (s1, r1) = ask("Bad")
    s2, r2 = post("/api/v1/requests", dict(body, name="replayed"))
    print(s1, r1.get("error"), s2, r2.get("error"))
elif mode == "spoof":  # a forged X-Forwarded-For: Caddy replaces it, so this is still the visitor (cooldown, or already asked)
    _, (s, r) = ask("spoofed", {"X-Forwarded-For": "203.0.113.9"})
    print(s, r.get("error"))
else:
    s, r = post("/api/v1/requests/status", {"claim_code": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA"})
    print(s, r.get("error"))
'
attack() { echo "$V keygen $VK --nebula-cert /opt/nebula/nebula-cert >/dev/null; python3 -c '$ATTACK' $SITE $VK /opt/k8s-lan-party/page-ca.crt $1" | vis; }
replay_refused() { local o; o=$(attack replay); echo "$o"; [ "$o" = "400 bad-name 409 pow-used" ]; }
spoof_and_guess_refused() { local a b d
  a=$(attack spoof); b=$(attack wrong); d=$(echo "curl -s -m5 -o /dev/null -w '%{http_code}' http://$PUB_ADDR:8088/api/v1/status" | vis)
  echo "spoof: $a | wrong code: $b | page port: $d"
  case "$a" in "429 cooldown" | "409 already-asked") ;; *) return 1 ;; esac
  [ "$b" = "404 unknown" ] && [ "$d" = 000 ]; }
vis_wait_bg() { echo "nohup $V wait $SITE $VA --interval 10 </dev/null >/root/wait.log 2>&1 &" | vis; }
vis_collected() { echo "grep -q '^join: ' /root/wait.log" | vis; }
decoy_out_vis_in() { cycle_until $((LEASE + 300)) vis_collected && ! k get node decoy && "$MESH" host leases | grep -q '^visitor '; }
bundle_is_mine() { # the bundle the visitor opened: its own host.key (never on the host or the server), a certificate for its key, public
  echo "cmp $VK/host.key $VK/visitor/host.key && ls $VK/visitor | tr '\n' ' '" | vis | grep -qx 'ca.crt config.yml host.crt host.key join.env ' &&
  [ ! -e "$STATE/bundles/visitor/host.key" ] &&
  "$CACHE/nebula-cert" print -json -path "$STATE/bundles/visitor/host.crt" | python3 -c 'import sys,json; d=json.load(sys.stdin)[0]["details"]; sys.exit(0 if d["groups"] == ["public"] else 1)'; }
claim_once() { [ "$(vis_curl "-o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"claim_code\":\"$(code_of_vis)\"}' $SITE/api/v1/requests/claim")" = 410 ]; }
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
vis_joins() { echo "edge-mesh join $VK/visitor --accept-golden-rule </dev/null" | vis && cycle_until 240 ready visitor; }
vis_api() { echo "curl -sk -m5 -o /dev/null -w '%{http_code}' https://$CP_NEB:6443/readyz" | vis; }
visitor_gone() { ! k get node visitor; }
vis_kicked() { # the lease ends: one tick expels the visitor, the page says ended, and its tunnel no longer reaches the API
  local end; end=$(python3 -c 'import json,sys; print(next(l["end"] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == "visitor"))' "$STATE/admission/state.json")
  local s=$(( end - $(date +%s) + 2 )); [ "$s" -le 0 ] || sleep "$s"
  cycle_until 120 visitor_gone || return 1
  echo "$V status $SITE $VA" | vis | grep -q 'your lease ended' && sleep 20 && [ "$(vis_api)" = 000 ]; }
panic_closes() { # a request made just before the panic is never pulled; the page says closed and refuses new ones
  python3 "$PG/visit.py" request "$SITE" late --keys "$STATE/page/decoy" --cacert "$CA" >/dev/null 2>&1   # a new name, the host's address
  "$MESH" host panic >/dev/null && pull | grep -q 'closed (panic) | nothing pulled' || return 1
  ! "$MESH" host waitlist | grep -q late &&
  [ "$(api "$SITE/api/v1/status" | jget closed_reason)" = "closed by the host" ] &&
  [ "$(api -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{}' "$SITE/api/v1/requests")" = 503 ]; }
reopens() { "$MESH" host panic --clear >/dev/null && cycle && [ "$(api "$SITE/api/v1/status" | jget open)" = True ]; }
report_size_limits() { # M2 of the review: a big report of the puller passes Caddy (512 KB) while the public routes stay at 16 KB
  local big visitor
  big=$(python3 -c 'import json; print(json.dumps({"closed": False, "status": {"slots_total": 1, "slots_used": 0, "waiting": 0, "eligible": 0, "lease_seconds": 1, "next_ends": []}, "updates": [{"id": "i-%016x" % n, "state": "ended"} for n in range(200)], "pad": "x" * 60000}))')
  [ "$(printf '%s' "$big" | api -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat "$TOKEN")" -H 'Content-Type: application/json' --data-binary @- "$SITE/api/v1/puller/report")" = 400 ] || return 1   # not 413: it reached the page, which refuses the unknown field
  visitor=$(printf '{"claim_code": "%s"}' "$(python3 -c 'print("A" * 20000)')")
  [ "$(printf '%s' "$visitor" | api -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- "$SITE/api/v1/requests/status")" = 413 ]; }
caddy_stays() { echo 'test -x /usr/local/bin/caddy && systemctl is-active -q caddy && ! test -e /etc/caddy/conf.d/k8s-lan-party-page.caddy && ! test -e /opt/k8s-lan-party-page' | pub; }
remove_keeps_caddy() { # M3 of the review: the default removes the page only; a Caddy that was not installed by us is never removed
  "$PS" remove --ssh "$PUB_SSH" && caddy_stays || return 1
  echo 'mv /etc/caddy/.k8s-lan-party-installed /root/caddy-marker' | pub && local o; o=$("$PS" remove --ssh "$PUB_SSH" --with-caddy 2>&1); echo "$o"
  echo "$o" | grep -q '^kept: Caddy was already there' && caddy_stays; }
server_clean() { # the marker back: now --with-caddy removes exactly what the script installed
  echo 'mv /root/caddy-marker /etc/caddy/.k8s-lan-party-installed' | pub &&
  "$PS" remove --ssh "$PUB_SSH" --with-caddy && "$LHS" remove --ssh "$PUB_SSH" &&
  echo '! test -e /opt/k8s-lan-party-page && ! test -e /etc/caddy && ! test -e /usr/local/bin/caddy && ! test -e /etc/nebula && ! systemctl is-active -q caddy && ! systemctl is-active -q nebula' | pub &&
  echo 'pkill -f "k3s agent" ; pkill nebula; rm -rf /root/mykeys /opt/k8s-lan-party /tmp/replay.json; true' | vis; }

g08() { cacert && closed_before_pull; }
g09() { "$MESH" host auto on >/dev/null && same_numbers; }
decoy_is_ready() { decoy_state | grep -q 'your place is ready'; }
g12() { cycle_until 120 decoy_is_ready && decoy_claims; }
g18() { cycle && vis_waits; }
g20() { vis_wait_bg && decoy_out_vis_in; }
g22() { bundle_is_mine && claim_once; }

step G01 "a public server and a visitor's machine on another network ($([ "$EXTERNAL" = 1 ] && echo 'external machines' || echo 'Incus VMs'))" servers_up
step G02 "the lighthouse on the public server (lighthouse-vps.sh install)" lh_install
step G03 "the page on the public server: Caddy, the unit, the site (page-server.sh install)" page_install
step G04 "install again: nothing changed" unchanged "$PS" install --ssh "$PUB_SSH" --site "$SITE" --tls internal --with-caddy
step G05 "the control plane, behind the external lighthouse" cp_up
step G06 "the puller's token stays on the host (0600); the server holds only its SHA-256" token_only_hash
step G07 "page-server.sh verify: services, loopback-only page, the API through Caddy, headers, no key on disk" "$PS" verify --ssh "$PUB_SSH" --site "$SITE"
step G08 "before the control plane reports, the page says closed and refuses a request (503)" g08
step G09 "auto on; a tick and a pull: the page is open and shows the same numbers as admission-status" g09
step G09b "body limits per route: the puller's 60 KB report passes Caddy (not 413), a visitor's 20 KB body is refused (413)" report_size_limits
step G10 "the public server reaches the control plane over Nebula but not the Kubernetes API; the page's process only the loopback" api_not_from_server
step G11 "a decoy asks through the page with curl (the calls of PUBLIC-PAGE.md): a claim code" decoy_asks
step G12 "a tick approves the decoy (the only place); the page says ready; collected with curl, opened with its own key (visit.py open)" g12
step G13 "the server holds no CA key, no join token in clear, no private key but its own, no claim code" no_secret_on_server
step G14 "the visitor installs nebula and k3s (checksums checked), edge-mesh and visit.py" vis_tools
step G15 "the visitor makes its own keys (the private ones stay on its machine, 0600)" vis_keys
step G16 "from the visitor: a proof of work is spent on first use, even refused; its replay is refused (409)" replay_refused
step G17 "the visitor asks through the page (proof of work solved): a claim code" vis_asks
step G18 "after a pull the visitor waits: position 1 of 1, estimated when the decoy's lease ends" g18
step G19 "from the visitor: a forged X-Forwarded-For is still its own address, a guessed claim code is unknown (404), the page's port is closed" spoof_and_guess_refused
step G20 "the visitor waits (visit.py wait); the decoy's lease ends: one tick expels it and approves the visitor; the visitor collects its sealed bundle" g20
step G21 "the bundle has the visitor's own key (never on the host), a public certificate for it; a second claim is refused (410)" g22
step G22 "the visitor joins with edge-mesh join and is Ready" vis_joins
step G23 "the lease ends: the visitor is expelled, the page says so, its tunnel no longer reaches the API" vis_kicked
step G24 "the server holds no join token in clear, no private key but its own, no claim code (after the whole flow)" no_secret_on_server
step G25 "panic: the puller stops (a last request is never pulled), the page says closed and refuses requests (503)" panic_closes
step G26 "panic --clear: the page opens again" reopens
step G26b "remove keeps a Caddy that was not installed by this script (default and --with-caddy without the marker)" remove_keeps_caddy
step G27 "the server is clean: the page, the Caddy this script installed and the lighthouse removed; the visitor's machine cleaned" server_clean

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
