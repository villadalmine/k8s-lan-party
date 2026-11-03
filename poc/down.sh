#!/usr/bin/env bash
# Tear everything down: containers, network, state. Safe to run twice.
set -u
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${EMESH_STATE:-$POC/.state}"   # the same rule as lib.sh
# the runtime the game was opened with (.state/game.env, lib.sh), unless RUNTIME is set: a forgotten RUNTIME must not take the Docker path
# and delete the state of an Incus game whose VMs keep running
[ -n "${RUNTIME:-}" ] || RUNTIME=$(sed -n 's/^GAME_RUNTIME=//p' "$STATE/game.env" 2>/dev/null | head -1)
case "${RUNTIME:-docker}" in docker|incus) ;; *) echo "RUNTIME '$RUNTIME' (from $STATE/game.env?) is not docker or incus: set RUNTIME=docker or RUNTIME=incus and run again; nothing was removed"; exit 2 ;; esac
[ "${RUNTIME:-docker}" = incus ] && RUNTIME=incus exec "$POC/vm/down.sh"
. "$POC/stack.env"
: "${DOCKER_NET:?DOCKER_NET is empty: docker would match every container with a network; nothing was removed}"
[ ! -f "$STATE/lighthouse.env" ] || echo "note: the external lighthouse keeps running; remove it first with poc/lighthouse-vps.sh remove (its certificate dies with the CA)"
# every container on the game's network (lh, cp, n1, n2, and every player `mesh play <name>` made: alice, bob...; issue #36), then the fixed names
# (fwl and htl join n2's namespace, so it is not on the network) and the network itself
ids=$(docker ps -aq --filter "network=${DOCKER_NET:?}" 2>/dev/null || true)
# shellcheck disable=SC2086
[ -z "$ids" ] || docker rm -f $ids >/dev/null 2>&1 || true
# shellcheck disable=SC2086
docker rm -f $GAME_CONTAINERS >/dev/null 2>&1 || true
docker network rm "$DOCKER_NET" >/dev/null 2>&1 || true
rm -rf "$STATE"
echo "poc down"
