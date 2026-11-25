#!/usr/bin/env bash
# A small match left running for days on the host's own lab, to watch how it plays (issue #101, the owner's option B): poc-doom up to DM27
# (romero's server, the host's own machine hostpc, the host's spectator of romero's game through the party romero accepted), then two robots
# that play again and again (romero's and petersen's, whose party stays open) and the host console with its Watch view. Private: the console
# binds the loopback only; reach it through an SSH tunnel. Nothing public changes (watching from the public page is #113).
#   poc/doom/week.sh up        build it (about 40 minutes), start the robots and the console, write the link to .week/week/console-url
#   poc/doom/week.sh play      petersen's player console on her machine (#102), so you play romero's match from your browser as petersen:
#                              her console listens on her machine's loopback (WEEK_PLAY, 127.0.0.1:18792), and a relay on her lab address
#                              passes this host's connections only; the link goes to .week/week/player-url (0600), the tunnel to
#                              .week/week/tunnel. `up` runs it too
#   poc/doom/week.sh status    machines, the host's spectators, robots and console
#   poc/doom/week.sh url       a new console link (the old one dies); --to-file writes it to .week/week/console-url (0600) and prints only
#                              that path: for a bot or a tool, so the token never reaches its output
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
WEB="${WEEK_WEB:-127.0.0.1:18791}"; PLAYWEB="${WEEK_PLAY:-127.0.0.1:18792}"; DAYS="${WEEK_DAYS:-7}"; RUN="$EMESH_STATE/week"; ROBOT_SECONDS=600
case " default emesh-demo " in *" $INCUS_PROJECT "*) echo "project $INCUS_PROJECT: never touched"; exit 2 ;; esac
case " emeshbr emeshdemo " in *" $VMNET "*) echo "bridge $VMNET: never touched"; exit 2 ;; esac
[[ "$DAYS" =~ ^[1-9][0-9]?$ ]] && [[ "$WEB" =~ ^127\.0\.0\.1:[0-9]{4,5}$ ]] && [[ "$PLAYWEB" =~ ^127\.0\.0\.1:[0-9]{4,5}$ ]] || {
  echo "WEEK_DAYS is 1-99, WEEK_WEB and WEEK_PLAY are 127.0.0.1:<port>"; exit 2; }
SVC=doom.play-romero.svc.cluster.local

robot() { # robot <player> <pod> <server> <pattern>: plays ROBOT_SECONDS, again and again; when a machine is frozen or away it waits
  while :; do
    incus exec "emesh-$1" -- edge-mesh kubectl exec "$2" -- env PATTERN="$4" CONNECT_WAIT=40 client.sh "$3" 10666 "$ROBOT_SECONDS" "$1" >>"$RUN/$1.log" 2>&1 || sleep 30
    sleep 5
  done; }
started() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }   # the process's start time: a pid reused after a reboot has another
alive() { # alive <name>: the process it started is still that process (a pid file left by a reboot names someone else's pid)
  [ -s "$RUN/$1.pid" ] || return 1
  local pid when; read -r pid when <"$RUN/$1.pid"
  [ -n "$pid" ] && [ -n "$when" ] && [ "$(started "$pid")" = "$when" ]; }
start_bg() { # start_bg <name> <command...>: once, detached, its pid kept
  alive "$1" && return 0
  nohup "${@:2}" >>"$RUN/$1.out" 2>&1 </dev/null & echo "$! $(started $!)" >"$RUN/$1.pid"; }
stop_bg() { local pid; if alive "$1"; then read -r pid _ <"$RUN/$1.pid"; pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null; fi; rm -f "$RUN/$1.pid"; }
robots_on() {
  export -f robot; export RUN ROBOT_SECONDS
  start_bg robot-romero bash -c 'robot romero doom-bot doom b'
  start_bg robot-petersen bash -c "robot petersen doom-client $SVC c"; }
robots_off() { stop_bg robot-romero; stop_bg robot-petersen; }
machines() { incus list emesh- -c n -f csv; }
PL=emesh-petersen; PLAYER_HOME=/home/player; LIB=/usr/local/lib/edge-mesh/poc
player_console() { # petersen's own console, as a non-root user of her machine with her namespace's kubeconfig only (the way edge-mesh join
  # installs it), started by systemd there; this host reaches its loopback only through the relay below (on her lab address, this host's
  # connections only)
  incus exec "$PL" -- sh -c "id -u player >/dev/null 2>&1 || useradd -m -s /bin/sh player; mkdir -p $LIB/doom" || return 1
  # the files docs/JOIN.md installs on a player's machine (tests/test_player_console_files.py keeps that list complete)
  tar cf - --exclude=__pycache__ -C "$POC" console page/page.py page/seal.py page/news.py admission.py pool.py meshlock.py participant.py \
    party.py watcher.py doom/pod.sh | incus exec "$PL" -- tar xf - -C "$LIB" || return 1
  incus exec "$PL" -- python3 "$LIB/console/player.py" install-kubeconfig --source /etc/edge-mesh/kubeconfig --user player >/dev/null || return 1
  incus exec "$PL" -- sh -c "systemctl stop edge-mesh-console 2>/dev/null; systemctl reset-failed edge-mesh-console 2>/dev/null
    systemd-run -q --unit edge-mesh-console --uid player -p Environment=HOME=$PLAYER_HOME -p WorkingDirectory=$PLAYER_HOME \
      python3 $LIB/console/player.py serve --web $PLAYWEB" || return 1
  # Incus proxies only NAT into a VM (to its own address, not its loopback), so a small relay on her machine's lab address passes this host's
  # connections, and no one else's (only the bridge's gateway, this host), to her console on her loopback. Her console stays loopback-only.
  local gw ip; gw="$UNDERLAY_NET.1"; ip=$(vm_ip)
  incus exec "$PL" -- sh -c "systemctl stop edge-mesh-relay 2>/dev/null; systemctl reset-failed edge-mesh-relay 2>/dev/null
    systemd-run -q --unit edge-mesh-relay --uid player python3 -c '$RELAY' $ip ${PLAYWEB##*:} $gw ${PLAYWEB%:*} ${PLAYWEB##*:}" || return 1
  for _ in $(seq 20); do incus exec "$PL" -- python3 -c "import socket; socket.create_connection(('${PLAYWEB%:*}', ${PLAYWEB##*:}), 2)" 2>/dev/null && break; sleep 1; done
  player_url; }
vm_ip() { incus list "$PL" -c 4 -f csv | tr -d '"' | tr ' ' '\n' | grep -m1 "^${UNDERLAY_NET//./\\.}\."; }
# relay <listen ip> <port> <the only peer allowed> <to ip> <to port>: one thread pair per connection
RELAY='import socket,sys,threading
lip,lp,peer,tip,tp=sys.argv[1],int(sys.argv[2]),sys.argv[3],sys.argv[4],int(sys.argv[5])
def pipe(a,b):
    try:
        while True:
            d=a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    for x in (a,b):
        try: x.shutdown(socket.SHUT_RDWR)
        except OSError: pass
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((lip,lp)); s.listen(64)
while True:
    c,addr=s.accept()
    if addr[0]!=peer: c.close(); continue
    try: u=socket.create_connection((tip,tp),5)
    except OSError: c.close(); continue
    threading.Thread(target=pipe,args=(c,u),daemon=True).start(); threading.Thread(target=pipe,args=(u,c),daemon=True).start()'
player_url() {
  local uid img; uid=$(incus exec "$PL" -- id -u player) || return 1
  img=$(incus exec "$PL" -- sh -c "k3s crictl images 2>/dev/null | awk '/emesh-doom/ {print \$1\":\"\$2; exit}'")
  mkdir -p "$RUN"; chmod 700 "$RUN"
  (umask 077; incus exec "$PL" --user "$uid" --env HOME="$PLAYER_HOME" -- python3 "$LIB/console/player.py" url --web "$PLAYWEB" >"$RUN/player-url") || return 1
  echo "$img" >"$RUN/doom-image"
  echo "ssh -N -L ${WEB##*:}:$WEB -L ${PLAYWEB##*:}:$(vm_ip):${PLAYWEB##*:} <this host>" >"$RUN/tunnel"
  echo "petersen's console: the link is in $RUN/player-url, the tunnel in $RUN/tunnel; in Games, type the image $img and press Play on romero's game"; }

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
    player_console || echo "petersen's console did not start: poc/doom/week.sh play"
    sleep 3; "$0" url --to-file   # the link holds the console's token: into a file of mode 0600, never this command's output
    echo "  from your machine: the tunnel in $RUN/tunnel, then open the links in $RUN/console-url (watch) and $RUN/player-url (play)" ;;
  play) mkdir -p "$RUN"; player_console ;;
  url)
    if [ "${2:-}" = --to-file ]; then
      mkdir -p "$RUN"; chmod 700 "$RUN"; (umask 077; "$POC/mesh" host console url --web "$WEB" >"$RUN/console-url") || exit 1
      echo "token rotated; the link is in $RUN/console-url"; exit 0
    fi
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
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
