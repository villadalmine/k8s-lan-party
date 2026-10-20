# edge-mesh-lab

A collaborative, "torrent-like" Kubernetes mesh. You run k3s at home (in a VM, privileged Docker or WSL2), join the
maintainer's control plane and share compute. Self-sovereign, secure, simple to join.

**Status (2026-10-16): product, MVP phase 1, private repository.** The architecture is decided; the proof of
concept (PoC) is being built. Phase 1 is the **golden rule** and what enforces it.

## The golden rule

> **What runs on someone else's machine can be seen by its owner.**

So placement is by trust, not by hope: nodes are `own`, `friends` or `public`, workloads are `shareable` or
`private`, and Kyverno enforces it. Read the [golden rules](docs/GOLDEN-RULES.md) (one page) and the
[design](docs/DESIGN.md).

![Layers, endpoints and the join flow](docs/diagrams/layers-endpoints-join.svg)

## How you join: one bundle + one command

1. You receive a bundle (Nebula certificate, `config.yml`, a one-time k3s token). **Persistent participants** get it
   by pull request (`participants/<you>.yaml`, signed with cosign, approved by the maintainer's merge). The **public
   demo** is automatic while a slot is free (1 h lease, one per IP, FIFO waitlist).
2. On a privileged Linux (VM, privileged Docker or WSL2): `edge-mesh join <bundle>`.

The command shows the golden rule and asks you to accept it. Revoking is one command too: `edge-mesh revoke <node>`.

## Try it

The deterministic proof of concept (`make poc`) lands in the next pull request; until then there is nothing to run.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md): sign your commits (DCO), declare AI help, one concern per PR.

## Licence

Apache 2.0 (see [LICENSE](LICENSE)); an open decision, see DESIGN.md section 11.
