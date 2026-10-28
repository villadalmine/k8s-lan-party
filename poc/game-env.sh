# shellcheck shell=bash
# Sourced by `mesh` and `kc` BEFORE lib.sh: the settings the game was opened with (`mesh host init` records RUNTIME, CNI and TAINT in
# .state/game.env). Every later command inherits them: an invite, an approval, the timer's tick, so a cron line without CNI=cilium cannot hand
# out a flannel bundle for a Cilium game and `RUNTIME` need not be repeated. A variable set by hand wins (and `mesh host invite` refuses a CNI
# or TAINT that differs from the game's). The PoC suites do not read it: each one builds its own game with its own settings.
# shellcheck disable=SC1091
if [ -f "${EMESH_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.state}/game.env" ]; then
  . "${EMESH_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.state}/game.env"
  export RUNTIME="${RUNTIME:-$GAME_RUNTIME}" CNI="${CNI:-$GAME_CNI}" TAINT="${TAINT:-$GAME_TAINT}"
fi
