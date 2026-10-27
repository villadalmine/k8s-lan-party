#!/usr/bin/env python3
"""A k3s node password made by the host, so that a Node name belongs to one bundle.

k3s keeps, for every node name, the hash of the password the agent presented the first time
(secret kube-system/<name>.node-password.k3s, type k3s.cattle.io/node-password; the agent keeps the
password in /etc/rancher/node/password). Any later registration or certificate request under that
name must present the same password. The host creates that secret when it issues a bundle, with a
random password that only the bundle carries (NODE_PASSWORD in join.env; `edge-mesh join` writes it
to /etc/rancher/node/password). Another visitor with a bundle of his own and a valid join token
cannot then register under this name, even at this bundle's Nebula IP.

The hash is the format of k3s pkg/authenticator/hash/scrypt.go: "$1:<salt hex>:15:8:1:<key base64,
no padding>", scrypt N=2^15, r=8, p=1, a 64-byte key, an 8-byte salt.

    printf '%s' "$password" | node_password.py secret <node-name>    # the Secret manifest (JSON), for kubectl create -f -
    node_password.py new                                             # a random password
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys

N_LOG2, R, P, KEY_LEN, SALT_LEN = 15, 8, 1, 64, 8


def _key(password: str, salt: bytes, n_log2: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=1 << n_log2, r=r, p=p, dklen=length, maxmem=1 << 27)


def create_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(SALT_LEN)
    enc = base64.b64encode(_key(password, salt, N_LOG2, R, P, KEY_LEN)).decode().rstrip("=")
    return f"$1:{salt.hex()}:{N_LOG2}:{R}:{P}:{enc}"


def verify(hashed: str, password: str) -> bool:
    """The check k3s does (VerifyHash), for the tests."""
    version, salt, n_log2, r, p, enc = hashed.lstrip("$").split(":")
    if version != "1":
        return False
    dk = base64.b64decode(enc + "=" * (-len(enc) % 4))
    return hmac.compare_digest(dk, _key(password, bytes.fromhex(salt), int(n_log2), int(r), int(p), len(dk)))


def secret(node: str, password: str) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "type": "k3s.cattle.io/node-password", "immutable": True,
            "metadata": {"name": f"{node.lower()}.node-password.k3s", "namespace": "kube-system"},
            "data": {"hash": base64.b64encode(create_hash(password).encode()).decode()}}


def main(argv: list[str]) -> int:
    if argv[:1] == ["new"]:
        print(secrets.token_hex(32))
        return 0
    if len(argv) == 2 and argv[0] == "secret":
        password = sys.stdin.read().strip()
        if not password:
            print("no password on stdin", file=sys.stderr)
            return 2
        print(json.dumps(secret(argv[1], password), sort_keys=True))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
