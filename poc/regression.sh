#!/usr/bin/env bash
# make regression (issue #62): every suite of docs/FEATURES.yml, in the catalog's order, one at a time, each with its own state directory;
# a failure stops nothing. Then one table, feature x result with the commit and the date, between the regression markers of
# docs/POC-STATUS.md (scripts/features.py table). The same commit on the same host and UTC day gives the same table: no timings, no random names.
#   make regression                          everything (several hours on Incus)
#   make regression ONLY="poc-join poc-ca"   a subset; the other suites are listed as NOT RUN
#   poc/regression.sh down                   remove what an interrupted run left: only what $REGRESSION_DIR/project.created says it made
# Runtimes: a suite that runs on docker or incus uses REGRESSION_RUNTIME (default: docker when `docker info` works, else incus); a suite whose
# runtime the host lacks is SKIP(docker), SKIP(kind) or SKIP(incus), never FAIL. On a Docker host the network emesh, the containers lh cp n1 n2
# and a kind cluster emesh must not exist before the run (someone's game: the cleanup would remove it). Incus suites run in their own project (INCUS_PROJECT, empty at
# the start) on an underlay that must be free on this host (UNDERLAY_NET, default 10.83.0; poc-nat uses the next three /24 after +20).
# Before each Incus suite the storage pool must be under REGRESSION_POOL_MAX % (default 85), else SKIP(pool).
set -u
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ROOT="$(cd "$POC/.." && pwd)"
PY="${PYTHON:-python3}"
REG="${REGRESSION_DIR:-$POC/.regression}"
export UNDERLAY_NET="${UNDERLAY_NET:-10.83.0}" INCUS_PROJECT="${INCUS_PROJECT:-emesh-regression}" VMNET="${VMNET:-emregbr}"
[[ "$UNDERLAY_NET" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.([0-9]{1,3})$ ]] && [ "${BASH_REMATCH[1]}" -le 232 ] ||
  { echo "UNDERLAY_NET=$UNDERLAY_NET: three octets, the last at most 232"; exit 2; }
pfx=${UNDERLAY_NET%.*}; o3=${UNDERLAY_NET##*.}
export NAT_PUB="${NAT_PUB:-$pfx.$((o3 + 20))}" NAT_A="${NAT_A:-$pfx.$((o3 + 21))}" NAT_B="${NAT_B:-$pfx.$((o3 + 22))}"
export NET_PUB="${NET_PUB:-emregpub}" NET_A="${NET_A:-emrega}" NET_B="${NET_B:-emregb}"
NETS="$UNDERLAY_NET $NAT_PUB $NAT_A $NAT_B"; BRIDGES="$VMNET $NET_PUB $NET_A $NET_B"
# never used, whatever the host says: the second game of docs/HOST-YOUR-OWN.md (a live game may sit there), and the PoC's own Nebula and pool
NEVER="10.79.0 10.77.0 10.200.0"
# never touched either: the default project, the live game's project and bridge, the PoC's default bridge (a game someone hosts)
NEVER_PROJECTS="default emesh-demo"; NEVER_BRIDGES="emeshdemo emeshbr"
MARK="$REG/project.created"   # what this run made: project=<name>, created=yes|no, bridges=<names>; `down` acts only on it
TIMEOUT="${REGRESSION_TIMEOUT:-7200}"; POOL_MAX="${REGRESSION_POOL_MAX:-85}"; POOL="${INCUS_POOL:-default}"

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
have_incus() { command -v incus >/dev/null 2>&1 && incus info >/dev/null 2>&1; }
have_kind() { have_docker && command -v kind >/dev/null && command -v helm >/dev/null && command -v kubectl >/dev/null; }
have() { case "$1" in none) return 0 ;; docker) have_docker ;; incus) have_incus ;; kind) have_kind ;; *) return 1 ;; esac; }

guard_project() { # the project every incus command of the suites uses (vm/down.sh deletes every emesh-* VM of its project)
  local b
  case " $NEVER_PROJECTS " in *" $INCUS_PROJECT "*) echo "INCUS_PROJECT=$INCUS_PROJECT: refused, never touched; make regression needs a project of its own"; return 1 ;; esac
  for b in $BRIDGES; do case " $NEVER_BRIDGES " in *" $b "*) echo "bridge $b: refused, never touched; set VMNET/NET_* to names of this run"; return 1 ;; esac; done
}
mark() { sed -n "s/^$1=//p" "$MARK" 2>/dev/null; }
owned() { # this run's marker names this project and these bridges: only then is anything in them removed
  guard_project || return 1
  [ -f "$MARK" ] || { echo "no $MARK: project $INCUS_PROJECT was not set up by make regression here; nothing removed"; return 1; }
  [ "$(mark project)" = "$INCUS_PROJECT" ] && [ "$(mark bridges)" = "$BRIDGES" ] ||
    { echo "$MARK names project $(mark project) and bridges '$(mark bridges)', not $INCUS_PROJECT and '$BRIDGES'; nothing removed"; return 1; }
}
sweep() { # remove every instance of this run's project and this run's bridges (the project was empty when the run started)
  owned || return 1
  local n; for n in $(incus list -c n -f csv 2>/dev/null); do incus delete -f "$n" >/dev/null 2>&1; done
  for n in $BRIDGES; do incus network delete "$n" >/dev/null 2>&1; done; return 0
}
leftovers() { # what this run left: instances in the project, its bridges, docker containers on the PoC network
  local n
  if have_incus && incus project show "$INCUS_PROJECT" >/dev/null 2>&1; then
    for n in $(incus list -c n -f csv 2>/dev/null); do echo "instance $n"; done
  fi
  if have_incus; then for n in $BRIDGES; do incus network show "$n" >/dev/null 2>&1 && echo "bridge $n"; done; fi
  have_docker && docker ps -aq --filter network=emesh 2>/dev/null | sed 's/^/container /'
  return 0
}
for net in $NETS; do case " $NEVER " in *" $net "*)
  echo "$net.0/24 is reserved (the second game of HOST-YOUR-OWN.md, the PoC's Nebula, its LB pool): pick another UNDERLAY_NET"; exit 2 ;; esac; done
finish_project() { # the run (or down) is over: the project goes if this run created it, then the marker
  [ "$(mark created)" != yes ] || incus project delete "$INCUS_PROJECT" >/dev/null 2>&1; rm -f "$MARK"
}
if [ "${1:-}" = down ]; then
  owned || exit 2
  sweep && finish_project; rm -rf "$REG/state"; echo "regression down"; exit 0
fi

preflight_incus() { # the underlay and the bridges must be free, the project ours and empty
  local net used
  guard_project || return 1
  command -v ip >/dev/null || { echo "no 'ip' command: cannot check that $NETS are free"; return 1; }
  used=$(ip -4 -o addr | awk '{print $4}')
  for net in $NETS; do
    if printf '%s\n' "$used" | grep -q "^${net//./\\.}\."; then echo "$net.0/24 is in use on this host (ip -4 -o addr): pick another UNDERLAY_NET"; return 1; fi
  done
  for net in $BRIDGES; do incus network show "$net" >/dev/null 2>&1 && { echo "bridge $net exists: poc/regression.sh down, or set VMNET/NET_*"; return 1; }; done
  incus query "/1.0/storage-pools/$POOL/resources" >/dev/null 2>&1 || { echo "storage pool $POOL cannot be read: set INCUS_POOL"; return 1; }
  [ ! -f "$MARK" ] || { echo "$MARK exists: an earlier run was interrupted; poc/regression.sh down first"; return 1; }
  local created=no
  if ! incus project show "$INCUS_PROJECT" >/dev/null 2>&1; then
    incus project create "$INCUS_PROJECT" -c features.images=false -c features.profiles=false >/dev/null || return 1; created=yes
  fi
  [ -z "$(incus list -c n -f csv)" ] || { echo "Incus project $INCUS_PROJECT is not empty: make regression uses a project of its own"; return 1; }
  printf 'project=%s\ncreated=%s\nbridges=%s\n' "$INCUS_PROJECT" "$created" "$BRIDGES" >"$MARK"
}
preflight_docker() { # nothing of a Docker game may exist: the cleanup (down.sh, kind delete) would remove it
  local c
  docker network inspect emesh >/dev/null 2>&1 && { echo "docker network emesh exists (a game on this host?): nothing was run"; return 1; }
  for c in lh cp n1 n2; do docker inspect "$c" >/dev/null 2>&1 && { echo "docker container $c exists (a game on this host?): nothing was run"; return 1; }; done
  ! { command -v kind >/dev/null && kind get clusters 2>/dev/null | grep -qx emesh; } || { echo "kind cluster emesh exists: nothing was run"; return 1; }
}
pool_ok() { # pool_ok: the storage pool under POOL_MAX %
  local pct; pct=$(incus query "/1.0/storage-pools/$POOL/resources" 2>/dev/null | "$PY" -c 'import json,sys; s=json.load(sys.stdin)["space"]; print(s["used"]*100//s["total"])') || return 1
  echo "pool $POOL at $pct %" >>"$REG/regression.log"; [ "$pct" -lt "$POOL_MAX" ]
}

mkdir -p "$REG/runs" "$REG/state"; rm -f "$REG/runs/"*; : >"$REG/regression.log"
"$PY" "$ROOT/scripts/features.py" validate >"$REG/regression.log" || { cat "$REG/regression.log"; exit 2; }
SUITES=$("$PY" "$ROOT/scripts/features.py" suites) || exit 2
for o in ${ONLY:-}; do printf '%s\n' "$SUITES" | cut -f1 | grep -qx -- "$o" || { echo "ONLY: $o is not a suite of docs/FEATURES.yml"; exit 2; }; done
DUAL="${REGRESSION_RUNTIME:-}"; [ -n "$DUAL" ] || { if have_docker; then DUAL=docker; else DUAL=incus; fi; }
selected() { [ -z "${ONLY:-}" ] || case " $ONLY " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
pick() { # pick <runtimes, comma-separated>: the one this run uses (DUAL first), or nothing when the host has none of them
  local r; for r in ${1//,/ }; do [ "$r" = "$DUAL" ] && have "$r" && { echo "$r"; return; }; done
  for r in ${1//,/ }; do have "$r" && { echo "$r"; return; }; done
}
KEEPFOR=" "; USES_INCUS=""; USES_DOCKER=""
while IFS=$'\t' read -r id runtimes _ game _; do
  selected "$id" || continue
  [ "$game" = - ] || KEEPFOR="$KEEPFOR$game "; case "$(pick "$runtimes")" in incus) USES_INCUS=1 ;; docker | kind) USES_DOCKER=1 ;; esac
done <<<"$SUITES"
# the underlay is checked before anything runs: a clash with a network of this host aborts the whole run
if [ -n "$USES_DOCKER" ] && ! preflight_docker >>"$REG/regression.log" 2>&1; then tail -1 "$REG/regression.log"; exit 2; fi
if [ -n "$USES_INCUS" ] && ! preflight_incus >>"$REG/regression.log" 2>&1; then tail -1 "$REG/regression.log"; echo "nothing was run"; exit 2; fi

run_suite() { # run_suite <id> <runtime> <script> <state> <game>: stdin is /dev/null (an incus exec must not eat the suite list)
  local id="$1" rt="$2" script="$3" st="$4" game="$5" keep=0 rc
  case "$KEEPFOR" in *" $id "*) keep=1 ;; esac
  [ "$game" != - ] || rm -rf "$st"
  ( cd "$ROOT" && export EMESH_STATE="$st" KEEP="$keep"; [ "$rt" = none ] || export RUNTIME="$rt"
    # a suite that checks another one's game (poc-golden on poc-vm's): that game opened the golden way, placement installed first
    if [ "$game" != - ]; then export RUNTIME=incus CNI=cilium TAINT=yes; "$POC/install-placement.sh" >"$REG/runs/$id.placement.log" 2>&1; fi
    exec timeout --kill-after=60 "$TIMEOUT" "$ROOT/$script" ) </dev/null >"$REG/runs/$id.out" 2>&1
  rc=$?; [ "$rc" = 124 ] || [ "$rc" = 137 ] && return 124; return 0
}
cleanup() { # cleanup <id> <runtime> <state>: what the suite may have left (a timeout, a KEEP for the next suite is handled by that one)
  case "$KEEPFOR" in *" $1 "*) return 0 ;; esac
  case "$2" in
    incus) [ "$1" != poc-nat ] || EMESH_STATE="$3" "$POC/poc-nat.sh" down </dev/null >/dev/null 2>&1
      RUNTIME=incus EMESH_STATE="$3" "$POC/vm/down.sh" </dev/null >/dev/null 2>&1; sweep </dev/null ;;
    docker) RUNTIME=docker EMESH_STATE="$3" "$POC/down.sh" </dev/null >/dev/null 2>&1 ;;
    kind) kind delete cluster --name emesh </dev/null >/dev/null 2>&1 ;;
  esac
  rm -rf "$3"; return 0
}

while IFS=$'\t' read -r -u 3 id runtimes script game _; do
  selected "$id" || continue
  rt=$(pick "$runtimes")
  [ -n "$rt" ] || { printf '%s\tSKIP(%s)\n' "${runtimes%%,*}" "${runtimes%%,*}" >"$REG/runs/$id.status"; continue; }
  if [ "$rt" = incus ] && ! pool_ok; then printf 'incus\tSKIP(pool)\n' >"$REG/runs/$id.status"; continue; fi
  st="$REG/state/$id"; [ "$game" = - ] || st="$REG/state/$game"
  if [ "$game" != - ] && [ ! -d "$st" ]; then printf '%s\tSKIP(no %s game)\n' "$rt" "$game" >"$REG/runs/$id.status"; continue; fi
  echo "### $id ($rt)"
  run_suite "$id" "$rt" "$script" "$st" "$game"; rc=$?
  if [ "$rc" = 124 ]; then printf '%s\tTIMEOUT\n' "$rt" >"$REG/runs/$id.status"; else printf '%s\tRAN\n' "$rt" >"$REG/runs/$id.status"; fi
  grep -E '^(FAIL|RESULT)' "$REG/runs/$id.out"
  if [ "$game" != - ]; then KEEPFOR=${KEEPFOR/ $game / }; cleanup "$game" "$rt" "$st"; fi
  cleanup "$id" "$rt" "$st"
done 3<<<"$SUITES"
kept=$KEEPFOR; KEEPFOR=" "; for g in $kept; do cleanup "$g" incus "$REG/state/$g"; done   # a kept game whose checker was skipped

left=$(leftovers)
have_incus && [ -f "$MARK" ] && [ -z "$left" ] && finish_project
commit=$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
[ -z "$(git -C "$ROOT" status --porcelain -- poc scripts Makefile docs/FEATURES.yml 2>/dev/null)" ] || commit="$commit+dirty"
envline="runtime for docker|incus suites: $DUAL; docker $(have_docker && echo yes || echo no), incus $(have_incus && echo yes || echo no), kind $(have_kind && echo yes || echo no); left behind: ${left:-nothing}"
envline=${envline//$'\n'/, }
write=(--write "$ROOT/docs/POC-STATUS.md"); [ -z "${REGRESSION_NO_WRITE:-}" ] || write=()
"$PY" "$ROOT/scripts/features.py" table --runs "$REG/runs" --commit "$commit" --date "$(date -u +%F)" --env "$envline" "${write[@]}" \
  | tee "$REG/table.md"
# exit 1 when a suite that ran has no RESULT PASS (failed, timed out, died before its RESULT line) or something was left behind; SKIP is no failure
[ "${PIPESTATUS[0]}" = 0 ] && [ -z "$left" ]
