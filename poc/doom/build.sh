#!/usr/bin/env bash
# Build the Doom image (issue #75 part B) once and print the path of its tarball. The name carries a hash of everything that goes into the
# image (Containerfile, the two scripts, the pins of doom.env), so the cache is exact: the same inputs give the same file, any change builds
# again. Builds with podman or docker when this machine has one; otherwise in a throwaway Incus VM of the run's project (emesh-doombuild, on
# the game's underlay: `poc/vm/down.sh` removes it too). Prints, on its last two lines: the image name:tag and the tarball path.
#   poc/doom/build.sh          idempotent: a second run prints the same two lines and builds nothing
set -eu
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; POC="$(dirname "$D")"
. "$POC/stack.env"; . "$D/doom.env"
CACHE="${EMESH_CACHE:-$POC/.cache}/doom"; mkdir -p "$CACHE"
HASH=$(cat "$D/Containerfile" "$D/server.sh" "$D/client.sh" | { cat; echo "$DOOM_DEBIAN_IMAGE $DOOM_ODAMEX_VERSION $DOOM_ODAMEX_SHA256 $DOOM_FREEDOOM_VERSION $DOOM_FREEDOOM_SHA256"; } | sha256sum | cut -c1-12)
IMAGE="localhost/emesh-doom:$DOOM_ODAMEX_VERSION-$HASH"; TAR="$CACHE/emesh-doom-$DOOM_ODAMEX_VERSION-$HASH.tar"
ARGS=(--build-arg "DEBIAN_IMAGE=$DOOM_DEBIAN_IMAGE" --build-arg "ODAMEX_VERSION=$DOOM_ODAMEX_VERSION" --build-arg "ODAMEX_SHA256=$DOOM_ODAMEX_SHA256"
      --build-arg "FREEDOOM_VERSION=$DOOM_FREEDOOM_VERSION" --build-arg "FREEDOOM_SHA256=$DOOM_FREEDOOM_SHA256")
if [ ! -s "$TAR" ]; then
  if command -v podman >/dev/null 2>&1; then CE=podman; elif command -v docker >/dev/null 2>&1; then CE=docker; else CE=; fi
  if [ -n "$CE" ]; then
    "$CE" build -q -t "$IMAGE" "${ARGS[@]}" -f "$D/Containerfile" "$D" >&2
    "$CE" save -o "$TAR.part" "$IMAGE" >&2
  else # no container engine here: a VM of the run's project builds it (needs the game's bridge: after `mesh host init`)
    . "$POC/lib.sh"; . "$POC/vm/lib-vm.sh"
    B=emesh-doombuild
    vm_up doombuild 6 8GiB "$UNDERLAY_NET.9" >&2
    trap 'incus delete -f "$B" >/dev/null 2>&1 || true' EXIT
    incus exec "$B" -- sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq podman >/dev/null && mkdir -p /root/doom' >&2
    tar cf - -C "$D" Containerfile server.sh client.sh | incus exec "$B" -- tar xf - -C /root/doom
    incus exec "$B" -- podman build -q -t "$IMAGE" "${ARGS[@]}" -f /root/doom/Containerfile /root/doom >&2
    incus exec "$B" -- podman save -o /root/doom.tar "$IMAGE" >&2
    incus file pull "$B/root/doom.tar" "$TAR.part" >&2
  fi
  mv "$TAR.part" "$TAR"
  find "$CACHE" -name 'emesh-doom-*.tar' ! -name "$(basename "$TAR")" -delete   # one image in the cache: the one that matches the inputs
fi
echo "$IMAGE"; echo "$TAR"
