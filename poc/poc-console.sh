#!/usr/bin/env bash
# make poc-console: the host console's daemon with the CLI as its client (issue #88, docs/CONSOLE.md) end to end on Incus VMs. The daemon runs
# (`mesh host console`); `mesh host request|approve|burn|kick|panic --clear` typed in a shell go through it (the plan and the confirmation made by
# the CLI itself, the job followed to its end, the same output and exit code as direct), and each leaves one audit line (source socket). What
# must never depend on the daemon does not: the tick runs direct (its nested calls too), a daemon killed with its socket left behind changes
# nothing for the CLI, a lease still ends with the daemon stopped (K02), and `panic` is always direct. Needs a Linux host with Incus. PASS/FAIL
# per step; identical across runs. ADM_LEASE (seconds, default 900): long enough for two VMs to join in ana's lease. KEEP=1 leaves the VMs up.
# The daemon also serves the web console (#89) on the host's loopback, port WEB_PORT (default 18090): K01 checks that a player's machine
# cannot reach it while the host can, with the token of `mesh host console url`.
set -u
export RUNTIME=incus MAX_VMS=4 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-console.log"; : >"$LOG"   # kept after the run for diagnosis (.cache survives down.sh; .state does not)
MESH="$POC/mesh"
LEASE="${ADM_LEASE:-900}"
export POOL_CONFIG="$STATE/pool-test.yaml"
# the idle kick off: ana and bob are expelled by the steps that test it, not early because a lease of the test's length ran idle
sed -e "s/^lease_seconds: [0-9]*/lease_seconds: $LEASE/" -e "s/^idle_kick_seconds: [0-9]*/idle_kick_seconds: 0/" "$POC/pool.yaml" >"$POOL_CONFIG"
unset EMESH_DIRECT EMESH_AUDITED EMESH_LOCK_HELD
SOCK="$STATE/console/api.sock"
AUDIT="$STATE/console/audit.jsonl"
ADM="$STATE/admission/state.json"
CONSOLE_PID=""
WEB_PORT="${WEB_PORT:-18090}"

ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
out_has() { local want="$1"; shift; local o; o=$("$@" 2>&1); echo "$o"; echo "$o" | grep -qF -- "$want"; }
play_ready() { "$MESH" play "$1" && wait_for 240 ready "$1"; }
answers() { [ -S "$SOCK" ] && python3 - "$SOCK" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
s.sendall(b"GET /api/v1/health HTTP/1.0\r\nHost: localhost\r\n\r\n")
sys.exit(0 if b'"ok": true' in s.recv(4096) else 1)
PY
}
daemon_up() { "$MESH" host console --web "127.0.0.1:$WEB_PORT" >>"$LOG" 2>&1 & CONSOLE_PID=$!; wait_for 30 answers; }
daemon_stop() { [ -z "$CONSOLE_PID" ] || { kill "$CONSOLE_PID" 2>/dev/null; wait "$CONSOLE_PID" 2>/dev/null; CONSOLE_PID=""; }; [ ! -S "$SOCK" ]; }
lines() { [ -f "$AUDIT" ] && wc -l <"$AUDIT" || echo 0; }
last_is() { # last_is <source> <verb> [confirmed]: the audit's last line
  python3 - "$AUDIT" "$@" <<'PY'
import json, sys
l = json.loads(open(sys.argv[1]).read().splitlines()[-1])
print(l)
want = {"source": sys.argv[2], "verb": sys.argv[3], **({"confirmed": sys.argv[4] == "true"} if len(sys.argv) > 4 else {})}
sys.exit(0 if all(l.get(k) == v for k, v in want.items()) else 1)
PY
}
via() { local n; n=$(lines); out_has "$1" "$MESH" "${@:4}" && [ "$(lines)" -eq $((n + 1)) ] && last_is "$2" "$3"; }
via_confirmed() { local n; n=$(lines); out_has "$1" "$MESH" "${@:4}" && [ "$(lines)" -eq $((n + 1)) ] && last_is "$2" "$3" true; }
same_both_ways() { # `mesh host burn` through the daemon and with EMESH_DIRECT=1: the same stdout, stderr and exit code
  local a b ra rb
  a=$("$MESH" host burn 2>&1); ra=$?
  b=$(EMESH_DIRECT=1 "$MESH" host burn 2>&1); rb=$?
  echo "api: $ra $a | direct: $rb $b"
  [ "$a" = "$b" ] && [ "$ra" = "$rb" ] && [ "$ra" = 0 ] && tail -2 "$AUDIT" | grep -q '"source": "socket"' && last_is direct burn
}
web_local_only() { # the host reaches the page and, with the link's token, the API; nothing but the loopback listens on that port; ana's
  # machine reaches the host's bridge address (a control listener there) but is refused on the web port. The host itself has no Nebula
  # address (the lighthouse and the control plane are VMs): the underlay bridge is the only way from a player's machine to the host.
  local link tok rc ctl=$((WEB_PORT + 1)) cpid ok=1 empty
  [ -n "${UNDERLAY_NET:-}" ] || { echo "UNDERLAY_NET is empty"; return 1; }
  link=$("$MESH" host console url --web "127.0.0.1:$WEB_PORT" 2>/dev/null); tok="${link##*#t=}"
  curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/" || { echo "the host does not reach the page"; return 1; }
  curl -fsS -H "Authorization: Bearer $tok" "http://127.0.0.1:$WEB_PORT/api/v1/health" | grep -q '"ok": true' ||
    { echo "the host does not reach the API with the link's token"; return 1; }
  [ "$(ss -Hltn "sport = :$WEB_PORT" | awk '{print $4}' | sort -u)" = "127.0.0.1:$WEB_PORT" ] ||
    { echo "something other than 127.0.0.1 listens on $WEB_PORT:"; ss -Hltn "sport = :$WEB_PORT"; return 1; }
  empty=$(mktemp -d)   # the control serves an empty directory: never the clone (.git, .state with the CA) to the VMs on the bridge
  python3 -m http.server --bind "$UNDERLAY_NET.1" --directory "$empty" "$ctl" >/dev/null 2>&1 & cpid=$!
  wait_for 10 curl -fsS -o /dev/null "http://$UNDERLAY_NET.1:$ctl/"
  dx ana curl -sS -m 5 -o /dev/null "http://$UNDERLAY_NET.1:$ctl/" || { echo "control: ana does not reach $UNDERLAY_NET.1:$ctl"; ok=0; }
  kill "$cpid" 2>/dev/null; wait "$cpid" 2>/dev/null; rmdir "$empty"
  [ "$ok" = 1 ] || return 1
  dx ana curl -sS -m 5 -o /dev/null "http://$UNDERLAY_NET.1:$WEB_PORT/"; rc=$?
  echo "the host: page and API; only 127.0.0.1 listens; ana -> $UNDERLAY_NET.1:$ctl (control): reached; -> :$WEB_PORT: curl exit $rc"
  [ "$rc" = 7 ]   # 7: connection refused; any other failure (no curl, a failed exec, a timeout) proves nothing
}
kick_bob() { via_confirmed "" socket kick host kick bob && ! k get node bob && ! machine_running bob; }
tick_direct() { # as typed in a shell (no EMESH_DIRECT) with the daemon up: the tick is not sent, and its nested burn and kicks neither
  local n j; n=$(lines); j=$(grep -c '"source": "socket"' "$AUDIT"); out_has "tick:" "$MESH" host auto --once && [ "$(lines)" -eq "$n" ] &&
  [ "$(grep -c '"source": "socket"' "$AUDIT")" -eq "$j" ]; }
killed_leaves_socket() { kill -9 "$CONSOLE_PID"; wait "$CONSOLE_PID" 2>/dev/null; CONSOLE_PID=""; [ -S "$SOCK" ] && ! answers; }
lease_end() { python3 -c 'import json,sys; print(next(l["end"] for l in json.load(open(sys.argv[1]))["pool"]["leases"] if l["name"] == sys.argv[2]))' "$ADM" "$1"; }
expires_without_daemon() { # the timer's command, with no daemon at all: ana's lease ends and she is expelled
  [ ! -S "$SOCK" ] || rm -f "$SOCK"
  local s=$(( $(lease_end ana) - $(date +%s) + 2 )); [ "$s" -le 0 ] || sleep "$s"
  out_has "expelled 1 |" env EMESH_DIRECT=1 "$MESH" host auto --once && ! k get node ana && ! machine_running ana
}
panic_direct() { daemon_up || return 1; via "panic: on | expelled 1" direct panic host panic; }
audit_clean() { # 0600, one line per action (10), no state path and no home path in it
  [ "$(stat -c %a "$AUDIT")" = 600 ] && [ "$(lines)" -eq 10 ] && ! grep -qF "$STATE" "$AUDIT" && ! grep -qF "$HOME" "$AUDIT"
}

step K10 "mesh host init: lighthouse and control plane" "$MESH" host init
step K11 "the console daemon starts and answers on its socket" daemon_up
step K12 "ana asks for a place through the daemon: position 1, one audit line (socket)" via "ana: waiting, position 1 of 1" socket request host request ana --ip 198.51.100.10
step K13 "bob asks through the daemon: position 2" via "bob: waiting, position 2 of 2" socket request host request bob --ip 198.51.100.20
step K14 "approve through the daemon (the CLI confirms the plan itself): ana, confirmed in the audit" via_confirmed "approved ana (public)" socket approve host approve
step K15 "burn through the daemon and direct: the same output and exit code" same_both_ways
step K16 "ana joins with her bundle and is Ready" play_ready ana
step K17 "approve bob through the daemon: bob, confirmed" via_confirmed "approved bob (public)" socket approve host approve
step K18 "bob joins with his bundle and is Ready" play_ready bob
step K01 "the web console: the host reaches it on the loopback only, a player's machine that reaches the host is refused on its port" web_local_only
step K19 "kick bob through the daemon (the CLI types the name): node and machine gone, one audit line" kick_bob
step K20 "the tick typed with the daemon up runs direct, its nested calls too: no audit line" tick_direct
step K21 "the daemon is killed (SIGKILL) and leaves its socket behind, answering nothing" killed_leaves_socket
step K22 "carol asks with no daemon behind the socket: direct, one audit line" via "carol: waiting" direct request host request carol --ip 2001:db8:c::1
step K02 "with the daemon stopped, the timer's tick expels ana at her lease's end" expires_without_daemon
step K23 "with the daemon back, panic is still direct (and expels bob's lease)" panic_direct
step K24 "panic --clear goes through the daemon" via "panic: cleared" socket panic-clear host panic --clear
step K25 "the audit: 0600, one line per action, no state or home path" audit_clean
daemon_stop >/dev/null 2>&1

if [ "${KEEP:-0}" != 1 ]; then "$POC/vm/down.sh" >/dev/null 2>&1; fi
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
