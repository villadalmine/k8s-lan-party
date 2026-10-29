---
tipo: runbook
id: k8s-lan-party-public-page
version: 0.1.0
fecha_alta: 2026-10-28
fecha_ultimo_cambio: 2026-10-28
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
estado: construida y probada (make test; make poc-page en VMs Incus y con un servidor y un visitante en la nube); opcional
depende_de: [docs/ADMISSION.md, docs/HOST-YOUR-OWN.md, poc/page/page.py, poc/page/puller.py, poc/page/seal.py, poc/page/visit.py, poc/page/page-server.sh, poc/lighthouse-vps.sh]
lo_usan: [README.md, docs/HOST-YOUR-OWN.md, docs/ADMISSION.md, docs/JOIN.md]
docs: [docs/ADMISSION.md, docs/HOST-YOUR-OWN.md, docs/DESIGN.md]
scripts: [poc/page/page.py, poc/page/puller.py, poc/page/seal.py, poc/page/visit.py, poc/page/page-server.sh, poc/poc-page.sh, poc/test_page.py]
runbooks: [docs/HOST-YOUR-OWN.md]
tags: [pagina-publica, admision, anti-bot, api, caddy, puller, opcional]
---

# The public page: let strangers ask for a place by themselves

## Do I need it?

**No, unless you run a pool open to people you cannot contact.** The page is **optional**. It exists for one case: a public demo where anybody on
the internet asks for a place on their own, with nobody in between. To play with people you know, what is already there is enough and costs nothing more.

| Your case | Use | What it adds | What it costs you |
| :--- | :--- | :--- | :--- |
| friends, a LAN party | `mesh host invite <name>` and send the bundle privately ([HOST-YOUR-OWN.md](HOST-YOUR-OWN.md) section 2) | nothing to run | nothing |
| a course or a closed group | the list with manual approval (`mesh host request` / `approve`), or the automatic tick (B1) with **you** delivering each bundle ([ADMISSION.md](ADMISSION.md)) | limits per person, a waiting list, expiry at the hour; the tick needs no human | a timer on your host |
| a demo open to the internet, strangers you cannot reach | the tick (B1) **plus this page** | strangers ask, wait, collect and join by themselves (a browser, a script or an agent) | a public server with TLS (it can be the lighthouse's), a second timer on your host, more attack surface: read the threat model in [ADMISSION.md](ADMISSION.md) ("B2: threats and their tests") before you open it |

What the page never adds: a credential on the public server. The CA, the join tokens and the Kubernetes API stay with you; the page only keeps a
list and opaque sealed bundles that only each visitor can open.

## How it works

```mermaid
sequenceDiagram
  autonumber
  participant V as visitor (browser, script or agent)
  participant P as public server: Caddy + page (no credential)
  participant H as your host: puller + mesh + CA
  V->>V: own keys: nebula-cert keygen, openssl genpkey X25519 (private halves stay)
  V->>P: GET /api/v1/challenge, solve the proof of work
  V->>P: POST /api/v1/requests (name, two public keys, solved challenge)
  P-->>V: claim code, shown once (the page keeps its hash)
  H->>P: every minute, outbound only: GET /api/v1/puller/intake
  H->>H: check everything again, mesh host request; the tick approves within the pool's limits
  H->>P: POST /api/v1/puller/report: positions, estimates, the bundle sealed to the visitor's key
  V->>P: POST /api/v1/requests/status (claim code): position, estimate
  V->>P: POST /api/v1/requests/claim (claim code): the sealed bundle, once
  V->>V: open it with the seal key, add its own host.key, sudo edge-mesh join
  Note over H,V: at the end of the lease the tick expels the node; the page says "ended"
```

The pieces, all in `poc/page/`, Python standard library only: `page.py` (the page), `puller.py` (your host's side), `seal.py` (key checks and the
sealed envelope: X25519 + HKDF-SHA256 + AES-256-CTR + HMAC-SHA256, through `openssl`), `visit.py` (the visitor's side), `page-server.sh` (install it
on a server), `static/` (one stylesheet, one script for the browser's proof of work), `deploy/` (the systemd units and the Caddy site).

## Requirements

- **Your host** already runs a game with the automatic tick (`mesh host auto on` and its timer, [HOST-YOUR-OWN.md](HOST-YOUR-OWN.md) "The automatic
  pool") and has `python3` and `openssl`. A lighthouse on a public server ([HOST-YOUR-OWN.md](HOST-YOUR-OWN.md) "A public lighthouse").
- **A public server** with `python3` (Ubuntu 24.04 or similar, systemd), a DNS name that points at it (`page.example.org` here) and TCP 443 (and 80
  for ACME) open. It can be the lighthouse's server. Caddy: yours if it already runs one with `import /etc/caddy/conf.d/*.caddy` in its Caddyfile,
  or `--with-caddy` installs a pinned one. The Caddy site sets the body size limits per route (16 KB for visitors, 512 KB for the puller's report), so they hold on a Caddy that was already there; Caddy has no per-site body timeout, so on a Caddy that was already there add `servers { timeouts { read_header 10s read_body 30s } }` to its global options (the page's own 20 s deadline per request covers a dripping body either way). Any other reverse proxy works if it terminates TLS, limits and times the request body the same way, replaces `X-Forwarded-For` with the client's address
  and forwards to `127.0.0.1:8088`.
- An ssh command that runs as root there, for example `ssh -i ~/.ssh/server admin@page.example.org sudo`.

## Set it up

From the repository's `poc/` directory on your host:

```bash
SSH="ssh -i ~/.ssh/server admin@page.example.org sudo"
./page/page-server.sh install --ssh "$SSH" --site page.example.org         # the page, its unit, the Caddy site (add --with-caddy if it has none)
./page/page-server.sh puller-token --ssh "$SSH"                             # the token is made HERE (.state/page/puller.token, 0600); only its SHA-256 goes there
./page/page-server.sh verify --ssh "$SSH" --site page.example.org           # ok: page service active ... ok: the puller's hash is set
python3 page/puller.py run --url https://page.example.org --token-file .state/page/puller.token   # puller: open | listed 0 | ... (the page opens)
```

Every phase is idempotent: run it again and it prints no `changed:` line. Then the puller's timer on your host, a minute apart from the tick
(`poc/page/deploy/emesh-page-puller.service` and `.timer`: copy them to `/etc/systemd/system/`, replace `<your user>`, `<the clone>` and the address):

```bash
sudo cp page/deploy/emesh-page-puller.service page/deploy/emesh-page-puller.timer /etc/systemd/system/
sudoedit /etc/systemd/system/emesh-page-puller.service                     # your user, the clone, https://page.example.org
sudo systemctl daemon-reload && sudo systemctl enable --now emesh-page-puller.timer
journalctl -u emesh-page-puller -n 5                                        # puller: open | listed 1 | refused 0 | ready 0 | ...
```

The puller's caps, besides the pool's own limits: `--max-per-run 10`, `--max-per-hour 30`, `--max-waiting 20` (a compromised page can at most fill
your list up to these). The page's own limits (proof-of-work difficulty, requests per address, cooldown, a global ceiling per hour, wrong claim codes
per address, body size, connections) are in `poc/page/page.yaml`, installed as `/opt/k8s-lan-party-page/page.yaml`.

## See it, open it, close it

- With your eyes: `https://page.example.org/`: free places, the length of the list, when the next place frees, the form, and "where am I?" with a claim code.
- From a script: `curl -s https://page.example.org/api/v1/status`.
- **Closed** shows (and new requests get 503) when any of these holds: you ran `mesh host panic` (the next puller run reports it and pulls nothing), the
  puller has not reported for 5 minutes (your host or its timer is down), or the operator switch on the server:
  `./page/page-server.sh close --ssh "$SSH"` (and `open`). `mesh host panic --clear` plus one puller run reopens it.
- Take it away: `./page/page-server.sh remove --ssh "$SSH"` (the page, its unit, its state and its Caddy site; `--with-caddy` also removes the Caddy parts this script
  installed, as its marker `/etc/caddy/.k8s-lan-party-installed` says, and never a Caddy that was already there), then `sudo systemctl disable --now emesh-page-puller.timer` on your host.

## For visitors: the same flow from a browser, a script or an agent

The visitor's machine needs `python3`, `openssl`, `nebula` and `nebula-cert` (in the Nebula release) and `k3s` ([JOIN.md](JOIN.md) "Install"), plus
`poc/page/visit.py` and `poc/page/seal.py` from this repository. With `visit.py`:

```bash
python3 visit.py keygen ./mykeys                                    # host.key/host.pub (Nebula), seal.key/seal.pub; the private keys stay here
python3 visit.py request https://page.example.org myname --keys ./mykeys   # proof of work, then: Claim code: ABCDE-... (also in ./mykeys/claim-code)
python3 visit.py status https://page.example.org --keys ./mykeys    # myname: in the list, position 2 of 3; estimated around 2026-10-28T16:05:00Z
python3 visit.py wait https://page.example.org --keys ./mykeys      # polls; when ready: collects once, opens, checks, prints the join line
sudo edge-mesh join ./mykeys/myname --accept-golden-rule
```

Keep `wait` running: once your place is ready the bundle must be collected and used within 8 minutes (its join token dies then). In a browser the page
does the proof of work in JavaScript; you paste the two public files and get the claim code, and then collect with `visit.py wait --code <code>` on
the machine that has the keys (the bundle opens only there).

The same with curl (what `visit.py` does; `python3` only reads JSON and solves the puzzle):

```bash
PAGE=https://page.example.org
curl -s $PAGE/api/v1/status
C=$(curl -s $PAGE/api/v1/challenge)
CH=$(printf %s "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["challenge"])')
BITS=$(printf %s "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["difficulty"])')
NONCE=$(python3 visit.py pow "$CH" "$BITS")
python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "nebula_public_key": open("mykeys/host.pub").read(),
  "seal_public_key": open("mykeys/seal.pub").read(), "challenge": sys.argv[2], "nonce": sys.argv[3]}))' myname "$CH" "$NONCE" >req.json
curl -s -H 'Content-Type: application/json' --data @req.json $PAGE/api/v1/requests          # {"claim_code": "ABCDE-...", ...}
curl -s -H 'Content-Type: application/json' -d '{"claim_code": "ABCDE-..."}' $PAGE/api/v1/requests/status
curl -s -H 'Content-Type: application/json' -d '{"claim_code": "ABCDE-..."}' $PAGE/api/v1/requests/claim >claim.json
python3 -c 'import json; json.dump(json.load(open("claim.json"))["sealed"], open("sealed.json", "w"))'
python3 visit.py open ./mykeys sealed.json                                                    # bundle: mykeys/myname; join: sudo edge-mesh join ...
```

`make poc-page` runs these exact calls (G11, the decoy) next to `visit.py` (the visitor).

## The API, version 1

Stable: within `/api/v1` fields are only added, never renamed or removed; a breaking change is `/api/v2`. Every body is JSON (`Content-Type:
application/json`, at most 4096 bytes for visitors), **exactly** the fields listed (an unknown or duplicated field is a 400). Every answer is JSON
with `Cache-Control: no-store` and the security headers; errors are `{"error": "<word>", "message": "<sentence>"}`, with `Retry-After` on 429. No
cookies, no CORS headers (a browser on another site cannot call it), no CAPTCHA: the same limits hold for people, scripts and agents.

| Endpoint | Who | Send | 2xx answer | Errors |
| :--- | :--- | :--- | :--- | :--- |
| `GET /api/v1/status` | anyone | | 200 `open`, `closed_reason`, `slots_total`, `slots_used`, `slots_free`, `waiting`, `eligible`, `lease_seconds`, `next_end_in_seconds` (list), `reported_at`, `difficulty`, `api` | |
| `GET /api/v1/challenge` | anyone | | 200 `challenge`, `difficulty`, `expires_at`, `algorithm` (`sha256-leading-zero-bits`), `solve` | |
| `POST /api/v1/requests` | a visitor | `name`, `nebula_public_key` (PEM `NEBULA X25519 PUBLIC KEY`), `seal_public_key` (PEM X25519 `PUBLIC KEY`), `challenge`, `nonce` (1 to 20 digits: `sha256(challenge + ":" + nonce)` has `difficulty` leading zero bits) | 201 `claim_code` (shown once), `name`, `state` = `received`, `message` | 400 `bad-request`, `bad-json`, `bad-name`, `bad-key`, `bad-pow`; 403 `bad-pow`, `pow-expired`; 409 `pow-used`, `already-asked`, `name-taken`, `key-taken`; 411, 413, 415; 429 `rate`, `cooldown`, `busy`; 503 `closed`, `full`, `busy` |
| `POST /api/v1/requests/status` | the visitor | `claim_code` | 200 `name`, `state`, `message`, and when they apply `position`, `of`, `eta`, `eta_in_seconds`, `lease_end`, `reason` | 400; 404 `unknown`; 429 `too-many-tries` |
| `POST /api/v1/requests/claim` | the visitor | `claim_code` | 200 once: `state` = `ready`, `name`, `sealed` (`v`, `alg`, `epk`, `ct`, `mac`), `lease_end`, `join` | 409 `not-ready`; 410 `gone` (collected, ended, expired, closed); 404; 429 |
| `GET /api/v1/puller/intake` | your puller (`Authorization: Bearer <token>`) | | 200 `intakes`: `id`, `name`, `ip`, `nebula_public_key`, `seal_public_key`, `received_at` (at most 20) | 401, 429, 503 `no-puller` |
| `POST /api/v1/puller/report` | your puller | `closed`, `status` (`slots_total`, `slots_used`, `waiting`, `eligible`, `lease_seconds`, `next_ends`), `updates` (`id`, `state`, `position`, `of`, `eta`, `reason`, `lease_end`, `sealed`) | 200 `applied`, `ignored` | 400 `bad-report`, 401, 429 |

The states a visitor sees: `received` (the control plane has not read it yet), `waiting` / `deferred` (in the list; deferred = not eligible yet),
`ready` (collect now), `claimed`, `refused` (with a `reason`), `ended` (the lease is over), `expired` (not taken or not collected in time),
`closed` (the host closed admission).

## Lessons

- 2026-10-28: a page that only shows an IP address (a lab without a name) needs `default_sni` in Caddy: a client that opens `https://<ip>` sends no
  server name. `page-server.sh install --with-caddy` sets it to the site's address.
- 2026-10-28: the public server is a lighthouse too, and a lighthouse is a member of the mesh: every host now accepts the Kubernetes API, WireGuard
  and VXLAN only from the groups `own`, `friends` and `public` (`issue.sh`), never from the lighthouse; `make poc-page` G10 checks it from the server.
