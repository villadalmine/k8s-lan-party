# shellcheck shell=bash
# Sourced by `mesh` and `kc` BEFORE lib.sh: the settings the game was opened with (`mesh host init` records RUNTIME, CNI and TAINT in
# .state/game.env). Every later command inherits them: an invite, an approval, the timer's tick, so a cron line without CNI=cilium cannot hand
# out a flannel bundle for a Cilium game and `RUNTIME` need not be repeated. A variable set by hand wins (and `mesh host invite` refuses a CNI
# or TAINT that differs from the game's). The file is READ, never sourced, and each value must match a strict list: a file that says anything
# else is ignored with a warning. The PoC suites do not read it: each one builds its own game with its own settings.
_ge="${EMESH_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.state}/game.env"
if [ -f "$_ge" ]; then
  _v() { sed -n "s/^$1=//p" "$_ge" | head -1; }
  GAME_RUNTIME=$(_v GAME_RUNTIME); GAME_CNI=$(_v GAME_CNI); GAME_TAINT=$(_v GAME_TAINT)
  case "$GAME_RUNTIME:$GAME_CNI:$GAME_TAINT" in
    docker:flannel:no|docker:flannel:yes|docker:cilium:no|docker:cilium:yes|incus:flannel:no|incus:flannel:yes|incus:cilium:no|incus:cilium:yes)
      export RUNTIME="${RUNTIME:-$GAME_RUNTIME}" CNI="${CNI:-$GAME_CNI}" TAINT="${TAINT:-$GAME_TAINT}" ;;
    *) echo "warning: $_ge is not 'GAME_RUNTIME=docker|incus', 'GAME_CNI=flannel|cilium', 'GAME_TAINT=no|yes': ignored" >&2; GAME_RUNTIME=""; GAME_CNI=""; GAME_TAINT="" ;;
  esac
  unset -f _v
fi
unset _ge
