#!/usr/bin/env bash
# Tear everything down: containers, network, state. Safe to run twice.
set -u
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "${RUNTIME:-docker}" = incus ] && exec "$POC/vm/down.sh"
. "$POC/stack.env"
STATE="${EMESH_STATE:-$POC/.state}"   # the same rule as lib.sh
[ ! -f "$STATE/lighthouse.env" ] || echo "note: the external lighthouse keeps running; remove it first with poc/lighthouse-vps.sh remove (its certificate dies with the CA)"
docker rm -f lh cp n1 n2 >/dev/null 2>&1 || true
# and every player machine of this game (`mesh play <name>` names the container after the player): all that is left on the game's network.
# Found 2026-10-28 following TRY-IT.md recipe 2: `mesh host down` left alice running and the network in use.
ids=$(docker ps -aq --filter "network=$DOCKER_NET" 2>/dev/null || true)
# shellcheck disable=SC2086
[ -z "$ids" ] || docker rm -f $ids >/dev/null 2>&1 || true
docker network rm "$DOCKER_NET" >/dev/null 2>&1 || true
rm -rf "$STATE"
echo "poc down"
