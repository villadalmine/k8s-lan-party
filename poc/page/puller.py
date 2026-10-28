#!/usr/bin/env python3
"""The puller: the control plane's side of the public page (docs/ADMISSION.md B2, docs/PUBLIC-PAGE.md). It runs on the host, next to
`mesh` and the CA, from a timer (every minute, after `mesh host auto --once`), and only ever connects **out** to the page.

One run, in order:
1. `mesh host admission-status --json`. With panic on: tell the page it is closed and stop (nothing is pulled).
2. Read the new requests from the page and **trust none of it**: the name, the address and both keys are checked again here (the page
   may be compromised), a name that exists anywhere (a bundle, a public key, a request) is refused, a key already used by any bundle or
   request is refused, and at most --max-per-run requests a run, --max-per-hour an hour and --max-waiting in the list are taken. A good
   one becomes `mesh host request <name> --ip <ip> --note "public page"` (an argument list: no shell) and its Nebula public key, rebuilt
   from its 32 bytes, goes to admission/pubkeys/<name>.pub, where `mesh host invite` signs it (the bundle then has no private key).
3. For every request it took: the position and an estimate while it waits; once the automatic tick approved it, the outbox bundle,
   checked (only the expected files, no private key, the certificate is for the visitor's key), **sealed to the visitor's seal key**,
   so the page holds an opaque blob; and the end (lease over, rejected).
4. Report all that, with the public numbers (no names, no addresses), in one call.

The page can at worst make this puller add requests to the list, inside the caps above and the pool's own limits; it can never make the
host sign anything: the tick approves within the slots, and a bundle only ever signs a key that came through these checks.

    puller.py --url https://page.example.org --token-file FILE [--cacert FILE] [--mesh poc/mesh] [--state poc/.state] run

Exit codes: 0 done (also when closed); 1 the page or mesh failed (the next run tries again; nothing is lost); 2 bad arguments.
"""
from __future__ import annotations

import argparse
import io
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from admission import NAME, RESERVED, write_atomic  # noqa: E402
import seal as keys  # noqa: E402

BUNDLE_FILES = {"ca.crt", "host.crt", "config.yml", "join.env"}
REFUSE = {2: "invalid request", 3: "that name is already used: ask again with another name",
          6: "not eligible now (the daily cap per address)", 7: "admission is closed"}


class Fail(Exception):
    pass


class Puller:
    def __init__(self, a):
        self.a = a
        self.state = Path(a.state)
        self.dir = self.state / "page"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pubkeys = self.state / "admission" / "pubkeys"
        self.now = a.now if a.now is not None else int(time.time())
        self.mine = self._load()

    # --- local files ----------------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        p = self.dir / "puller.json"
        if not p.exists():
            return {"intakes": {}, "accepted": []}
        try:
            d = json.loads(p.read_text())
            assert isinstance(d.get("intakes"), dict) and isinstance(d.get("accepted"), list)
            return d
        except (ValueError, AssertionError):
            raise Fail(f"{p} is unreadable: restore it from the backup (nothing was changed)") from None

    def _save(self) -> None:
        self.mine["accepted"] = [t for t in self.mine["accepted"] if self.now - t < 3600]
        write_atomic(self.dir / "puller.json", self.mine)

    # --- the page -------------------------------------------------------------------------------------------------------
    def call(self, path: str, body: dict | None = None) -> dict:
        token = Path(self.a.token_file).read_text().strip()
        ctx = ssl.create_default_context(cafile=self.a.cacert) if self.a.url.startswith("https://") else None
        req = urllib.request.Request(self.a.url.rstrip("/") + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "User-Agent": "k8s-lan-party-puller/1"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                data = r.read(4 << 20)
        except urllib.error.HTTPError as e:
            raise Fail(f"the page answered {e.code} on {path}") from None
        except (urllib.error.URLError, OSError) as e:
            raise Fail(f"the page did not answer on {path}: {e}") from None
        try:
            d = json.loads(data)
        except ValueError:
            raise Fail(f"the page sent no JSON on {path}") from None
        if not isinstance(d, dict):
            raise Fail(f"the page sent no JSON object on {path}")
        return d

    # --- mesh -----------------------------------------------------------------------------------------------------------
    def mesh(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run([self.a.mesh, "host", *args], capture_output=True, text=True, timeout=timeout)

    def status(self) -> dict:
        r = self.mesh("admission-status", "--json")
        try:
            d = json.loads(r.stdout)
            assert r.returncode == 0 and isinstance(d, dict)
            return d
        except (ValueError, AssertionError):
            raise Fail(f"mesh host admission-status --json failed (exit {r.returncode}): {r.stderr.strip()[:200]}") from None

    def cert_pub(self, crt: Path) -> str:
        r = subprocess.run([self.a.nebula_cert, "print", "-json", "-path", str(crt)], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            d = d[0] if isinstance(d, list) else d
            return (d.get("details", {}).get("publicKey") or d.get("publicKey") or "").lower()
        except (ValueError, AttributeError, IndexError):
            return ""

    def used_keys(self) -> set[str]:
        """The Nebula public keys (hex) of every certificate the host issued (players, the control plane, the lighthouse) and of every
        request this puller took: a visitor may not bring any of them."""
        out = {self.cert_pub(c) for c in (self.state / "bundles").glob("*/host.crt")}
        for f in self.pubkeys.glob("*.pub") if self.pubkeys.is_dir() else ():
            try:
                out.add(keys.nebula_pub(f.read_text()).hex())
            except (keys.KeyRefused, OSError):
                pass
        return out - {""}

    # --- one run --------------------------------------------------------------------------------------------------------
    @staticmethod
    def numbers(st: dict) -> dict:
        return {"slots_total": st["slots_total"], "slots_used": st["slots_used"], "waiting": st["waiting"],
                "eligible": st["eligible"], "lease_seconds": st["lease_seconds"], "next_ends": sorted(l["end"] for l in st["leases"])}

    def eta(self, st: dict, r: dict) -> int | None:
        """position / slots * lease, corrected by the real end times of the leases (docs/ADMISSION.md B2)."""
        total = st["slots_total"]
        if total <= 0:
            return None
        free = total - st["slots_used"]
        p = r["position"]
        if p <= free:
            t = self.now + 60   # the next tick
        else:
            k = p - free - 1
            ends = sorted(l["end"] for l in st["leases"]) or [self.now]
            t = ends[k % len(ends)] + st["lease_seconds"] * (k // len(ends)) + 60
        return max(t, r.get("eligible_at") or 0)

    def check(self, it, st: dict, used: set[str]) -> tuple[str, str, bytes, bytes]:
        """An intake as the page sent it -> (name, ip, nebula key, seal key), or ValueError with the reason the visitor will read."""
        if not isinstance(it, dict):
            raise ValueError("invalid request")
        name, ip = it.get("name"), it.get("ip")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
            raise ValueError("invalid name")
        try:
            ip = str(ipaddress.ip_address(ip))
        except (ValueError, TypeError):
            raise ValueError("invalid address") from None
        try:
            neb = keys.nebula_pub(it.get("nebula_public_key"))
            sk = keys.seal_pub(it.get("seal_public_key"))
        except keys.KeyRefused as e:
            raise ValueError(str(e)) from None
        if neb == sk:
            raise ValueError("the two keys must be different keys")
        if (self.state / "bundles" / name).exists() or (self.pubkeys / f"{name}.pub").exists() or \
                any(r["name"] == name for r in st["requests"]):
            raise ValueError("that name is already used: ask again with another name")
        if neb.hex() in used:
            raise ValueError("that Nebula key is already used: make your own keys")
        return name, ip, neb, sk

    def take(self, st: dict) -> tuple[list, int, int]:
        """Pull and take new requests; returns (updates, listed, refused)."""
        got = self.call("/api/v1/puller/intake").get("intakes")
        if not isinstance(got, list):
            raise Fail("the page sent no list of intakes")
        updates, listed, refused = [], 0, 0
        used = self.used_keys()
        waiting = st["waiting"]
        for it in got[: self.a.max_per_run]:
            iid = it.get("id") if isinstance(it, dict) else None
            if not isinstance(iid, str) or not iid.startswith("i-") or len(iid) != 18:
                continue
            if iid in self.mine["intakes"]:   # taken in a run whose report did not arrive: the end of this run reports it again
                continue
            if len([t for t in self.mine["accepted"] if self.now - t < 3600]) >= self.a.max_per_hour:
                break   # the rest waits on the page for the next hour
            try:
                name, ip, neb, sk = self.check(it, st, used)
                if waiting >= self.a.max_waiting:
                    raise ValueError("the list is full: try later")
            except ValueError as e:
                updates.append({"id": iid, "state": "refused", "reason": str(e)[:120]})
                refused += 1
                continue
            self.pubkeys.mkdir(mode=0o700, parents=True, exist_ok=True)
            pub = self.pubkeys / f"{name}.pub"
            fd = os.open(pub, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never over an existing file
            with os.fdopen(fd, "w") as f:
                f.write(keys.nebula_pem(neb))
            r = self.mesh("request", name, "--ip", ip, "--note", "public page")
            if r.returncode != 0:
                pub.unlink()
                if r.returncode in REFUSE:
                    updates.append({"id": iid, "state": "refused", "reason": REFUSE[r.returncode]})
                    refused += 1
                continue   # anything else: left on the page as received, the next run tries again
            seal_file = self.dir / "seal" / f"{name}.pem"
            seal_file.parent.mkdir(mode=0o700, exist_ok=True)
            write_atomic(seal_file, keys.seal_pem(sk).encode())
            self.mine["intakes"][iid] = {"name": name, "state": "listed", "at": self.now}
            self.mine["accepted"].append(self.now)
            used.add(neb.hex())
            waiting += 1
            listed += 1
        return updates, listed, refused

    def bundle_for(self, name: str) -> bytes:
        """The outbox bundle, checked before it is sealed: only the four expected files under <name>/, no private key, and a
        certificate for exactly the key the visitor sent."""
        path = self.state / "admission" / "outbox" / f"{name}.tar.gz"
        data = path.read_bytes()
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.name == name and m.isdir():
                    continue
                base = m.name[len(name) + 1:] if m.name.startswith(name + "/") else ""
                if not m.isreg() or base not in BUNDLE_FILES:
                    raise Fail(f"{name}: unexpected entry in the outbox bundle")
                body = tar.extractfile(m).read()
                if b"PRIVATE KEY" in body:
                    raise Fail(f"{name}: the outbox bundle holds a private key; it is not sent")
                seen.add(base)
        if seen != BUNDLE_FILES:
            raise Fail(f"{name}: the outbox bundle is incomplete")
        want = keys.nebula_pub((self.pubkeys / f"{name}.pub").read_text()).hex()
        if self.cert_pub(self.state / "bundles" / name / "host.crt") != want:
            raise Fail(f"{name}: the certificate is not for the visitor's key; it is not sent")
        return data

    def follow(self, st: dict) -> tuple[list, dict]:
        """One update per request this puller took, and the local state each update moves it to once the page has it."""
        by_name = {r["name"]: r for r in st["requests"]}
        updates, after = [], {}
        for iid, rec in self.mine["intakes"].items():
            if rec["state"] == "done":
                continue
            r = by_name.get(rec["name"])
            if rec["state"] == "listed" and r is None:   # taken, but the request is not there (a failed run): the page is told again
                continue
            if r is None or r["state"] == "expired":
                updates.append({"id": iid, "state": "ended", "reason": "the lease ended"})
                after[iid] = "done"
            elif r["state"] == "rejected":
                updates.append({"id": iid, "state": "refused", "reason": "rejected by the host"})
                after[iid] = "done"
            elif r["state"] in ("waiting", "deferred"):
                u = {"id": iid, "state": r["state"], "position": r["position"], "of": st["waiting"]}
                e = self.eta(st, r)
                if e:
                    u["eta"] = e
                updates.append(u)
            elif r["state"] == "approved" and rec["state"] == "listed" and r.get("lease_end"):
                try:
                    data = self.bundle_for(rec["name"])
                    sk = keys.seal_pub((self.dir / "seal" / f"{rec['name']}.pem").read_text())
                    updates.append({"id": iid, "state": "ready", "sealed": keys.seal(sk, data), "lease_end": r["lease_end"]})
                    after[iid] = "delivered"
                except (Fail, OSError, keys.KeyRefused, RuntimeError) as e:
                    print(f"puller: {rec['name']}: not delivered this run: {e}", file=sys.stderr)
        return updates, after

    def run(self) -> int:
        st = self.status()
        if st["panic"]:
            self.call("/api/v1/puller/report", {"closed": True, "status": self.numbers(st), "updates": []})
            print("puller: closed (panic) | nothing pulled")
            return 0
        updates, listed, refused = self.take(st)
        self._save()   # the requests made in mesh are remembered before anything else can fail
        st = self.status()
        more, after = self.follow(st)
        updates += more
        res = self.call("/api/v1/puller/report", {"closed": False, "status": self.numbers(st), "updates": updates})
        for iid, s in after.items():
            self.mine["intakes"][iid]["state"] = s
            if s == "done":   # the visitor's keys are not needed any more
                n = self.mine["intakes"][iid]["name"]
                for f in (self.pubkeys / f"{n}.pub", self.dir / "seal" / f"{n}.pem"):
                    if f.exists():
                        f.unlink()
        self._save()
        ready = sum(1 for u in updates if u["state"] == "ready")
        ended = sum(1 for u in updates if u["state"] == "ended")
        print(f"puller: open | listed {listed} | refused {refused} | ready {ready} | ended {ended} | "
              f"applied {res.get('applied', '?')} | slots {st['slots_used']}/{st['slots_total']} used | {st['waiting']} in the list")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the control plane's side of the public page (docs/PUBLIC-PAGE.md)")
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--url", required=True, help="the page, https://... (outbound only)")
    ap.add_argument("--token-file", required=True, help="the puller's token (the page keeps only its SHA-256)")
    ap.add_argument("--cacert", help="the page's CA, when it is not a public one (the lab)")
    ap.add_argument("--mesh", default=str(HERE.parent / "mesh"))
    ap.add_argument("--state", default=os.environ.get("EMESH_STATE", str(HERE.parent / ".state")))
    ap.add_argument("--nebula-cert", default=os.environ.get("EMESH_CACHE", str(HERE.parent / ".cache")) + "/nebula-cert")
    ap.add_argument("--max-per-run", type=int, default=10)
    ap.add_argument("--max-per-hour", type=int, default=30)
    ap.add_argument("--max-waiting", type=int, default=20)
    ap.add_argument("--now", type=int)
    a = ap.parse_args(argv)
    os.umask(0o077)   # every file this writes holds requests, addresses or keys
    if not a.url.startswith(("https://", "http://127.0.0.1", "http://[::1]")):
        print("the page must be https:// (plain http only to this machine, for tests)", file=sys.stderr)
        return 2
    if os.name == "posix" and Path(a.token_file).stat().st_mode & 0o077:
        print(f"{a.token_file} is readable by others: chmod 600 it", file=sys.stderr)
        return 2
    try:
        return Puller(a).run()
    except (Fail, subprocess.TimeoutExpired) as e:
        print(f"puller: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
