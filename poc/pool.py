#!/usr/bin/env python3
"""Capacity pool for the public demo: fixed slots, 1 h lease, per-identity rules, FIFO waitlist.

Pure and deterministic: time is always passed in (`now`, seconds), never read from the clock, so the PoC can
simulate an hour in no time. State lives in a JSON file. The settings are in poc/pool.yaml (read here by a tiny
parser, so there is no dependency).

The rules are per IDENTITY: an opaque string, the client IP normalized by `identity()` (an IPv4 address or its /24,
an IPv6 /64) or `name:<name>` when there is no IP. The state keeps it under the historical key `ip`.
Per identity: one active lease, a cooldown after it, at most `daily_leases_per_identity` leases in any 24 h
(a ROLLING window, not the calendar day: docs/ADMISSION.md says why), and a request rate limit.
The manual admission ledger (admission.py, `mesh host approve`) uses the same checks through `eligible_at()`.

    pool.py --state S --config C request <ip> <now> [--xff <chain> --peer <socket-ip>]
    pool.py --state S --config C tick <now>        # expire leases; prints the leases to reap, in order
    pool.py --state S --config C kick <lease-id> <now>
    pool.py --state S --config C status <now>
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

DAY = 86400


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


def _norm(ip: str) -> str:
    """An address in one spelling (IPv4-mapped IPv6 as IPv4, IPv6 compressed); anything else as given."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip.strip()
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def client_ip(peer: str, xff: str | None, trusted: list[str]) -> str:
    """The client IP as seen by the proxy. X-Forwarded-For is believed ONLY when the socket peer is a trusted
    proxy; otherwise the header is ignored (anyone can send it). With a trusted peer the right-most entry that is
    not itself a trusted proxy is the client (what Caddy appends). Both sides are compared in one spelling, so a
    proxy seen as `::ffff:127.0.0.1` on a dual-stack socket is still the trusted `127.0.0.1`."""
    trusted_n = {_norm(t) for t in trusted}
    peer = _norm(peer)
    if peer not in trusted_n or not xff:
        return peer
    for hop in reversed([_norm(h) for h in xff.split(",") if h.strip()]):
        if hop not in trusted_n:
            return hop
    return peer


def identity(ip: str | None = None, name: str | None = None, v4_prefix: int = 32, v6_prefix: int = 64) -> str:
    """The opaque identity the per-person limits count against.

    IPv4: the address (`v4_prefix` 32) or its network (`24`: one home or office, not one device). IPv6: the /64 (one
    customer usually gets at least a /64, so counting single addresses would be free to dodge). An IPv4-mapped IPv6
    address counts as IPv4. No IP: `name:<name>` (the maintainer typed the request by hand). Raises ValueError for a
    string that is not an IP address."""
    if not ip:
        if not name:
            raise ValueError("an identity needs an IP or a name")
        return f"name:{name}"
    addr = ipaddress.ip_address(ip.strip())
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.version == 4:
        if v4_prefix >= 32:
            return str(addr)
        return str(ipaddress.ip_network(f"{addr}/{v4_prefix}", strict=False))
    return str(ipaddress.ip_network(f"{addr}/{min(v6_prefix, 128)}", strict=False))


class Pool:
    def __init__(self, cfg: dict, state: dict | None = None):
        self.cfg = cfg
        s = state or {}
        self.leases: list[dict] = s.get("leases", [])      # {id, ip, start, end, slot}
        self.waitlist: list[str] = s.get("waitlist", [])   # ips, FIFO
        self.cooldown: dict[str, int] = s.get("cooldown", {})   # ip -> until
        self.requests: dict[str, list[int]] = s.get("requests", {})  # ip -> times
        self.next_id: int = s.get("next_id", 1)
        self.grants: dict[str, list[int]] = s.get("grants", {})   # identity -> lease start times (daily cap)

    def retention(self) -> int:
        """How long an identity is kept once nothing needs it: never less than the daily-cap window or the cooldown."""
        return max(self.cfg.get("identity_retention_seconds", DAY + self.cfg["cooldown_seconds"]), DAY,
                   self.cfg["cooldown_seconds"])

    def prune(self, now: int) -> None:
        """Forget what no rule needs any more (deterministic, run before every write): grants outside the 24 h window,
        cooldowns that ended, request times outside the rate window. Active leases and the waitlist stay."""
        self.grants = {i: ts for i, ts in ((i, [t for t in v if now - t < DAY]) for i, v in self.grants.items()) if ts}
        self.cooldown = {i: until for i, until in self.cooldown.items() if until > now}
        w = self.cfg.get("rate_window_seconds", 600)
        self.requests = {i: ts for i, ts in ((i, [t for t in v if now - t < w]) for i, v in self.requests.items()) if ts}

    def dump(self) -> dict:
        return {"leases": self.leases, "waitlist": self.waitlist, "cooldown": self.cooldown,
                "requests": self.requests, "next_id": self.next_id, "grants": self.grants}

    def _grant(self, ip: str, now: int, meta: dict | None = None) -> dict:
        used = {l["slot"] for l in self.leases}
        slot = next(i for i in range(self.cfg["slots"]) if i not in used)
        lease = {"id": f"lease-{self.next_id}", "ip": ip, "start": now,
                 "end": now + self.cfg["lease_seconds"], "slot": slot}
        lease.update(meta or {})
        self.next_id += 1
        self.leases.append(lease)
        self.grants[ip] = [t for t in self.grants.get(ip, []) if now - t < DAY] + [now]
        return lease

    def grant(self, ip: str, now: int, meta: dict | None = None) -> dict:
        """A lease for an identity the caller already checked with `eligible_at` and `slots_free` (manual admission)."""
        if self.slots_free() <= 0:
            raise ValueError("no free slot")
        return self._grant(ip, now, meta)

    def slots_free(self) -> int:
        return self.cfg["slots"] - len(self.leases)

    def daily_cap_until(self, ip: str, now: int) -> int:
        """0 if the identity is under its daily cap, else when it is again (rolling 24 h: the oldest lease that
        counts leaves the window)."""
        cap = self.cfg.get("daily_leases_per_identity", 2)
        recent = sorted(t for t in self.grants.get(ip, []) if now - t < DAY)
        if len(recent) < cap:
            return 0
        return recent[len(recent) - cap] + DAY

    def eligible_at(self, ip: str, now: int) -> tuple[int, str]:
        """(when, why): the first time this identity may get a lease, and the rule that holds it back ("" when it
        may now). An active lease holds it until that lease ends plus the cooldown."""
        holds = [(0, "")]
        for l in self.leases:
            if l["ip"] == ip:
                holds.append((l["end"] + self.cfg["cooldown_seconds"], "active-lease"))
        holds.append((self.cooldown.get(ip, 0), "cooldown"))
        holds.append((self.daily_cap_until(ip, now), "daily-cap"))
        when, why = max(holds)
        return (now, "") if when <= now else (when, why)

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
        if self.daily_cap_until(ip, now) > now:
            return ("refused", "daily-cap")
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
        for ip in list(self.waitlist):   # FIFO, skipping (and keeping in place) whoever is not eligible yet
            if self.eligible_at(ip, now)[0] <= now:
                self.waitlist.remove(ip)
                promoted = self._grant(ip, now)
                break
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
        ip = identity(ip, v4_prefix=cfg.get("identity_ipv4_prefix", 32), v6_prefix=cfg.get("identity_ipv6_prefix", 64))
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
    pool.prune(int(a.args[-1]) if a.args else 0)
    sp.write_text(json.dumps(pool.dump(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
