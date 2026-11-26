# The live page: watch a match on the public site, and join to play

Status: **proposed, decisions taken by default** (RFC of [#113](https://github.com/villadalmine/k8s-lan-party/issues/113), milestone v0.7.0).
The owner asked on 2026-11-25 for a page on the project's public domain that shows a match live, with an option to jump in and play, and on
2026-11-26 for the work to go on until v0.7 is done. Each decision below (L1-L8) takes the recommended option of #113; a row changes only if
the owner says so. It builds on the public page ([PUBLIC-PAGE.md](PUBLIC-PAGE.md)) and on the consoles of v0.6.0
([CONSOLE-MODES.md](CONSOLE-MODES.md)), and changes none of their decisions unless a row says so.

## What was asked

1. a page on the public domain, next to the public page, that shows a match **live**, for anyone, without joining;
2. a **"join and play"** option on it;
3. only if it violates nothing (the v0.6.0 RFC's threat 12 said: no new public surface; this RFC is where that surface is decided).

## The short answer

| Asked | Violates something? | The shape proposed |
| :--- | :--- | :--- |
| A live view for anyone | **Not if** only the host's own match is shown, every player in it knows it is broadcast, and the frames leave from the host's own machine, outbound | the **house match**: a game the host runs on its own machine (`house`, a namespace of the host like `watch-host`), robots and the players who join it; the host's spectator of it; a **streamer** on the host that pushes its frames to the page, outbound only, like the puller |
| Join and play | **No**, by the designed path: the visitor asks on the page, joins with their own machine, and plays from their own console (#102) | the button opens the page's ask flow; the house **offers** a party to every admitted player and the player **accepts** it with one click in their console (both consents, as always); then **Play** |
| Play without installing anything | **Yes**: a public input path into the host's lab and anonymous workloads on the host's machines | **out** (L7) |

## L1: what is shown - only the house match

The page shows one match: the **house match**, a game server the host runs in the namespace `house` (labelled `edge-mesh/house=host`, made
by `mesh host house init`, like `watch-host` in D6: restricted, its own quota, default-deny, no credential but the host's, its pods on an
`own` node that is never the control plane). Robots play it so there is always something to see. A player's own server is **never** shown:
nobody is broadcast who did not choose to play in the house.

Players who join the house match are broadcast, and they are told so **before** they consent: the house's party offer, the player console's
card and the live page all say "this match is broadcast on the public page". Leaving the party leaves the broadcast.

## L2: where the frames come from - the host's spectator, through the host's own machine

The frames are those of a spectator of the house match, a pod in `house` on the host's own machine (the same client as D7's spectators). A
player's node never sends anything to the public site.

## L3: how the frames reach the page - a streamer on the host, outbound only

`poc/page/streamer.py` runs on the host (a systemd unit, like the puller). It reads the spectator's frames the way the host console does
(`watcher.Tap`: one `kubectl exec -i`, the pod's secret on stdin) and **pushes** them to the page: `POST /api/v1/live/frame` with a bearer
token of its own (the **stream token**: not the puller's, so one leaked token gives one power), a JPEG body of at most 256 KiB, at most 2
a second. The page never connects to the host: no port is opened anywhere, as for the puller. With panic on, or the host's admission
closed, the streamer stops sending and tells the page `offline`.

## L4: what the page keeps and serves

The page keeps **the last frame only, in memory**: never on disk, never a history. It serves:

| Endpoint | Who | Answer |
| :--- | :--- | :--- |
| `GET /live` | anyone | the live page (HTML, the page's CSP, no inline script) |
| `GET /api/v1/live` | anyone | `live` (true/false), `since`, `players` (the count the streamer reports, no names), `viewers` (an estimate) |
| `GET /api/v1/live/frame` | anyone | the last JPEG (`Cache-Control: no-store`), or 404 `offline` |
| `POST /api/v1/live/frame` | the streamer (`Authorization: Bearer <stream token>`) | 204; 400 not a JPEG or too large, 401, 413, 429 |
| `POST /api/v1/live/offline` | the streamer | 204: the page shows "offline" at once |

`offline` also when no frame came for 15 seconds. There is **no input path** from a visitor: the live page has no form and the API takes no
visitor body for it.

## L5: limits - viewers, rate, bandwidth

A frame is about 8-10 KB (320x200 JPEG). The page shows 2 frames a second, so a viewer costs about 20 KB/s. Limits: 4 frame requests a
second per origin (the page's own per-origin limiter: an IPv6 /64 is one origin), and **at most 120 frame answers a second in total**
(about 60 viewers, about 1.2 MB/s from the VPS); past it a 429 with `Retry-After` and the page says "too many viewers, try again". The
stream token's POSTs: at most 4 a second.

## L6: where it lives - a path of the public page's server

`https://<the public page>/live`: the same server, the same TLS (Caddy), the same security headers and the same deploy
(`page-server.sh`), linked from the public page and from What's new. A subdomain would add a certificate and a site for nothing. The page
server keeps holding no credential of the cluster: it keeps the SHA-256 of the stream token, like the puller's.

## L7: what is out

- **Playing without installing** (#113's option 2): a public input path into the host's lab, anonymous game clients on the host's machines,
  strangers' keys and frames crossing the VPS. Out of v0.7; it would need its own RFC.
- **Sound.** As in v0.6.0.
- **A player's own server on the live page.** Never (L1).
- **Publishing the consoles** (#116): after v0.7.

## L8: join and play - the house offers, the player accepts

The live page's **Join and play** button leads to the public page's ask flow (`/#ask`), with a line that says what follows: ask, collect the
bundle, `edge-mesh join`, open `edge-mesh console`, accept the house's party, press **Play**. On the host, `mesh host house open` makes the
house **offer** a party to every admitted player: the host writes the house's consent (`party-<player>` in `house`), and every reconcile (the
tick runs one) keeps doing it for each new admission; `mesh host house close` withdraws the offers, which also closes the parties of whoever
was in. A player cannot read the `house` namespace, so the console cannot show the offer itself: it shows a fixed **The house match**
section that says the match is broadcast, with **Join the house match (broadcast)** (`party accept house`) and **Leave the house match**;
the player's own consent then reads `waiting: the host has no house match yet`, `waiting for house to accept` (the house is closed) or
`active`, and once active the game is in *Games I can join* with **Play**. The rule of every party holds: nothing opens without the
player's own consent. `house` is a reserved name (admission, `mesh host invite`, the consoles), like `watch-host`.

## Threats and their tests

| # | Threat | Answer | Test |
| :- | :--- | :--- | :--- |
| LV01 | someone pushes frames to the page | the stream token (its hash only on the page); 401 without it | page unit test; lab |
| LV02 | a pushed body is not a picture, or is huge | JPEG checked (SOI/EOI, at most 256 KiB, at most 640x400), else 400/413 | page unit test |
| LV03 | the page stores frames | memory only, the last one; nothing under the page's state directory | page unit test |
| LV04 | viewers exhaust the VPS | per-origin and global frame limits (L5) | page unit test |
| LV05 | a player is broadcast without knowing | only the house is shown; its offer, the console card and the live page say so | lab: a player's own server never reaches the page |
| LV06 | the stream keeps going after panic | the streamer stops and sends `offline`; 15 s without a frame is offline anyway | streamer unit test; lab |
| LV07 | a visitor reaches the lab through the live page | no input path; the page never connects to the host | page unit test (no visitor POST on `/api/v1/live*`) |
| LV08 | the house runs on the control plane or holds credentials | `house` like `watch-host`: own node, guard, no credential | lab (as DM28) |
| LV09 | the house's offer opens a party alone | the player must accept; one consent opens nothing | party unit test; lab |
| LV10 | the stream token leaks | it can push pictures only (no intake, no report); `mesh host live rotate-token` | streamer unit test |

## The pieces, in order

1. This RFC (#113).
2. The page: the live endpoints, `/live`, its script and style, the limits; unit tests LV01-LV04, LV07.
3. The streamer and `mesh host live start|stop|status` (the stream token: `page-server.sh stream-token`, which makes it on the host and
   sends only its SHA-256; to rotate it, delete the token file and run it again); the house (`mesh host house
   init|start|stop|open|close|status|frame|down`: its server, two robots `robot-a` and `robot-b`, its spectator following `robot-a`; quota
   `house_quota_*` of pool.yaml, by default 2000m, 1536 MiB, 6 pods) and the offer in every party reconcile; unit tests LV06, LV09, LV10.
4. The player console: the house match section (Join, Leave) and the house's game card with Play.
5. `make poc-live` on Incus: a page server, the host, the house with robots, the streamer; a visitor sees changing frames; panic makes it
   offline; a visitor joins, accepts the house and plays (LV05, LV08 on the lab).
6. Deploy on the public server, What's new v0.7.0, the release.

## Decisions

| # | Decision | Default taken |
| :- | :--- | :--- |
| L1 | what is shown | the house match only |
| L2 | the frames' origin | the host's spectator, on the host's own machine |
| L3 | the path to the page | a streamer on the host, outbound, its own token |
| L4 | what the page keeps | the last frame, in memory |
| L5 | limits | 2 fps shown; 4/s per origin; 120 frames/s in total |
| L6 | where | `/live` on the public page's server |
| L7 | out | playing without installing; sound; a player's own server; publishing the consoles (#116) |
| L8 | join and play | the house offers, the player accepts in their console, then Play |
