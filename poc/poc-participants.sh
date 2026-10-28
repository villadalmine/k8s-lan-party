#!/usr/bin/env bash
# make poc-participants (33 checks): join by signed pull request (issue #12, docs/DESIGN.md section 5.1, participants/README.md), offline.
# The real cosign verifies Sigstore bundles made by a LOCAL TEST CA (poc/sigstore_test_ca.py: "TEST ONLY", made in the state directory,
# never committed) in place of Fulcio; the pull request gate (participants.py check) runs against a throw-away git repository with one
# branch per pull request; the issuing side (participants.py sync) runs the real poc/issue.sh and poc/revoke.sh against a stub control
# plane (a fake `docker` on PATH that answers the k3s and kubectl calls), with the real nebula-cert, cosign and age.
# What a stub cannot show, a Node losing the mesh after the removal, poc-participants-e2e.sh shows on Incus VMs.
# Output: one PASS/FAIL per step, identical across runs. No network after the pinned tools are cached: every step runs with a proxy that
# refuses connections (HTTPS_PROXY to a closed port).
set -u
export EMESH_STATE="${EMESH_STATE:-$(cd "$(dirname "$0")" && pwd)/.state/participants-poc}"
rm -rf "$EMESH_STATE"
. "$(dirname "$0")/lib.sh"
: >"$LOG"
export RUNTIME=docker
F="$STATE/fixture"; R="$F/repo"; mkdir -p "$F/bin" "$F/keys"
PY="python3 $POC/participants.py"
TCA="python3 $POC/sigstore_test_ca.py"

. "$POC/participants-tools.sh"
python3 -c 'import yaml, cryptography' 2>/dev/null || { echo "needs python3 with pyyaml and cryptography (pip install pyyaml cryptography)"; exit 2; }
export HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy=
export TUF_ROOT="$F/tuf"   # an empty TUF cache: nothing below may reach the public Sigstore

# --- the stub control plane: what issue.sh, revoke.sh and `mesh host burn` ask of docker, k3s and kubectl -----------------------------
cat >"$F/bin/docker" <<'EOF'
#!/bin/sh
echo "$*" >>"$FAKE_LOG"
[ "$1" = exec ] || exit 0
shift; [ "$1" = -i ] && shift; shift
case "$1 $2 $3" in
  "k3s token create") n=$(cat "$FAKE_DIR/tokens" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$FAKE_DIR/tokens"
                      printf 'K10fakeca::t%05d.%016d\n' "$n" "$n" ;;
  "k3s token list") echo "TOKEN TTL EXPIRES" ;;
  "kubectl get nodes") [ "$5" = json ] && echo '{"items": []}' ;;
  "kubectl get node") exit 1 ;;
  "kubectl create -f"|"kubectl apply -f") cat >/dev/null ;;
esac
exit 0
EOF
chmod +x "$F/bin/docker"
printf '#!/bin/sh
echo "revoke: the API did not answer (stub)"; exit 4
' >"$F/bin/revoke-fails"; chmod +x "$F/bin/revoke-fails"
export PATH="$F/bin:$PATH" FAKE_LOG="$F/docker.log" FAKE_DIR="$F"
: >"$FAKE_LOG"

# --- the fixture: the TEST CA, participants' keys, a repository whose main holds the policy and bob ------------------------------------
TROOT="$F/testca/trusted_root.json"
keys() { # keys <login>: a Nebula key pair (the private half stays "on their machine": $F/keys) and an age identity
  "$NC" keygen -out-key "$F/keys/$1.key" -out-pub "$F/keys/$1.pub" && "$AGE_KEYGEN" -o "$F/keys/$1.age" 2>/dev/null
}
recipient() { sed -n 's/^# public key: //p' "$F/keys/$1.age"; }
g() { git -C "$R" -c user.name=fixture -c user.email=fixture@example.invalid -c commit.gpgsign=false "$@"; }
pfile() { # pfile <login> <github_id> <node> [trust] [cpu] [key-of]: write participants/<login>.yaml in the work tree
  { printf 'apiVersion: k8s-lan-party/v1\nkind: Participant\ngithub: %s\ngithub_id: %s\nnode: %s\ntrust: %s\n' "$1" "$2" "$3" "${4:-friends}"
    printf 'resources: {cpu: %s, memory_gib: 4}\nnebula_public_key: |\n' "${5:-2}"
    sed 's/^/  /' "$F/keys/${6:-$1}.pub"
    printf 'recipient: %s\n' "$(recipient "${6:-$1}")"; } >"$R/participants/$1.yaml"
}
sign_as() { # sign_as <file-login> <signer-login> <owner-id> [sigstore_test_ca.py options]: the bundle as if <signer>'s fork had signed it
  local f="$1" s="$2" id="$3"; shift 3
  $TCA sign "$F/testca" "$R/participants/$f.yaml" "$R/participants/$f.sigstore.json" --login "$s" --owner-id "$id" "$@"
}
branch() { g checkout -q -f -B "$1" main && g clean -qfd; }
commit() { g add -A . && g commit -q -m "$1"; }
check() { # check <head> <author> <author-id> [base]: the pull request gate as CI runs it, with the TEST root
  $PY check --repo "$R" --base "${4:-main}" --head "$1" --author "$2" --author-id "$3" --cosign "$COSIGN" --test-root "$TROOT"
}
passes() { local o rc=0; o=$("$@" 2>&1) || rc=$?; echo "$o"; [ "$rc" = 0 ]; }
fails_with() { local want="$1" o rc=0; shift; o=$("$@" 2>&1) || rc=$?; echo "$o"; [ "$rc" = 1 ] && echo "$o" | grep -qF -- "$want"; }

fixture() {
  $TCA init "$F/testca" && $TCA init "$F/otherca" || return 1
  for u in alice bob carol mallory; do keys "$u" || return 1; done
  mkdir -p "$R/participants" && git init -q -b main "$R" &&
  sed 's/^maintainers: .*/maintainers: [maint]/' "$POC/../participants/policy.yaml" >"$R/participants/policy.yaml" &&
  echo "fixture" >"$R/README.md" && mkdir -p "$R/poc" && echo "#" >"$R/poc/mesh" && commit "policy" || return 1
  pfile bob 1002 bob-pc && sign_as bob bob 1002 && commit "bob joins" || return 1
  COSIGN_PASSWORD="" "$COSIGN" generate-key-pair --output-key-prefix "$F/issuer-TEST" >/dev/null 2>&1 &&
  COSIGN_PASSWORD="" "$COSIGN" generate-key-pair --output-key-prefix "$F/mallory-cosign" >/dev/null 2>&1 &&
  "$COSIGN" signing-config create --out "$F/signing-config-offline.json" >/dev/null 2>&1 || return 1
  # the TEST marker: every authority of the root the suite trusts says TEST ONLY
  python3 -c 'import json,sys; sys.path.insert(0, sys.argv[2]); import participants as p
r = json.load(open(sys.argv[1])); assert r["certificateAuthorities"] and all(c["subject"]["organization"] == p.TEST_ORG for c in r["certificateAuthorities"])
assert not r["tlogs"] and not r["ctlogs"]' "$TROOT" "$POC"
}

# --- the pull request gate ------------------------------------------------------------------------------------------------------------
t_valid() { branch pr-alice && pfile alice 1001 alice-pc && sign_as alice alice 1001 && commit "alice joins" || return 1
  local o; o=$(check pr-alice alice 1001) || { echo "$o"; return 1; }; echo "$o"; [ "$o" = "OK 2 file(s) under participants/ checked" ]; }
t_other_identity() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice bob 1002 && commit x &&
  fails_with "does not verify for alice" check pr-x alice 1001; }
t_other_account() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice alice 9999 && commit x &&
  fails_with "signed from the GitHub account with id 9999" check pr-x alice 1001; }
t_replay() { fails_with "only alice may add or change it (the PR author is mallory)" check pr-alice mallory 1003; }
t_unsigned() { branch pr-x && pfile alice 1001 alice-pc && commit x && fails_with "not signed" check pr-x alice 1001; }
t_tampered() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice alice 1001 && pfile alice 1001 alice-pc friends 64 && commit x &&
  fails_with "invalid signature" check pr-x alice 1001; }
t_untrusted_ca() { branch pr-x && pfile alice 1001 alice-pc &&
  $TCA sign "$F/otherca" "$R/participants/alice.yaml" "$R/participants/alice.sigstore.json" --login alice --owner-id 1001 && commit x &&
  fails_with "does not verify for alice" check pr-x alice 1001; }
t_wrong_issuer() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice alice 1001 --issuer https://github.com/login/oauth && commit x &&
  fails_with "does not verify for alice" check pr-x alice 1001; }
t_other_workflow() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice alice 1001 --workflow .github/workflows/other.yml && commit x &&
  fails_with "does not verify for alice" check pr-x alice 1001; }
t_key_signed() { # a signature made with a cosign key (no certificate, no identity) is not a keyless signature
  branch pr-x && pfile alice 1001 alice-pc &&
  COSIGN_PASSWORD="" "$COSIGN" sign-blob --yes --key "$F/mallory-cosign.key" --signing-config "$F/signing-config-offline.json"     --bundle "$R/participants/alice.sigstore.json" "$R/participants/alice.yaml" >/dev/null 2>&1 && commit x &&
  fails_with "must carry exactly one Fulcio certificate" check pr-x alice 1001; }
t_trust_above() { branch pr-own && pfile alice 1001 alice-pc own && sign_as alice alice 1001 && commit "alice asks for own" &&
  fails_with "only the maintainer can raise it" check pr-own alice 1001; }
t_grant_in_pr() { branch pr-x && pfile alice 1001 alice-pc own && sign_as alice alice 1001 &&
  sed -i 's/^grants: {}/grants: {alice: own}/' "$R/participants/policy.yaml" && commit x &&
  fails_with "participants/policy.yaml: only the maintainer changes it" check pr-x alice 1001 && fails_with "only the maintainer can raise it" check pr-x alice 1001; }
t_granted() { # the maintainer merged a grant first: the same pull request now passes against that base
  branch main-granted && sed -i 's/^grants: {}/grants: {alice: own}/' "$R/participants/policy.yaml" && commit "grant alice own" &&
  passes check pr-own alice 1001 main-granted; }
t_unique() { branch pr-x && pfile alice 1001 bob-pc && sign_as alice alice 1001 && commit x &&
  fails_with "the node name 'bob-pc' is already bob's" check pr-x alice 1001; }
t_schema() { branch pr-x && pfile alice 1001 cp && sign_as alice alice 1001 && commit x && fails_with "node 'cp' is reserved" check pr-x alice 1001 &&
  branch pr-y && pfile alice 1001 alice-pc && echo "trust: own" >>"$R/participants/alice.yaml" && sign_as alice alice 1001 && commit y &&
  fails_with "duplicate key 'trust'" check pr-y alice 1001; }
t_mixed() { branch pr-x && pfile alice 1001 alice-pc && sign_as alice alice 1001 && echo "# more" >>"$R/poc/mesh" && commit x &&
  fails_with "move poc/mesh to another pull request" check pr-x alice 1001; }
t_removal_pr() { branch pr-bob-leaves && g rm -q participants/bob.yaml participants/bob.sigstore.json && commit "bob leaves" &&
  passes check pr-bob-leaves bob 1002 && passes check pr-bob-leaves maint 1 && fails_with "only bob or the maintainer may remove it" check pr-bob-leaves mallory 1003 &&
  fails_with "only bob's account (id 1002) or the maintainer may remove it" check pr-bob-leaves bob 9999; }   # a re-registered login
t_nothing() { branch pr-docs && echo more >>"$R/README.md" && commit docs || return 1
  local o; o=$(check pr-docs mallory 1003) || { echo "$o"; return 1; }; echo "$o"; [ "$o" = "OK no participant file changed" ]; }
t_interlock() { # --test-root refuses a trusted root that is not marked TEST ONLY: the insecure flags never meet a real root
  sed "s/$(python3 -c "import sys; sys.path.insert(0, '$POC'); import participants as p; print(p.TEST_ORG)")/Some Real CA/" "$TROOT" >"$F/unmarked.json"
  local o; o=$($PY check --repo "$R" --base main --head pr-alice --author alice --author-id 1001 --cosign "$COSIGN" --test-root "$F/unmarked.json" 2>&1)
  echo "$o"; echo "$o" | grep -q "not a local TEST root"
}

# --- the issuing side -------------------------------------------------------------------------------------------------------------------
SYNC_ARGS=(--repo "$R" --ref main --state "$STATE" --issuer-key "$F/issuer-TEST.key" --signing-config "$F/signing-config-offline.json"
           --cosign "$COSIGN" --age "$AGE" --nebula-cert "$NC" --test-root "$TROOT")
sync() { COSIGN_PASSWORD="" $PY sync "${SYNC_ARGS[@]}"; }
merge() { g checkout -q main && g merge -q --no-ff -m "merge $1" "$1"; }
fp_of() { "$NC" print -json -path "$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["fingerprint"])'; }
open_as() { # open_as <whose file> <with whose age key> <out>
  $PY open "$STATE/participants-outbox/$1.age" --identity "$F/keys/$2.age" --issuer-pub "$F/issuer-TEST.pub" --out "$3" --key "$F/keys/$2.key" \
    --cosign "$COSIGN" --age "$AGE" --offline-test
}
hosts_up() { "$POC/issue.sh" lh "$LH_NEB" lighthouse 24h && "$POC/issue.sh" cp "$CP_NEB" own 24h; }
t_sync_issues() { merge pr-alice && hosts_up >/dev/null || return 1
  local o; o=$(sync); echo "$o"
  echo "$o" | grep -qx "issued alice (node alice-pc, 10.77.0.250, friends): participants-outbox/alice.age" &&
  echo "$o" | grep -qx "issued bob (node bob-pc, 10.77.0.249, friends): participants-outbox/bob.age" &&
  [ "$(stat -c %a "$STATE/participants-outbox/alice.age")" = 600 ] && [ "$(stat -c %a "$STATE/participants-outbox")" = 700 ]; }
t_ciphertext() { local f="$STATE/participants-outbox/alice.age"
  head -c 21 "$f" | grep -q "^age-encryption.org/v1" && ! grep -qa "K3S_TOKEN\|NODE_PASSWORD\|NEBULA CERTIFICATE" "$f"; }
t_open() { open_as alice alice "$F/alice-bundle" || return 1
  local b="$F/alice-bundle"
  [ -f "$b/host.key" ] && [ "$(stat -c %a "$b/join.env")" = 600 ] && grep -qx "NODE_NAME=alice-pc" "$b/join.env" && grep -qx "TRUST=friends" "$b/join.env" &&
  grep -qx "TAINT=yes" "$b/join.env" && grep -q "^K3S_TOKEN=K10fakeca::" "$b/join.env" &&
  "$NC" print -json -path "$b/host.crt" | python3 -c '
import base64, json, sys
c = json.load(sys.stdin)[0]; d = c["details"]
mine = c["publicKey"] == base64.b64decode(open(sys.argv[1]).read().split("-----")[2].strip()).hex()
ok = d["name"] == "alice-pc" and d["groups"] == ["friends"] and mine and d["issuer"] == sys.argv[2]
print(d["name"], d["groups"], "her own key" if mine else "NOT her key", "persistent CA" if d["issuer"] == sys.argv[2] else "another CA")
sys.exit(0 if ok else 1)' \
    "$F/keys/alice.pub" "$(fp_of "$STATE/ca/ca.crt")"; }
t_not_for_bob() { fails_with "cannot decrypt" open_as alice bob "$F/bob-reads-alice"; }
t_forged() { # mallory packs a bundle of her own, signs it with HER cosign key and encrypts it to alice's public recipient
  mkdir -p "$F/forged" && for n in ca.crt config.yml host.crt join.env; do echo "forged $n" >"$F/forged/$n"; done
  COSIGN_PASSWORD="" PYTHONPATH="$POC" python3 -c 'import sys; from pathlib import Path; import participants as p
p.deliver(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7])' \
    "$F/forged" "$(recipient alice)" "$F/forged.age" "$F/mallory-cosign.key" "$COSIGN" "$AGE" "$F/signing-config-offline.json" || return 1
  fails_with "REFUSED: the bundle is not signed by the issuer key" $PY open "$F/forged.age" --identity "$F/keys/alice.age" \
    --issuer-pub "$F/issuer-TEST.pub" --out "$F/forged-out" --cosign "$COSIGN" --age "$AGE" --offline-test && [ ! -e "$F/forged-out/join.env" ]; }
state_sum() { (cd "$STATE" && cat participants/ledger.json participants-outbox/*.age bundles/*/host.crt | sha256sum); }
t_idempotent() { local a b o; a=$(state_sum); o=$(sync); echo "$o"; b=$(state_sum)
  [ "$a" = "$b" ] && echo "$o" | grep -qx "sync: issued 0, revoked 0, unchanged 2, problems 0"; }
t_reverify() { # a file pushed straight to main (no pull request, no CI) that does not verify: the issuer re-verifies and does not issue it
  g checkout -q main && pfile carol 1004 carol-pc && sign_as carol mallory 1003 && commit "carol, signed by mallory, pushed to main" || return 1
  local o rc=0; o=$(sync) || rc=$?; echo "$o"
  [ "$rc" = 1 ] && echo "$o" | grep -q "not valid: participants/carol.yaml: the signature does not verify for carol" &&
  [ ! -e "$STATE/participants-outbox/carol.age" ] && echo "$o" | grep -qx "unchanged alice (node alice-pc)" &&
  g rm -q participants/carol.yaml participants/carol.sigstore.json && commit "carol out"; }
t_update() { g checkout -q main && pfile bob 1002 bob-pc friends 4 && sign_as bob bob 1002 && commit "bob: 4 cpus" || return 1
  local o; o=$(sync); echo "$o"; echo "$o" | grep -qx "updated bob (node bob-pc): same certificate" && echo "$o" | grep -q "sync: issued 0, revoked 0"; }
t_removal() { # alice's file leaves main: one sync revokes her (blocklist on every host, node deleted, token gone, nothing left to deliver)
  local fp; fp=$(fp_of "$STATE/bundles/alice-pc/host.crt"); : >"$FAKE_LOG"
  g checkout -q main && g rm -q participants/alice.yaml participants/alice.sigstore.json && commit "alice leaves" || return 1
  local o; o=$(sync); echo "$o"
  echo "$o" | grep -qx "revoked alice (node alice-pc): its file is gone from main or does not pass any more" &&
  grep -qx "$fp" "$STATE/blocklist" && for h in lh cp bob-pc alice-pc; do grep -q "$fp" "$STATE/bundles/$h/config.yml" || return 1; done &&
  grep -q "kubectl delete node alice-pc" "$FAKE_LOG" && [ ! -e "$STATE/participants-outbox/alice.age" ] &&
  ! python3 -c 'import json,sys; sys.exit(0 if "alice" in json.load(open(sys.argv[1]))["participants"] else 1)' "$STATE/participants/ledger.json"; }
t_idempotent_after() { local o; o=$(sync); echo "$o"; echo "$o" | grep -qx "sync: issued 0, revoked 0, unchanged 1, problems 0"; }
t_revoke_fails_first() { # bob changes his key and node; the revoke of the old certificate fails (exit 4): nothing new is issued to him
  # until it is revoked, so the ledger keeps the certificate that is still valid (review finding 2); the next sync does both
  keys bob2 && g checkout -q main && pfile bob 1002 bob-pc2 friends 4 bob2 && sign_as bob bob 1002 && commit "bob: new key and node" || return 1
  local old o rc=0; old=$(fp_of "$STATE/bundles/bob-pc/host.crt")
  o=$(COSIGN_PASSWORD="" $PY sync "${SYNC_ARGS[@]}" --revoke-cmd "$F/bin/revoke-fails") || rc=$?; echo "$o"
  [ "$rc" = 1 ] && echo "$o" | grep -q "^revoke failed: bob (node bob-pc)" &&
  echo "$o" | grep -qx "not issued: bob: the old certificate (node bob-pc) is not revoked yet" && [ ! -e "$STATE/bundles/bob-pc2" ] &&
  python3 -c 'import json,sys; e = json.load(open(sys.argv[1]))["participants"]["bob"]; sys.exit(0 if e["node"] == "bob-pc" and e["fingerprint"] == sys.argv[2] else 1)'     "$STATE/participants/ledger.json" "$old" || return 1
  o=$(sync); echo "$o"
  echo "$o" | grep -q "^revoked bob (node bob-pc)" && echo "$o" | grep -q "^issued bob (node bob-pc2, " && grep -qx "$old" "$STATE/blocklist"; }
t_name_never_reused() { # carol, properly signed, asks for alice's old node name: CI cannot know it was used; the issuer refuses it
  g checkout -q main && pfile carol 1004 alice-pc && sign_as carol carol 1004 && commit "carol asks for alice-pc" || return 1
  local o rc=0; o=$(sync) || rc=$?; echo "$o"
  [ "$rc" = 1 ] && echo "$o" | grep -qx "not issued: carol: node name 'alice-pc' was used before" && [ ! -e "$STATE/participants-outbox/carol.age" ]; }
t_broken_policy() { # a policy that does not load: nothing is revoked, nothing issued
  local a; a=$(state_sum); g checkout -q main && echo "grants: [" >>"$R/participants/policy.yaml" && commit "broken policy" || return 1
  local o rc=0; o=$(sync) || rc=$?; echo "$o"
  [ "$rc" = 2 ] && echo "$o" | grep -q "^sync: nothing done" && [ "$a" = "$(state_sum)" ] && g revert --no-edit HEAD >/dev/null; }

step S00 "fixture: a LOCAL TEST CA marked TEST ONLY (no Fulcio, no Rekor), four participants' keys, main holds the policy and bob" fixture
step S01 "a valid signed pull request passes (signer = the file's login = the PR author, schema, unique node, allowed trust)" t_valid
step S02 "NEGATIVE: the same file signed by another identity (bob's fork) fails" t_other_identity
step S03 "NEGATIVE: signed from the right login but another account id (a re-registered login) fails" t_other_account
step S04 "NEGATIVE: alice's valid signed file replayed in a pull request by mallory fails" t_replay
step S05 "NEGATIVE: an unsigned file fails" t_unsigned
step S06 "NEGATIVE: a file changed after it was signed fails" t_tampered
step S07 "NEGATIVE: a signature by a CA that is not trusted fails" t_untrusted_ca
step S08 "NEGATIVE: a certificate from another OIDC issuer (a GitHub login, not Actions) fails" t_wrong_issuer
step S09 "NEGATIVE: a certificate of another workflow in the fork fails" t_other_workflow
step S09b "NEGATIVE: a signature made with a key instead of keyless fails" t_key_signed
step S10 "NEGATIVE: asking for trust own (above friends) fails without the maintainer" t_trust_above
step S11 "NEGATIVE: a grant added by the pull request itself does not count (the base policy decides)" t_grant_in_pr
step S12 "with the maintainer's grant merged first, the same request passes" t_granted
step S13 "NEGATIVE: a node name another participant has fails" t_unique
step S14 "NEGATIVE: a reserved node name and a duplicated YAML key fail" t_schema
step S15 "NEGATIVE: a join pull request that also changes code fails" t_mixed
step S16 "removal: the participant or the maintainer may remove a file, nobody else" t_removal_pr
step S17 "a pull request without participant files passes the check (it always reports)" t_nothing
step S18 "NEGATIVE: --test-root refuses a trusted root that is not marked TEST ONLY" t_interlock
step S19 "the maintainer merges: one sync issues alice and bob (their own Nebula keys, persistent CA) into a 0700 outbox" t_sync_issues
step S20 "the delivered file is age ciphertext: no token, no node password, no certificate in the clear" t_ciphertext
step S21 "alice opens it: the issuer's signature verifies, her certificate holds her own public key, friends, persistent CA, taint on" t_open
step S22 "NEGATIVE: bob's key cannot open alice's bundle" t_not_for_bob
step S23 "NEGATIVE: a bundle anybody encrypted to alice's public key, signed with another key, is refused" t_forged
step S24 "sync again: nothing changes (ledger, outbox and certificates byte for byte)" t_idempotent
step S25 "NEGATIVE: a file pushed to main without verifying is not issued (the issuer verifies again)" t_reverify
step S26 "a change of resources only keeps the certificate (updated, not issued again)" t_update
step S27 "removal revokes access: alice's file leaves main, one sync puts her certificate on every host's blocklist and deletes her node" t_removal
step S28 "sync again after the removal: nothing to do" t_idempotent_after
step S28b "NEGATIVE: a revoke that fails blocks the new certificate until the old one is revoked" t_revoke_fails_first
step S29 "NEGATIVE: a node name issued before is never issued again (carol asks for alice-pc)" t_name_never_reused
step S30 "NEGATIVE: a broken policy on main changes nothing (no mass revocation)" t_broken_policy

echo "----"
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
