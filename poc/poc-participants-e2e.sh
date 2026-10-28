#!/usr/bin/env bash
# make poc-participants-e2e: join by signed pull request end to end on Incus VMs (issue #12). A lighthouse and a control plane (the
# maintainer's host); alice's machine makes her Nebula key and her age key itself (the private halves never leave it); her file, signed by
# the LOCAL TEST CA that plays Fulcio (poc/sigstore_test_ca.py, TEST ONLY, as in make poc-participants), passes the pull request gate and
# is merged into main; one `participants.py sync` on the maintainer's side verifies it again and issues her bundle, encrypted to her key;
# on her machine `participants.py open` decrypts it and checks the issuer's signature, and `edge-mesh join` brings her node up. Then her
# file is deleted on main, and one sync revokes her: she loses the mesh and the API, and her Node does not come back.
# Needs a Linux host with Incus (as poc-admission). PASS/FAIL per step, identical across runs. KEEP=1 leaves the VMs up.
set -u
export RUNTIME=incus MAX_VMS=3 CNI=flannel
. "$(dirname "$0")/lib.sh"
. "$POC/vm/lib-vm.sh"
"$POC/vm/down.sh" >/dev/null 2>&1
mkdir -p "$STATE"
LOG="$CACHE/poc-participants-e2e.log"; : >"$LOG"   # kept after the run (.cache survives down.sh; .state does not)
. "$POC/participants-tools.sh"                      # the pinned cosign, age and nebula-cert (checked): COSIGN, AGE, AGE_KEYGEN, NC
F="$STATE/fixture"; R="$F/repo"; mkdir -p "$F"
PY="python3 $POC/participants.py"
TCA="python3 $POC/sigstore_test_ca.py"
TROOT="$F/testca/trusted_root.json"
ALICE_IP="$UNDERLAY_NET.150"
ax() { incus exec emesh-alice -- "$@"; }   # her machine. Not named after her node on purpose: revoke.sh pushes the blocklist to every
# machine named after a bundle, and the maintainer cannot reach a participant's machine; she has to lose the mesh because the hosts refuse her
g() { git -C "$R" -c user.name=fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false "$@"; }
sync() { COSIGN_PASSWORD="" $PY sync --repo "$R" --ref main --state "$STATE" --issuer-key "$F/issuer-TEST.key" \
  --signing-config "$F/signing-config-offline.json" --cosign "$COSIGN" --age "$AGE" --nebula-cert "$NC" --test-root "$TROOT"; }
ready() { [ "$(k get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
api_from_alice() { ax curl -sk -m5 -o /dev/null -w '%{http_code}' "https://$CP_NEB:6443/readyz" 2>/dev/null; }

host_up() {
  HOST_ONLY=1 "$POC/vm/up.sh" && $TCA init "$F/testca" >/dev/null && git init -q -b main "$R" && mkdir -p "$R/participants" &&
  sed 's/^maintainers: .*/maintainers: [maint]/' "$POC/../participants/policy.yaml" >"$R/participants/policy.yaml" &&
  g add -A && g commit -q -m policy &&
  COSIGN_PASSWORD="" "$COSIGN" generate-key-pair --output-key-prefix "$F/issuer-TEST" >/dev/null 2>&1 &&
  "$COSIGN" signing-config create --out "$F/signing-config-offline.json" >/dev/null 2>&1
}
alice_machine() { # her VM, with what a participant installs: nebula-cert, age, cosign, python3 (participants.py open)
  vm_up alice 2 3GiB "$ALICE_IP" || return 1
  local f
  for f in "$NC:/usr/local/bin/nebula-cert" "$AGE:/usr/local/bin/age" "$AGE_KEYGEN:/usr/local/bin/age-keygen" \
           "$COSIGN:/usr/local/bin/cosign" "$POC/participants.py:/root/participants.py" "$F/issuer-TEST.pub:/root/issuer.pub"; do
    incus file push -q "${f%%:*}" "emesh-alice${f#*:}" || return 1
  done
  ax chmod +x /usr/local/bin/nebula-cert /usr/local/bin/age /usr/local/bin/age-keygen /usr/local/bin/cosign &&
  ax sh -c 'mkdir -p /root/keys && cd /root/keys && nebula-cert keygen -out-key host.key -out-pub host.pub && age-keygen -o age.key 2>/dev/null &&
    chmod 600 host.key age.key'
}
alice_files() { # what she commits: her PUBLIC keys only, and the signature her fork's workflow makes (the TEST CA plays Fulcio here)
  local rcp
  ax cat /root/keys/host.pub >"$F/alice.pub" && rcp=$(ax sed -n 's/^# public key: //p' /root/keys/age.key) && [ -n "$rcp" ] || return 1
  { printf 'apiVersion: k8s-lan-party/v1\nkind: Participant\ngithub: alice\ngithub_id: 1001\nnode: alice-pc\ntrust: friends\n'
    printf 'resources: {cpu: 2, memory_gib: 3}\nnebula_public_key: |\n'; sed 's/^/  /' "$F/alice.pub"; printf 'recipient: %s\n' "$rcp"; } \
    >"$R/participants/alice.yaml"
  g checkout -q -b pr-alice && $TCA sign "$F/testca" "$R/participants/alice.yaml" "$R/participants/alice.sigstore.json" --login alice --owner-id 1001 &&
  g add -A && g commit -q -m "alice joins" &&
  $PY check --repo "$R" --base main --head pr-alice --author alice --author-id 1001 --cosign "$COSIGN" --test-root "$TROOT" &&
  g checkout -q main && g merge -q --no-ff -m "merge alice" pr-alice
}
issued() { local o; o=$(sync); echo "$o"; echo "$o" | grep -qx "issued alice (node alice-pc, ${LH_NEB%.*}.250, friends): participants-outbox/alice.age"; }
alice_joins() { # the file travels to her privately (here: incus file push); on her machine: decrypt, verify, unpack, join with her own key
  incus file push -q "$STATE/participants-outbox/alice.age" emesh-alice/root/alice.age &&
  ax date -s "@$(date +%s)" >/dev/null &&
  ax python3 /root/participants.py open /root/alice.age --identity /root/keys/age.key --issuer-pub /root/issuer.pub --out /root/bundle \
    --key /root/keys/host.key --offline-test &&
  ax edge-mesh join /root/bundle --accept-golden-rule && wait_for 240 ready alice-pc
}
key_stayed_home() { # the private key in her bundle is the one made on her machine; the issuing side never had it
  local secret; secret=$(ax sed -n 2p /root/keys/host.key)
  [ -n "$secret" ] && [ ! -e "$STATE/bundles/alice-pc/host.key" ] && ! grep -rqsF "$secret" "$STATE"
}
reaches_and_tainted() {
  [ "$(api_from_alice)" != 000 ] && ax ping -c1 -W2 "$LH_NEB" >/dev/null &&
  [ "$(k get node alice-pc -o jsonpath='{.spec.taints[0].key}={.spec.taints[0].value}')" = "trust=friends" ]
}
removed() {
  g rm -q participants/alice.yaml participants/alice.sigstore.json && g commit -q -m "alice leaves" || return 1
  local o; o=$(sync); echo "$o"; echo "$o" | grep -qx "revoked alice (node alice-pc): its file is gone from main or does not pass any more"
}
out_of_mesh() { ! ax ping -c1 -W2 "$LH_NEB" >/dev/null 2>&1 && [ "$(api_from_alice)" = 000 ]; }
node_gone() { ! k get node alice-pc >/dev/null 2>&1 && sleep 40 && ! k get node alice-pc >/dev/null 2>&1; }
nothing_left() {
  [ ! -e "$STATE/participants-outbox/alice.age" ] || return 1
  local o; o=$(sync); echo "$o"; echo "$o" | grep -qx "sync: issued 0, revoked 0, unchanged 0, problems 0"
}

step E01 "the maintainer's host: lighthouse and control plane up (Incus VMs), a repository with the policy, a TEST issuer key" host_up
step E02 "alice's machine makes her Nebula key and her age key itself" alice_machine
step E03 "her signed file passes the pull request gate and is merged into main" alice_files
step E04 "one sync on the maintainer's host verifies main again and issues her bundle, encrypted to her" issued
step E05 "on her machine: decrypt, verify the issuer's signature, join with her own key: her node is Ready" alice_joins
step E06 "her private key never reached the maintainer's side" key_stayed_home
step E07 "she reaches the lighthouse and the API over Nebula; her node carries the friends taint" reaches_and_tainted
step E08 "her file is deleted on main: one sync revokes her" removed
step E09 "removal revokes access: she reaches neither the lighthouse nor the API" wait_for 90 out_of_mesh
step E10 "her Node is gone and stays gone" node_gone
step E11 "nothing is left to deliver; sync again does nothing" nothing_left

[ "${KEEP:-0}" = 1 ] || "$POC/vm/down.sh" >/dev/null 2>&1
echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
