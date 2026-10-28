#!/usr/bin/env python3
"""A LOCAL TEST CA that plays Fulcio for the PoC fixture of join by signed PR (make poc-participants). TEST ONLY.

The real check verifies against the public Sigstore (Fulcio certificates for GitHub Actions, the Rekor log): that needs the network and a
real workflow run, so the offline fixture cannot use it. This script makes, in a throw-away directory, a CA and certificates shaped like
Fulcio's (the SAN URI of the signing workflow, the OIDC issuer and the repository owner's account id as Fulcio extensions) and Sigstore
bundles v0.3 signed with them. `participants.py --test-root` accepts the trusted root it writes only because every authority in it is
marked "TEST ONLY" (participants.TEST_ORG); nothing it makes is ever committed (keys live in the temporary directory and go with it).

  sigstore_test_ca.py init <dir>                                     the TEST CA and <dir>/trusted_root.json
  sigstore_test_ca.py sign <dir> <blob> <bundle-out> --login L --owner-id N [--fork F] [--issuer URL] [--workflow PATH] [--ref REF]
                                                                     a bundle as if L's signing workflow had signed <blob>
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

sys.path.insert(0, str(Path(__file__).resolve().parent))
from participants import BUNDLE_V03, OIDC_ISSUER, SIGN_WORKFLOW, TEST_ORG  # noqa: E402

F = "1.3.6.1.4.1.57264.1."


def utf8(s: str) -> bytes:
    b = s.encode()
    if len(b) > 127:
        raise SystemExit("value too long for this test encoder")
    return bytes([0x0C, len(b)]) + b


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def der(c: x509.Certificate) -> bytes:
    return c.public_bytes(serialization.Encoding.DER)


def init(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, TEST_ORG), x509.NameAttribute(NameOID.COMMON_NAME, "TEST Fulcio")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
          .sign(key, hashes.SHA256()))
    (d / "ca.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (d / "ca.key").chmod(0o600)
    (d / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    root = {"mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1", "tlogs": [], "ctlogs": [], "timestampAuthorities": [],
            "certificateAuthorities": [{"subject": {"organization": TEST_ORG, "commonName": "TEST Fulcio"}, "uri": "https://fulcio.test.invalid",
                                        "certChain": {"certificates": [{"rawBytes": b64(der(ca))}]},
                                        "validFor": {"start": (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}}]}
    (d / "trusted_root.json").write_text(json.dumps(root, indent=1) + "\n")
    print(f"TEST CA in {d} (trusted_root.json marked {TEST_ORG!r})")


def sign(d: Path, blob: Path, out: Path, login: str, owner_id: str, fork: str, issuer: str, workflow: str, ref: str) -> None:
    ca_key = serialization.load_pem_private_key((d / "ca.key").read_bytes(), None)
    ca = x509.load_pem_x509_certificate((d / "ca.crt").read_bytes())
    now = dt.datetime.now(dt.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    ident = f"https://github.com/{login}/{fork}/{workflow}@{ref}"
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(minutes=10))
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(ident)]), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.UnrecognizedExtension(ObjectIdentifier(F + "1"), issuer.encode()), critical=False)
            .add_extension(x509.UnrecognizedExtension(ObjectIdentifier(F + "8"), utf8(issuer)), critical=False)
            .add_extension(x509.UnrecognizedExtension(ObjectIdentifier(F + "16"), utf8(f"https://github.com/{login}")), critical=False)
            .add_extension(x509.UnrecognizedExtension(ObjectIdentifier(F + "17"), utf8(owner_id)), critical=False)
            .sign(ca_key, hashes.SHA256()))
    data = blob.read_bytes()
    bundle = {"mediaType": BUNDLE_V03,
              "verificationMaterial": {"certificate": {"rawBytes": b64(der(leaf))}, "tlogEntries": [], "timestampVerificationData": {}},
              "messageSignature": {"messageDigest": {"algorithm": "SHA2_256", "digest": b64(hashlib.sha256(data).digest())},
                                   "signature": b64(key.sign(data, ec.ECDSA(hashes.SHA256())))}}
    out.write_text(json.dumps(bundle) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="a LOCAL TEST CA playing Fulcio (PoC fixture only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("dir")
    s = sub.add_parser("sign")
    s.add_argument("dir")
    s.add_argument("blob")
    s.add_argument("out")
    s.add_argument("--login", required=True)
    s.add_argument("--owner-id", required=True)
    s.add_argument("--fork", default="k8s-lan-party")
    s.add_argument("--issuer", default=OIDC_ISSUER)
    s.add_argument("--workflow", default=SIGN_WORKFLOW)
    s.add_argument("--ref", default="refs/heads/join")
    a = ap.parse_args()
    if a.cmd == "init":
        init(Path(a.dir))
    else:
        sign(Path(a.dir), Path(a.blob), Path(a.out), a.login, a.owner_id, a.fork, a.issuer, a.workflow, a.ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
