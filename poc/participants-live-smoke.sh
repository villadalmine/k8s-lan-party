#!/usr/bin/env bash
# CI only (.github/workflows/participants.yml, job participants-live): the LIVE path of the participants check, against the public
# Sigstore, without a second GitHub account. The job signs a throw-away blob with cosign keyless (this run's OIDC token -> a real Fulcio
# certificate naming this workflow, a real Rekor entry), then runs the same verification code the check runs on a participant's file
# (participants.Verifier.verify_identity: cosign verify-blob with no test root and no insecure flag, the bundle v0.3 shape, the OIDC
# issuer and the owner account id read from the real certificate), with this workflow's identity in place of a fork's sign.yml.
# What it does not cover: sign.yml itself in a participant's fork (that needs another account; issue #12 keeps it open).
# Output: one PASS/FAIL per step; exit 1 on any FAIL. Needs: cosign on PATH, python3 with cryptography, id-token: write.
set -u
: "${GITHUB_WORKFLOW_REF:?run it in GitHub Actions}" "${GITHUB_REPOSITORY_OWNER_ID:?run it in GitHub Actions}" "${ACTIONS_ID_TOKEN_REQUEST_URL:?the job needs id-token: write}"
POC="$(cd "$(dirname "$0")" && pwd)"
d=$(mktemp -d)
printf 'k8s-lan-party participants live smoke, run %s\n' "${GITHUB_RUN_ID:-?}" >"$d/blob"
cosign sign-blob --yes --bundle "$d/bundle.json" "$d/blob" >"$d/sign.log" 2>&1 || { cat "$d/sign.log"; echo "FAIL L00 cosign sign-blob keyless"; exit 1; }
echo "PASS L00 cosign sign-blob keyless: a real Fulcio certificate and Rekor entry for this run"
PYTHONPATH="$POC" python3 - "$d" "$GITHUB_WORKFLOW_REF" "$GITHUB_REPOSITORY_OWNER_ID" <<'PY'
import re, sys
from pathlib import Path
import participants as P

d, ref, owner = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
blob, bundle = (d / "blob").read_bytes(), (d / "bundle.json").read_bytes()
rx = "^" + re.escape("https://github.com/" + ref) + "$"          # this workflow, exactly (the check builds the sign.yml one the same way)
v = P.Verifier("unused", cosign="cosign")                         # no --test-root: the public Sigstore, tlog and SCT verified
fails = 0


def step(sid, desc, problems, want_ok, want=""):
    global fails
    ok = (not problems) if want_ok else (bool(problems) and all(want in p for p in problems[:1]))
    print(f"{'PASS' if ok else 'FAIL'} {sid} {desc}" + ("" if ok else f": {problems}"))
    fails += 0 if ok else 1


step("L01", "the real bundle verifies: identity, issuer, owner account id", v.verify_identity(rx, "this workflow", owner, blob, bundle, "smoke", "bundle"), True)
step("L02", "NEGATIVE: another account id fails", v.verify_identity(rx, "this workflow", owner + 1, blob, bundle, "smoke", "bundle"), False,
     "signed from the GitHub account with id")
step("L03", "NEGATIVE: a changed blob fails", v.verify_identity(rx, "this workflow", owner, blob + b"x", bundle, "smoke", "bundle"), False,
     "does not verify")
other = "^" + re.escape("https://github.com/" + ref.replace("participants.yml", "sign.yml")) + "$"
step("L04", "NEGATIVE: another identity (sign.yml) fails", v.verify_identity(other, "sign.yml", owner, blob, bundle, "smoke", "bundle"), False,
     "does not verify")
sys.exit(1 if fails else 0)
PY
