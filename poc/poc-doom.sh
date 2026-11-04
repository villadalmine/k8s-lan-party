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
# POOL_LEASE (default 1800 s) must cover DM03-DM18. KEEP=1 leaves the VMs. SHOWCASE=<dir> also writes the player's and the host's screens there.
set -u
export RUNTIME=incus MAX_VMS=5 CNI="${CNI:-cilium}" TAINT=no   # TAINT pinned as poc-pool (issue #59)
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-doom.log"; : >"$LOG"
MESH="$POC/mesh"
LEASE="${POOL_LEASE:-1800}"
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
  IMG=$(echo "$out" | tail -2 | head -1); TAR=$(echo "$out" | tail -1); echo "$IMG $TAR"; [ -s "$TAR" ] && [ -n "$IMG" ]; }
admitted() { "$MESH" host request romero --ip 198.51.100.10 && "$MESH" host request carmack --ip 198.51.100.20 && "$MESH" host request petersen --ip 198.51.100.30 &&
  "$MESH" host auto on && say host "three friends ask for a place; one tick of the host admits all three" && out_has "approved 3 | slots 3/3 used" show host "mesh host auto --once" tick; }
joined() { local w; for w in $PEOPLE; do "$MESH" play "$w" || return 1; done; for w in $PEOPLE; do wait_for 420 ready "$w" || return 1; done; }
loaded() { # no registry: the tarball goes into each player's node (k3s ctr), which then has the image and pulls nothing
  local who; for who in $PEOPLE; do
    incus file push -q "$TAR" "emesh-$who/root/doom.tar" && incus exec "emesh-$who" -- sh -c 'k3s ctr -n k8s.io images import /root/doom.tar >/dev/null && rm -f /root/doom.tar' &&
    incus exec "emesh-$who" -- sh -c "k3s crictl images 2>/dev/null | grep -q 'emesh-doom'" || return 1; done; }
srv_init() { server_log | grep -q 'Odamex Server Initialized'; }
server_up() { # romero's server and its UDP Service; Ready; the log says the map is loaded and the public master servers are not contacted
  "$POC/doom/pod.sh" server "$IMG" | K romero apply -f - && K romero expose pod doom-server --name doom --port 10666 --protocol UDP &&
  K romero wait --for=condition=Ready pod/doom-server --timeout=240s &&
  wait_for 60 srv_init &&
  server_log | grep -q 'Masters will not be contacted because sv_usemasters is 0' && server_log | grep -q 'MAP15' &&
  say player "romero: a Doom deathmatch server (Odamex, FreeDM) in his namespace, on a UDP Service" && show player "sudo edge-mesh kubectl get pods,svc" K romero get pods,svc; }
robots_up() { # a robot pod and a UDP echo (the probe: an open party from a closed one, without joining the game) in every namespace
  local w; "$POC/doom/pod.sh" client "$IMG" doom-bot | K romero apply -f - || return 1
  for w in carmack petersen; do "$POC/doom/pod.sh" client "$IMG" | K "$w" apply -f - || return 1; done
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
third_out() { # carmack is in the game: petersen's robot still gets nothing (the party is carmack's and romero's), and carmack and petersen do not reach each other
  say player "carmack is playing. petersen's robot tries the same server: carmack's party with romero is nothing to her"
  closed_now petersen romero && nobody petersen 0 && closed_both carmack petersen; }
third_in() { say player "romero invites petersen; petersen accepts"; say host "a second party, romero and petersen: another pair, another policy"
  out_has "your consent is recorded" show player "sudo edge-mesh party invite petersen" P romero party invite petersen &&
  out_has "your consent is recorded" show player "sudo edge-mesh party accept romero" P petersen party accept romero &&
  out_has "party: romero -> petersen: active: ports 10666/UDP" show host "mesh host auto --once" tick &&
  out_has "active: ports 10666/UDP" show host "mesh host party" "$MESH" host party list &&
  wait_for 90 open_both petersen romero && open_both carmack romero && closed_both carmack petersen; }   # carmack and petersen stay closed: a party is a pair
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
  python3 - "$log" "$PLAY" <<'PY'
import re, sys
log, play = sys.argv[1], int(sys.argv[2])
def at(pat):
    m = re.search(r"\[(\d\d):(\d\d):(\d\d)\] " + pat, log)
    return m and int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
ok = True
for who, least in (("romero", 1), ("carmack", 3 * play - 20), ("petersen", play - 5)):
    c, j, d = at(who + r" has connected\."), at(who + r" joined the game\."), at(who + r" disconnected\. \((?:\d+) (?:KILLS|FRAGS), \d+ DEATHS\)")
    good = None not in (c, j, d) and c <= j < d and d - j >= least
    print(f"server: {who} joined and stayed {d - j if None not in (c, j, d) else '?'} s")
    ok = ok and good
sys.exit(0 if ok else 1)
PY
}
three_connects() { [ "$(connects romero)" -eq 1 ] && [ "$(connects carmack)" -eq 1 ] && [ "$(connects petersen)" -eq 1 ] && [ "$(server_log | grep -c ' has connected\.')" -eq 3 ]; }
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
step DM12 "with carmack playing, petersen still gets no answer and carmack and petersen do not reach each other (negative)" third_out
step DM13 "romero invites petersen and she accepts: a second pair opens; carmack and petersen stay closed, both ways" third_in
step DM14 "petersen's robot joins through her own party, sees MAP15, runs and shoots, quits: three players" play_third
step DM15 "the long sessions end; carmack's screen is a GIF" finish_match
step DM16 "the server is the witness: the three connected, joined, stayed and left, each with frags and deaths on one line" server_saw
step DM17 "the server saw exactly three connections, one each, and nobody else" three_connects
step DM18 "carmack leaves: the tick closes his party, he gets no answer again, petersen's party stays open" leave_closes
step DM19 "two policies are left (romero and petersen's pair); reconcile twice says unchanged" idempotent
step DM20 "nothing non-free is in the image: FreeDM only, no doom.wad or doom2.wad, both licences present" free_only

[ -z "$SC" ] || [ "$FAILS" -ne 0 ] || { cp "$OUTD/match.gif" "$SC/match.gif"; for s in player host; do   # only a run that passed becomes a recording (the GIF first: down removes the state)
  python3 "$SCP" redact "$SC/$s.raw" "$SC/$s.tsv" --strip "$POC/" --strip "${POC%/*}/" || FAILS=$((FAILS+1)); done; }
if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
