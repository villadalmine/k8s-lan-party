#!/usr/bin/env bash
# One read-only Odamex spectator, pinned by participant admission to the watcher's own node.
# Its JPEG endpoint is authenticated and reached by that node's local daemon through this pod's IP.
set -eu
HOST="${1:?server host}"; PORT="${2:?server port}"; TARGET="${3:?player to follow}"; WATCHER="${4:?watcher name}"
NAME="${WATCH_NAME:-watch-$WATCHER}"
case "$HOST" in doom.play-*.svc.cluster.local) ;; *) echo "watch client needs an in-cluster Doom service" >&2; exit 2 ;; esac
case "$PORT" in 10666) ;; *) echo "watch client only supports the consented Odamex port" >&2; exit 2 ;; esac
[[ "$TARGET" =~ ^[a-z][a-z0-9-]{1,19}$ && "$WATCHER" =~ ^[a-z][a-z0-9-]{1,19}$ && "$NAME" =~ ^[a-z][a-z0-9-]{1,29}$ ]] || { echo "invalid Doom player name" >&2; exit 2; }
[[ "${WATCH_TOKEN:-}" =~ ^[a-f0-9]{64}$ ]] || { echo "watch token missing or invalid" >&2; exit 2; }
export DISPLAY=:99 SDL_AUDIODRIVER=dummy HOME=/tmp
mkdir -p /tmp/doom
rm -f /tmp/doom/* /tmp/.odamex/odamex.log
Xvfb :99 -screen 0 320x200x24 -nolisten tcp >/tmp/doom/xvfb.log 2>&1 & XVFB=$!
cd /opt/odamex
./odamex -iwad /opt/wad/freedm.wad -waddir /opt/wad -width 320 -height 200 +cl_name "$NAME" -connect "$HOST:$PORT" >/tmp/doom/client.out 2>&1 & GAME=$!
STREAM=
cleanup() { [ -z "$STREAM" ] || kill "$STREAM" 2>/dev/null || true; kill "$GAME" "$XVFB" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
t=0
until grep -q 'Requesting server state' /tmp/.odamex/odamex.log 2>/dev/null && grep -q '^MAP[0-9][0-9]:' /tmp/.odamex/odamex.log 2>/dev/null; do
  kill -0 "$GAME" 2>/dev/null || { echo "Odamex exited before connecting" >&2; exit 1; }
  sleep 1; t=$((t + 1)); [ "$t" -lt "${CONNECT_WAIT:-40}" ] || { echo "spectator did not connect in ${CONNECT_WAIT:-40}s" >&2; exit 1; }
done
# A newly connected client is a spectator. Do not send `join`: `spy <player>` follows the target without entering the match.
xdotool key grave; sleep 0.3; xdotool type --delay 100 "players"; sleep 0.3; xdotool key Return; sleep 0.3; xdotool key grave
t=0
until grep -q 'PLAYERS IN GAME:' /tmp/.odamex/odamex.log 2>/dev/null && grep -q "$NAME" /tmp/.odamex/odamex.log 2>/dev/null; do
  sleep 1; t=$((t + 1)); [ "$t" -lt 10 ] || { echo "Odamex roster did not announce spectator" >&2; exit 1; }
done
xdotool key grave; sleep 0.3; xdotool type --delay 100 "spy $TARGET"; sleep 0.3; xdotool key Return; sleep 0.3; xdotool key grave
t=0
until grep -q "Following player '$TARGET'" /tmp/.odamex/odamex.log 2>/dev/null; do
  sleep 1; t=$((t + 1)); [ "$t" -lt 10 ] || { echo "Odamex did not accept spectator target" >&2; exit 1; }
done
python3 /usr/local/bin/watch_stream.py & STREAM=$!
echo "watch client $NAME: following $TARGET; authenticated frame stream on pod port 17777"
while kill -0 "$GAME" 2>/dev/null && kill -0 "$STREAM" 2>/dev/null; do sleep 2; done
echo "watch client $NAME: spectator or frame stream exited"
exit 1
