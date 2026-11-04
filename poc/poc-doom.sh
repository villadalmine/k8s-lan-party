#!/usr/bin/env bash
# make poc-doom: the Doom demo over a LAN party, part B of issue #75 (DESIGN.md section 14.3), end to end on Incus VMs. alice and bob join; alice's
# namespace runs an Odamex server with Freedoom on 10666/UDP; an automated Odamex client in bob's namespace plays on it for 40 s. The match goes over
# Nebula and through the party's NetworkPolicy pair, nothing else: without consent, with one side's consent and after `leave` the same client gets no
# answer and alice's server log has no player. The proof is on the server (it logs who connected, joined and left, and when) and on the client (it
# received the map); the client also records its virtual screen, a GIF of the match (SHOWCASE keeps it, make showcase-doom). The image has Odamex
# (GPL) and Freedoom (BSD) and nothing non-free (no doom.wad, no doom2.wad); it is built once (poc/doom/build.sh) and loaded into the nodes.
# Needs a Linux host with Incus. PASS/FAIL per step; identical across runs. CNI=cilium by default (the demo's CNI); CNI=flannel also runs.
# POOL_LEASE (default 1800 s) must cover DM03-DM13. KEEP=1 leaves the VMs. SHOWCASE=<dir> also writes the player's and the host's screens there.
set -u
export RUNTIME=incus MAX_VMS=4 CNI="${CNI:-cilium}" TAINT=no   # TAINT pinned as poc-pool (issue #59)
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-doom.log"; : >"$LOG"
MESH="$POC/mesh"
LEASE="${POOL_LEASE:-1800}"
PLAY="${DOOM_SECONDS:-40}"   # how long the client plays
export POOL_CONFIG="$STATE/pool-test.yaml"
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^slots: [0-9]*/slots: 2/" -e "s/^idle_kick_seconds: [0-9]*/idle_kick_seconds: 0/" \
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
SVC=doom.play-alice.svc.cluster.local
CLIENT="sudo edge-mesh kubectl exec doom-client -- client.sh $SVC 10666"   # what bob types (shown on the player's screen)
play() { # play <seconds> [RECORD=1]: bob's client, in bob's namespace, plays on alice's server by the Service name; its screen is the output
  local rec="${2:-0}"; show player "$CLIENT $1 bob" K bob exec doom-client -- env RECORD="$rec" CONNECT_WAIT=40 client.sh "$SVC" 10666 "$1" bob; }
PROBE=doom-probe.play-alice.svc.cluster.local
probe() { K bob exec doom-client -- client.sh --probe "$PROBE" 10666; }   # is alice's namespace reachable on 10666/UDP right now (no game, no log line)
open_now() { probe; }
closed_now() { local rc; probe; rc=$?; [ "$rc" -eq 3 ]; }   # exactly 3: no answer; any other failure is not a closed party
server_log() { K alice exec doom-server -- cat /tmp/server.log; }
connects() { server_log | grep -c ' has connected\.'; }

host_init() { "$MESH" host init && { [ "$CNI" != cilium ] || "$POC/cilium.sh"; }; }
image() { # the image, once (cached by the hash of its inputs), and its name
  local out; out=$("$POC/doom/build.sh") || { echo "$out"; return 1; }
  IMG=$(echo "$out" | tail -2 | head -1); TAR=$(echo "$out" | tail -1); echo "$IMG $TAR"; [ -s "$TAR" ] && [ -n "$IMG" ]; }
admitted() { "$MESH" host request alice --ip 198.51.100.10 && "$MESH" host request bob --ip 198.51.100.20 &&
  "$MESH" host auto on && say host "two friends ask for a place; one tick of the host admits both" && out_has "approved 2 | slots 2/2 used" show host "mesh host auto --once" tick; }
joined() { "$MESH" play alice && "$MESH" play bob && wait_for 420 ready alice && wait_for 420 ready bob; }
loaded() { # no registry: the tarball goes into each player's node (k3s ctr), which then has the image and pulls nothing
  local who; for who in alice bob; do
    incus file push -q "$TAR" "emesh-$who/root/doom.tar" && incus exec "emesh-$who" -- sh -c 'k3s ctr -n k8s.io images import /root/doom.tar >/dev/null && rm -f /root/doom.tar' &&
    incus exec "emesh-$who" -- sh -c "k3s crictl images 2>/dev/null | grep -q 'emesh-doom'" || return 1; done; }
srv_init() { server_log | grep -q 'Odamex Server Initialized'; }
server_up() { # alice's server and its UDP Service; Ready; the log says the map is loaded and the public master servers are not contacted
  "$POC/doom/pod.sh" server "$IMG" | K alice apply -f - && K alice expose pod doom-server --name doom --port 10666 --protocol UDP &&
  "$POC/doom/pod.sh" probe "$AGNHOST_IMAGE" | K alice apply -f - && K alice expose pod doom-probe --name doom-probe --port 10666 --protocol UDP &&
  K alice wait --for=condition=Ready pod/doom-server pod/doom-probe --timeout=240s &&
  wait_for 60 srv_init &&
  server_log | grep -q 'Masters will not be contacted because sv_usemasters is 0' && server_log | grep -q 'MAP01' &&
  say player "alice: a Doom server (Odamex, Freedoom) in her namespace, on a UDP Service" && show player "sudo edge-mesh kubectl get pods,svc" K alice get pods,svc; }
client_up() { "$POC/doom/pod.sh" client "$IMG" | K bob apply -f - && K bob wait --for=condition=Ready pod/doom-client --timeout=240s &&
  say player "bob: a client pod in his own namespace (a virtual screen, a recorder, a script that plays)" && show player "sudo edge-mesh kubectl get pods" K bob get pods; }
nobody() { # the client reaches no server (exit 3: nobody answered) and alice's server log has no player: $1 = how many connects it may hold
  local o rc; o=$(show player "$CLIENT 10 bob" K bob exec doom-client -- env CONNECT_WAIT=15 client.sh "$SVC" 10666 10 bob 2>&1); rc=$?; echo "$o"; echo "exit $rc"
  [ "$rc" -eq 3 ] && echo "$o" | grep -q 'no server answered' && [ "$(connects)" -eq "$1" ]; }
no_consent() { say player "alice runs the server in her namespace; bob's client tries it. Nobody has agreed to anything yet"; closed_now && nobody 0; }
one_side() { say player "alice invites bob: her consent alone opens nothing"
  out_has "your consent is recorded" show player "sudo edge-mesh party invite bob" P alice party invite bob && tick &&
  out_has "waiting for bob to accept" show player "sudo edge-mesh party list" P alice party list && closed_now && nobody 0 &&
  [ -z "$(k get networkpolicies -A -l edge-mesh/party -o name)" ]; }
open_party() { say player "bob accepts: now both agreed"; say host "the host's tick: both consents, both namespaces live, so it opens the party"
  out_has "your consent is recorded" show player "sudo edge-mesh party accept alice" P bob party accept alice &&
  out_has "party: alice -> bob: active: ports 10666/UDP" show host "mesh host auto --once" tick &&
  out_has "active: ports 10666/UDP" show player "sudo edge-mesh party list" P bob party list &&
  out_has "active: ports 10666/UDP" show host "mesh host party" "$MESH" host party list && wait_for 90 open_now; }
match() { # the match: bob's client joins through the party, plays $PLAY s and quits; it saw the map, and its screen is a GIF
  local o; o=$(play "$PLAY" 1 2>&1); echo "$o"
  echo "$o" | grep -q 'client bob: map MAP01' && echo "$o" | grep -q 'client bob: in the game' && echo "$o" | grep -q 'left the game' || return 1
  K bob exec doom-client -- cat /tmp/doom/match.gif >"$OUTD/match.gif" && [ "$(head -c 6 "$OUTD/match.gif")" = GIF89a ] && [ "$(wc -c <"$OUTD/match.gif")" -lt 5000000 ] &&
    echo "match.gif: $(wc -c <"$OUTD/match.gif") bytes"; }
server_saw() { # alice's server, not the client, is the witness: bob connected, joined the game, stayed, left, with his frags and deaths
  local log; log=$(server_log) || return 1
  say host "alice's server is the witness: who connected, joined and left, and when"
  show host "kubectl exec -n play-alice doom-server -- grep -E 'Masters|MAP01|connected|joined|disconnected' /tmp/server.log" \
    k exec -n play-alice doom-server -- grep -E 'Masters|MAP01|connected|joined|disconnected' /tmp/server.log >/dev/null
  echo "$log" | grep -E 'has connected|joined the game|disconnected'
  python3 - "$log" "$PLAY" <<'PY'
import re, sys
log, play = sys.argv[1], int(sys.argv[2])
def at(pat):
    m = re.search(r"\[(\d\d):(\d\d):(\d\d)\] " + pat, log)
    return m and int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
c, j, d = at(r"bob has connected\."), at(r"bob joined the game\."), at(r"bob disconnected\. \(\d+ KILLS, \d+ DEATHS\)")
ok = None not in (c, j, d) and c <= j < d and d - j >= play - 5
print(f"server: bob joined and stayed {d - j if None not in (c, j, d) else '?'} s (played {play} s)")
sys.exit(0 if ok else 1)
PY
}
one_connect() { [ "$(connects)" -eq 1 ]; }
leave_closes() { say player "bob leaves the party: the same client, the same server, and nobody answers again"
  out_has "left: the host closes the party" show player "sudo edge-mesh party leave" P bob party leave &&
  out_has "party: alice -> bob: closed: bob left the party" show host "mesh host auto --once" tick &&
  [ -z "$(k get networkpolicies -A -l edge-mesh/party -o name)" ] && wait_for 90 closed_now && nobody 1; }   # the CNI takes a moment to drop the rules
idempotent() { out_has "party: unchanged" show host "mesh host party reconcile" "$MESH" host party reconcile && out_has "party: unchanged" "$MESH" host party reconcile &&
  [ -z "$(k get configmaps -A -l edge-mesh/party-consent -o name)" ]; }
free_only() { # nothing non-free is in the image: Freedoom is the only wad, no doom.wad or doom2.wad anywhere, and both licences are there
  local o; o=$(K alice exec doom-server -- sh -c 'ls /opt/wad; find / -xdev -iname "doom*.wad" -o -iname "plutonia.wad" -o -iname "tnt.wad" 2>/dev/null; ls /opt/licenses'); echo "$o"
  [ "$(echo "$o" | sed -n 1p)" = freedoom2.wad ] && ! echo "$o" | grep -qi '^/.*doom.*\.wad' && echo "$o" | grep -q 'odamex-LICENSE-GPL-2.0' &&
  echo "$o" | grep -q 'freedoom-COPYING-BSD-3-Clause.txt'; }

step DM01 "mesh host init: lighthouse and control plane (and Cilium with CNI=cilium)" host_init
step DM02 "the Doom image (Odamex 12.3.0, Freedoom 0.13.0, pinned) is built once and cached by the hash of its inputs" image
step DM03 "alice and bob ask; one tick admits both" admitted
step DM04 "alice and bob join and are Ready" joined
step DM05 "the image is loaded into both nodes (no registry, nothing pulled)" loaded
step DM06 "alice: the server (a pod, a UDP Service on 10666) is up on Freedoom MAP01 and not announced to the public master servers" server_up
step DM07 "bob: the automated client (Xvfb, ffmpeg, xdotool) is Ready in his namespace" client_up
step DM08 "no consent: the client gets no answer from alice's server, and her log has no player" no_consent
step DM09 "alice invites: one consent is not a party; the client still gets no answer and the log has no player" one_side
step DM10 "bob accepts: the tick opens 10666/UDP between them" open_party
step DM11 "the match: bob's client joins through the party, sees MAP01, runs and shoots for the time, quits; its screen is a GIF" match
step DM12 "the server is the witness: bob connected, joined, stayed and left, with his frags and deaths on one line" server_saw
step DM13 "the server saw exactly one connection (bob's)" one_connect
step DM14 "bob leaves: the tick closes the party; the same client gets no answer again and the log has no new player" leave_closes
step DM15 "reconcile twice says unchanged; no consent and no policy is left" idempotent
step DM16 "nothing non-free is in the image: Freedoom only, no doom.wad or doom2.wad, both licences present" free_only

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
[ -z "$SC" ] || [ "$FAILS" -ne 0 ] || { cp "$OUTD/match.gif" "$SC/match.gif"; for s in player host; do   # only a run that passed becomes a recording
  python3 "$SCP" redact "$SC/$s.raw" "$SC/$s.tsv" --strip "$POC/" --strip "${POC%/*}/" || FAILS=$((FAILS+1)); done; }
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
