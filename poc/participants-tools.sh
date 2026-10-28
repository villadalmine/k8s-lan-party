# shellcheck shell=bash
# Sourced by poc-participants.sh and poc-participants-e2e.sh (after lib.sh): the pinned cosign, age and nebula-cert of join by signed PR,
# fetched once into $CACHE. cosign is checked against its release's cosign_checksums.txt, age against the sha256 pinned in stack.env (it
# publishes no checksum file), nebula against the release's SHASUM256.txt. Sets COSIGN, AGE, AGE_KEYGEN and NC.
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "unsupported machine $(uname -m)"; exit 2 ;; esac
fetch_tools() {
  local t; t=$(mktemp -d)
  if [ ! -x "$CACHE/cosign-$COSIGN_VERSION" ]; then
    curl -sSfL -o "$t/cosign-linux-$ARCH" "https://github.com/sigstore/cosign/releases/download/v$COSIGN_VERSION/cosign-linux-$ARCH" &&
    curl -sSfL -o "$t/sums" "https://github.com/sigstore/cosign/releases/download/v$COSIGN_VERSION/cosign_checksums.txt" &&
    (cd "$t" && grep " cosign-linux-$ARCH\$" sums | sha256sum -c - >/dev/null) || { echo "cosign: download or checksum failed"; return 1; }
    install -m 755 "$t/cosign-linux-$ARCH" "$CACHE/cosign-$COSIGN_VERSION"
  fi
  if [ ! -x "$CACHE/age-$AGE_VERSION/age" ]; then
    local sum; if [ "$ARCH" = amd64 ]; then sum=$AGE_SHA256_AMD64; else sum=$AGE_SHA256_ARM64; fi
    curl -sSfL -o "$t/age.tgz" "https://github.com/FiloSottile/age/releases/download/v$AGE_VERSION/age-v$AGE_VERSION-linux-$ARCH.tar.gz" &&
    echo "$sum  $t/age.tgz" | sha256sum -c - >/dev/null || { echo "age: download or checksum failed"; return 1; }
    mkdir -p "$CACHE/age-$AGE_VERSION" && tar xzf "$t/age.tgz" -C "$CACHE/age-$AGE_VERSION" --strip-components=1 age/age age/age-keygen
  fi
  if [ ! -x "$CACHE/nebula-cert" ]; then
    curl -sSfL -o "$t/nebula.tgz" "https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/nebula-linux-$ARCH.tar.gz" &&
    curl -sSfL -o "$t/sums" "https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/SHASUM256.txt" &&
    (cd "$t" && [ "$(sha256sum nebula.tgz | cut -d' ' -f1)" = "$(grep " nebula-linux-$ARCH.tar.gz\$" sums | cut -d' ' -f1)" ]) ||
      { echo "nebula: download or checksum failed"; return 1; }
    tar xzf "$t/nebula.tgz" -C "$CACHE"
  fi
  rm -rf "$t"
}
fetch_tools || { echo "the pinned tools could not be fetched"; exit 2; }
COSIGN="$CACHE/cosign-$COSIGN_VERSION"; AGE="$CACHE/age-$AGE_VERSION/age"; AGE_KEYGEN="$CACHE/age-$AGE_VERSION/age-keygen"; NC="$CACHE/nebula-cert"
