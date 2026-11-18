#!/usr/bin/env bash
# A small match left running for days on the host's own lab, to watch how it plays (issue #101, the owner's option B): poc-doom up to DM27
# (romero's server, the host's own machine hostpc, the host's spectator of romero's game through the party romero accepted), then two robots
# that play again and again (romero's and petersen's, whose party stays open) and the host console with its Watch view. Private: the console
# binds the loopback only; reach it through an SSH tunnel. Nothing public changes (watching from the public page is #113).
#   poc/doom/week.sh up        build it (about 40 minutes), start the robots and the console, print the link
#   poc/doom/week.sh status    machines, the host's spectators, robots and console
#   poc/doom/week.sh url       a new console link (the old one dies)
#   poc/doom/week.sh pause     stop the robots and freeze every machine (RAM kept, no CPU); the console stays and says it cannot read
#   poc/doom/week.sh resume    unfreeze, wait for the nodes, start the robots again
#   poc/doom/week.sh down      remove everything it made
# Its own Incus project and bridge (never default, emesh-demo or emeshbr): WEEK_PROJECT (emesh-doomweek), WEEK_NET (emdoomwk),
# WEEK_UNDERLAY (10.84.0, must be free), WEEK_WEB (127.0.0.1:18791), WEEK_DAYS (7: the leases and the certificates). PERSIST=1, so a
# reboot of the lab brings nebula and k3s back (then `resume`).
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; POC="$(dirname "$D")"
export INCUS_PROJECT="${WEEK_PROJECT:-emesh-doomweek}" VMNET="${WEEK_NET:-emdoomwk}" UNDERLAY_NET="${WEEK_UNDERLAY:-10.84.0}" PERSIST=1 RUNTIME=incus
export EMESH_STATE="${WEEK_STATE:-$POC/.week}"
WEB="${WEEK_WEB:-127.0.0.1:18791}"; DAYS="${WEEK_DAYS:-7}"; RUN="$EMESH_STATE/week"; ROBOT_SECONDS=600
case " default emesh-demo " in *" $INCUS_PROJECT "*) echo "project $INCUS_PROJECT: never touched"; exit 2 ;; esac
case " emeshbr emeshdemo " in *" $VMNET "*) echo "bridge $VMNET: never touched"; exit 2 ;; esac
[[ "$DAYS" =~ ^[1-9][0-9]?$ ]] && [[ "$WEB" =~ ^127\.0\.0\.1:[0-9]{4,5}$ ]] || { echo "WEEK_DAYS is 1-99, WEEK_WEB is 127.0.0.1:<port>"; exit 2; }
SVC=doom.play-romero.svc.cluster.local

robot() { # robot <player> <pod> <server> <pattern>: plays ROBOT_SECONDS, again and again; when a machine is frozen or away it waits
  while :; do
    incus exec "emesh-$1" -- edge-mesh kubectl exec "$2" -- env PATTERN="$4" CONNECT_WAIT=40 client.sh "$3" 10666 "$ROBOT_SECONDS" "$1" >>"$RUN/$1.log" 2>&1 || sleep 30
    sleep 5
  done; }
alive() { [ -s "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
start_bg() { # start_bg <name> <command...>: once, detached, its pid kept
  alive "$1" && return 0
  nohup "${@:2}" >>"$RUN/$1.out" 2>&1 </dev/null & echo $! >"$RUN/$1.pid"; }
stop_bg() { if alive "$1"; then pkill -P "$(cat "$RUN/$1.pid")" 2>/dev/null; kill "$(cat "$RUN/$1.pid")" 2>/dev/null; fi; rm -f "$RUN/$1.pid"; }
robots_on() {
  export -f robot; export RUN ROBOT_SECONDS
  start_bg robot-romero bash -c 'robot romero doom-bot doom b'
  start_bg robot-petersen bash -c "robot petersen doom-client $SVC c"; }
robots_off() { stop_bg robot-romero; stop_bg robot-petersen; }
machines() { incus list emesh- -c n -f csv; }

case "${1:-}" in
  up)
    command -v incus >/dev/null || { echo "needs Incus"; exit 2; }
    [ -z "$(ip -4 -o addr | awk '{print $4}' | grep "^${UNDERLAY_NET//./\\.}\.")" ] || { echo "$UNDERLAY_NET.0/24 is in use here: set WEEK_UNDERLAY"; exit 2; }
    incus project show "$INCUS_PROJECT" >/dev/null 2>&1 || incus project create "$INCUS_PROJECT" -c features.images=false -c features.profiles=false >/dev/null || exit 1
    [ -z "$(machines)" ] || { echo "project $INCUS_PROJECT has machines: poc/doom/week.sh down first"; exit 2; }
    mkdir -p "$EMESH_STATE"
    secs=$((DAYS * 86400))
    UNTIL=DM27 POOL_LEASE="$secs" OWN_CERT_DURATION="$((DAYS * 24 + 24))h" CP_CERT_DURATION="$((DAYS * 24 + 48))h" LH_CERT_DURATION="$((DAYS * 24 + 48))h" "$POC/poc-doom.sh" || {
      echo "the match did not come up: poc/doom/week.sh down, and see $POC/.cache/poc-doom.log"; exit 1; }
    mkdir -p "$RUN"; chmod 700 "$RUN"
    robots_on
    start_bg console "$POC/mesh" host console --web "$WEB"
    sleep 3; "$0" url ;;
  url)
    "$POC/mesh" host console url --web "$WEB" | sed 's/^/  /'
    echo "  from your machine: ssh -L ${WEB##*:}:$WEB <this host>, then open the link with localhost:${WEB##*:}; the Watch tab shows the game" ;;
  status)
    echo "project $INCUS_PROJECT:"; incus list emesh- -c ns -f csv | sed 's/^/  /'
    for b in robot-romero robot-petersen console; do if alive "$b"; then echo "$b: running"; else echo "$b: stopped"; fi; done
    "$POC/mesh" host watch status --json --read-only 2>/dev/null || echo "the cluster does not answer (paused?)" ;;
  pause)
    robots_off
    for n in $(machines); do incus pause "$n" 2>/dev/null; done
    echo "paused: $(machines | tr '\n' ' ')(poc/doom/week.sh resume)" ;;
  resume)
    for n in $(machines); do incus start "$n" 2>/dev/null || incus resume "$n" 2>/dev/null; done
    for _ in $(seq 60); do "$POC/kc" get nodes >/dev/null 2>&1 && break; sleep 5; done
    "$POC/kc" wait --for=condition=Ready nodes --all --timeout=300s >/dev/null || echo "not every node is Ready yet"
    mkdir -p "$RUN"; robots_on; alive console || start_bg console "$POC/mesh" host console --web "$WEB"
    "$0" status ;;
  down)
    robots_off; stop_bg console
    "$POC/vm/down.sh"
    [ -n "$(incus list -c n -f csv 2>/dev/null)" ] || incus project delete "$INCUS_PROJECT" >/dev/null 2>&1
    echo "week match removed" ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
