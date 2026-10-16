# edge-mesh-lab

A collaborative, "torrent-like" edge Kubernetes mesh: people run a small lab at home
(Docker with k3s inside, a VM, WSL2, anything) and either join a shared control plane or
share compute with each other. Self-sovereign, secure, easy to join.

**Status (2026-10-16): design phase, private repository.** Nothing here runs yet. Phase 1 covers
only the technical challenges of *login* and *joining*: see [docs/DESIGN.md](docs/DESIGN.md).
This repository has a security design and a threat model, so it stays private until the
maintainer decides otherwise.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md): sign your commits (DCO), declare AI help, one concern per PR.

## Licence

Apache 2.0 (see [LICENSE](LICENSE)); open decision, see DESIGN.md.
