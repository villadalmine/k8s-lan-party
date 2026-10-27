#!/usr/bin/env python3
"""Capacity pool for the public demo: fixed slots, 1 h lease, per-IP rules, FIFO waitlist.

Pure and deterministic: time is always passed in (`now`, seconds), never read from the clock, so the PoC can
simulate an hour in no time. State lives in a JSON file. The settings are in poc/pool.yaml (read here by a tiny
parser, so there is no dependency).

    pool.py --state S --config C request <ip> <now> [--xff <chain> --peer <socket-ip>]
    pool.py --state S --config C tick <now>        # expire leases; prints the leases to reap, in order
    pool.py --state S --config C kick <lease-id> <now>
    pool.py --state S --config C status <now>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_config(path: str) -> dict:
    cfg: dict = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if v.startswith("["):
            cfg[k] = [x.strip().strip("'\"") for x in v.strip("[]").split(",") if x.strip()]
        else:
            cfg[k] = int(v)
    return cfg


def client_ip(peer: str, xff: str | None, trusted: list[str]) -> str:
    """The client IP as seen by the proxy. X-Forwarded-For is believed ONLY when the socket peer is a trusted
    proxy; otherwise the header is ignored (anyone can send it). With a trusted peer the right-most entry that is
    not itself a trusted proxy is the client (what Caddy appends)."""
    if peer not in trusted or not xff:
        return peer
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        if hop not in trusted:
            return hop
    return peer


class Pool:
    def __init__(self, cfg: dict, state: dict | None = None):
        self.cfg = cfg
        s = state or {}
        self.leases: list[dict] = s.get("leases", [])      # {id, ip, start, end, slot}
        self.waitlist: list[str] = s.get("waitlist", [])   # ips, FIFO
        self.cooldown: dict[str, int] = s.get("cooldown", {})   # ip -> until
        self.requests: dict[str, list[int]] = s.get("requests", {})  # ip -> times
        self.next_id: int = s.get("next_id", 1)

    def dump(self) -> dict:
        return {"leases": self.leases, "waitlist": self.waitlist, "cooldown": self.cooldown,
                "requests": self.requests, "next_id": self.next_id}

    def _grant(self, ip: str, now: int) -> dict:
        used = {l["slot"] for l in self.leases}
        slot = next(i for i in range(self.cfg["slots"]) if i not in used)
        lease = {"id": f"lease-{self.next_id}", "ip": ip, "start": now,
                 "end": now + self.cfg["lease_seconds"], "slot": slot}
        self.next_id += 1
        self.leases.append(lease)
        return lease

    def request(self, ip: str, now: int) -> tuple:
        window = self.cfg["rate_window_seconds"]
        hist = [t for t in self.requests.get(ip, []) if now - t < window]
        if len(hist) >= self.cfg["rate_max_requests"]:
            self.requests[ip] = hist
            return ("refused", "rate-limited")
        hist.append(now)
        self.requests[ip] = hist
        if any(l["ip"] == ip for l in self.leases):
            return ("refused", "ip-has-active-lease")
        if self.cooldown.get(ip, 0) > now:
            return ("refused", "cooldown")
        if ip in self.waitlist:
            return ("waitlist", self.waitlist.index(ip) + 1)
        if len(self.leases) < self.cfg["slots"] and not self.waitlist:
            return ("granted", self._grant(ip, now))
        self.waitlist.append(ip)
        return ("waitlist", len(self.waitlist))

    def reap(self, lease_id: str, now: int) -> tuple[dict, dict | None]:
        """Remove one lease (expiry or kick). Starts the IP cooldown, frees the slot, and gives it to the head of
        the waitlist. Returns (reaped lease, the new lease or None)."""
        lease = next(l for l in self.leases if l["id"] == lease_id)
        self.leases.remove(lease)
        self.cooldown[lease["ip"]] = now + self.cfg["cooldown_seconds"]
        promoted = None
        while self.waitlist and promoted is None:
            ip = self.waitlist.pop(0)
            if not any(l["ip"] == ip for l in self.leases):
                promoted = self._grant(ip, now)
        return lease, promoted

    def expired(self, now: int) -> list[str]:
        return [l["id"] for l in sorted(self.leases, key=lambda l: (l["end"], l["id"])) if l["end"] <= now]

    def status(self, now: int) -> dict:
        return {"slots_total": self.cfg["slots"], "slots_used": len(self.leases),
                "slots_free": self.cfg["slots"] - len(self.leases), "waitlist": len(self.waitlist),
                "leases": [{"id": l["id"], "ip": l["ip"], "seconds_left": max(0, l["end"] - now)}
                           for l in sorted(self.leases, key=lambda l: l["id"])]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("action", choices=["request", "tick", "kick", "status"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--xff")
    ap.add_argument("--peer")
    a = ap.parse_args()
    cfg = load_config(a.config)
    sp = Path(a.state)
    pool = Pool(cfg, json.loads(sp.read_text()) if sp.exists() else None)
    if a.action == "request":
        ip, now = a.args[0], int(a.args[1])
        if a.peer:
            ip = client_ip(a.peer, a.xff, cfg.get("trusted_proxies", []))
        res = pool.request(ip, now)
        print(json.dumps({"ip": ip, "result": res[0], "detail": res[1]}, sort_keys=True))
    elif a.action == "tick":
        now = int(a.args[0])
        for lid in pool.expired(now):
            lease, promoted = pool.reap(lid, now)
            print(json.dumps({"reaped": lease, "promoted": promoted}, sort_keys=True))
    elif a.action == "kick":
        lease, promoted = pool.reap(a.args[0], int(a.args[1]))
        print(json.dumps({"reaped": lease, "promoted": promoted}, sort_keys=True))
    else:
        print(json.dumps(pool.status(int(a.args[0])), sort_keys=True))
    sp.write_text(json.dumps(pool.dump(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
