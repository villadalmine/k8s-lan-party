#!/usr/bin/env bash
# Runs INSIDE the docker VM of `make regression` with REGRESSION_RUNTIME=docker-vm (poc/regression.sh, issue #62): Docker from Ubuntu's archive,
# kind, kubectl and helm, every version pinned in stack.env and every download checked against its published SHA-256. Idempotent: a second
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
fetch() { # fetch <url> <sha256> <dest>: download, check, install (nothing when the installed file already has that sum)
  [ -f "$3" ] && echo "$2  $3" | sha256sum -c --status && return 0
  curl -fsSL -o "$3.part" "$1" && echo "$2  $3.part" | sha256sum -c --status || { echo "checksum mismatch: $1"; rm -f "$3.part"; return 1; }
  mv "$3.part" "$3"; chmod +x "$3"
}
sum_of() { curl -fsSL "$1" | awk '{print $1}'; }
K=https://github.com/kubernetes-sigs/kind/releases/download/v$KIND_VERSION/kind-linux-$A
fetch "$K" "$(sum_of "$K.sha256sum")" /usr/local/bin/kind
C=https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$A/kubectl
fetch "$C" "$(sum_of "$C.sha256")" /usr/local/bin/kubectl
H=https://get.helm.sh/helm-v$HELM_VERSION-linux-$A.tar.gz
if ! helm version --short 2>/dev/null | grep -q "^v$HELM_VERSION+"; then
  fetch "$H" "$(sum_of "$H.sha256sum")" /tmp/helm.tgz && tar xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 "linux-$A/helm" && rm -f /tmp/helm.tgz
fi
echo "docker-vm ready: docker.io $DOCKER_APT_VERSION, kind $KIND_VERSION, kubectl $KUBECTL_VERSION, helm $HELM_VERSION"
