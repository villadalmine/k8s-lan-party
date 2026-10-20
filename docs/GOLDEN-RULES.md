---
tipo: doc
id: edge-mesh-lab-golden-rules
version: 0.1.0
fecha_alta: 2026-10-16
fecha_ultimo_cambio: 2026-10-16
categoria: plataforma
dueno_agente: casa-orquestador
pieza: edge-mesh-lab
meta: meta.yml
depende_de: [docs/DESIGN.md]
lo_usan: [README.md]
docs: [docs/DESIGN.md]
scripts: []
runbooks: []
tags: [regla-de-oro, mvp, seguridad]
---

# Golden rules (MVP phase 1)

The non-negotiables. Anything that breaks one of these is a bug in the product, not a trade-off. The reasoning is in
[DESIGN.md](DESIGN.md).

1. **What runs on someone else's machine can be seen by its owner.** No encryption protects a pod from the owner of
   its node (the keys and the plaintext are in memory on a host where the owner is root). We never claim otherwise,
   and we say it at join time.
2. **Placement by trust, enforced.** Nodes are `own`, `friends` or `public`; workloads are `shareable` or `private`
   (the default). A `private` workload runs only on nodes owned by its owner (trust is relative to the owner). Kyverno, labels and taints enforce it; nothing
   depends on goodwill. By default a user's pods run only on their own nodes.
3. **One job per layer.** Nebula: join and connectivity. Cilium tunnel: the CNI. Cilium WireGuard: transit
   encryption, always on. Cilium mutual auth: identity, per policy. Kyverno: placement.
4. **Joining is one bundle and one command**, and credentials travel encrypted: Nebula handshake, then the k3s token
   over TLS with the CA pinned. Tokens are one-time (burned server-side once the node is Ready), per node, and expire. The shared `K3S_TOKEN` is never handed out.
5. **Revocation is one command** and is hard: certificate on the Nebula blocklist, node deleted, token dead.
6. **The Kubernetes API is never public.** It is reachable only over Nebula.
7. **The maintainer's endpoints run only on the maintainer's own nodes**: the AI gateway and the LoadBalancer with BGP. Remote nodes
   never announce.
8. **Persistent participants join only by signed PR** (cosign keyless from a GitHub Actions workflow, the maintainer merges). The public demo is
   automatic, inside a fixed pool with a 1 h lease and automatic kick.
9. **Self-sovereign.** No third-party SaaS in the trust path; our CA, our control plane.
10. **Proved, not assumed.** Every claim marked "prove in the PoC" has a deterministic test with a recorded
    PASS or FAIL.
