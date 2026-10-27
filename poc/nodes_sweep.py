#!/usr/bin/env python3
"""Which Nodes must go when a bundle is revoked: the sweep of `revoke.sh` (and so of `mesh host kick` and `reap`).

A copy of a bundle, started on a second machine before its join token was burned, registers a Node whose name and addresses its own
kubelet chooses: any name, the LAN address, two addresses (dual stack). None of that is evidence. What the visitor cannot choose is the
list of bundles the host issued, so the sweep is keyed on the NAME: every Node that is not named after a live bundle (a bundle whose
certificate is not on the blocklist; `cp` is the control plane) goes. The addresses only add evidence against a Node that does carry a
live name: one that reports the revoked bundle's Nebula IP, or that is not at its own bundle's Nebula IP, is a copy too. Every
InternalIP is compared, one by one (a dual-stack Node has several).

    kubectl get nodes -o json | nodes_sweep.py --gone-ip 10.77.0.6 cp=10.77.0.2 carol=10.77.0.7
    -> one line per Node to delete: <name><TAB><why>; nothing when the cluster is clean. Exit 2 when the input is not a Node list.
"""
from __future__ import annotations

import argparse
import json
import sys


def internal_ips(node: dict) -> list[str]:
    return [a.get("address", "") for a in (node.get("status") or {}).get("addresses") or [] if a.get("type") == "InternalIP"]


def strays(nodes: dict, live: dict[str, str], gone_ip: str) -> list[tuple[str, str]]:
    out = []
    for node in nodes["items"]:
        name = node["metadata"]["name"]
        ips = internal_ips(node)
        if name not in live:
            out.append((name, "not named after a live bundle"))
        elif gone_ip in ips:
            out.append((name, f"reports the revoked address {gone_ip}"))
        elif ips and live[name] and live[name] not in ips:
            out.append((name, f"named {name} but not at its address {live[name]} ({' '.join(ips)})"))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gone-ip", required=True, help="the Nebula IP of the revoked bundle")
    ap.add_argument("live", nargs="*", help="name=nebula-ip of every live bundle")
    a = ap.parse_args(argv)
    live = dict(x.split("=", 1) for x in a.live if "=" in x)
    try:
        nodes = json.load(sys.stdin)
        found = strays(nodes, live, a.gone_ip)
    except (ValueError, KeyError, TypeError) as e:
        print(f"not a Node list: {e.__class__.__name__}", file=sys.stderr)
        return 2
    for name, why in found:
        print(f"{name}\t{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
