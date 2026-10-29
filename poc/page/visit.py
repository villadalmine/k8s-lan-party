#!/usr/bin/env python3
"""The visitor's side of the public page (docs/PUBLIC-PAGE.md): make your own keys, ask for a place, wait, collect the sealed bundle
and open it with your own key. Python standard library, `openssl` and `nebula-cert` (which you need to join anyway). A person, a script
and an agent use it the same way; everything it does is also a plain HTTPS call (the curl examples in docs/PUBLIC-PAGE.md).

    visit.py keygen DIR                          host.key/host.pub (Nebula) and seal.key/seal.pub in DIR (0700); the private keys stay there
    visit.py request URL NAME --keys DIR         solve the proof of work, ask; the claim code goes to DIR/claim-code (0600) and the screen
    visit.py status URL --keys DIR [--code C]    where the request is: position, estimate, ready, ended
    visit.py wait URL --keys DIR [--code C]      ask every --interval seconds; when ready, collect (once), open and check the bundle,
                                                 write DIR/<name>/ with your own host.key, and print the join command
    visit.py pow CHALLENGE BITS                  just the proof of work (for the curl path)
    visit.py open DIR SEALED.json                open a sealed bundle collected with curl into DIR/<name>/

Options: --cacert FILE (a page with a private CA, as in the lab), --nebula-cert PATH, --interval S (20), --timeout S (7200).
Exit codes: 0 done; 1 the page or a tool failed; 2 bad input; 3 refused or nothing to collect; 4 still waiting when --timeout ran out.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seal as keys  # noqa: E402

BUNDLE_FILES = {"ca.crt", "host.crt", "config.yml", "join.env"}   # always there
OPTIONAL_FILES = {"kubeconfig"}                                      # the player's own kubeconfig (issue #9), when the host made one   # what a sealed bundle may hold; never a private key


class Stop(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def call(url: str, path: str, body: dict | None = None, cacert: str | None = None) -> tuple[int, dict]:
    ctx = ssl.create_default_context(cafile=cacert) if url.startswith("https://") else None
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "k8s-lan-party-visit/1"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return r.status, json.loads(r.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(1 << 16) or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Stop(1, f"the page did not answer: {e}") from None


def leading_zero_bits(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b:
            return n + 8 - b.bit_length()
        n += 8
    return n


def pow_solve(challenge: str, bits: int) -> str:
    if not 0 < bits <= 40:
        raise Stop(2, "a difficulty between 1 and 40 bits")
    base = hashlib.sha256(f"{challenge}:".encode())
    n = 0
    while True:
        h = base.copy()
        h.update(str(n).encode())
        if leading_zero_bits(h.digest()) >= bits:
            return str(n)
        n += 1


def private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def keygen(d: Path, nebula_cert: str) -> None:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    if not (d / "host.key").exists():
        subprocess.run([nebula_cert, "keygen", "-out-key", str(d / "host.key"), "-out-pub", str(d / "host.pub")], check=True)
        os.chmod(d / "host.key", 0o600)
    if not (d / "seal.key").exists():
        old = os.umask(0o077)
        try:
            subprocess.run(["openssl", "genpkey", "-algorithm", "X25519", "-out", str(d / "seal.key")], check=True)
        finally:
            os.umask(old)
        subprocess.run(["openssl", "pkey", "-in", str(d / "seal.key"), "-pubout", "-out", str(d / "seal.pub")], check=True)
    keys.nebula_pub((d / "host.pub").read_text())   # what the page will check, checked here first
    keys.seal_pub((d / "seal.pub").read_text())
    print(f"keys: {d} (host.key and seal.key never leave this machine; host.pub and seal.pub go to the page)")


def request(url: str, name: str, d: Path, cacert: str | None) -> str:
    status, c = call(url, "/api/v1/challenge", cacert=cacert)
    if status != 200:
        raise Stop(1, f"no challenge: {c.get('message', status)}")
    t0 = time.time()
    nonce = pow_solve(c["challenge"], int(c["difficulty"]))
    body = {"name": name, "nebula_public_key": (d / "host.pub").read_text(), "seal_public_key": (d / "seal.pub").read_text(),
            "challenge": c["challenge"], "nonce": nonce}
    status, r = call(url, "/api/v1/requests", body, cacert)
    if status != 201:
        raise Stop(3 if status in (403, 409, 429, 503) else 1, f"not taken ({status} {r.get('error', '')}): {r.get('message', '')}")
    private_file(d / "claim-code", (r["claim_code"] + "\n").encode())
    (d / "name").write_text(name + "\n")
    print(f"asked as {name} (proof of work: {time.time() - t0:.1f} s). Claim code: {r['claim_code']} (saved in {d / 'claim-code'})")
    return r["claim_code"]


def code_of(d: Path, code: str | None) -> str:
    if code:
        return code
    try:
        return (d / "claim-code").read_text().strip()
    except OSError:
        raise Stop(2, f"no claim code: give --code or ask first (visit.py request), which saves it in {d / 'claim-code'}") from None


def open_bundle(d: Path, env: dict, nebula_cert: str) -> Path:
    """Open the envelope with seal.key, check the tar (only the expected files, no private key, nothing outside <name>/), check that
    the certificate is for OUR Nebula key, then write <d>/<name>/ with our own host.key next to it."""
    try:
        data = keys.unseal(d / "seal.key", env)
    except (ValueError, RuntimeError) as e:
        raise Stop(1, f"the bundle does not open with your seal key: {e}") from None
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        top = {m.name.split("/", 1)[0] for m in members}
        if len(top) != 1:
            raise Stop(1, "the bundle has more than one directory")
        name = top.pop()
        if not keys_name_ok(name):
            raise Stop(1, "the bundle's directory is not a valid name")
        for m in members:
            if m.name == name and m.isdir():
                continue
            base = m.name[len(name) + 1:] if m.name.startswith(name + "/") else ""
            if not m.isreg() or base not in BUNDLE_FILES | OPTIONAL_FILES:
                raise Stop(1, f"unexpected entry in the bundle: {m.name[:60]!r}")
            files[base] = tar.extractfile(m).read()
    if not BUNDLE_FILES <= set(files):
        raise Stop(1, "the bundle is missing a file")
    if any(b"PRIVATE KEY" in v for v in files.values()):
        raise Stop(1, "the bundle holds a private key: it should not; do not use it")
    out = d / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(mode=0o700)
    for f, v in files.items():
        private_file(out / f, v)
    shutil.copyfile(d / "host.key", out / "host.key")
    os.chmod(out / "host.key", 0o600)
    mine = keys.nebula_pub((d / "host.pub").read_text()).hex()
    r = subprocess.run([nebula_cert, "print", "-json", "-path", str(out / "host.crt")], capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
        info = info[0] if isinstance(info, list) else info
        theirs = (info.get("details", {}).get("publicKey") or info.get("publicKey") or "").lower()
    except ValueError:
        theirs = ""
    if theirs != mine:
        shutil.rmtree(out)
        raise Stop(1, "the certificate in the bundle is not for your Nebula key: do not use it")
    return out


def keys_name_ok(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9-]{1,19}", name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ask the public page for a place (docs/PUBLIC-PAGE.md)")
    ap.add_argument("cmd", choices=["keygen", "request", "status", "wait", "pow", "open"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--keys", default="./mykeys")
    ap.add_argument("--code")
    ap.add_argument("--cacert")
    ap.add_argument("--nebula-cert", default=shutil.which("nebula-cert") or "/opt/nebula/nebula-cert")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=7200)
    a = ap.parse_args(argv)
    d = Path(a.keys)
    try:
        if a.cmd == "keygen":
            keygen(Path(a.args[0]) if a.args else d, a.nebula_cert)
        elif a.cmd == "pow":
            if len(a.args) != 2 or not a.args[1].isdigit():
                raise Stop(2, "pow CHALLENGE BITS")
            print(pow_solve(a.args[0], int(a.args[1])))
        elif a.cmd == "open":
            if len(a.args) != 2:
                raise Stop(2, "open DIR SEALED.json")
            d = Path(a.args[0])
            out = open_bundle(d, json.loads(Path(a.args[1]).read_text()), a.nebula_cert)
            print(f"bundle: {out}\njoin: sudo edge-mesh join {out} --accept-golden-rule")
        elif a.cmd == "request":
            if len(a.args) != 2:
                raise Stop(2, "request URL NAME --keys DIR")
            request(a.args[0], a.args[1], d, a.cacert)
        else:
            if len(a.args) != 1:
                raise Stop(2, f"{a.cmd} URL --keys DIR")
            url, code = a.args[0], code_of(d, a.code)
            deadline = time.time() + a.timeout
            last = ""
            while True:
                status, r = call(url, "/api/v1/requests/status", {"claim_code": code}, a.cacert)
                if status != 200:
                    raise Stop(3 if status in (404, 410, 429) else 1, f"{status} {r.get('error', '')}: {r.get('message', '')}")
                line = f"{r['name']}: {r['message']}"
                if line != last:
                    print(line, flush=True)
                    last = line
                if a.cmd == "status":
                    return 0
                if r["state"] == "ready":
                    status, c = call(url, "/api/v1/requests/claim", {"claim_code": code}, a.cacert)
                    if status != 200:
                        raise Stop(3, f"could not collect ({status}): {c.get('message', '')}")
                    out = open_bundle(d, c["sealed"], a.nebula_cert)
                    print(f"bundle: {out} (lease until {time.strftime('%H:%M:%S UTC', time.gmtime(c['lease_end']))})")
                    print(f"join: sudo edge-mesh join {out} --accept-golden-rule", flush=True)
                    return 0
                if r["state"] not in ("received", "waiting", "deferred"):
                    raise Stop(3, f"nothing to wait for: {r['message']}")
                if time.time() > deadline:
                    raise Stop(4, "still waiting; run wait again")
                time.sleep(max(5, a.interval))
    except Stop as e:
        print(e, file=sys.stderr)
        return e.code
    except (keys.KeyRefused, OSError, subprocess.CalledProcessError) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
