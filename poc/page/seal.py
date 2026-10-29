#!/usr/bin/env python3
"""The keys of the public page (docs/ADMISSION.md B2) and the envelope the bundle travels in.

Two public keys come from the visitor, both made on the visitor's own machine; the private halves never leave it:
- the **Nebula key** (`nebula-cert keygen`): the control plane signs it into the node certificate (`issue.sh` with IN_PUB), so the
  bundle that travels back has no private key in it;
- the **seal key** (`openssl genpkey -algorithm X25519`): the control plane encrypts the bundle to it, so the public server only ever
  stores an opaque blob. The bundle carries the join token and the node password; the server never sees them in clear.

Both are checked strictly before anything else looks at them (`nebula_pub`, `seal_pub`): one PEM block of the one expected type, strict
base64, exactly 32 bytes of key, canonical, not a small-order point (an all-zero shared secret), nothing before or after the block.
A certificate, a CA, an Ed25519 or P-256 key, two blocks, trailing text or a key of another length are refused.

The envelope (`seal` / `unseal`): X25519 between a one-time key and the visitor's seal key (openssl, from key files: no secret on a
command line), HKDF-SHA256 (RFC 5869, here with hmac) with both public keys as the salt, then AES-256-CTR (openssl enc, the key on its
stdin) and HMAC-SHA256 over everything (encrypt-then-MAC, checked in constant time before anything is decrypted). Version 1:

    {"v": 1, "alg": "x25519-hkdf-sha256-aes256ctr-hmac-sha256", "epk": b64(32), "ct": b64, "mac": b64(32)}

Needs `openssl` (1.1.1 or newer) for X25519 and AES; everything else is the Python standard library.

    seal.py check-nebula <file> | check-seal <file>       exit 0 when the key passes, 2 with the reason
    seal.py seal <seal.pub> <in> <out.json>                 (tests and the curl path)
    seal.py unseal <seal.key> <in.json> <out>
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

P = 2 ** 255 - 19
# The u-coordinates of the X25519 points of small order (RFC 7748 section 7; libsodium's list): a key equal to one of them, in any
# encoding, makes the shared secret predictable. Non-canonical encodings (u >= p, or the top bit set) are refused before this.
_SMALL = {0, 1, P - 1,
          int.from_bytes(bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"), "little"),
          int.from_bytes(bytes.fromhex("5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157"), "little")}
SPKI_X25519 = bytes.fromhex("302a300506032b656e032100")   # SubjectPublicKeyInfo, id-X25519, then the 32 key bytes
ALG = "x25519-hkdf-sha256-aes256ctr-hmac-sha256"
INFO = b"k8s-lan-party/b2/seal/v1"
MAX_PEM = 400          # characters of one public key as it may arrive (a real one is about 120)
MAX_SEALED = 65536     # bytes of ciphertext: a bundle is about 3 KB


class KeyRefused(ValueError):
    """A public key that does not pass; the message says why and never repeats the input."""


def _pem_block(text: str, label: str) -> bytes:
    if not isinstance(text, str) or len(text) > MAX_PEM:
        raise KeyRefused(f"not a {label} (too long or not text)")
    t = text.replace("\r\n", "\n").strip()
    m = re.fullmatch(rf"-----BEGIN {label}-----\n([A-Za-z0-9+/=\n]+)\n-----END {label}-----", t)
    if not m:
        raise KeyRefused(f"not exactly one '{label}' PEM block")
    try:
        return base64.b64decode(m.group(1).replace("\n", ""), validate=True)
    except (binascii.Error, ValueError):
        raise KeyRefused(f"the {label} is not valid base64") from None


def _x25519_ok(raw: bytes, what: str) -> bytes:
    if len(raw) != 32:
        raise KeyRefused(f"the {what} is not 32 bytes")
    u = int.from_bytes(raw, "little")
    if u >> 255 or u >= P:
        raise KeyRefused(f"the {what} is not a canonical X25519 key")
    if u in _SMALL:
        raise KeyRefused(f"the {what} is a weak (small-order) X25519 key")
    return raw


def nebula_pub(text: str) -> bytes:
    """The 32 bytes of a Nebula X25519 public key (`nebula-cert keygen`), or KeyRefused. Only this type: a CA or a certificate (they
    are other PEM types), an Ed25519 or P-256 key is refused."""
    return _x25519_ok(_pem_block(text, "NEBULA X25519 PUBLIC KEY"), "Nebula public key")


def seal_pub(text: str) -> bytes:
    """The 32 bytes of an X25519 public key in SubjectPublicKeyInfo PEM (`openssl pkey -pubout`), or KeyRefused."""
    der = _pem_block(text, "PUBLIC KEY")
    if len(der) != len(SPKI_X25519) + 32 or not der.startswith(SPKI_X25519):
        raise KeyRefused("the seal key is not an X25519 public key")
    return _x25519_ok(der[len(SPKI_X25519):], "seal public key")


def nebula_pem(raw: bytes) -> str:
    """The canonical PEM of a checked Nebula public key (what is written for nebula-cert: rebuilt from the bytes, never copied)."""
    return "-----BEGIN NEBULA X25519 PUBLIC KEY-----\n" + base64.b64encode(_x25519_ok(raw, "key")).decode() + \
        "\n-----END NEBULA X25519 PUBLIC KEY-----\n"


def seal_pem(raw: bytes) -> str:
    return "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(SPKI_X25519 + _x25519_ok(raw, "key")).decode() + \
        "\n-----END PUBLIC KEY-----\n"


# --- the envelope ------------------------------------------------------------------------------------------------------

def hkdf(ikm: bytes, salt: bytes, info: bytes, n: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out, i = out + t, i + 1
    return out[:n]


def _openssl(args: list[str], stdin: bytes | None = None, openssl: str = "openssl") -> bytes:
    r = subprocess.run([openssl, *args], input=stdin, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def _derive(d: Path, own_key: Path, peer_raw: bytes, openssl: str) -> bytes:
    peer = d / "peer.pem"
    peer.write_text(seal_pem(peer_raw))
    _openssl(["pkeyutl", "-derive", "-inkey", str(own_key), "-peerkey", str(peer), "-out", str(d / "z")], openssl=openssl)
    z = (d / "z").read_bytes()
    if len(z) != 32 or not any(z):
        raise RuntimeError("X25519 gave no usable shared secret")
    return z


def _aes_ctr(d: Path, key: bytes, data: bytes, decrypt: bool, openssl: str) -> bytes:
    # the key goes in on stdin as a one-line pass phrase (256 random bits: PBKDF2 needs no iterations); never on the command line
    (d / "in").write_bytes(data)
    args = ["enc", "-aes-256-ctr", "-pbkdf2", "-iter", "1", "-md", "sha256", "-pass", "stdin", "-in", str(d / "in"), "-out", str(d / "out")]
    _openssl(args + (["-d"] if decrypt else []), stdin=key.hex().encode() + b"\n", openssl=openssl)
    return (d / "out").read_bytes()


def _keys(z: bytes, epk: bytes, rpk: bytes) -> tuple[bytes, bytes]:
    k = hkdf(z, epk + rpk, INFO, 64)
    return k[:32], k[32:]


def _mac(kmac: bytes, epk: bytes, rpk: bytes, ct: bytes) -> bytes:
    return hmac.new(kmac, b"v1" + ALG.encode() + epk + rpk + ct, hashlib.sha256).digest()


def seal(recipient_raw: bytes, plaintext: bytes, openssl: str = "openssl") -> dict:
    rpk = _x25519_ok(recipient_raw, "seal public key")
    with tempfile.TemporaryDirectory(prefix="seal-") as t:   # 0700, removed at the end
        d = Path(t)
        _openssl(["genpkey", "-algorithm", "X25519", "-out", str(d / "e.key")], openssl=openssl)
        der = _openssl(["pkey", "-in", str(d / "e.key"), "-pubout", "-outform", "DER"], openssl=openssl)
        if not der.startswith(SPKI_X25519) or len(der) != 44:
            raise RuntimeError("openssl gave an unexpected X25519 public key")
        epk = der[12:]
        kenc, kmac = _keys(_derive(d, d / "e.key", rpk, openssl), epk, rpk)
        ct = _aes_ctr(d, kenc, plaintext, False, openssl)
    return {"v": 1, "alg": ALG, "epk": base64.b64encode(epk).decode(), "ct": base64.b64encode(ct).decode(),
            "mac": base64.b64encode(_mac(kmac, epk, rpk, ct)).decode()}


def envelope_ok(env) -> bool:
    """The shape of an envelope (what the page checks before storing one; it cannot check more: it has no key)."""
    if not isinstance(env, dict) or set(env) != {"v", "alg", "epk", "ct", "mac"} or env["v"] != 1 or env["alg"] != ALG:
        return False
    try:
        epk = base64.b64decode(env["epk"], validate=True)
        mac = base64.b64decode(env["mac"], validate=True)
        ct = base64.b64decode(env["ct"], validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    return len(epk) == 32 and len(mac) == 32 and 16 <= len(ct) <= MAX_SEALED


def unseal(seal_key: str | Path, env: dict, openssl: str = "openssl") -> bytes:
    """The plaintext, or ValueError when the envelope was changed, is for another key, or is not an envelope."""
    if not envelope_ok(env):
        raise ValueError("not a sealed bundle (version 1)")
    epk, ct, mac = (base64.b64decode(env[k]) for k in ("epk", "ct", "mac"))
    _x25519_ok(epk, "one-time key")
    with tempfile.TemporaryDirectory(prefix="unseal-") as t:
        d = Path(t)
        der = _openssl(["pkey", "-in", str(seal_key), "-pubout", "-outform", "DER"], openssl=openssl)
        if not der.startswith(SPKI_X25519) or len(der) != 44:
            raise ValueError("the seal key is not an X25519 private key")
        rpk = der[12:]
        kenc, kmac = _keys(_derive(d, Path(seal_key), epk, openssl), epk, rpk)
        if not hmac.compare_digest(_mac(kmac, epk, rpk, ct), mac):
            raise ValueError("the sealed bundle was changed or is for another key")
        return _aes_ctr(d, kenc, ct, True, openssl)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] in ("check-nebula", "check-seal"):
        try:
            (nebula_pub if argv[0] == "check-nebula" else seal_pub)(Path(argv[1]).read_text())
        except (KeyRefused, OSError, UnicodeDecodeError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        print("ok")
        return 0
    if len(argv) == 4 and argv[0] == "seal":
        env = seal(seal_pub(Path(argv[1]).read_text()), Path(argv[2]).read_bytes())
        Path(argv[3]).write_text(json.dumps(env))
        return 0
    if len(argv) == 4 and argv[0] == "unseal":
        data = unseal(argv[1], json.loads(Path(argv[2]).read_text()))
        fd = os.open(argv[3], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return 0
    print(__doc__.split("\n\n")[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
