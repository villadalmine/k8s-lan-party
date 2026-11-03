#!/usr/bin/env bash
# Runs INSIDE the docker VM of `make regression` with REGRESSION_RUNTIME=docker-vm (poc/regression.sh, issue #62): Docker from Ubuntu's archive,
# kind, kubectl and helm, every version and every SHA-256 pinned in stack.env (a download is checked against the pin, not against a sum
# fetched next to it). Idempotent: a second
# run changes nothing and prints the same line.
set -eu
. "$(dirname "$0")/../stack.env"
case "$(uname -m)" in aarch64 | arm64) A=arm64 ;; *) A=amd64 ;; esac
export DEBIAN_FRONTEND=noninteractive
if [ "$(dpkg-query -W -f='${Version}' docker.io 2>/dev/null)" != "$DOCKER_APT_VERSION" ]; then
  apt-get update -qq
  apt-get install -y -qq "docker.io=$DOCKER_APT_VERSION" curl ca-certificates python3 iproute2 >/dev/null ||
    { echo "docker.io=$DOCKER_APT_VERSION is not in the archive any more: look the version up again and pin it in stack.env"; exit 1; }
fi
systemctl enable --now docker >/dev/null 2>&1
fetch() { # fetch <url> <pinned sha256> <dest>: download, check against the pin, install (nothing when the file there already matches it)
  [ -f "$3" ] && echo "$2  $3" | sha256sum -c --status && return 0
  curl -fsSL -o "$3.part" "$1" || { echo "download failed: $1"; rm -f "$3.part"; return 1; }
  echo "$2  $3.part" | sha256sum -c --status || { echo "checksum mismatch: $1 is not the pinned file (stack.env)"; rm -f "$3.part"; return 1; }
  mv "$3.part" "$3"; chmod +x "$3"
}
pin() { local v="${1}_SHA256_${A^^}"; echo "${!v}"; }   # pin KIND -> $KIND_SHA256_AMD64 (or _ARM64)
fetch "https://github.com/kubernetes-sigs/kind/releases/download/v$KIND_VERSION/kind-linux-$A" "$(pin KIND)" /usr/local/bin/kind
fetch "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$A/kubectl" "$(pin KUBECTL)" /usr/local/bin/kubectl
if ! helm version --short 2>/dev/null | grep -q "^v$HELM_VERSION+"; then
  fetch "https://get.helm.sh/helm-v$HELM_VERSION-linux-$A.tar.gz" "$(pin HELM)" /tmp/helm.tgz &&
    tar xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 "linux-$A/helm" && rm -f /tmp/helm.tgz
fi
echo "docker-vm ready: docker.io $DOCKER_APT_VERSION, kind $KIND_VERSION, kubectl $KUBECTL_VERSION, helm $HELM_VERSION"
