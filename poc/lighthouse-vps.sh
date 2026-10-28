#!/usr/bin/env bash
# lighthouse-vps.sh: run the Nebula lighthouse on a small public server, away from your control plane and from your CA.
# Why: players behind NAT must find the control plane through a lighthouse with a public UDP address. Only that machine is public;
# the CA key and the Kubernetes API stay with you (the API listens on its Nebula IP only).
#
#   lighthouse-vps.sh install <public-host> --ssh "<ssh command>"      all three phases (CA and ssh key on the same machine)
#   lighthouse-vps.sh prepare <public-host> --ssh "<ssh command>" [--out DIR]    on the machine with the server's ssh key:
#                                              install nebula, make the lighthouse key ON the server, write DIR/lh.pub
#   lighthouse-vps.sh sign <public-host> <lh.pub> [--duration 4320h] [--relay|--no-relay]   on the machine with the CA (.state/ca): sign it
#                                              -> .state/bundles/lh. --relay: the lighthouse is also a Nebula relay for players behind
#                                              symmetric NAT, and every bundle issued from now on names it (remembered; --no-relay undoes it)
#   lighthouse-vps.sh deploy --ssh "<ssh command>" [BUNDLE_DIR]        push certificate + config, start or reload (BUNDLE_DIR default: .state/bundles/lh)
#   lighthouse-vps.sh sync --ssh "<ssh command>"                       after `mesh host revoke`: push the new blocklist to the lighthouse
#   lighthouse-vps.sh verify --ssh "<ssh command>"                     service, UDP port, certificate days left, CA chain
#   lighthouse-vps.sh remove --ssh "<ssh command>"                     stop it and delete its keys and config (the binaries stay)
# <ssh command> is how to run a command there, e.g. "ssh -i ~/.ssh/key root@203.0.113.7"; the script feeds it a script on stdin.
# Every phase is idempotent: a second run prints no "changed:" line. The server's private key never leaves the server.
# The server's firewall is yours: open UDP 4242 (and nothing else besides ssh). The private key is generated on the server.
set -eu
. "$(dirname "$0")/lib.sh"
usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift
RSH=""; OUT="./lighthouse-out"; DUR="${LH_CERT_DURATION:-4320h}"; ARGS=(); RELAY_SET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) RSH="$2"; shift 2 ;; --out) OUT="$2"; shift 2 ;; --duration) DUR="$2"; shift 2 ;;
    --relay) RELAY_SET=yes; shift ;; --no-relay) RELAY_SET=no; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

r() { $RSH bash -s; }    # a script on stdin runs there (word splitting of $RSH is intended: it is a command line)
need_ssh() { [ -n "$RSH" ] || { echo "$cmd needs --ssh \"<ssh command>\""; exit 2; }; }
rput() { # rput <local-file> <remote-path> <mode>: copies only when the content differs
  local b64; b64=$(base64 -w0 <"$1")
  r <<EOF
set -eu
d='$2'; install -d -m 755 "\$(dirname "\$d")"
printf %s '$b64' | base64 -d >"\$d.new"
if [ -f "\$d" ] && cmp -s "\$d.new" "\$d"; then rm -f "\$d.new"; else mv "\$d.new" "\$d"; echo "changed: \$d"; fi
chmod $3 "\$d"
EOF
}
fetch_local() { # the pinned nebula-cert, next to the scripts
  [ -x "$CACHE/nebula-cert" ] && return 0
  curl -fsSL "https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/nebula-linux-amd64.tar.gz" | tar xz -C "$CACHE"
}

prepare() { # prepare <public-host>
  need_ssh; mkdir -p "$OUT"
  r <<EOF
set -eu
V=$NEBULA_VERSION
if ! (command -v nebula >/dev/null && nebula -version 2>&1 | grep -q "\$V"); then
  case "\$(uname -m)" in x86_64) A=amd64 ;; aarch64) A=arm64 ;; *) echo "unsupported architecture"; exit 1 ;; esac
  T=\$(mktemp -d); cd "\$T"
  curl -fsSL -O "https://github.com/slackhq/nebula/releases/download/v\$V/nebula-linux-\$A.tar.gz"
  curl -fsSL -O "https://github.com/slackhq/nebula/releases/download/v\$V/SHASUM256.txt"
  grep " nebula-linux-\$A.tar.gz\\\$" SHASUM256.txt | sha256sum -c - >/dev/null
  tar xzf "nebula-linux-\$A.tar.gz"; install -m 755 nebula nebula-cert /usr/local/bin/; cd /; rm -rf "\$T"
  echo "changed: nebula \$V installed (sha256 checked)"
fi
install -d -m 700 /etc/nebula
if [ ! -f /etc/nebula/host.key ]; then
  (umask 077; nebula-cert keygen -out-key /etc/nebula/host.key -out-pub /etc/nebula/host.pub)
  echo "changed: lighthouse key generated on the server (it never leaves it)"
fi
[ -f /etc/nebula/host.pub ] || { echo "host.pub is missing next to host.key"; exit 1; }
EOF
  echo 'cat /etc/nebula/host.pub' | r >"$OUT/lh.pub"
  echo "public key: $OUT/lh.pub"
}

sign() { # sign <public-host> <lh.pub>
  [ $# -eq 2 ] || usage
  local host="$1" pub sum cur
  [[ "$host" =~ ^[A-Za-z0-9.:-]+$ ]] || { echo "the public host must be a name or an address (letters, digits, . : -): $host"; exit 2; }   # it goes into lighthouse.env and YAML
  pub="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; fetch_local
  sum=$(sha256sum <"$pub" | cut -d' ' -f1)
  { grep -v '^LH_EXTERNAL=' "$STATE/lighthouse.env" 2>/dev/null || true; printf 'LH_EXTERNAL=%q\n' "$host"; } >"$STATE/lighthouse.env.new"
  mv "$STATE/lighthouse.env.new" "$STATE/lighthouse.env"
  if [ -n "$RELAY_SET" ]; then   # remembered: issue.sh reads LH_RELAY from lighthouse.env for this config and every later bundle
    local was; was=$(sed -n 's/^LH_RELAY=//p' "$STATE/lighthouse.env"); [ -n "$was" ] || was=no
    { grep -v '^LH_RELAY=' "$STATE/lighthouse.env" || true; echo "LH_RELAY=$RELAY_SET"; } >"$STATE/lighthouse.env.new" && mv "$STATE/lighthouse.env.new" "$STATE/lighthouse.env"
    [ "$was" = "$RELAY_SET" ] || echo "changed: relay $([ "$RELAY_SET" = yes ] && echo on || echo off) (bundles issued from now on follow; deploy it, then 'mesh host refresh cp')"
  fi
  cur="$STATE/bundles/lh"
  if [ -f "$cur/host.crt" ] && [ "$(cat "$cur/pub.sha256" 2>/dev/null)" = "$sum" ] &&
     python3 - "$CACHE/nebula-cert" "$cur/host.crt" <<'PY'; then
import json, subprocess, sys, datetime as d
n = json.loads(subprocess.check_output([sys.argv[1], "print", "-json", "-path", sys.argv[2]]))[0]["details"]["notAfter"]
left = d.datetime.fromisoformat(n.replace("Z", "+00:00")) - d.datetime.now(d.timezone.utc)
sys.exit(0 if left.days >= 30 else 1)
PY
    # same certificate, but the config is regenerated every time: it carries the current blocklist and any new section (stats)
    LH_EXTERNAL="$host" CONFIG_ONLY=1 "$POC/issue.sh" lh x x x >/dev/null
    echo "lighthouse certificate: unchanged (same key, more than 30 days left)"; return 0
  fi
  LH_EXTERNAL="$host" IN_PUB="$pub" "$POC/issue.sh" lh "$LH_NEB" lighthouse "$DUR" >/dev/null
  printf '%s\n' "$sum" >"$cur/pub.sha256"
  echo "changed: lighthouse certificate signed for $host ($DUR); bundle: $cur"
}

deploy() { # deploy [bundle-dir]
  need_ssh
  local B="${1:-$STATE/bundles/lh}"
  # on the CA machine (default bundle) remember how to reach the server, so `mesh host revoke` can push the blocklist by itself
  if [ $# -eq 0 ] && [ -f "$STATE/lighthouse.env" ]; then
    { grep -v '^LH_RSH=' "$STATE/lighthouse.env" || true; printf 'LH_RSH=%q\n' "$RSH"; } >"$STATE/lighthouse.env.new" && mv "$STATE/lighthouse.env.new" "$STATE/lighthouse.env"
  fi
  [ -f "$B/host.crt" ] && [ -f "$B/config.yml" ] && [ -f "$B/ca.crt" ] || { echo "no lighthouse bundle in $B (run sign first)"; exit 3; }
  local unit; unit=$(mktemp)
  cat >"$unit" <<'UNIT'
[Unit]
Description=Nebula lighthouse (edge-mesh)
Wants=network-online.target
After=network-online.target
[Service]
ExecStart=/usr/local/bin/nebula -config /etc/nebula/config.yml
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
[Install]
WantedBy=multi-user.target
UNIT
  # `stats` and `listen` are read only at start: when they differ from what runs, a reload is not enough. `relay` too: turning the relay on
  # or off restarts the lighthouse (tunnels come back in seconds), so it never depends on what Nebula re-reads on SIGHUP
  local restart=0 old new
  old=$(echo 'grep -E "^(stats|listen|relay):" /etc/nebula/config.yml 2>/dev/null || true' | r); new=$(grep -E "^(stats|listen|relay):" "$B/config.yml" || true)
  [ "$old" = "$new" ] || restart=1
  local out; out=$( { rput "$B/ca.crt" /etc/nebula/ca.crt 644; rput "$B/host.crt" /etc/nebula/host.crt 644
                      rput "$B/config.yml" /etc/nebula/config.yml 600; rput "$unit" /etc/systemd/system/nebula.service 644; } )
  rm -f "$unit"; [ -z "$out" ] || echo "$out"
  r <<EOF
set -eu
systemctl daemon-reload
if ! systemctl is-enabled -q nebula; then systemctl enable nebula >/dev/null 2>&1; fi
state() { systemctl is-active nebula 2>/dev/null || true; }
# a restart already scheduled by systemd (the first start can fail on a server whose clock is a few seconds behind) is not a change: let it finish
if [ "\$(state)" = activating ]; then for i in \$(seq 20); do sleep 1; [ "\$(state)" = active ] && break; done; fi
if [ "\$(state)" != active ]; then systemctl reset-failed nebula 2>/dev/null || true; systemctl restart nebula; echo "changed: nebula started"
elif echo '$out' | grep -q nebula.service; then systemctl restart nebula; echo "changed: nebula restarted (unit)"
elif [ '$restart' = 1 ] && [ -n '$out' ]; then systemctl restart nebula; echo "changed: nebula restarted (stats, listen or relay section)"
elif [ -n '$out' ]; then systemctl reload nebula; echo "changed: nebula reloaded (certificate or config)"; fi
for i in \$(seq 25); do [ "\$(state)" = active ] && break; sleep 1; done
[ "\$(state)" = active ] || { echo "FAIL: nebula is not active; last log lines:"; journalctl -u nebula --no-pager -n 8 | cut -c1-200; exit 1; }
EOF
}

verify() {
  need_ssh
  r <<'EOF'
fail=0; ok() { echo "ok:   $*"; }; no() { echo "FAIL: $*"; fail=1; }
systemctl is-active -q nebula && ok "nebula service active" || no "nebula service is not active"
ss -lun | grep -q ':4242 ' && ok "listening on udp/4242" || no "nothing listens on udp/4242"
[ "$(stat -c %a /etc/nebula/host.key 2>/dev/null)" = 600 ] && ok "host.key mode 600" || no "host.key missing or not mode 600"
nebula-cert verify -ca /etc/nebula/ca.crt -crt /etc/nebula/host.crt >/dev/null 2>&1 && ok "certificate chains to the CA" || no "certificate does not chain to the CA"
d=$(nebula-cert print -json -path /etc/nebula/host.crt | python3 -c 'import sys,json,datetime as d;n=json.load(sys.stdin)[0]["details"]["notAfter"];print((d.datetime.fromisoformat(n.replace("Z","+00:00"))-d.datetime.now(d.timezone.utc)).days)')
[ "${d:-0}" -ge 30 ] && ok "certificate valid for $d more days" || no "certificate has only ${d:-0} days left: run sign and deploy"
grep -q 'am_lighthouse: true' /etc/nebula/config.yml && ok "config says am_lighthouse: true" || no "config is not a lighthouse"
if grep -q 'am_relay: true' /etc/nebula/config.yml; then ok "relay on: players behind symmetric NAT go through this server (it carries their traffic)"
else ok "relay off"; fi
exit $fail
EOF
}

remove() {
  need_ssh
  r <<'EOF'
systemctl disable --now nebula >/dev/null 2>&1 || true
rm -f /etc/systemd/system/nebula.service; systemctl daemon-reload
rm -rf /etc/nebula
echo "removed: nebula service, keys and config (the nebula binaries stay in /usr/local/bin)"
EOF
  rm -f "$STATE/lighthouse.env"
}

case "$cmd" in
  prepare) [ $# -eq 1 ] || usage; prepare "$1" ;;
  sign) sign "$@" ;;
  deploy) deploy "$@" ;;
  install) [ $# -eq 1 ] || usage; prepare "$1"; sign "$1" "$OUT/lh.pub"; deploy ;;
  sync) deploy ;;
  verify) verify ;;
  remove) remove ;;
  *) usage ;;
esac
