"""The panel's fields (#119): the one allowlist both sides use. The host (poc/panel.py) sends only these, of these types; the public page
(page.py) accepts only these, of these types, whoever sent them. Every field is a count or a state of the whole host, never a name, an
address, a key or a token; each belongs to a feature of docs/FEATURES.yml, whose learn block the page shows next to it.

Python standard library only (the page server installs this file too).
"""
from __future__ import annotations

# field -> (the feature of docs/FEATURES.yml it shows, its type). Every one is a count or a state of the whole host: no identity.
FIELDS: dict[str, tuple[str, type]] = {
    "nodes_total": ("join", int),                    # machines in the cluster, every one joined over the Nebula mesh
    "nodes_ready": ("join", int),
    "player_nodes": ("pool", int),                   # machines of players with a lease (edge-mesh/participant)
    "player_namespaces": ("pool", int),              # play-<name>: one per player, quota and NetworkPolicy
    "pods_running": ("placement", int),
    "player_pods_running": ("placement", int),       # in the players' namespaces, on their own machines
    "cpu_used_millicores": ("placement", int),       # the kubelets' own measure (stats/summary), all nodes
    "cpu_total_millicores": ("placement", int),      # allocatable
    "memory_used_mib": ("placement", int),
    "memory_total_mib": ("placement", int),
    "wireguard": ("encrypted-pod-network", bool),    # Cilium encrypts pod traffic between nodes
    "node_encryption": ("encrypted-pod-network", bool),
    "mutual_auth": ("mutual-auth", bool),
    "parties_active": ("party", int),                # pairs of players that both said yes (not the house match)
    "parties_waiting": ("party", int),               # one side said yes: nothing opens
    "games_running": ("doom", int),                  # Doom servers: players' own and the house's
    "house_open": ("live-page", bool),
    "house_players": ("live-page", int),
    "admitted_total": ("admission-auto", int),       # since the host began: approvals, the end of leases, refusals
    "expelled_total": ("admission-auto", int),
    "refused_total": ("admission-auto", int),
}
MAX_INT = 10 ** 12
# what the page writes next to each value (an integer, or yes/no)
LABELS = {
    "nodes_total": "machines in the cluster", "nodes_ready": "of them ready", "player_nodes": "players' own machines",
    "wireguard": "pod traffic encrypted (WireGuard)", "node_encryption": "node traffic encrypted too", "mutual_auth": "mutual authentication (SPIRE)",
    "player_namespaces": "players' namespaces", "player_pods_running": "pods running in them", "pods_running": "pods running in all",
    "cpu_used_millicores": "CPU in use (millicores)", "cpu_total_millicores": "CPU available (millicores)",
    "memory_used_mib": "memory in use (MiB)", "memory_total_mib": "memory available (MiB)",
    "parties_active": "parties open between players", "parties_waiting": "invitations waiting for the other side",
    "games_running": "Doom servers running", "house_open": "the house match is open to players", "house_players": "players in the house match",
    "admitted_total": "players admitted", "expelled_total": "leases ended", "refused_total": "requests refused",
}
assert set(LABELS) == set(FIELDS)
# the page's sections, in order: a title and the fields under it
GROUPS = (
    ("The mesh: machines that joined from anywhere", ("nodes_total", "nodes_ready", "player_nodes")),
    ("Encryption between pods", ("wireguard", "node_encryption", "mutual_auth")),
    ("The pool: one namespace and one machine per player", ("player_namespaces", "player_pods_running", "pods_running")),
    ("What the machines are doing", ("cpu_used_millicores", "cpu_total_millicores", "memory_used_mib", "memory_total_mib")),
    ("Parties: both sides said yes", ("parties_active", "parties_waiting")),
    ("Games", ("games_running", "house_open", "house_players")),
    ("Admission, since the host began", ("admitted_total", "expelled_total", "refused_total")),
)
assert sorted(f for _, fs in GROUPS for f in fs) == sorted(FIELDS), "every field in exactly one group"


def valid(panel) -> bool:
    """The page's check of what it receives: the same allowlist and types, nothing else (it trusts no sender)."""
    if not isinstance(panel, dict) or len(panel) > len(FIELDS):
        return False
    for k, v in panel.items():
        if k not in FIELDS:
            return False
        want = FIELDS[k][1]
        if want is bool and not isinstance(v, bool):
            return False
        if want is int and not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_INT):
            return False
    return True


