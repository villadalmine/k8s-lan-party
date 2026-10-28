#!/usr/bin/env bash
# page-server.sh: put the public page (docs/PUBLIC-PAGE.md) on YOUR public server, behind Caddy, and wire the puller of your control plane.
# Run from the machine with the repository (and, for `puller-token`, the control plane's state); every phase is idempotent: a second run
# prints no "changed:" line.
#
#   page-server.sh install --ssh "<ssh command>" --site <address> [--tls internal] [--with-caddy]
#        the page (/opt/k8s-lan-party-page, root-owned), its unit (k8s-lan-party-page.service, loopback only, no capabilities, no outbound
#        network) and a Caddy site (/etc/caddy/conf.d/k8s-lan-party-page.caddy). --site: a DNS name for ACME (page.example.org) or, in a lab,
#        https://<ip> with --tls internal. --with-caddy: also install Caddy (pinned, sha512 checked) when the server has none.
#   page-server.sh puller-token --ssh "<ssh command>" [--token-file FILE]
#        make the puller's token HERE (default <state>/page/puller.token, 0600) and send only its SHA-256 to the server
#   page-server.sh verify --ssh "<ssh command>" --site <address>      services, loopback-only listener, the API through Caddy, no key on disk
#   page-server.sh close|open --ssh "<ssh command>"                    the operator's switch on the server (the host's panic closes it too)
#   page-server.sh cacert --ssh "<ssh command>"                        print Caddy's internal root (only with --tls internal: the lab)
#   page-server.sh remove --ssh "<ssh command>" [--with-caddy]         stop it and delete the page, its unit, its state and its Caddy site; --with-caddy also
#        removes the Caddy parts THIS script installed (a marker says which), never a Caddy that was already there
# <ssh command> runs a command there as root, e.g. "ssh -i key admin@203.0.113.7 sudo"; the script feeds it a script on stdin.
# The server's firewall is yours: open TCP 443 (and 80 for ACME). Nothing of the mesh lives on the server but the lighthouse (lighthouse-vps.sh).
set -eu
. "$(dirname "$0")/../lib.sh"
usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 1 ] || usage
cmd="$1"; shift
RSH=""; SITE=""; TLS=""; WITH_CADDY=0; TOKEN_FILE="$STATE/page/puller.token"
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) RSH="$2"; shift 2 ;; --site) SITE="$2"; shift 2 ;; --tls) TLS="$2"; shift 2 ;; --with-caddy) WITH_CADDY=1; shift ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;; *) usage ;;
  esac
done
[ -n "$RSH" ] || { echo "$cmd needs --ssh \"<ssh command>\""; exit 2; }
r() { $RSH bash -s; }    # a script on stdin runs there (word splitting of $RSH is intended: it is a command line)
PAGE="$POC/page"
CADDY_VERSION=2.11.7   # looked up live on 2026-10-28; the sha512 below is from its caddy_2.11.7_checksums.txt
CADDY_SHA512_amd64=a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7
CADDY_SHA512_arm64=3db36ba90c7a6e8dda40ee3dd71fa08844c76b5fb08f61b31e5e78d2ed38e71c51dc7baed875e50d1ca1279196e84302967237386ae87c91ae9f2aaceada682e
SITE_RE='^(https://)?[][A-Za-z0-9.:-]+$'
site_ok() { [[ "$SITE" =~ $SITE_RE ]] || { echo "--site must be a name or https://<address> (letters, digits, . : - [ ])"; exit 2; }; }
case "$TLS" in "" | internal) ;; *) echo "--tls takes only: internal"; exit 2 ;; esac

MARK=/etc/caddy/.k8s-lan-party-installed   # what THIS script installed; `remove --with-caddy` deletes only that
install_caddy() { # Caddy itself, only when the server has none (a real server usually has its own: then only the site is added)
  [ -n "$SITE" ] || { echo "install needs --site"; exit 2; }; site_ok
  local SNI; SNI=$(printf '%s' "${SITE#https://}" | tr -d '[]')
  r <<EOF
set -eu
install -d -m 755 /etc/caddy
mk() { grep -qx "\$1" $MARK 2>/dev/null || { echo "\$1" >>$MARK; }; }
if ! command -v caddy >/dev/null; then
  case "\$(uname -m)" in x86_64) A=amd64; S=$CADDY_SHA512_amd64 ;; aarch64) A=arm64; S=$CADDY_SHA512_arm64 ;; *) echo "unsupported architecture"; exit 1 ;; esac
  T=\$(mktemp -d); cd "\$T"
  curl -fsSL -o c.tgz "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_\$A.tar.gz"
  echo "\$S  c.tgz" | sha512sum -c - >/dev/null
  tar xzf c.tgz caddy && install -m 755 caddy /usr/local/bin/caddy; cd /; rm -rf "\$T"
  echo "changed: caddy $CADDY_VERSION installed (sha512 checked)"
  mk binary
fi
id caddy >/dev/null 2>&1 || { useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy; mk user; echo "changed: user caddy"; }
install -d -m 755 /etc/caddy /etc/caddy/conf.d
if [ ! -f /etc/caddy/Caddyfile ]; then
  # default_sni: a visitor that opens https://<ip> sends no server name; Caddy then picks the certificate of this one (the lab's IP site)
  printf '{\n\tskip_install_trust\n\tdefault_sni %s\n\tservers {\n\t\ttimeouts {\n\t\t\tread_header 10s\n\t\t\tread_body 30s\n\t\t\tidle 2m\n\t\t}\n\t}\n}\nimport /etc/caddy/conf.d/*.caddy\n' "$SNI" >/etc/caddy/Caddyfile
  mk caddyfile; echo "changed: /etc/caddy/Caddyfile"
fi
if [ ! -f /etc/systemd/system/caddy.service ] && [ ! -f /lib/systemd/system/caddy.service ]; then
  cat >/etc/systemd/system/caddy.service <<'U'
[Unit]
Description=Caddy (k8s-lan-party lab)
After=network-online.target
Wants=network-online.target
[Service]
User=caddy
Group=caddy
Environment=XDG_DATA_HOME=/var/lib/caddy XDG_CONFIG_HOME=/var/lib/caddy/config
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes
Restart=on-failure
[Install]
WantedBy=multi-user.target
U
  mk service; systemctl daemon-reload; systemctl enable --now caddy >/dev/null 2>&1; echo "changed: caddy service"
fi
EOF
}

install_page() {
  [ -n "$SITE" ] || { echo "install needs --site"; exit 2; }; site_ok
  local t; t=$(mktemp -d)
  mkdir -p "$t/opt/static"
  cp "$PAGE/page.py" "$PAGE/seal.py" "$POC/pool.py" "$POC/admission.py" "$PAGE/page.yaml" "$t/opt/"
  cp "$PAGE/static/page.js" "$PAGE/static/page.css" "$t/opt/static/"
  cp "$PAGE/deploy/k8s-lan-party-page.service" "$t/"
  local tls_line=""; [ "$TLS" = internal ] && tls_line="tls internal"
  sed -e "s#^page.example.org {#$SITE {#" -e "s|# @TLS@|$tls_line|" "$PAGE/deploy/k8s-lan-party-page.caddy" >"$t/site.caddy"
  local b64; b64=$(tar czf - -C "$t" . | base64 -w0); rm -rf "$t"
  r <<EOF
set -eu
command -v python3 >/dev/null || { echo "python3 is missing on the server (apt-get install python3)"; exit 1; }
T=\$(mktemp -d); printf %s '$b64' | base64 -d | tar xzf - -C "\$T"
put() { # put <src> <dst> <mode>: only when the content differs
  install -d -m 755 "\$(dirname "\$2")"
  if [ -f "\$2" ] && cmp -s "\$1" "\$2"; then chmod \$3 "\$2"; return 0; fi
  install -m \$3 -o root -g root "\$1" "\$2"; echo "changed: \$2"
}
code=""
for f in page.py seal.py pool.py admission.py page.yaml static/page.js static/page.css; do
  o=\$(put "\$T/opt/\$f" "/opt/k8s-lan-party-page/\$f" 644); [ -z "\$o" ] || { echo "\$o"; code=1; }
done
unit=\$(put "\$T/k8s-lan-party-page.service" /etc/systemd/system/k8s-lan-party-page.service 644); [ -z "\$unit" ] || echo "\$unit"
install -d -m 755 /etc/k8s-lan-party-page
site=\$(put "\$T/site.caddy" /etc/caddy/conf.d/k8s-lan-party-page.caddy 644); [ -z "\$site" ] || echo "\$site"
rm -rf "\$T"
[ -z "\$unit" ] || systemctl daemon-reload
if ! systemctl is-enabled -q k8s-lan-party-page 2>/dev/null; then systemctl enable k8s-lan-party-page >/dev/null 2>&1; fi
if ! systemctl is-active -q k8s-lan-party-page; then systemctl restart k8s-lan-party-page; echo "changed: page started"
elif [ -n "\$unit\$code" ]; then systemctl restart k8s-lan-party-page; echo "changed: page restarted"; fi
if [ -n "\$site" ]; then
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || { echo "FAIL: caddy validate"; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -3; exit 1; }
  systemctl reload caddy; echo "changed: caddy reloaded"
fi
for i in \$(seq 20); do systemctl is-active -q k8s-lan-party-page && break; sleep 1; done
systemctl is-active -q k8s-lan-party-page || { echo "FAIL: the page is not active"; journalctl -u k8s-lan-party-page --no-pager -n 8 | cut -c1-200; exit 1; }
EOF
}

puller_token() {
  if [ ! -s "$TOKEN_FILE" ]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"; (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$TOKEN_FILE")
    echo "changed: puller token made in $TOKEN_FILE (it stays here)"
  fi
  chmod 600 "$TOKEN_FILE"
  local h; h=$(tr -d '\n' <"$TOKEN_FILE" | sha256sum | cut -d' ' -f1)
  r <<EOF
set -eu
install -d -m 755 /etc/k8s-lan-party-page
if [ "\$(cat /etc/k8s-lan-party-page/puller.sha256 2>/dev/null)" != "$h" ]; then
  printf '%s' "$h" | python3 -I /opt/k8s-lan-party-page/page.py set-puller-hash --state /var/lib/k8s-lan-party-page --control /etc/k8s-lan-party-page >/dev/null
  echo "changed: puller hash set (the server never sees the token)"
fi
EOF
}

verify() {
  [ -n "$SITE" ] || { echo "verify needs --site"; exit 2; }; site_ok
  local url="$SITE"; case "$url" in https://*) ;; *) url="https://$url" ;; esac
  r <<EOF
fail=0; ok() { echo "ok:   \$*"; }; no() { echo "FAIL: \$*"; fail=1; }
systemctl is-active -q k8s-lan-party-page && ok "page service active" || no "page service is not active"
systemctl is-active -q caddy && ok "caddy active" || no "caddy is not active"
l=\$(ss -ltnH 'sport = :8088' | awk '{print \$4}')
[ "\$l" = "127.0.0.1:8088" ] && ok "the page listens on 127.0.0.1:8088 only" || no "the page listens on: \$l"
s=\$(curl -sk --connect-to ::127.0.0.1: -o /dev/null -w '%{http_code}' "$url/api/v1/status")
[ "\$s" = 200 ] && ok "the API answers through Caddy" || no "the API through Caddy: \$s"
h=\$(curl -skI --connect-to ::127.0.0.1: "$url/" | tr -d '\r')
echo "\$h" | grep -qi '^strict-transport-security:' && echo "\$h" | grep -qi "^content-security-policy: default-src 'none'" &&
  ok "HSTS and the CSP are sent" || no "a security header is missing"
d=/var/lib/private/k8s-lan-party-page
[ "\$(stat -c %a \$d 2>/dev/null)" = 700 ] && ok "state directory 0700" || no "state directory mode: \$(stat -c %a \$d 2>/dev/null)"
if grep -rlE 'PRIVATE KEY|K10[0-9a-f]{20}' \$d /opt/k8s-lan-party-page /etc/k8s-lan-party-page >/dev/null 2>&1; then no "a private key or a join token on disk"; else ok "no private key and no join token in the page's files"; fi
[ -s /etc/k8s-lan-party-page/puller.sha256 ] && ok "the puller's hash is set" || no "no puller hash (run puller-token)"
exit \$fail
EOF
}

remove() {
  r <<EOF
systemctl disable --now k8s-lan-party-page >/dev/null 2>&1 || true
rm -f /etc/systemd/system/k8s-lan-party-page.service; systemctl daemon-reload
rm -rf /opt/k8s-lan-party-page /etc/k8s-lan-party-page /var/lib/private/k8s-lan-party-page /var/lib/k8s-lan-party-page
if [ -f /etc/caddy/conf.d/k8s-lan-party-page.caddy ]; then rm -f /etc/caddy/conf.d/k8s-lan-party-page.caddy; systemctl reload caddy 2>/dev/null || true; fi
if [ "$WITH_CADDY" = 1 ]; then
  if [ -f $MARK ]; then   # only what this script installed, as the marker says; a Caddy that was already there is never touched
    has() { grep -qx "\$1" $MARK; }
    if has service; then systemctl disable --now caddy >/dev/null 2>&1 || true; rm -f /etc/systemd/system/caddy.service; fi
    if has binary; then rm -f /usr/local/bin/caddy; fi
    if has caddyfile; then rm -f /etc/caddy/Caddyfile; fi
    if has user; then rm -rf /var/lib/caddy; userdel caddy 2>/dev/null || true; fi
    rm -f $MARK; rmdir /etc/caddy/conf.d /etc/caddy 2>/dev/null || true; systemctl daemon-reload
    echo "removed: the parts of Caddy this script installed"
  else
    echo "kept: Caddy was already there (no marker $MARK): not touched"
  fi
fi
echo "removed: the page, its unit, its state and its Caddy site"
EOF
}

case "$cmd" in
  install) [ "$WITH_CADDY" = 1 ] && install_caddy; install_page ;;
  puller-token) puller_token ;;
  verify) verify ;;
  close) echo 'python3 -I /opt/k8s-lan-party-page/page.py close --state /var/lib/k8s-lan-party-page --control /etc/k8s-lan-party-page' | r ;;
  open) echo 'python3 -I /opt/k8s-lan-party-page/page.py open --state /var/lib/k8s-lan-party-page --control /etc/k8s-lan-party-page' | r ;;
  cacert) echo 'cat /var/lib/caddy/caddy/pki/authorities/local/root.crt' | r ;;
  remove) remove ;;
  *) usage ;;
esac
