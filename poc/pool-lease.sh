# shellcheck shell=bash
# Sourced by up.sh and vm/up.sh (issues #69, #71): the pool's lease, the one number a public certificate lives (golden rule 8, G8.2).
#   lease_seconds    prints lease_seconds of ${POOL_CONFIG:-$POC/pool.yaml}; status 2 and a message when it is missing, zero or not an
#                    integer: nebula-cert reads a zero duration as "until the CA expires", so a broken pool.yaml must stop the game, not
#                    hand out year-long public certificates
lease_seconds() {
  local f="${POOL_CONFIG:-$POC/pool.yaml}" v
  v=$(sed -n 's/^lease_seconds:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$f" 2>/dev/null | head -1)
  case "$v" in "" | *[!0-9]* | 0*) echo "$f: lease_seconds must be a positive integer" >&2; return 2 ;; esac
  echo "$v"
}
