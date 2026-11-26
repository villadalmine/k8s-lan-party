#!/usr/bin/env bash
# A player's own game client (issue #102): Odamex on this pod's virtual screen, joined to the game as a player, played from the player's
# browser tab. The tab sees the screen and sends keys through the player's daemon, on the player's own node, to this pod's IP behind the
# per-pod secret (D7, WB08); the keys go to this pod's display :99 only (WB07). Pinned to the player's own node by private admission.
#   play-client.sh <server host> <port> <player>
set -eu
HOST="${1:?server host}"; PORT="${2:?server port}"; NAME="${3:?player name}"
# a player's own game, another player's, or the house match (docs/LIVE-PAGE.md: a player plays it from their own node)
case "$HOST" in doom|doom.play-*.svc.cluster.local|doom.house.svc.cluster.local) ;; *) echo "play client needs an in-cluster Doom service" >&2; exit 2 ;; esac
case "$PORT" in 10666) ;; *) echo "play client only supports the consented Odamex port" >&2; exit 2 ;; esac
[[ "$NAME" =~ ^[a-z][a-z0-9-]{1,19}$ ]] || { echo "invalid Doom player name" >&2; exit 2; }
[[ "${WATCH_TOKEN:-}" =~ ^[a-f0-9]{64}$ ]] || { echo "token missing or invalid" >&2; exit 2; }
export DISPLAY=:99 SDL_AUDIODRIVER=dummy HOME=/tmp
mkdir -p /tmp/doom
rm -f /tmp/doom/* /tmp/.odamex/odamex.log
Xvfb :99 -screen 0 320x200x24 -nolisten tcp >/tmp/doom/xvfb.log 2>&1 & XVFB=$!
sleep 2
IP=$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1; exit}'); [ -n "$IP" ] || IP="$HOST"
cd /opt/odamex
./odamex -iwad /opt/wad/freedm.wad -waddir /opt/wad -width 320 -height 200 +cl_name "$NAME" -connect "$IP:$PORT" >/tmp/doom/client.out 2>&1 & GAME=$!
STREAM=
cleanup() { [ -z "$STREAM" ] || kill "$STREAM" 2>/dev/null || true; kill "$GAME" "$XVFB" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
t=0
until grep -q 'Requesting server state' /tmp/.odamex/odamex.log 2>/dev/null && grep -q '^MAP[0-9][0-9]:' /tmp/.odamex/odamex.log 2>/dev/null; do
  kill -0 "$GAME" 2>/dev/null || { echo "Odamex exited before connecting" >&2; exit 1; }
  sleep 1; t=$((t + 1)); [ "$t" -lt "${CONNECT_WAIT:-40}" ] || { echo "the game did not answer in ${CONNECT_WAIT:-40}s" >&2; exit 1; }
done
# a new client is a spectator: `join` in Odamex's console makes it a player, the same keystrokes as the robots (client.sh). Only this script
# types: the tab's keys never include the console key or Enter (watch_stream.py KEYS).
key() { xdotool keydown "$1"; sleep 0.25; xdotool keyup "$1"; sleep 0.25; }
sleep 1; key grave; xdotool type --delay 100 "join"; sleep 0.3; key Return; key grave
PLAY_INPUT=1 WATCH_FPS="${PLAY_FPS:-10}" python3 /usr/local/bin/watch_stream.py & STREAM=$!
echo "play client $NAME: in the game; frames and keys on pod port 17777"
while kill -0 "$GAME" 2>/dev/null && kill -0 "$STREAM" 2>/dev/null; do sleep 2; done
echo "play client $NAME: the game or the stream exited"
exit 1
