---
tipo: runbook
id: k8s-lan-party-admission
version: 0.2.0
fecha_alta: 2026-10-27
fecha_ultimo_cambio: 2026-10-27
categoria: plataforma
dueno_agente: casa-orquestador
pieza: k8s-lan-party
meta: ../meta.yml
estado: opcion A y fase B1 (pool automatico) construidas y probadas (VMs Incus); B2 y B3 solo diseno
depende_de: [docs/DESIGN.md, poc/pool.yaml, poc/pool.py, poc/admission.py, poc/mesh]
lo_usan: [README.md, docs/DEMO.md, docs/POC-STATUS.md, docs/HOST-YOUR-OWN.md]
docs: [docs/DEMO.md, docs/HOST-YOUR-OWN.md, docs/DESIGN.md]
scripts: [poc/admission.py, poc/pool.py, poc/pool.yaml, poc/mesh, poc/issue.sh, poc/revoke.sh, poc/poc-admission.sh, poc/poc-admission-auto.sh, poc/test_admission.py, poc/test_admission_auto.py, poc/test_pool.py]
runbooks: [docs/DEMO.md]
tags: [admision, pool, lista-de-espera, anti-bot, demo, automatico, panic, eventos, metricas]
---

# Admission: how other people get a place in the mesh

**Status (2026-10-27).** **Option A (manual approval) is built and tested**: unit tests with a simulated clock and `make poc-admission` on Incus VMs.
**Phase B1 (the automatic tick) is built and tested**: `mesh host auto --once` approves, expels at the end of the lease and promotes the next one with
no human in the loop, with a switch, a panic button, events, a notify hook and metrics (`make test`, `make poc-admission-auto` on Incus VMs).
**B2 (the public page that takes requests) and B3 (the opening) are design only.** Phase 1 is still not complete (the gap table in
[POC-STATUS.md](POC-STATUS.md)); the cluster side of the pool (quotas, the idle kick) and the page are issue #11, which stays open until B2 and B3 exist.

The maintainer decided on 2026-10-27: **start with A** (a human approves every place) and keep a written path to **B** (a public, automatic pool);
later the same day: **build B in phases (B1, B2, B3) so that bots run it** without the maintainer stepping in.
The numbers are parameters in [`poc/pool.yaml`](../poc/pool.yaml), not code: changing a limit is a one-line change there.

## The maintainer's rules, as we read them

> Two places at a time, one hour each. Whoever comes next waits in a first-come, first-served list. When an hour ends the place is freed and the
> next one in the list may enter. At most two hours a day per person, so it is not always the same one, and a pause between two hours of the same
> person. Some way to detect people "by IP or something" so everybody gets a chance.

How that became parameters (**the maintainer corrects this table, not the code**):

| Rule | Our reading | Parameter |
| :--- | :--- | :--- |
| two places at a time | at most 2 participant nodes with an active lease | `slots: 2` |
| one hour each | the lease, the certificate lifetime and the moment `reap` expels are the same hour, starting at the approval | `lease_seconds: 3600` |
| a list, in order | FIFO by the time of the request; `approve` without a name takes the first one that is **eligible now** (someone in cooldown keeps their place but is skipped) | none (the order is the rule) |
| at most two a day per person | at most 2 leases per identity in **any 24 hours** (rolling window, see below) | `daily_leases_per_identity: 2` |
| a pause between two | one hour after a lease ends before the same identity may get the next one | `cooldown_seconds: 3600` |
| "by IP or something" | an identity is the visitor's IP, normalized (below); with no IP, the name | `identity_ipv4_prefix: 32`, `identity_ipv6_prefix: 64` |

**Why a rolling 24 h window and not the calendar day.** With a calendar day (UTC) someone can take two hours just before midnight and two more just
after it: four hours in a row, exactly what the rule wants to avoid. A rolling window has no reset to game, needs no time zone, and "when can I come
back" has one exact answer: when the oldest of the last two leases is 24 hours old. A test fixes it (`test_rolling_not_calendar_day`).

**What an identity is.** One opaque string per person, computed by `pool.identity()`:

| Visitor address | Identity | Why |
| :--- | :--- | :--- |
| IPv4 `203.0.113.7` | `203.0.113.7` (`identity_ipv4_prefix: 32`) or `203.0.113.0/24` (`24`) | 32 for option A (a human looks anyway); 24 when the pool opens, because one person can easily get several addresses of one /24 |
| IPv6 `2001:db8:1:2::77` | `2001:db8:1:2::/64` | a home usually gets at least a /64: counting single IPv6 addresses would be free to dodge |
| IPv4-mapped IPv6 `::ffff:203.0.113.7` | `203.0.113.7` | the same client seen through a dual-stack socket |
| none (the request was typed by hand) | `name:<name>` | option A only: the maintainer is the identity check |
| behind the reverse proxy | the client from `X-Forwarded-For`, **only when the socket peer is in `trusted_proxies`**; from anyone else the header is ignored | anyone can send the header |

Names are checked in full against `[a-z][a-z0-9-]{1,19}` (they become the node name, the certificate name and a directory), `lh`, `cp`, `n1`, `n2` are
reserved, and a name is used once (as with `mesh host invite`).

## A. Manual approval (built)

```mermaid
sequenceDiagram
  autonumber
  participant V as visitor
  participant H as host (mesh host ...)
  participant L as ledger (admission/state.json)
  participant C as cluster (CA, k3s, Nebula)
  V->>H: "can I join?" (any channel: chat, mail, a form)
  H->>L: request alice --ip 198.51.100.10
  L-->>H: waiting, position 1 of 3 (or: deferred until 15:00 UTC, cooldown)
  H->>L: approve
  L->>L: a place free? alice eligible? (pool.py)
  L->>C: mesh host invite alice --ttl 3600s --trust public (join token 8 min at most)
  L-->>H: approved alice until 16:02 UTC; bundle: .state/bundles/alice
  H->>V: the bundle, privately
  V->>C: edge-mesh join ./bundle --accept-golden-rule
  Note over H,C: one hour later
  H->>L: reap (by hand, or a timer)
  L->>C: mesh host kick alice (cordon, drain, blocklist, delete node)
  L-->>H: reaped 1; the place is free for the next one
```

### The commands

Every command runs from the repository's `poc/` directory (`./mesh host --help` lists them). The state is
`poc/.state/admission/state.json`; `POOL_CONFIG=<file>` points at another `pool.yaml`.

```bash
./mesh host request alice --ip 198.51.100.10 --note "from the study chat"   # waiting, position 1 of 1 [added]
./mesh host request bob                                                      # no IP known: the identity is the name
./mesh host waitlist                       # POS NAME STATE ELIGIBLE IDENTITY NOTE (waiting = eligible now, deferred = not yet)
./mesh host approve                        # the first eligible one; or: ./mesh host approve bob --trust friends
./mesh host leases                         # NAME TRUST ENDS LEFT IDENTITY
./mesh host reject bob --reason "asked twice"
./mesh host reap                           # expel every lease that ended; again: "reaped 0"
./mesh host admission-status               # admission: slots 1/2 used | 1 in the list (1 eligible now) | next to end: alice in 41m10s
```

| Command | What it does | Exit codes |
| :--- | :--- | :--- |
| `request <name> [--ip IP] [--note TEXT]` | adds the person to the list; prints the position, or when they become eligible and why (`cooldown`, `daily-cap`, `active-lease`). The same name and identity again: the same position. Another name from an identity already in the list: not added | 2 bad name or IP, 3 name already used |
| `waitlist` | the list in order | |
| `approve [<name>] [--trust public\|friends]` | checks a place is free and the identity is eligible (pool.py), runs `mesh host invite <name> --ttl <lease>s` (join token: `TOKEN_TTL`, 8 min at most; over that `approve` refuses, exit 2), records the lease; prints the bundle path and the end time. If the invite fails nothing is recorded | 3 not in the list, 4 nobody eligible (prints who is next and when), 5 no free place (prints which lease ends next), 6 that person is not eligible yet |
| `reject <name> [--reason]` | takes a request off the list (idempotent) | 3 |
| `leases` | active leases with the time left | |
| `reap [--all \| --name N]` | first `mesh host burn` (the join token of every registered node); then for every lease that ended (or all, or one, now): `mesh host kick`, which takes the name off the node guard and sweeps every Node not named after a live bundle, then cooldown, place freed, request `expired`, history kept. A kick that fails, or anything left to sweep, keeps the lease (and its place) for the next run | 1 a kick failed or a Node is left (the others are recorded), 3 `--name` has no active lease |
| `admission-status` | one line for a bot or a status page | |

`--trust public` (the default) signs with the **pool CA**, which can only mint `public` certificates; `--trust friends` signs with the persistent CA.

### Who sees what

| Actor | Sees | Does not see |
| :--- | :--- | :--- |
| visitor | their position or the time they become eligible (told by the host), then their bundle and their node | the list, other people's identities |
| host | the list with identities and notes, the leases, the history | |
| the cluster | a node with the trust label and taint of its level; the golden rule places only shareable workloads on it | who was in the list |

### Where the state is, and what it promises

- `poc/.state/admission/state.json`: the requests (`id`, `name`, `identity`, `note`, `ts`, `state` = `waiting|deferred|approved|rejected|expired`), the
  active leases (`name`, `trust`, `start`, `end`, `ip` = the identity, `bundle`) and the history of ended leases, which feeds the daily cap.
- Every command takes an **exclusive lock** (`poc/.state/admission/lock`: `fcntl.flock` on Linux and macOS, `msvcrt.locking` on Windows) and writes
  the file **atomically** (temporary file, `fsync`, rename): two commands at the same moment never lose a request (a unit test runs 12 at once).
  A command that fails writes nothing, except `reap`, which records the kicks that succeeded and keeps a lease whose kick failed for the next run.
  A corrupt `state.json` is reported (exit 1) and left untouched: restore it from the backup.
- **Retention.** It holds visitors' IP addresses, which are personal data: kept only on the host, never published, and forgotten on a schedule.
  Before every write the ledger drops what no rule needs any more: daily-cap times older than 24 h, cooldowns that ended, request-rate times
  older than the rate window. A lease or request that closed more than `identity_retention_seconds` ago (25 h: the 24 h window plus the cooldown;
  the code never goes below either) keeps its name and times (a name is used once) but loses the identity, the note and the reason. Open requests
  and active leases keep theirs. A unit test with the simulated clock checks that no expired identity is left (`RetentionTest`).

### How someone is expelled

`reap` is the one way, by hand or from a timer (`* * * * * cd <repo>/poc && RUNTIME=incus ./mesh host reap`). **Every minute is a security
requirement, not a convenience:** `reap` also burns join tokens and rewrites the node guard (see "the life of a name" below). First it runs `mesh host burn`; then for each ended lease
it runs `mesh host kick <name>`: cordon, drain, the certificate fingerprint on the Nebula blocklist of every host (and the external lighthouse when it
is reachable), delete the node, take its name off the **node guard**, and **sweep every Node that is not named after a live bundle** (below), and
wait until nothing is left to sweep: if something is still there after 30 s, the API does not answer or the bundle has no `NODE_IP`, `kick` fails
(exit 4), `reap` keeps the lease and its place and tries again on its next run.

**Copies of a bundle.** A copy of a bundle started on a second machine (before its join token was burned) chooses its own node name and addresses: a
plain `k3s agent` reports its LAN address, or two addresses. Neither is evidence, so nothing keys on them alone. The keys are what the visitor cannot
choose, the bundles the host issued:
- **the node guard** (`poc/node-guard.yaml`, a ValidatingAdmissionPolicy on `nodes` and `nodes/status` for kubelets only): a Node may be registered or
  updated only under the name of a bundle that is issued and not revoked, and only with that bundle's Nebula IP as its InternalIP (every InternalIP is
  checked). Its list is the ConfigMap `kube-system/edge-mesh-nodes`, rewritten under a lock by `mesh host invite`, `revoke` and every `reap` (`mesh
  host burn`): it holds the live bundles that have a Node, or whose join token still works. A copy under its own name, or under an issued name with
  another address, is refused at registration (`poc-admission` A15, a copy with a LAN and an IPv6 address);
- **the node password** (`poc/node_password.py`): when it issues a bundle the host makes a random password for that name, stores its hash where k3s
  keeps node passwords (`kube-system/<name>.node-password.k3s`, k3s's own scrypt format) and puts the password in the bundle (`NODE_PASSWORD`, which
  `edge-mesh join` writes to `/etc/rancher/node/password`). k3s registers a name only with its password, so another visitor, with a valid token of his
  own and the other bundle's Nebula IP on a dummy interface, cannot take an issued name that has no Node yet (A16), and its owner still joins (A18);
- **the life of a name with no Node yet**, so that the node password always outlives the name's place on the guard: k3s garbage-collects a node
  password that no Node took once it is 10 minutes old (checked every minute), and from then on anybody with a working token of his own could take
  the name with his own password. So the guard admits such a name only while its join token works **and** its node password secret exists, a join
  token lives **at most 8 minutes** (`TOKEN_TTL` over 8 minutes is refused by `issue.sh` and by `approve`, before anything is made), and every
  `reap` (the timer, every minute), `players`, `invite` and `revoke` rewrite the guard. An unused name leaves the guard at most one `reap` after its
  token dies, before k3s may drop its password; a name whose password is gone leaves at the next `reap` whatever its token says (A17). **This needs
  the `reap` timer** (option A needs it anyway for the leases): without any `reap`, `players`, `invite` or `revoke` for over 10 minutes after an invite,
  an issued name that nobody took would stay on the guard after k3s dropped its password. That residual is known and of low risk (it needs a name
  issued but not joined for over 10 minutes, another visitor with a working token at that moment, and no `mesh host` command in between), and
  `invite` prints a note when nothing rewrote the guard in the last 90 seconds;
- **the sweep** (`poc/nodes_sweep.py`, run by `revoke`, so by `kick` and `reap`): every Node not named after a live bundle goes, and so does a
  live-named one that reports the revoked bundle's address, or not its own. It covers what the guard did not see (a Node registered before the guard
  existed).

So a Node name belongs to the one bundle that carries its password; a copy of a bundle can at most register again under that bundle's own name,
which is the same visitor. The blocklist cuts the copy's tunnel with the lease (`poc-admission` A30: started again after the kick, the copy cannot
reach the API). The certificate expires at the same hour anyway (`disconnect_invalid: true` closes the tunnel), so a host that forgets
`reap` still loses the node; `reap` is what frees the place and starts the cooldown. **Early end:** `reap --name <name>`. **Abuse switch:** set
`slots: 0` (no new approvals) and run `reap --all`; since B1, `mesh host panic` does both in one command and keeps auto off (section B1).

### Limits of option A

- The host is the anti-bot: one human looks at every request. It does not scale, and that is the point.
- An identity typed by hand is whatever the host types: two people behind one carrier NAT share one IPv4 identity, and the host decides.
- The lease starts at the approval, not at the join: a bundle that takes 40 minutes to reach the visitor leaves them 20.
- **The join token is not one-time by itself.** A k3s bootstrap token authenticates any machine until it expires or is deleted. The host burns it once
  the node registered (`mesh host burn`, run by `reap`, `players` and `revoke`), so it is valid until **the first `reap` after the node registered, or
  8 minutes (`TOKEN_TTL`, at most 8m), whichever comes first**; with `reap` every minute that is about a minute. In that window a second machine with a copy of the bundle
  can talk to the API, but the node guard refuses a Node under any name that is not an issued bundle's, and k3s refuses an issued name without that
  bundle's node password (see "Copies of a bundle" above; A15-A18). The next `reap` burns the token, so after it no copy gets that far (A11-A12); the kick at the end of the lease sweeps
  whatever is left and the blocklist cuts the copy's tunnel (A29-A30). **Why 8 minutes at most:** the token is the only part of the bundle a second machine can
  reuse, so it lives as little as a hand delivery allows: the visitor installs the tools first ([JOIN.md](JOIN.md)) and asks for approval when
  ready (`TOKEN_TTL` may be shorter, `5m`, never longer: see "the life of a name" above).
- Not built (issue #11): per-namespace quotas and LimitRange for visitors, the idle kick, the page. The automatic tick is B1, below.

### Proof

| Check | Where |
| :--- | :--- |
| the sweep keyed on bundle names (dual-stack, LAN-address and impostor Nodes), `revoke` exit 4 when a Node stays, the API does not answer or `NODE_IP` is missing (stubbed `kubectl`); FIFO, slots, daily cap (rolling), cooldown, retention (no expired identity left), a taken name skipped, notes without control characters, a corrupt ledger left untouched, identity normalization, `X-Forwarded-For` only from a trusted proxy, invalid names, `reap` idempotent, `approve` with nobody eligible, a failed invite records nothing, 12 concurrent writers | `make test` (`poc/test_pool.py`, `poc/test_admission.py`), no Docker |
| request, approve, join, Ready, `reap` burns the join token and a second machine presenting it is refused, a non-cooperative copy of a bundle (plain `k3s agent`, LAN and IPv6 addresses) is refused by the node guard, a guest who takes another guest's issued name at its Nebula IP is refused by the node password and the owner still joins, a name whose node password k3s dropped leaves the guard, the copy's tunnel is cut with the lease, places full, the lease ends, `reap` expels (node gone, fingerprint on the blocklist), the next one in line enters, `reap --all` | `make poc-admission` (A01-A31), Incus VMs; the lease is shortened by `ADM_LEASE` (default 1800 s) |

## B. The automatic pool, in phases

The maintainer decided on 2026-10-27 to build the automatic pool in phases, so that bots run it and nobody has to step in: **B1** (the automatic tick,
built, this section), **B2** (a public page that takes requests, design only), **B3** (the opening, design only). The rules do not change between phases:
2 places, 1 h leases, a FIFO list, at most 2 leases a day per identity (rolling 24 h), a cooldown, identities by IP (/32 or /24, IPv6 /64).

```mermaid
flowchart LR
  R["request (B1: the host or its bot runs mesh host request; B2: the public page)"] --> Q["the list (state.json)"]
  T["host timer, every minute: mesh host auto --once"] --> K
  subgraph K["one tick (under the lock)"]
    direction TB
    K1["1. burn the join token of every registered node"] --> K2["2. expel every lease that ended: kick (revoke, node guard, sweep: no stray Node left), outbox file deleted"]
    K2 --> K3["3. expiring: 5 min before the end, once"]
    K3 --> K4["4. only with auto on and no panic: approve the first eligible while a place is free (limits re-checked), invite, bundle to the outbox"]
  end
  Q --> K4
  K4 --> O["outbox/NAME.tar.gz (0600)"]
  O -->|"B1: the host delivers it; B2: the page, with a claim code"| V["visitor: edge-mesh join"]
  V --> N["node Ready, public, 1 h certificate"]
  N -->|"the hour ends"| K2
  K --> E["events.jsonl + ADMISSION_NOTIFY_CMD (type, summary)"]
  E --> B["the host's bot or ntfy (not in this repo)"]
  P["mesh host panic: auto off, slots 0, expel everyone"] --> K
```

### B1. The automatic tick (built and tested)

**What is automatic now:** promotion from the list, the bundle into the outbox, the warning 5 minutes before the end, the expulsion at the end, the
promotion of the next one, and an event for each of those (to a log and to a hook the host wires to its bot). **What is not yet:** taking requests from
the public (B1 requests are still typed by the host or by the host's own bot: `mesh host request`) and handing the bundle to the visitor (the host
delivers `outbox/<name>.tar.gz`; in B2 a service does it). So the anti-bot layers of B2 are not needed yet: nobody outside can put a request in the list.

| Command | What it does | Exit codes |
| :--- | :--- | :--- |
| `auto --once` | **one tick**, idempotent (a tick with nothing to do writes nothing): `mesh host burn`; expel every lease that ended (`kick`: blocklist, the name off the node guard, the sweep of every Node not named after a live bundle; it fails with exit 4 while a Node is left, the API does not answer or the guard was not updated, and then the lease and its place are kept and the next tick retries); `expiring` once per lease when `lease_warning_seconds` (300) are left; then, **only with auto on and no panic**, while a place is free: the first eligible request in FIFO order (cooldown and daily cap checked by pool.py, again inside `approve`), `mesh host invite --trust public` (pool CA, certificate = the lease, the node password, join token `TOKEN_TTL`, 8 minutes at most), the lease, the bundle into the outbox. One line: `tick: auto on \| expelled 2 \| approved 1 \| slots 1/2 used \| 1 in the list (0 eligible now)` | 1 a kick, an invite or a bundle failed (the rest of the pass is recorded; the next tick tries again); 2 `TOKEN_TTL` over 8 minutes: this tick approves nobody, the expulsions still run |
| `auto on \| off \| status` | the switch, in `admission/auto.json` (written atomically). **Off by default**: the demo is switched on on purpose. Off stops approvals only: **expiry always runs**. `status`: `auto: on \| panic: off \| slots 2/2 used \| 1 in the list (1 eligible now) \| last tick: ...` | 7 `on` while panic is on |
| `panic [--clear]` | the abuse switch: auto off, **effective slots 0**, `burn`, every active lease expelled now (how = `panic`), the outbox emptied. Idempotent: again, `panic: on (already) \| expelled 0` and no new event; a kick that failed is tried again by the next tick (in panic the tick expels every lease). While panic is on, `request`, `approve` and `auto on` exit 7. `--clear` reopens admission with the slots of pool.yaml; **auto stays off** until `auto on` | 1 a kick failed, 7 |
| `bundle <name>` | the approved bundle as `admission/outbox/<name>.tar.gz`: regular files of `bundles/<name>` only (a link is not followed), mode 0600 in a 0700 directory, fixed owner and times (the same bundle gives the same bytes), written atomically; an outbox that is a symbolic link is refused, and the name (already `[a-z][a-z0-9-]`) is the file name, so nothing is written outside the outbox. Prints `join: tar xzf alice.tar.gz && sudo edge-mesh join ./alice --accept-golden-rule`. The tick does it for every approval; this command packs it again. Deleted when the lease ends | 2 bad name, 3 no active lease |
| `admission-metrics` | Prometheus textfile format, fixed order, no names and no identities (below) | |

With auto on, a request over the **daily cap** is refused (exit 6, event `refused`, not listed): over the cap is a no, not a place in the list for
tomorrow, when nobody may be there to take it. A request in **cooldown** or with an active lease is kept, deferred, and the tick approves it when its
time comes. With auto off, `request` behaves as in A.

**Events.** Every fact is one JSON line in `admission/events.jsonl` (0600; `seq`, `ts`, `time`, `type`, `name`, `identity`, `summary`, and `how`,
`end`, `path`, `why`, `retry_at` where they apply): `request`, `refused`, `approved` (`by`: auto or host), `bundle_ready` (with the outbox path),
`expiring`, `expelled` (`how`: expired, ended early, panic), `rejected`, `kick_failed`, `invite_failed`, `auto_on`, `auto_off`, `panic`,
`panic_cleared`. They are appended under the same lock, right after the ledger is written, so the log's order is the order of the commits. The log
keeps the last `events_max_lines` (5000) lines and blanks the identity of every event older than `identity_retention_seconds`, like the ledger; the
counters behind the metrics live in the ledger, so they survive that rotation.

**The notify hook.** If `ADMISSION_NOTIFY_CMD` is set, after the lock is released it is called once per event with **two arguments**: the type and a
short summary (`alice: approved automatically (public) until 2026-10-27T16:02:11Z`). It is an argument list, never a shell: the variable is the host's
own setting, split once with `shlex`, and nothing a visitor typed is parsed (names are `[a-z][a-z0-9-]` anyway, and the note never reaches the
summary). The summary carries no note, key, token, bundle content or local path; the identity is appended only with `ADMISSION_NOTIFY_IP=1`. A hook
that fails, hangs (10 s) or does not exist is reported on stderr; the tick goes on and the event is already in the log. **The repository knows no ntfy
and no bot**: the host points the variable at its own script.

**Metrics** (`mesh host admission-metrics`; the host publishes them, for example `... > metrics.prom.tmp && mv metrics.prom.tmp
<textfile-dir>/emesh_admission.prom` for node_exporter's textfile collector):

| Metric | Meaning |
| :--- | :--- |
| `emesh_admission_slots_total` | places at the same time (0 while panic is on) |
| `emesh_admission_slots_used`, `emesh_admission_leases_active` | active leases |
| `emesh_admission_waiting` | requests in the list (waiting or deferred) |
| `emesh_admission_lease_seconds_remaining_min` | seconds left of the lease that ends first (0 with no lease) |
| `emesh_admission_auto_enabled`, `emesh_admission_panic` | the switch, 0 or 1 |
| `emesh_admission_events_total{type="..."}` | counter per event type, every type always present |
| `emesh_admission_last_tick_timestamp_seconds` | when the last tick ran (0 = never): alert when it is older than a few minutes |

**The scheduler is the host's.** There is no loop in the repository: a systemd timer or cron runs `mesh host auto --once` every minute (each tick
starts with `mesh host burn`, which rewrites the node guard, with auto on, off or in panic, so this timer is also the every-minute guard refresh that
A's `reap` timer gives; a tick that cannot read its ledger or switch exits 1 before the burn, which the last-tick metric shows)
([HOST-YOUR-OWN.md](HOST-YOUR-OWN.md) has both). Two ticks at the same moment are safe: the whole tick runs under the ledger's lock, so the second one
sees the places the first one took (a unit test runs 8 at once with a slow fake invite: exactly 2 leases, the identity over its cap never invited).

**What a malicious visitor can do with B1.** The same as with A: a `public` node for one lease, with golden-rule placement, a certificate that dies with
the lease, a join token valid until the next tick after the node registered (about a minute) or `TOKEN_TTL` (8 minutes at most). A copy of the bundle on a second machine can
talk to the API inside that window, but the node guard refuses its Node (it is not an issued name at its Nebula IP); the tick that ends the lease
frees the place only once `kick` confirmed no stray Node is left, and the blocklist cuts the copy's tunnel (`poc-admission-auto` AA13, AA18, AA20). The tick only ever signs `public` (pool CA). Abuse is stopped with `mesh host panic`.

**Proof.**

| Check | Where |
| :--- | :--- |
| FIFO up to the slots in one tick; a tick expels then promotes in one pass; a no-op tick leaves state, switch and log byte-identical; daily cap and cooldown inside auto (a listed request over the cap is never approved before its time); a failed invite records nothing and the next tick retries; a failed kick keeps the lease and its place; `expiring` once; the switch and its events; panic expels everyone, closes everything (exit 7) and is idempotent; events in order with exactly two hook arguments; the identity only with `ADMISSION_NOTIFY_IP=1`; hostile names and notes (quotes, `;`, newlines, `$(...)`, backticks) never reach a shell and no file is created; a failing or missing hook breaks nothing; the log is bounded and forgets identities; the outbox (0600, deterministic bytes, links not followed, names outside refused, deleted at the end); the metrics text; 8 concurrent ticks as processes | `make test` (`poc/test_admission_auto.py`, simulated clock, no Docker). Three mutations fail it: the tick without its expel step, the daily cap ignored, the hook run through a shell |
| `auto` off by default; a tick with auto off approves nobody; `auto on`; one tick approves alice and bob, carol waits; a no-op tick; the outbox bundle is 0600, the same files, a certificate of exactly the lease; a copy of bob's bundle on a second machine (plain k3s agent, its own name, LAN and IPv6 addresses) reaches the API and is refused by the node guard; both join and are Ready; the next tick burns the tokens; `expiring` once; the leases end and one tick expels both, leaves no Node but the control plane and only then promotes carol; the outbox files go; the copy, started again, cannot reach the API; a machine presenting alice's burned token is refused; carol joins; with auto off the tick only warns and expels; `auto on` approves the same identity's second lease; its third request is refused by the daily cap; panic, panic again (no event), panic holds (exit 7), `--clear`; the hook got every event in order without IP, key or path; the metrics | `make poc-admission-auto` (AA01-AA37), Incus VMs; `ADM_LEASE` (default 1200 s) and `ADM_COOLDOWN` (default 60 s) shorten the test |

### B2. A public page that takes requests (design only)

> Nothing of B2 is built. It keeps B1 working, has its own test, and is applied only with the maintainer's OK. The anti-bot layers come **with** the
> public intake, never after it.

```mermaid
flowchart LR
  V["visitor"] -->|"HTTPS: request, proof of work, own public key"| P["public page (VPS): queue + status, NO signing key"]
  P -->|"position, estimated time"| V
  H["home: admission.py + pool CA"] -->|"outbound only: pull new requests"| P
  H -->|"push: certificate for the visitor's public key + join token"| P
  P -->|"visitor fetches with a claim code"| V
  T["timer: auto --once (B1)"] --> H
  V -->|"Nebula UDP 4242"| L["lighthouse (VPS)"]
```

**The page: the list, the position and an estimate.** A small page on the public server shows the free places, the length of the list and, after a
request, the position and an estimated time (`position / slots * lease`, corrected by the leases' real end times). `admission-status` and
`admission-metrics` already give the numbers.

**Credentials: the page holds none.** Two designs were weighed:

| | the home pulls from a queue (**chosen**) | the public server signs with a request key of minimal scope |
| :--- | :--- | :--- |
| what lives on the internet-facing machine | requests and status only | a signing key (even a pool CA limited to `public`) |
| if that machine is taken over | an attacker can spam or empty the queue (a denial of service) | an attacker can mint `public` certificates for as many machines as they like, bypassing every limit, until the pool CA is dropped |
| home network | outbound connections only, nothing new is opened | nothing at home, but the key is on the VPS |

The pull model also fixes the bundle's secrecy: the visitor makes their own Nebula key (`nebula-cert keygen`, or the page does it in the browser) and
sends only the **public** key. The home signs it with `IN_PUB` (already in `issue.sh`, used for the lighthouse), so the bundle that travels back through
the public server has **no private key** (the B1 outbox bundle still has one: B1 is delivered by the host). The join token does travel; it lives
8 minutes at most, is burned once the node registers, and is useless without the private key (the API only answers on the Nebula IP). The visitor fetches the
result with a random claim code shown only to them; the server stores its hash. Test: the page answers with the same numbers as `admission-status`;
the server's disk holds no `*.key`.

**Anti-bot and fairness, in layers (each with its honest limit):**

| Layer | What it does | What it does not stop |
| :--- | :--- | :--- |
| identity caps | `identity_ipv4_prefix: 24`, IPv6 /64, behind a trusted proxy only; the per-identity request rate limit (`rate_window_seconds`, `rate_max_requests`, already in pool.py) | many honest people behind one carrier NAT share one identity; a VPN or residential proxies give many identities |
| self-hosted proof of work | before a request is taken the browser solves a small puzzle (ALTCHA/hashcash style: the server hands out a random challenge signed with an HMAC and a difficulty; the client finds `n` so that `sha256(challenge + n)` starts with enough zero bits; the server checks it with Python's `hashlib` and `hmac`). No third-party service, no tracking | it costs a bot CPU seconds, not a wall: a patient bot pays and gets in |
| optional GitHub lane | sign in with GitHub (OAuth), accounts older than a minimum age (for example 90 days), the cap counted **per account**; DESIGN.md section 8 already sketches it | someone with several old accounts; it needs an OAuth app (the maintainer's step) |
| cooldown and daily cap | `cooldown_seconds`, `daily_leases_per_identity` (built; with auto on a request over the cap is refused) | identities that rotate |
| "fresh first" queue | a request from an identity that had a lease in the last 24 h goes **behind** everybody who has not had one; only then FIFO | nothing on its own; it shares out what the other layers let through |

**What none of this solves, and what we would do then.** Carrier-grade NAT (honest people blocked by a neighbour's use), VPNs and residential proxies
(one person, many identities), and patient bots (they pay the proof of work and wait their turn). The answers are blunt on purpose: **a global daily
cap** (at most N leases a day in total, so abuse has a ceiling), **manual approval for part of the places** (for example one of the two places stays A),
a higher proof-of-work difficulty, or the GitHub lane only. The pool's isolation still holds whatever gets through: a `public` node gets only shareable
workloads, its certificate dies in an hour, and the pool CA can sign nothing but `public`. And `mesh host panic` (built in B1) closes everything at once;
in B2 the page also says "closed" and the puller stops; as the last resort the pool CA is dropped from the trust bundle (`poc-ca.sh` proves only the pool
goes down). Practise it before announcing.

### B3. The opening (design only)

All of these, each the maintainer's explicit decision:

- [x] the repository is public (2026-10-27, not announced; see DESIGN.md, open decision 3); announcing it is the maintainer's "open it";
- [ ] B2 built and tested (the page, the anti-bot layers, the visitor's own key);
- [ ] issue #9: per-namespace RBAC and a kubeconfig per visitor (today a visitor has a node, not a namespace);
- [ ] issue #12: signed-PR participants, so the regulars are not in the free pool;
- [ ] issue #13: the gateway and BGP only on own nodes;
- [ ] issue #15: the relay for symmetric NAT (otherwise part of the visitors cannot join at all);
- [ ] issue #16: the go-public checklist in [DEMO.md](DEMO.md);
- [ ] issue #11: quotas, LimitRange and the idle kick for the pool (B1 closed the automatic tick; #11 stays open until B2 and B3).

## Parameters

All in [`poc/pool.yaml`](../poc/pool.yaml) except the token lifetime (an environment variable).

| Parameter | Value now | Meaning | Who decides |
| :--- | :--- | :--- | :--- |
| `slots` | 2 | places at the same time; 0 closes admission | maintainer |
| `lease_seconds` | 3600 | one lease, the certificate lifetime, when `reap` expels | maintainer |
| `cooldown_seconds` | 3600 | pause after a lease before the same identity's next one | maintainer (our reading of "a pause") |
| `daily_leases_per_identity` | 2 | leases per identity in any 24 h (rolling) | maintainer |
| `identity_ipv4_prefix` | 32 | 32 = one address; 24 = the whole /24 (planned for B2, when the public brings its own IP) | maintainer |
| `identity_ipv6_prefix` | 64 | an IPv6 /64 is one identity | maintainer |
| `rate_window_seconds`, `rate_max_requests` | 600, 5 | request rate per identity (for the public intake, B2; not used by B1) | maintainer |
| `trusted_proxies` | the reverse proxy's addresses | who may set `X-Forwarded-For` | whoever runs the border |
| `idle_kick_seconds` | 900 | early eviction of an idle node (not built, #11) | maintainer |
| `TOKEN_TTL` (environment) | 8m (1 to 8 minutes; more is refused) | lifetime of the k3s join token (burned earlier, once the node registers); it must die before k3s may drop the node password of an unused name | code (the cap) and host (shorter) |
| `identity_retention_seconds` | 90000 (25 h) | how long an identity is kept after its lease or request closed; never below 24 h or the cooldown | maintainer |
| `ADM_LEASE` (environment, test only) | 1800 | the shortened lease of `make poc-admission` | test |
| `lease_warning_seconds` | 300 | the tick records `expiring` once per lease when this little is left (B1) | maintainer |
| `events_max_lines` | 5000 | lines kept in `admission/events.jsonl`; the metric counters survive the rotation (B1) | host |
| `ADMISSION_NOTIFY_CMD` (environment) | unset | the host's command, called with `<type> <summary>` per event, never through a shell (B1) | host |
| `ADMISSION_NOTIFY_IP` (environment) | unset | `1` appends the identity to the summary the hook gets (B1) | host |
| `ADM_LEASE`, `ADM_COOLDOWN` (environment, test only) | 1200, 60 | the shortened lease and cooldown of `make poc-admission-auto` | test |
