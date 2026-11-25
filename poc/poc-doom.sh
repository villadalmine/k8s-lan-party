#!/usr/bin/env bash
# make poc-doom: the Doom demo over a LAN party, part B of issue #75 (DESIGN.md section 14.3), end to end on Incus VMs. The players carry the names of
# id Software's Doom creators, as a tribute (the game is FreeDM, from the Freedoom project, not id's Doom): romero hosts an Odamex deathmatch
# server on FreeDM MAP15 (10666/UDP); carmack and petersen are his friends. romero's own robot plays from his namespace; carmack's robot joins
# through a party and, while he plays, petersen's robot cannot reach the server (a party between two is nothing to a third); then romero invites
# petersen, she accepts, and she joins: three players. carmack and petersen never open to each other (a party is a pair). The proof is on the
# server: it logs who connected, joined and left, with frags and deaths (not a number of frags: scripted robots may not meet). Without consent,
# with one consent and after `leave` the same client gets no answer. The image has Odamex (GPL) and FreeDM (BSD) and nothing non-free; it is
# built once (poc/doom/build.sh) and loaded into the nodes. carmack's robot records its virtual screen as a GIF (SHOWCASE keeps it).
# Needs a Linux host with Incus. PASS/FAIL per step; identical across runs. CNI=cilium by default (the demo's CNI); CNI=flannel also runs.
# D6 (#101, DM24 on): the host's watcher needs an `own` machine that is not the control plane: hostpc, issued by the host like n1 (never through
# `mesh host invite`, which refuses own), joins after the match.
# POOL_LEASE (default 2700 s) must cover DM03-DM27 (romero's lease must still run when the host watches). KEEP=1 leaves the VMs. SHOWCASE=<dir> also writes the player's and the host's screens there.
# UNTIL=DMxx stops after that step and keeps the VMs (poc/doom/week.sh: UNTIL=DM27 leaves a match and the host's spectator running).
# OWN_CERT_DURATION (default 2h): the certificate of the host's own machine hostpc.
set -u
export RUNTIME=incus MAX_VMS=6 CNI="${CNI:-cilium}" TAINT=no   # TAINT pinned as poc-pool (issue #59)
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-doom.log"; : >"$LOG"
MESH="$POC/mesh"
LEASE="${POOL_LEASE:-2700}"
PLAY="${DOOM_SECONDS:-40}"   # how long petersen plays; carmack plays long enough to still be in the game when she has joined
LONG=$((PLAY * 3 + 30))
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^slots: [0-9]*/slots: 3/" -e "s/^idle_kick_seconds: [0-9]*/idle_kick_seconds: 0/" \
  "$POC/pool.yaml" >"$POOL_CONFIG"
ADM="$STATE/admission"; OUTD="$STATE/doom"; mkdir -p "$OUTD"
unset ADMISSION_NOTIFY_CMD ADMISSION_NOTIFY_IP
SC="${SHOWCASE:-}"; SCP="$POC/page/whatsnew/showcase.py"; [ -z "$SC" ] || { mkdir -p "$SC"; : >"$SC/player.raw"; : >"$SC/host.raw"; }
shot() { if [ -n "$SC" ]; then python3 "$SCP" stamp "$SC/$1.raw" "$2"; else cat; fi; }   # shot <player|host> <shown command>: stdin is its output
say() { [ -z "$SC" ] || python3 "$SCP" note "$SC/$1.raw" "$2"; }                         # say <side> <text>: a chapter title of the story
show() { local side="$1" shown="$2"; shift 2; "$@" 2>&1 | shot "$side" "$shown"; return "${PIPESTATUS[0]}"; }   # run; with SHOWCASE it is that screen

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
tick() { "$MESH" host auto --once; }
P() { incus exec "emesh-$1" -- edge-mesh "${@:2}"; }   # P <player> <edge-mesh arguments...>: the player's own command on their machine
K() { local who="$1"; shift; P "$who" kubectl "$@"; }
pod_of() { if [ "$1" = romero ]; then echo doom-bot; else echo doom-client; fi; }   # each player's robot pod (romero's is beside his server)
SVC=doom.play-romero.svc.cluster.local
PEOPLE="romero carmack petersen"
WATCH_TOKEN= WATCH_IP= WATCH_NAME=doom-watch-romero
shown() { echo "sudo edge-mesh kubectl exec $(pod_of "$1") -- client.sh $SVC 10666 $2 $1"; }   # what the player types (shown on the player's screen)
probe() { K "$1" exec "$(pod_of "$1")" -- client.sh --probe "doom-probe.play-$2.svc.cluster.local" 10666; }   # probe <from> <to's namespace>: UDP 10666, no game, no log line
open_now() { probe "$1" "$2"; }
closed_now() { local rc; probe "$1" "$2"; rc=$?; [ "$rc" -eq 3 ]; }   # exactly 3: no answer; any other failure is not a closed party
open_both() { open_now "$1" "$2" && open_now "$2" "$1"; }
closed_both() { closed_now "$1" "$2" && closed_now "$2" "$1"; }
server_log() { K romero exec doom-server -- cat /tmp/server.log; }
connects() { server_log | grep -c "^\[[0-9:]*\] $1 has connected\."; }   # connections of one player

host_init() { "$MESH" host init && { [ "$CNI" != cilium ] || "$POC/cilium.sh"; }; }
image() { # the image, once (cached by the hash of its inputs), and its name
  local out; out=$("$POC/doom/build.sh") || { echo "$out"; return 1; }
  DOOM_IMG=$(echo "$out" | tail -2 | head -1); TAR=$(echo "$out" | tail -1); echo "$DOOM_IMG $TAR"; [ -s "$TAR" ] && [ -n "$DOOM_IMG" ]; }
admitted() { "$MESH" host request romero --ip 198.51.100.10 && "$MESH" host request carmack --ip 198.51.100.20 && "$MESH" host request petersen --ip 198.51.100.30 &&
  "$MESH" host auto on && say host "three friends ask for a place; one tick of the host admits all three" && out_has "approved 3 | slots 3/3 used" show host "mesh host auto --once" tick; }
joined() { local w; for w in $PEOPLE; do "$MESH" play "$w" || return 1; done; for w in $PEOPLE; do wait_for 420 ready "$w" || return 1; done; }
loaded() { # no registry: the tarball goes into each player's node (k3s ctr), which then has the image and pulls nothing
  local who; for who in $PEOPLE; do
    incus file push -q "$TAR" "emesh-$who/root/doom.tar" && incus exec "emesh-$who" -- sh -c 'k3s ctr -n k8s.io images import /root/doom.tar >/dev/null && rm -f /root/doom.tar' &&
    incus exec "emesh-$who" -- sh -c "k3s crictl images 2>/dev/null | grep -q 'emesh-doom'" || return 1; done; }
srv_init() { server_log | grep -q 'Odamex Server Initialized'; }
server_up() { # romero's server and its UDP Service; Ready; the log says the map is loaded and the public master servers are not contacted
  "$POC/doom/pod.sh" server "$DOOM_IMG" | K romero apply -f - && K romero expose pod doom-server --name doom --port 10666 --protocol UDP &&
  K romero wait --for=condition=Ready pod/doom-server --timeout=240s &&
  wait_for 60 srv_init &&
  server_log | grep -q 'Masters will not be contacted because sv_usemasters is 0' && server_log | grep -q 'MAP15' &&
  say player "romero: a Doom deathmatch server (Odamex, FreeDM) in his namespace, on a UDP Service" && show player "sudo edge-mesh kubectl get pods,svc" K romero get pods,svc; }
robots_up() { # a robot pod and a UDP echo (the probe: an open party from a closed one, without joining the game) in every namespace
  local w; "$POC/doom/pod.sh" client "$DOOM_IMG" doom-bot | K romero apply -f - || return 1
  for w in carmack petersen; do "$POC/doom/pod.sh" client "$DOOM_IMG" | K "$w" apply -f - || return 1; done
  for w in $PEOPLE; do "$POC/doom/pod.sh" probe "$AGNHOST_IMAGE" | K "$w" apply -f - && K "$w" expose pod doom-probe --name doom-probe --port 10666 --protocol UDP || return 1; done
  for w in $PEOPLE; do K "$w" wait --for=condition=Ready "pod/$(pod_of "$w")" pod/doom-probe --timeout=240s || return 1; done
  say player "carmack and petersen: a robot pod each in their own namespace (a virtual screen, a recorder, a script that plays); romero's is beside his server"
  show player "sudo edge-mesh kubectl get pods" K carmack get pods; }
nobody() { # <who>'s robot reaches no server (exit 3: nobody answered) and the server log has no new connection of theirs: $2 = how many it may hold
  local o rc; o=$(show player "$(shown "$1" 10)" K "$1" exec "$(pod_of "$1")" -- env CONNECT_WAIT=15 client.sh "$SVC" 10666 10 "$1" 2>&1); rc=$?; echo "$o"; echo "exit $rc"
  [ "$rc" -eq 3 ] && echo "$o" | grep -q 'no server answered' && [ "$(connects "$1")" -eq "$2" ]; }
no_consent() { say player "romero runs the server in his namespace; his friends' robots try it. Nobody has agreed to anything yet"
  closed_now carmack romero && closed_now petersen romero && nobody carmack 0 && nobody petersen 0; }
one_side() { say player "romero invites carmack: his consent alone opens nothing"
  out_has "your consent is recorded" show player "sudo edge-mesh party invite carmack" P romero party invite carmack && tick &&
  out_has "waiting for carmack to accept" show player "sudo edge-mesh party list" P romero party list && closed_now carmack romero && nobody carmack 0 &&
  [ -z "$(k get networkpolicies -A -l edge-mesh/party -o name)" ]; }
open_party() { say player "carmack accepts: now both agreed"; say host "the host's tick: both consents, both namespaces live, so it opens the party"
  out_has "your consent is recorded" show player "sudo edge-mesh party accept romero" P carmack party accept romero &&
  out_has "party: romero -> carmack: active: ports 10666/UDP" show host "mesh host auto --once" tick &&
  out_has "active: ports 10666/UDP" show player "sudo edge-mesh party list" P carmack party list &&
  out_has "active: ports 10666/UDP" show host "mesh host party" "$MESH" host party list && wait_for 90 open_both carmack romero; }
start_match() { # romero's robot plays his own server (no party needed), then carmack's joins through the party and keeps playing; carmack's screen is the GIF
  say player "romero's robot joins his own server first (it is in his namespace: no party needed)"
  incus exec emesh-romero -- edge-mesh kubectl exec doom-bot -- env PATTERN=b CONNECT_WAIT=40 client.sh doom 10666 $((LONG + 60)) romero >"$STATE/romero.out" 2>&1 &
  BOT=$!; wait_for 60 sh -c "grep -q 'in the game' '$STATE/romero.out'" || return 1
  incus exec emesh-carmack -- edge-mesh kubectl exec doom-client -- env PATTERN=a RECORD=1 RECORD_AFTER=75 RECORD_SECONDS=30 CONNECT_WAIT=40 client.sh "$SVC" 10666 "$LONG" carmack >"$STATE/carmack.out" 2>&1 &
  CARM=$!; wait_for 90 sh -c "grep -q 'in the game' '$STATE/carmack.out'" && [ "$(connects carmack)" -eq 1 ]; }
watcher_up() { # D7: one authenticated spectator pod in carmack's namespace, private admission pins it to his own node
  localhost_always || return 1
  WATCH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))') || return 1
  WATCH_TOKEN="$WATCH_TOKEN" "$POC/doom/pod.sh" watcher "$DOOM_IMG" romero carmack carmack | K carmack apply -f - || return 1
  K carmack wait --for=condition=Ready "pod/$WATCH_NAME" --timeout=240s || return 1
  local pod; pod=$(K carmack get pod "$WATCH_NAME" -o json) || return 1
  read -r WATCH_IP node hostports < <(python3 -c 'import json,sys; p=json.load(sys.stdin); ports=[x for c in p["spec"]["containers"] for x in c.get("ports",[]) if "hostPort" in x]; print(p["status"].get("podIP", ""), p["spec"].get("nodeName", ""), len(ports))' <<<"$pod")
  echo "watcher pod: ip=$WATCH_IP node=$node hostPorts=$hostports"
  [ -n "$WATCH_IP" ] && [ "$node" = carmack ] && [ "$hostports" = 0 ] || return 1
  # `can-i create pods/portforward` would ask about a POD called portforward (yes: a player creates pods); the subresource is a flag
  local pf; pf=$(P carmack kubectl auth can-i create pods --subresource=portforward 2>&1); echo "carmack can-i create pods --subresource=portforward: $pf"
  [ "$(echo "$pf" | head -1)" = no ] || return 1
  wait_for 90 watcher_running && wait_for 60 watcher_announced || { watcher_why carmack; return 1; }
  # a restarted spectator connects twice (DM20 counts one): say why here, where it happened
  [ "$(K carmack get pod "$WATCH_NAME" -o jsonpath='{.status.containerStatuses[0].restartCount}')" = 0 ] || { watcher_why carmack; return 1; }
  K carmack logs "$WATCH_NAME" | grep -E 'following carmack|PLAYERS IN GAME|watch-carmack'
  server_log | grep 'watch-carmack has connected'
  echo "watch stream pod=$WATCH_NAME node=$node pod-ip=$WATCH_IP port=17777 hostPort=$hostports port-forward=denied"
}
localhost_always() { # Kubernetes' local node-to-pod path is pinned explicitly when the PoC uses Cilium
  [ "$CNI" != cilium ] && return 0
  local pod= conf
  for pod in $(k -n kube-system get pods -l k8s-app=cilium -o name); do
    conf=$(k -n kube-system exec "$pod" -- cilium-dbg config --all) || return 1
    echo "$conf" | grep -qiE 'allow.?localhost[^[:alnum:]]+always' || { echo "Cilium $pod does not report allow-localhost=always"; return 1; }
  done
  [ -n "$pod" ]
}
watcher_why() { # what the spectator saw, when it did not get there: its own output and Odamex's log (no token is in either)
  local who="$1" ns; ns=$([ "$who" = host ] && echo watch-host || echo "play-$who")
  local pod; pod=$([ "$who" = host ] && echo doom-watch-romero || echo "$WATCH_NAME")
  k -n "$ns" get pods -o wide; k -n "$ns" logs "$pod" --tail=40; k -n "$ns" logs "$pod" --previous --tail=40 2>&1 | sed 's/^/previous: /' 
  k -n "$ns" exec "$( [ "$who" = host ] && echo doom-watch-romero || echo "$WATCH_NAME")" -- sh -c 'tail -40 /tmp/.odamex/odamex.log; tail -20 /tmp/doom/client.out' 2>&1 | tail -60
  server_log | tail -15; }
watcher_running() { K carmack logs "$WATCH_NAME" 2>/dev/null | grep -q 'following carmack'; }
watcher_announced() { server_log | grep -q 'watch-carmack has connected'; }
watch_frame() { # token travels over stdin; it never appears in an exec argument or a log line
  local node="$1" token="$2" url="$3"
  printf '%s\n' "$token" | incus exec "emesh-$node" -- python3 -c 'import sys,urllib.request; u=sys.argv[1]; t=sys.stdin.readline().strip(); r=urllib.request.urlopen(urllib.request.Request(u,headers={"Authorization":"Bearer "+t}),timeout=5); sys.stdout.buffer.write(r.read())' "$url"
}
watch_code() { # status only, including connection failure as 000
  local node="$1" token="$2" url="$3"
  if [ -n "$token" ]; then
    printf '%s\n' "$token" | incus exec "emesh-$node" -- python3 -c 'import sys,urllib.request,urllib.error; u=sys.argv[1]; t=sys.stdin.readline().strip(); q=urllib.request.Request(u,headers={"Authorization":"Bearer "+t});
try: print(urllib.request.urlopen(q,timeout=5).status)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print("000")' "$url"
  else
    incus exec "emesh-$node" -- python3 -c 'import sys,urllib.request,urllib.error; u=sys.argv[1];
try: print(urllib.request.urlopen(u,timeout=5).status)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print("000")' "$url"
  fi
}
watcher_local_frames() { # D7: own node succeeds; missing/wrong credentials do not; port-forward is not granted
  local url="http://$WATCH_IP:17777/frame" code wrong tmp="$OUTD/watch.jpg" previous current changed=0
  code=$(watch_code carmack "" "$url") || return 1; [ "$code" = 401 ] || return 1
  wrong=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  code=$(watch_code carmack "$wrong" "$url"); [ "$code" = 401 ] || return 1
  wait_for 30 watch_frame_available || return 1
  watch_frame carmack "$WATCH_TOKEN" "$url" >"$tmp" || return 1
  python3 - "$tmp" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); b=p.read_bytes()
raise SystemExit(0 if b.startswith(b"\xff\xd8") and b.endswith(b"\xff\xd9") and 300 < len(b) < 2_000_000 else 1)
PY
  previous=$(sha256sum "$tmp" | cut -d' ' -f1)
  for _ in 1 2 3 4 5; do
    sleep 1
    watch_frame carmack "$WATCH_TOKEN" "$url" >"$tmp" || return 1
    current=$(sha256sum "$tmp" | cut -d' ' -f1)
    [ "$current" = "$previous" ] || { changed=1; break; }
  done
  [ "$changed" = 1 ] || return 1
  echo "watch stream: unauthenticated=401 wrong-token=401; player-node Pod-IP returned changing JPEG frames ($(wc -c <"$tmp") bytes); port-forward=denied"
}
watch_frame_available() { [ "$(watch_code carmack "$WATCH_TOKEN" "http://$WATCH_IP:17777/frame")" = 200 ]; }
third_out() { # carmack is in the game: petersen's robot still gets nothing (the party is carmack's and romero's), and carmack and petersen do not reach each other
  say player "carmack is playing. petersen's robot tries the same server: carmack's party with romero is nothing to her"
  closed_now petersen romero && nobody petersen 0 && closed_both carmack petersen; }
third_in() { say player "romero invites petersen; petersen accepts"; say host "a second party, romero and petersen: another pair, another policy"
  out_has "your consent is recorded" show player "sudo edge-mesh party invite petersen" P romero party invite petersen &&
  out_has "your consent is recorded" show player "sudo edge-mesh party accept romero" P petersen party accept romero &&
  out_has "party: romero -> petersen: active: ports 10666/UDP" show host "mesh host auto --once" tick &&
  out_has "active: ports 10666/UDP" show host "mesh host party" "$MESH" host party list &&
  wait_for 90 open_both petersen romero && open_both carmack romero && closed_both carmack petersen; }   # carmack and petersen stay closed: a party is a pair
watcher_isolation() { # the exact Pod IP is unreachable from another player's node and pod, despite a party to the game server
  local url="http://$WATCH_IP:17777/frame" node_code pod_code
  node_code=$(watch_code petersen "" "$url") || return 1
  pod_code=$(K petersen exec doom-client -- curl --silent --show-error --output /dev/null --write-out '%{http_code}' --connect-timeout 3 --max-time 5 "$url" 2>/dev/null) || pod_code=000
  [ "$node_code" = 000 ] && [ "$pod_code" = 000 ] || { echo "isolation failed: petersen node=$node_code pod=$pod_code"; return 1; }
  echo "watch stream Pod IP denied from petersen node and petersen namespace pod"
}
play_third() { # petersen's robot joins through her own party and plays in the same match
  local o; o=$(show player "$(shown petersen "$PLAY")" K petersen exec doom-client -- env PATTERN=c CONNECT_WAIT=40 client.sh "$SVC" 10666 "$PLAY" petersen 2>&1); echo "$o"
  echo "$o" | grep -q 'client petersen: map MAP15' && echo "$o" | grep -q 'client petersen: in the game' && echo "$o" | grep -q 'left the game'; }
finish_match() { # the long sessions end; carmack's screen was recorded
  wait "$CARM" "$BOT"; say player "carmack's session (played in the background while the others joined; the robot's own output)"
  show player "$(shown carmack "$LONG")" cat "$STATE/carmack.out" >/dev/null; grep -q 'left the game' "$STATE/carmack.out" && grep -q 'client carmack: map MAP15' "$STATE/carmack.out" || return 1
  K carmack exec doom-client -- cat /tmp/doom/match.gif >"$OUTD/match.gif" && [ "$(head -c 6 "$OUTD/match.gif")" = GIF89a ] && [ "$(wc -c <"$OUTD/match.gif")" -lt 5000000 ] &&
    echo "match.gif: $(wc -c <"$OUTD/match.gif") bytes"; }
server_saw() { # romero's server, not the clients, is the witness: the three connected, joined the game, stayed, left, with their frags and deaths
  local log; log=$(server_log) || return 1
  say host "romero's server is the witness: who connected, joined and left, and when, with frags and deaths"
  show host "kubectl exec -n play-romero doom-server -- grep -E 'Masters|MAP15|connected|joined|disconnected' /tmp/server.log" \
    k exec -n play-romero doom-server -- grep -E 'Masters|MAP15|connected|joined|disconnected' /tmp/server.log >/dev/null
  echo "$log" | grep -E 'has connected|joined the game|disconnected'
  echo "$log" | grep -q 'watch-carmack has connected' || return 1
  python3 - "$log" "$PLAY" <<'PY'
import re, sys
log, play = sys.argv[1], int(sys.argv[2])
def at(pat):
    m = re.search(r"\[(\d\d):(\d\d):(\d\d)\] " + pat, log)
    return m and int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
ok = True
for who, least in (("romero", 1), ("carmack", 3 * play - 20), ("petersen", play - 5)):
    c, j, d = at(who + r" has connected\."), at(who + r" joined the game\."), at(who + r" disconnected\. \((?:-?\d+) (?:KILLS|FRAGS), \d+ DEATHS\)")
    good = None not in (c, j, d) and c <= j < d and d - j >= least
    print(f"server: {who} joined and stayed {d - j if None not in (c, j, d) else '?'} s")
    ok = ok and good
sys.exit(0 if ok else 1)
PY
}
three_connects() { [ "$(connects romero)" -eq 1 ] && [ "$(connects carmack)" -eq 1 ] && [ "$(connects petersen)" -eq 1 ] &&
  [ "$(server_log | grep -c ' has connected\.')" -eq 4 ] && [ "$(server_log | grep -c '^\[[0-9:]*\] watch-carmack has connected\.')" -eq 1 ]; }
leave_closes() { say player "carmack leaves the party: the same robot, the same server, and nobody answers again; petersen's party stays"
  out_has "left: the host closes the party" show player "sudo edge-mesh party leave" P carmack party leave &&
  out_has "party: romero -> carmack: closed: carmack left the party" show host "mesh host auto --once" tick &&
  closed_both carmack romero && nobody carmack 1 && open_both petersen romero; }   # at once: no waiting, no other port; the same flow
idempotent() { # one pair is left (romero and petersen): its two policies; reconcile changes nothing
  local n; n=$(k get networkpolicies -A -l edge-mesh/party -o name | sort | tr '\n' ' ')
  [ "$n" = "networkpolicy.networking.k8s.io/party-petersen networkpolicy.networking.k8s.io/party-romero " ] &&
  out_has "party: unchanged" show host "mesh host party reconcile" "$MESH" host party reconcile && out_has "party: unchanged" "$MESH" host party reconcile; }
free_only() { # nothing non-free is in the image: FreeDM is the only wad, no doom.wad or doom2.wad anywhere, and both licences are there
  local o; o=$(K romero exec doom-server -- sh -c 'ls /opt/wad; find / -xdev -iname "doom*.wad" -o -iname "plutonia.wad" -o -iname "tnt.wad" -o -iname "freedoom*.wad" 2>/dev/null; ls /opt/licenses'); echo "$o"
  [ "$(echo "$o" | sed -n 1p)" = freedm.wad ] && ! echo "$o" | grep -qi '^/.*\.wad' && echo "$o" | grep -q 'odamex-LICENSE-GPL-2.0' &&
  echo "$o" | grep -q 'freedoom-COPYING-BSD-3-Clause.txt'; }

# D6 (#101): the host's watcher identity, watch-host, on the host's own machine
OWN=hostpc
watch_refused() { # with no own node but the control plane (cp is own; the players are friends), there is nowhere for the watcher to run
  local o rc; o=$("$MESH" host watch init 2>&1); rc=$?; echo "$o"; echo "exit $rc"
  [ "$rc" -eq 3 ] && echo "$o" | grep -q "not the control plane" && [ -z "$(k get namespace watch-host --ignore-not-found -o name)" ]; }
own_joins() { # the host issues its own machine (trust own, persistent CA) and it joins like n1 of poc-up; then the watcher can be made
  local ip="${LH_NEB%.*}.60"
  "$POC/issue.sh" "$OWN" "$ip" own "${OWN_CERT_DURATION:-2h}" && node_guard && vm_new "$OWN" &&
  incus exec "emesh-$OWN" -- edge-mesh join /bundle --accept-golden-rule && wait_for 420 ready "$OWN" &&
  k label node "$OWN" node-restriction.kubernetes.io/trust=own --overwrite >/dev/null || return 1
  say host "the host's own machine joins (trust own, not the control plane): the host can now have a watcher, watch-host"
  out_has "its pods run on: $OWN" show host "mesh host watch init" "$MESH" host watch init || return 1
  # no credential for anyone but the host: no ServiceAccount but the default, no Role, no RoleBinding; restricted; its own quota
  [ "$(k get namespace watch-host -o jsonpath='{.metadata.labels.edge-mesh/watcher}')" = host ] &&
  [ "$(k get namespace watch-host -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}')" = restricted ] &&
  [ -z "$(k get roles,rolebindings -n watch-host -o name)" ] && [ "$(k get serviceaccounts -n watch-host -o name)" = serviceaccount/default ] &&
  [ -n "$(k get resourcequota pool -n watch-host -o name)" ] && out_has "watcher: namespace watch-host" "$MESH" host watch status; }
HK() { k -n watch-host "$@"; }   # the host's own kubectl in its watcher namespace (cluster-admin: the host needs no consent to write there)
host_probe() { HK exec doom-client -- client.sh --probe doom-probe.play-romero.svc.cluster.local 10666; }
host_closed() { local rc; host_probe; rc=$?; [ "$rc" -eq 3 ]; }
host_watch_one_side() { # the host asks romero; one consent opens nothing: a client in watch-host, on hostpc, gets no answer from romero's namespace
  incus file push -q "$TAR" "emesh-$OWN/root/doom.tar" && incus exec "emesh-$OWN" -- sh -c 'k3s ctr -n k8s.io images import /root/doom.tar >/dev/null && rm -f /root/doom.tar' || return 1
  "$POC/doom/pod.sh" client "$DOOM_IMG" | HK apply -f - && HK wait --for=condition=Ready pod/doom-client --timeout=240s || return 1
  [ "$(HK get pod doom-client -o jsonpath='{.spec.nodeName}')" = "$OWN" ] || { echo "the watcher's pod is not on $OWN"; return 1; }
  say host "the host asks romero to let it watch: its half of a party, like any player's invite"
  out_has "the party opens when romero runs" show host "mesh host watch invite romero" "$MESH" host watch invite romero &&
  out_has "waiting for romero to accept" "$MESH" host watch status && host_closed &&
  [ -z "$(k get networkpolicies -n watch-host -l edge-mesh/party -o name)" ]; }
host_following() { HK logs doom-watch-romero 2>/dev/null | grep -q 'following romero'; }
host_announced() { server_log | grep -q 'watch-host has connected'; }
host_watch_open() { # romero accepts watch-host; the tick opens it; the host's spectator, on hostpc, follows romero's robot back in the game and is announced
  say player "romero accepts the host as a spectator of his server: the same consent as a player's"
  out_has "your consent is recorded" show player "sudo edge-mesh party accept watch-host" P romero party accept watch-host &&
  out_has "party: romero -> watch-host: active: ports 10666/UDP" show host "mesh host auto --once" tick &&
  wait_for 90 host_probe || return 1
  # the match of DM11-DM18 is over: romero's robot plays again, so there is somebody to follow
  incus exec emesh-romero -- edge-mesh kubectl exec doom-bot -- env PATTERN=b CONNECT_WAIT=40 client.sh doom 10666 120 romero >"$STATE/romero2.out" 2>&1 &
  BOT2=$!; wait_for 60 sh -c "grep -q 'in the game' '$STATE/romero2.out'" || return 1
  [ "$("$POC/doom/build.sh" --name)" = "$DOOM_IMG" ] || { echo "build.sh --name is not the image this run built"; return 1; }
  out_has "watcher: spectator of romero's game, following romero" show host "mesh host watch start romero" "$MESH" host watch start romero &&
  HK wait --for=condition=Ready pod/doom-watch-romero --timeout=240s || return 1
  local node; node=$(HK get pod doom-watch-romero -o jsonpath='{.spec.nodeName}'); echo "host spectator on $node"
  [ "$node" = "$OWN" ] && wait_for 90 host_following && wait_for 60 host_announced || { watcher_why host; return 1; }
  say host "romero's server names the host's spectator: nobody is watched without a line saying who came in"
  show host "kubectl exec -n play-romero doom-server -- grep 'watch-host has connected' /tmp/server.log" \
    k exec -n play-romero doom-server -- grep 'watch-host has connected' /tmp/server.log && host_views; }
host_views() { # the host console's reads: GET /watch (status --json --read-only) and GET /watch/romero/frame (frame), the same engine calls
  local st; st=$("$MESH" host watch status --json --read-only) || return 1; echo "$st"
  echo "$st" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d["spectators"]; o=sys.argv[1]
ok = d["watcher"] and any(p["owner"]=="romero" and p["status"].startswith("active") for p in d["parties"]) and [(x["owner"], x["node"], x["following"], x["ready"]) for x in s]==[("romero", o, "romero", True)]
sys.exit(0 if ok else 1)' "$OWN" || return 1
  # a spectator just Ready may have no picture for some seconds (Odamex still connecting): wait for the first, then read the second
  wait_for 60 sh -c "\"$MESH\" host watch frame romero >\"$STATE/host1.jpg\" 2>/dev/null" && sleep 2 &&
    "$MESH" host watch frame romero >"$STATE/host2.jpg" || return 1
  python3 - "$STATE/host1.jpg" "$STATE/host2.jpg" <<'PY' || return 1
import sys
a, b = (open(f, "rb").read() for f in sys.argv[1:3])
print("host frames:", len(a), len(b), "bytes; changed:", a != b)
sys.exit(0 if all(x[:2] == b"\xff\xd8" and x[-2:] == b"\xff\xd9" and len(x) > 1000 for x in (a, b)) and a != b else 1)
PY
  say host "the host's frames come through the API server to its own pod: nothing new listens, the mesh carries no frame for the host"
  [ -z "$(HK get pod doom-watch-romero -o jsonpath='{.spec.containers[*].ports[*].hostPort}')" ] && console_frames; }
console_frames() { # the same frames as the Watch view gets them: the console daemon's GET /api/v1/watch/romero/frame with the link's token,
  # through its persistent channel (watcher.Tap), the daemon's own environment and its rate limit; then the daemon stops (nothing later needs it)
  local web="127.0.0.1:${DOOM_WEB_PORT:-18093}" link tok pid rc=0
  "$MESH" host console --web "$web" >>"$LOG" 2>&1 & pid=$!
  wait_for 30 python3 -c "import socket; socket.create_connection(('${web%:*}', ${web##*:}), 2)" && link=$("$MESH" host console url --web "$web") || { kill "$pid"; wait "$pid" 2>/dev/null; return 1; }
  tok="${link##*#t=}"
  local get; get=$(cat <<'PY'
import sys, time, urllib.request
t = sys.stdin.readline().strip()
got = []
for _ in range(20):   # the first answers may be 502 while the channel opens
    try:
        r = urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer " + t}), timeout=30)
        b = r.read()
        if r.headers.get("Content-Type") == "image/jpeg" and b[:2] == b"\xff\xd8" and b[-2:] == b"\xff\xd9":
            got.append(b)
    except Exception:
        pass
    if len(got) >= 2 and got[-1] != got[0]:
        break
    time.sleep(1)
print("console daemon frames:", len(got), "JPEGs; changed:", len(got) >= 2 and got[-1] != got[0])
sys.exit(0 if len(got) >= 2 and got[-1] != got[0] else 1)
PY
)
  printf '%s\n' "$tok" | python3 -c "$get" "http://$web/api/v1/watch/romero/frame" || rc=1
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return $rc; }
guard_try() { # guard_try <what> <python expression on the pod text t>: a server-side dry run in watch-host (every admission runs, nothing is made)
  # small and valid (request <= limit, else the API refuses the object before any admission): the client (400m, 384Mi) and the spectator
  # (300m, 256Mi) already count against watch-host's 1000m and 1024Mi; a refusal must be the guard's, not the quota's or the schema's
  "$POC/doom/pod.sh" client "$DOOM_IMG" guard-probe |
    sed 's/requests: {cpu: 200m, memory: 128Mi}, limits: {cpu: 400m, memory: 384Mi/requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 100m, memory: 128Mi/' | python3 -c "import sys; t=sys.stdin.read(); sys.stdout.write($2)" |
    k -n watch-host apply --dry-run=server -o json -f - 2>&1; }
watcher_guard() { # each case is refused by poc/watcher-policy.yaml itself: Kyverno lets nodeName cp through (cp is own), and the cp taint is not a trust taint
  local o
  o=$(guard_try nodeName 't.replace("spec:\n", "spec:\n  nodeName: cp\n", 1)'); echo "nodeName cp: $o" | head -3
  echo "$o" | grep -q "may not set spec.nodeName" || return 1
  o=$(guard_try toleration 't.replace("spec:\n", "spec:\n  tolerations: [{key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule}]\n", 1)')
  echo "control-plane toleration: $o" | head -3; echo "$o" | grep -q "may not tolerate the control plane" || return 1
  o=$(guard_try everything 't.replace("spec:\n", "spec:\n  tolerations: [{operator: Exists}]\n", 1)')   # an empty key tolerates every taint
  echo "tolerate everything: $o" | head -3; echo "$o" | grep -q "may not tolerate the control plane" || return 1
  o=$(guard_try shareable 't.replace("labels: {app: guard-probe}", "labels: {app: guard-probe, edge-mesh/sensitivity: shareable}", 1)')
  echo "shareable: $o" | head -3; echo "$o" | grep -q "a watcher pod is private" || return 1
  # positive control: the plain pod is admitted, and what admission made of it keeps it off every control plane
  o=$(guard_try plain 't') || { echo "$o"; return 1; }
  echo "$o" | python3 -c 'import json,sys; p=json.load(sys.stdin); e=p["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]; print("admitted, affinity:", e); sys.exit(0 if {"key":"node-role.kubernetes.io/control-plane","operator":"DoesNotExist"} in e else 1)'; }
play_post() { # play_post <node> <token> <url> <json>: the HTTP status of one POST /input from that node (000: no answer); token on stdin
  printf '%s\n' "$2" | incus exec "emesh-$1" -- python3 -c 'import sys,urllib.request,urllib.error; u,b=sys.argv[1],sys.argv[2].encode(); t=sys.stdin.readline().strip()
q=urllib.request.Request(u,data=b,method="POST",headers={"Authorization":"Bearer "+t,"Content-Type":"application/json"})
try: print(urllib.request.urlopen(q,timeout=5).status)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print("000")' "$3" "$4"; }
play_joins() { [ "$(server_log | grep -c 'petersen joined the game')" -gt "$PLAY_JOINED" ]; }
play_up() { # #102: petersen plays romero's game from her own node, the way her console's Play button does it (pod.sh player, her kubeconfig)
  say player "petersen plays from her browser: her own game client runs on her machine, and only the picture and her keys cross to her tab"
  PLAY_JOINED=$(server_log | grep -c 'petersen joined the game')
  PLAY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))') || return 1
  K petersen delete pod doom-client --ignore-not-found --wait=true >/dev/null || return 1   # her robot is done: its quota goes to her own game
  WATCH_TOKEN="$PLAY_TOKEN" "$POC/doom/pod.sh" player "$DOOM_IMG" romero petersen | K petersen apply -f - >/dev/null &&
  K petersen wait --for=condition=Ready pod/doom-play-romero --timeout=240s || return 1
  local node; node=$(K petersen get pod doom-play-romero -o jsonpath='{.spec.nodeName}'); PLAY_IP=$(K petersen get pod doom-play-romero -o jsonpath='{.status.podIP}')
  echo "play pod on $node, pod IP $PLAY_IP, hostPort '$(K petersen get pod doom-play-romero -o jsonpath='{.spec.containers[*].ports[*].hostPort}')'"
  [ "$node" = petersen ] && [ -z "$(K petersen get pod doom-play-romero -o jsonpath='{.spec.containers[*].ports[*].hostPort}')" ] &&
    [ "$(K petersen get pod doom-play-romero -o jsonpath='{.spec.containers[0].imagePullPolicy}')" = Never ] && wait_for 90 play_joins || return 1
  show host "kubectl exec -n play-romero doom-server -- grep 'petersen joined the game' /tmp/server.log" \
    k exec -n play-romero doom-server -- grep 'petersen joined the game' /tmp/server.log; }
play_keys() { # #102 (WB07, WB08, the spike's numbers): from her node her keys move her game; nothing else gets in
  local url="http://$PLAY_IP:17777" d="$OUTD/play"; mkdir -p "$d"
  [ "$(play_post petersen "" "$url/input" '{"down":["ArrowLeft"]}')" = 401 ] || return 1                       # no secret
  [ "$(play_post petersen "$PLAY_TOKEN" "$url/input" '{"down":["`"]}')" = 400 ] || return 1                     # the console key
  [ "$(play_post petersen "$PLAY_TOKEN" "$url/input" '{"down":["Return"]}')" = 400 ] || return 1                # Enter (chat)
  [ "$(play_post carmack "$PLAY_TOKEN" "$url/input" '{"down":["ArrowLeft"]}')" = 000 ] || return 1              # another player's node
  wait_for 30 sh -c "[ \"\$(printf '%s\n' '$PLAY_TOKEN' | incus exec emesh-petersen -- python3 -c 'import sys,urllib.request; t=sys.stdin.readline().strip(); print(urllib.request.urlopen(urllib.request.Request(\"$url/frame\",headers={\"Authorization\":\"Bearer \"+t}),timeout=5).status)' 2>/dev/null)\" = 200 ]" || return 1
  watch_frame petersen "$PLAY_TOKEN" "$url/frame" >"$d/c.jpg" && sleep 1.5 && watch_frame petersen "$PLAY_TOKEN" "$url/frame" >"$d/d.jpg" || return 1   # no keys
  watch_frame petersen "$PLAY_TOKEN" "$url/frame" >"$d/a.jpg" &&
  [ "$(play_post petersen "$PLAY_TOKEN" "$url/input" '{"down":["ArrowLeft"]}')" = 204 ] && sleep 1.5 &&
  [ "$(play_post petersen "$PLAY_TOKEN" "$url/input" '{"up":["ArrowLeft"]}')" = 204 ] && sleep 0.3 &&
  watch_frame petersen "$PLAY_TOKEN" "$url/frame" >"$d/b.jpg" || return 1
  diff_of() { cat "$1" "$2" | K petersen exec -i doom-play-romero -- ffmpeg -hide_banner -f image2pipe -c:v mjpeg -i - \
    -vf "tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG" -f null - 2>&1 | sed -n 's/.*YAVG=\([0-9.]*\).*/\1/p' | tail -1; }
  local idle moved; idle=$(diff_of "$d/c.jpg" "$d/d.jpg"); moved=$(diff_of "$d/a.jpg" "$d/b.jpg")
  # the spike's numbers: the round trip of one key and of one frame, from her node to her pod (median of 10)
  printf '%s\n' "$PLAY_TOKEN" | incus exec emesh-petersen -- python3 -c 'import sys,time,statistics,urllib.request; t=sys.stdin.readline().strip(); u=sys.argv[1]
h={"Authorization":"Bearer "+t,"Content-Type":"application/json"}
def ms(f):
    s=time.monotonic(); f(); return (time.monotonic()-s)*1000
k=[ms(lambda: urllib.request.urlopen(urllib.request.Request(u+"/input",data=b"{\"down\":[\"1\"],\"up\":[\"1\"]}",method="POST",headers=h),timeout=5).read()) for _ in range(10)]
f=[ms(lambda: urllib.request.urlopen(urllib.request.Request(u+"/frame",headers=h),timeout=5).read()) for _ in range(10)]
print("spike: key round trip %.1f ms, frame round trip %.1f ms (median of 10, from her node); frames at 10 per second" % (statistics.median(k), statistics.median(f)))' "$url"
  echo "mean luma difference: without keys $idle, after 1.5 s of ArrowLeft $moved"
  python3 -c 'import sys; i,m=float(sys.argv[1] or 0),float(sys.argv[2] or 0); sys.exit(0 if m >= 8 and m > 2*i else 1)' "$idle" "$moved"; }
host_game() { # #102, host mode: game stop ends romero's server (typed confirmation on the web); game start brings it back, on his node
  out_has "game: romero's server stopped" show host "mesh host game stop romero" "$MESH" host game stop romero &&
  wait_for 60 sh -c "[ -z \"\$(\"$POC/kc\" get pod doom-server -n play-romero --ignore-not-found -o name)\" ]" &&
  [ -z "$(k get service doom -n play-romero --ignore-not-found -o name)" ] || return 1
  out_has "game: romero's server starting" show host "mesh host game start romero" "$MESH" host game start romero &&
  K romero wait --for=condition=Ready pod/doom-server --timeout=240s && wait_for 60 srv_init &&
  [ "$(k get pod doom-server -n play-romero -o jsonpath='{.spec.nodeName}')" = romero ] &&
  out_has "runs already" "$MESH" host game start romero &&
  K petersen delete pod doom-play-romero --ignore-not-found --wait=false >/dev/null; }
watch_cap() { # WB05: the host's spectator counts against watch-host's quota (the cap on the host's views); at the cap one more is refused
  # by the API server. Also the measure the cap is sized from: the spectator's real CPU over 20 s, from its own cgroup (cpu.stat).
  local u1 u2 q rq used o
  u1=$(HK exec doom-watch-romero -- sh -c 'grep usage_usec /sys/fs/cgroup/cpu.stat' | awk '{print $2}') && sleep 20 &&
  u2=$(HK exec doom-watch-romero -- sh -c 'grep usage_usec /sys/fs/cgroup/cpu.stat' | awk '{print $2}') || return 1
  echo "spectator CPU (following romero's robot): $(( (u2 - u1) / 20000 ))m over 20 s; its limit $(HK get pod doom-watch-romero -o jsonpath='{.spec.containers[0].resources.limits.cpu}'), its request $(HK get pod doom-watch-romero -o jsonpath='{.spec.containers[0].resources.requests.cpu}')"
  rq=$(HK get resourcequota -o jsonpath='{.items[0].metadata.name}') && q=$(HK get resourcequota "$rq" -o json) || return 1
  echo "$q" | python3 -c 'import json,sys; s=json.load(sys.stdin)["status"]; print("watch-host quota: hard", s["hard"], "used", s["used"])'
  used=$(echo "$q" | python3 -c 'import json,sys; u=json.load(sys.stdin)["status"]["used"]; print(u.get("pods","0"))')
  echo "$q" | python3 -c 'import json,sys; u=json.load(sys.stdin)["status"]["used"]; c=u.get("limits.cpu","0"); m=int(c[:-1]) if c.endswith("m") else int(float(c)*1000); sys.exit(0 if m >= 300 else 1)' || { echo "the spectator is not counted"; return 1; }
  say host "the host's views have a cap: watch-host's quota. Lowered to what runs now, one more spectator is refused by the API server"
  HK patch resourcequota "$rq" --type merge -p "{\"spec\":{\"hard\":{\"pods\":\"$used\"}}}" >/dev/null || return 1
  sleep 3   # the quota controller records the new hard limit
  o=$(WATCH_TOKEN=$(printf '%064d' 0) "$POC/doom/pod.sh" watcher "$DOOM_IMG" romero host romero | sed 's/doom-watch-romero/doom-watch-extra/g' | HK apply --dry-run=server -f - 2>&1)
  HK patch resourcequota "$rq" --type merge -p "$(echo "$q" | python3 -c 'import json,sys; print(json.dumps({"spec":{"hard":{"pods":json.load(sys.stdin)["spec"]["hard"]["pods"]}}}))')" >/dev/null || return 1
  echo "one more spectator at the cap: $o"; echo "$o" | grep -q "exceeded quota"; }
host_watch_closes() { # romero's second robot ended; the host leaves (the party closes), then removes its watcher
  wait "$BOT2" && grep -q 'left the game' "$STATE/romero2.out" || { cat "$STATE/romero2.out"; return 1; }
  out_has "watcher: left romero" show host "mesh host watch leave romero" "$MESH" host watch leave romero &&
  out_has "watcher: stopped the spectator of romero" show host "mesh host watch stop romero" "$MESH" host watch stop romero &&
  [ ! -e "$STATE/watch/romero.token" ] && ! "$MESH" host watch frame romero >/dev/null 2>&1 &&
  [ -z "$(k get networkpolicies -n watch-host -l edge-mesh/party -o name)" ] && [ -z "$(k get networkpolicies -n play-romero party-watch-host --ignore-not-found -o name)" ] &&
  wait_for 60 host_closed && out_has "watcher: removed" "$MESH" host watch down &&
  [ -z "$(k get namespace watch-host --ignore-not-found -o name)" ] && out_has "watcher: none" "$MESH" host watch status; }

if [ -n "${UNTIL:-}" ]; then   # stop after that step, the VMs kept: what runs then is left running
  [[ "$UNTIL" =~ ^DM[0-9]{2}$ ]] || { echo "UNTIL=$UNTIL: a step id like DM27"; exit 2; }
  eval "_$(declare -f step)"
  step() { _step "$@"; [ "$1" != "$UNTIL" ] || { echo "----"; if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS (until $UNTIL, the machines kept)"; else echo "RESULT FAIL ($FAILS)"; fi; exit "$FAILS"; }; }
fi
step DM01 "mesh host init: lighthouse and control plane (and Cilium with CNI=cilium)" host_init
step DM02 "the Doom image (Odamex 12.3.0, FreeDM 0.13.0, pinned) is built once and cached by the hash of its inputs" image
step DM03 "romero, carmack and petersen ask; one tick admits all three" admitted
step DM04 "the three join and are Ready" joined
step DM05 "the image is loaded into the three nodes (no registry, nothing pulled)" loaded
step DM06 "romero: the server (a pod, a UDP Service on 10666) is up on FreeDM MAP15 and not announced to the public master servers" server_up
step DM07 "the robots: one in each namespace (romero's beside his server), and a UDP echo in each, all Ready" robots_up
step DM08 "no consent: carmack's and petersen's robots get no answer from romero's server, and his log has no player" no_consent
step DM09 "romero invites carmack: one consent is not a party; carmack still gets no answer and the log has no player" one_side
step DM10 "carmack accepts: the tick opens 10666/UDP between romero and carmack" open_party
step DM11 "the deathmatch starts: romero's robot, then carmack's through the party, both in the game" start_match
step DM12 "D7: a spectator follows carmack in a watcher pod pinned to carmack's node; server and client log name it" watcher_up
step DM13 "D7: same-node Pod-IP returns authenticated changing JPEG frames; no port-forward or hostPort" watcher_local_frames
step DM14 "with carmack playing, petersen still gets no answer and carmack and petersen do not reach each other (negative)" third_out
step DM15 "romero invites petersen and she accepts: a second pair opens; carmack and petersen stay closed, both ways" third_in
step DM16 "D7: petersen's node and pod cannot reach carmack's watcher Pod IP" watcher_isolation
step DM17 "petersen's robot joins through her own party, sees MAP15, runs and shoots, quits: three players" play_third
step DM18 "the long sessions end; carmack's screen is a GIF" finish_match
step DM19 "the server is the witness: the three players and announced spectator connected, joined/stayed/left" server_saw
step DM20 "the server saw exactly three match players, one each, and no extra match player" three_connects
step DM21 "carmack leaves: the tick closes the party, he gets no answer again, petersen's party stays open" leave_closes
step DM22 "two policies are left (romero and petersen's pair); reconcile twice says unchanged" idempotent
step DM23 "nothing non-free is in the image: FreeDM only, no doom.wad or doom2.wad, both licences present" free_only
step DM24 "D6: with no own node but the control plane, mesh host watch init refuses and makes nothing" watch_refused
step DM25 "D6: the host's own machine joins (trust own); watch init makes watch-host: restricted, its own quota, no credential but the host's" own_joins
step DM26 "D6: the host asks romero (watch invite): one consent opens nothing; a pod of watch-host, on hostpc, gets no answer" host_watch_one_side
step DM27 "D6: romero accepts watch-host; watch start runs the host's spectator on hostpc, not the control plane; the server names it; the host's views (the CLI and the console daemon) read its frames" host_watch_open
step DM28 "D6: the watcher guard refuses a watch-host pod that names the control plane, tolerates its taint or is shareable; a plain one gets the affinity" watcher_guard
step DM29 "WB05: the host's spectator counts against watch-host's quota and, at the cap, one more is refused; its real CPU is measured" watch_cap
step DM30 "D6: watch leave closes the host's party at once; watch stop removes the spectator and its secret; watch down removes watch-host; romero's second robot ended cleanly" host_watch_closes
step DM31 "#102: petersen plays romero's game from her browser's path: her own game client on her node, no hostPort; the server says she joined" play_up
step DM32 "#102: from her node her keys move her game (frames change with keys, hardly without); no secret, the console key, Enter and another node are refused; latency measured" play_keys
step DM33 "#102: the host stops romero's game server (game stop) and starts it again (game start) on his node; a second start changes nothing" host_game

[ -z "$SC" ] || [ "$FAILS" -ne 0 ] || { cp "$OUTD/match.gif" "$SC/match.gif"; for s in player host; do   # only a run that passed becomes a recording (the GIF first: down removes the state)
  python3 "$SCP" redact "$SC/$s.raw" "$SC/$s.tsv" --strip "$STATE/" --strip "$POC/" --strip "${POC%/*}/" || FAILS=$((FAILS+1)); done; }
if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
