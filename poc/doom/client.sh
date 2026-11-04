#!/bin/bash
# The automated player (issue #75 part B): joins an Odamex server, runs and shoots for N seconds, quits. A virtual screen (Xvfb) holds the game;
# nothing is shown to anybody. With RECORD=1 the first RECORD_SECONDS of play are also written to /tmp/doom/match.gif (ffmpeg, 320x200).
#   client.sh <host> <port> <seconds> [name]      exit 0 played; 3 no server answered (the party is not open); 4 other failure
#   client.sh --probe <host> <port>               exit 0 when a UDP "hostname" is answered (poc/doom/pod.sh probe), 3 when not: a party open or closed
# It never reaches anything but the server it is told: no master server, no downloads (the wad is in the image).
if [ "${1:-}" = --probe ]; then   # one datagram on a new local port (a new flow, whatever an old one still has in conntrack), one answer or none in 3 s
  exec 3<>"/dev/udp/${2:?host}/${3:?port}" && printf hostname >&3 && [ "$(timeout 3 dd bs=64 count=1 <&3 2>/dev/null | wc -c)" -gt 0 ] && exit 0
  exit 3
fi
HOSTNAME_ARG="${1:?host}"; PORT="${2:?port}"; SECS="${3:?seconds}"; NAME="${4:-player}"
OUT="${OUT:-/tmp/doom}"; WAIT="${CONNECT_WAIT:-20}"; REC="${RECORD:-0}"; RECSECS="${RECORD_SECONDS:-24}"
export DISPLAY=:99 SDL_AUDIODRIVER=dummy HOME=/tmp
mkdir -p "$OUT"; rm -f "$OUT"/* /tmp/.odamex/odamex.log   # the game's log is appended to: an old session's lines would look like this one's
IP=$(getent ahostsv4 "$HOSTNAME_ARG" 2>/dev/null | awk '{print $1; exit}'); [ -n "$IP" ] || IP="$HOSTNAME_ARG"
echo "client $NAME: connecting to $HOSTNAME_ARG:$PORT"
Xvfb :99 -screen 0 320x200x24 -nolisten tcp >"$OUT/xvfb.log" 2>&1 & XVFB=$!
sleep 2
cd /opt/odamex || exit 4
./odamex -iwad /opt/wad/freedoom2.wad -waddir /opt/wad -width 320 -height 200 +cl_name "$NAME" -connect "$IP:$PORT" >"$OUT/client.out" 2>&1 & GAME=$!
LOG=/tmp/.odamex/odamex.log
key() { xdotool keydown "$1"; sleep 0.25; xdotool keyup "$1"; sleep 0.25; }   # a press shorter than a frame is lost
cleanup() { kill "$GAME" "$XVFB" 2>/dev/null; wait 2>/dev/null; }
t=0
until grep -q 'Requesting server state' "$LOG" 2>/dev/null && grep -q '^MAP[0-9][0-9]:' "$LOG" 2>/dev/null; do
  sleep 1; t=$((t + 1))
  if [ "$t" -ge "$WAIT" ]; then echo "client $NAME: no server answered at $HOSTNAME_ARG:$PORT in ${WAIT} s"; cleanup; exit 3; fi
done
sed -n 's/^> Hostname: /client '"$NAME"': server: /p; s/^MAP\([0-9][0-9]\): /client '"$NAME"': map MAP\1 /p' "$LOG" | head -2
sleep 1
key grave; xdotool type --delay 100 "join"; sleep 0.3; key Return; key grave   # leave spectator mode, close the console
sleep 1
echo "client $NAME: in the game, playing ${SECS} s"
if [ "$REC" = 1 ]; then
  ffmpeg -loglevel error -y -draw_mouse 0 -f x11grab -framerate 8 -video_size 320x200 -t "$RECSECS" -i :99 \
    -vf "split[a][b];[a]palettegen=max_colors=48:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" "$OUT/match.gif" 2>"$OUT/ffmpeg.log" & REC_PID=$!
fi
end=$(( $(date +%s) + SECS )); turn=Right
while [ "$(date +%s)" -lt "$end" ]; do   # run forward shooting, turn, alternate sides: deterministic moves, the monsters decide the rest
  xdotool keydown ctrl keydown Up; sleep 1.6; xdotool keyup Up; xdotool keydown "$turn"; sleep 0.7; xdotool keyup "$turn" keyup ctrl
  [ "$turn" = Right ] && turn=Left || turn=Right
done
[ -z "${REC_PID:-}" ] || wait "$REC_PID"
key grave; xdotool type --delay 100 "quit"; sleep 0.3; key Return
t=0; while kill -0 "$GAME" 2>/dev/null && [ "$t" -lt 10 ]; do sleep 1; t=$((t + 1)); done
echo "client $NAME: left the game after ${SECS} s"
cleanup
[ "$REC" != 1 ] || { [ -s "$OUT/match.gif" ] && echo "client $NAME: match.gif $(wc -c <"$OUT/match.gif") bytes"; }
exit 0
