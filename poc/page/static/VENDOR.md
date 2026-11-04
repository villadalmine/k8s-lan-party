---
tipo: doc
id: k8s-lan-party-page-vendor
version: 0.1.0
fecha_alta: 2026-11-04
fecha_ultimo_cambio: 2026-11-04
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../../../meta.yml
depende_de: []
lo_usan: [poc/page/page.py, poc/page/news.py, poc/test_whatsnew.py]
docs: [docs/PUBLIC-PAGE.md]
scripts: []
runbooks: []
tags: [pagina-publica, vendor, asciinema]
---

# Third-party files served by the page

Served from this server only (no CDN). `poc/test_whatsnew.py` fails when a file's SHA-256 differs from this table.

| File | Package | Version | Licence | SHA-256 |
| :--- | :--- | :--- | :--- | :--- |
| `asciinema-player.min.js` | npm `asciinema-player`, `dist/bundle/` | 3.17.0 | Apache-2.0 | `a13c37632e1b5c49fe9128417b9319a9b5bc64cb457dd5ae52cbba8a3aceb880` |
| `asciinema-player.css` | npm `asciinema-player`, `dist/bundle/` | 3.17.0 | Apache-2.0 | `f619fe17597043564f03b2c6918b3daf890ee8b912fb408542fba11afade4fdb` |

The tarball's npm integrity: `sha512-JbjNJmA2TLIeYNaOEja+kVSzXadKoqpIzVVmfBGNj2DmdtE/vExBCnkE8NYEcpaQcDvUYg8Ltt0urT80frACmw==`.
To update: download `https://registry.npmjs.org/asciinema-player/-/asciinema-player-<version>.tgz`, check that integrity
against `npm view asciinema-player@<version> dist.integrity`, copy the two files from `package/dist/bundle/`, and change this table.
The player compiles a terminal emulator to WebAssembly, so `/whats-new` (and only it) adds `'wasm-unsafe-eval'` to `script-src`.
