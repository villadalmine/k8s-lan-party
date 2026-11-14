# The console for everyone: host mode, player mode, the game in the browser

Status: **RFC of [#99](https://github.com/villadalmine/k8s-lan-party/issues/99)** (milestone v0.6.0). Nothing here is built. It is decided
when its PR merges and the decision is recorded in #99; until then #100 to #103 stay blocked. It builds on the host console of v0.4.0
([CONSOLE.md](CONSOLE.md)), and changes none of that document's decisions unless a row of *Decisions* below says so.

## What was asked

The host tried the web console of #89 and asked for the whole thing in the browser:

1. the console holds **everything the host can do**;
2. **every player can run the console too**, to see their own things; what a player cannot do is on the page and cannot be clicked;
3. from the console one can **watch the game**, with a view per player, and **start it and play it** in the browser tab;
4. two modes, **server and player**, where the server sees everything;
5. only **if it violates nothing**.

This RFC answers 5 first: what each of 1 to 4 would cost in the project's rules (the golden rule, pairwise consent, the CA that never
moves, nothing new in public), and the shape that costs nothing.

## The short answer

| Asked | Violates something? | The shape proposed |
| :--- | :--- | :--- |
| Host mode with every verb | **Yes, for four verbs** (`invite`, `bundle`, `refresh`, `approve --trust friends`: they use the persistent CA, write a private key or rewrite a running machine's configuration). Not for the rest | every verb is **on the page**; those four are shown disabled with the command to type on the host. The reads that are missing (activity, metrics, pool, the public page's state) become views |
| Player mode | **No**, if it runs on the player's own machine with the player's own credentials | `edge-mesh console`: a daemon on the player's loopback that talks to the cluster with the kubeconfig the bundle already gave. It gets no new power: RBAC decides, as it does for `edge-mesh kubectl` today |
| Watch the game, a view per player | **No**, if watching is a party like any other | a spectator client of the game (Odamex has one) per watched player, next to the watcher, sending frames to the watcher's own console. The host has no place in the party model today (no `play-` namespace), so how the host watches is its own decision (D6) |
| Play in the browser | **No**, if the game client runs on the player's machine | the player's own game client on a virtual screen, shown and driven from the player's console over the loopback. Keys never leave the machine except as the game's own traffic through the party |
| "The server sees everything" | **Partly.** The host already sees every lease, node, party and event. A player's screen is not the host's to see | the host sees the *game* (as a spectator, with consent), never a player's machine or screen |

## One page, two daemons

The page of #89 stays one page. What changes is which daemon serves it and which role the daemon gives.

| | Host mode | Player mode |
| :--- | :--- | :--- |
| Runs on | the host, `mesh host console --web` (v0.4.0) | the player's machine, `edge-mesh console` (new, #100) |
| Listens on | the loopback only; from elsewhere an SSH tunnel | the loopback only. A player has no reason for a tunnel: the browser is on that machine |
| Way in | a token in the link's fragment, only its hash stored (C05) | the same code, the same rules |
| Authority | the engine (`poc/mesh host ...`), role `console` | the player's kubeconfig over Nebula: their namespace, nothing else (the API answers 403 outside it) |
| Reads | status, queue, leases, players, parties, events, audit; new: activity, metrics, pool, page state | the player's lease and the time left, the node's state, their consents and what the host says of each (`edge-mesh party list`), the games they have a party for |
| Mutations | the registry of v0.4.0 | `party invite`, `party accept`, `party leave` (today's `edge-mesh party`), and two **new** verbs, `play` and `watch` (#101, #102) |
| Never | the four CA verbs and `init`, `down` (shown disabled); `console url`, `console rotate-token` (not shown); the lighthouse and page-server scripts | anything of the host; anything of another player; the bundle's private key in any answer |
| Runs as | the host's user, next to the state | **the player's own user, never root**: `edge-mesh join` runs as root and keeps `/etc/nebula/host.key` and `join.env` (the k3s token) root-only; player mode reads only a copy of the kubeconfig the join leaves readable by that user (0600), and nothing under `/etc/nebula` |

**The two daemons never talk to each other.** A player's console has no address of the host's console and no token for it; the host's
console still binds the loopback of the host. Player mode adds no listener on the host and nothing a player can reach that they could not
reach before.

**What a role cannot do is shown, not hidden.** `GET /actions` already says, per argument value, which roles may send it, and the page
disables the others (`(on the host only)`). #103 extends that to whole verbs: `GET /verbs` serves the full list with `allowed`, `why` and `closed_values` per verb, and
the page draws a disabled button with the reason and, in host mode, the command to type on the host. The check stays in the daemon: a
disabled button is a courtesy, the 403 is the rule.

## Host mode: every verb, and why four stay on the host

The verb table of CONSOLE.md already has a row per `mesh` verb. #103 makes the page show all of them.

| Verb | In the web today | Proposed | Why |
| :--- | :--- | :--- | :--- |
| request, reject, approve (public), tick, auto on/off, panic, panic-clear, burn, reap, kick, revoke, party reconcile/end | yes | unchanged | |
| `activity`, `admission-metrics`, the pool, the public page open/closed | no (CLI only) | **new read views** | reads; redacted like the others |
| `approve --trust friends`, `invite`, `bundle`, `refresh` | no | **shown disabled**, with the command | each makes the persistent CA sign, or writes a private key or a running machine's config. CONSOLE.md: "no request from the web can make it sign". A stolen web token must not mint a member |
| `init`, `down` | no | **shown disabled** | the daemon lives in the state they create and delete |
| `console url`, `console rotate-token` | no | **not shown** | a stolen token must not mint the next one |
| start and stop the game server | does not exist | **new verbs** (#102): `game-stop <owner>` in the registry, confirm by typing the owner's name, one audit line | the server is a pod in the hosting player's namespace. Stopping it is a new write by the host into a player's namespace, so it is a confirmed, audited verb and the owner sees it in their events. Starting one is that player's, from player mode |

**Decision asked (D1):** keep the four CA verbs out of the web. The alternative is a second factor typed on the host for each signature,
which is the CLI with more steps.

## Watching: a party, a spectator, a view per player

Today the deathmatch (poc/doom, v0.3.0) is an Odamex server in the hosting player's namespace, reached only through a party with that
player; `poc/doom/client.sh` is a player on a virtual screen (Xvfb, 320x200) that can already record its picture (ffmpeg). An Odamex
client enters as a **spectator** and stays one until it types `join` (client.sh does exactly that). That a spectator can follow one
player's eyes and change whom is Odamex's documented behaviour, **to be confirmed in the spike** with this build.

- **A view is a spectator client.** One per watched player, each on its own virtual screen, each following one player. Where it runs is
  not free: a bare process on the watcher's machine is not admitted by the party NetworkPolicy (it admits pods of `play-<peer>`), so the
  spectator is a **pod in the watcher's namespace, pinned to the watcher's own node**, like the robots of poc-doom. Its frames then have to
  reach the watcher's console on that machine, and there are two ways, neither free (**D7**):
  - `kubectl port-forward` to the pod: works under PSS restricted, but the frames go through the API server, that is through the host,
    over Nebula. For the host's own views that is the host's machine anyway; for a player's views it means the host relays (and could
    read) the frames of a game the host has no party with.
  - a `hostPort` on the watcher's node, bound to the loopback: no relay, but it needs an exception to PSS restricted for that pod and a
    rule that the port is never on a Nebula or LAN address.

  Either way only the game's own UDP crosses the party; what differs is the path of the picture, and threat 9 names it.
- **Watching is a party.** A spectator is a client of the server, and the server is reachable only through a party both sides accepted.
  So a player watches the games they could play. No new rule, no bypass: the NetworkPolicies of parties already enforce it (poc-doom DM
  steps prove a third player connects nothing).
- **The host is not in the party model.** `poc/party.py` admits only pods of a `play-<peer>` namespace labelled for the owner, the host
  has no such namespace (`mesh host invite` refuses `--trust own`), and a process on the control plane is a remote node for the server
  pod, so default-deny drops it. For the host to watch "through a party" the model needs a watcher identity for the host: for example a
  `watch-host` namespace on the host's own node that the owner can accept like a peer, and a change to `party.py` to admit it. **D6**
  asks for that; D2 depends on it.
- **The watched see the watcher.** The server announces a client that connects and the scoreboard lists spectators (**to be confirmed in
  the spike**; if this build does not, the watcher's daemon says it in the game's chat). Nobody is watched without a line saying who came in.
- **It is the game, not the machine.** A view shows what the game server sends about a player (position, what they see in the map). It
  is never a capture of that player's screen, desktop or keys.
- **Cost.** One game client per view: CPU on the watcher's side (a software renderer at 320x200), and the server's bandwidth per
  client. Four players watched are four clients. To be measured in the spike; the page offers one view at a time and tiles on request.

**Decisions asked (D2, D3):**
- D2: the host watches **through a party** (the server's owner accepts it once), not by its cluster-admin power. The golden rule says the
  owner of a node can see what runs on it, which here is the hosting player, not the host; the host's sight comes from being cluster-admin.
  Asking keeps "pairwise, by consent" true for the one case where the host is a participant. Needs D6.
- D3: the join text and JOIN.md say that a game can be watched by whoever has a party with its server, and that spectators are announced.

## Playing in the browser

The browser cannot be the game: Odamex speaks UDP, a tab cannot, and a Doom built for the browser would not speak Odamex's protocol
without a proxy that turns WebSockets into UDP (a new network path into a party: refused here).

So the game client stays a native program on a virtual screen, exactly as the robots run today: a pod in the player's namespace, pinned to
the player's own node (the party admits nothing else). The player's console shows that screen in the tab and sends the tab's keys and
mouse back. Between the pod and the console the path is the one of D7 (port-forward through the host, or a loopback `hostPort`), and for
playing the port-forward also adds the host's round trip to every key: the spike measures it.

| Way to carry the screen and the input | For | Against |
| :--- | :--- | :--- |
| **A. Our own**: frames as MJPEG or PNG over HTTP, input as small POSTs, injected with `xdotool` (already in the image) into the pod's virtual display only | no third-party code; reuses C06 (no inline script, no CDN) | latency of a POST per key; mouse look needs care; audio is another stream |
| **B. VNC in the container and a vendored, pinned noVNC** in the page | solved problem: keyboard, mouse, resize | a large third-party script in a page that has none today (the public page already vendors one, the asciinema player: the precedent exists); no audio |
| **C. WebRTC** (a media server in the container) | low latency, audio | the heaviest by far; UDP ports and a signalling path to reason about |

**Proposed:** A for **watching** (no input: an `<img>` that the daemon keeps feeding is enough), and a **spike** that plays one match
with A and one with B on a player's machine before choosing for **playing**. C is out of this milestone. Sound is out of the first
version and said so.

**Decision asked (D4):** accept a vendored noVNC if the spike says A is not playable, under the public page's rule (pinned version, hash
checked, no network at run time).

## What player mode needs from a bundle that it does not have today

- The kubeconfig is already in the bundle; `edge-mesh kubectl` already maps the API's refusals to messages (DESIGN.md 14.1). The daemon
  reuses both.
- "Time left" is already on the player's machine: the kubeconfig's token is a TokenRequest token made for exactly the lease
  (`poc/participant.py`), so its `exp` claim is the lease's end, and `host.crt`'s not-after says the same for the certificate. Player mode
  decodes the token locally (no call, no signature check needed for a countdown) and shows *minutes left*; a revoke ends it early, and
  the API's 401 says so (row 8 of the matrix). No new write for the engine. **D5** records that.
- The game image must be on the player's node to play or watch, and today it is not pulled from anywhere: `poc/doom/build.sh` builds it
  once (only its Debian base is pinned by digest), the suite loads it into the nodes, and the pods say `imagePullPolicy: Never`. How a
  remote player's node gets it is open: built on the player's machine by the same script (minutes, no new surface), or a registry (a new
  public surface, refused by threat 12 unless a later RFC says otherwise). Proposed: built locally, from the page, with the time shown
  first. Threat 13.

## Threats and their tests

In the style of CONSOLE.md. `PM..` are unit tests of the player daemon, `WB..` steps of the game suites (`P..` and `W..` are taken by
poc-lighthouse and poc-weakness); the issue that builds each is in brackets.

| # | Threat | What stops it | Test |
| :--- | :--- | :--- | :--- |
| 1 | a player's console reaches the host's console, or another player's | neither has the other's address or token; both bind the loopback only and refuse any other address at start | PM01 (the daemon refuses a non-loopback address), the K01 of v0.4.0 unchanged [#100] |
| 2 | player mode is a way around RBAC | it holds only the player's kubeconfig; every call is the API's to refuse | PM02: a read outside the namespace is a 403 shown as "outside your namespace" [#100] |
| 3 | the bundle's private key, the k3s token or the kubeconfig leak through the player's API | the same redaction as the host's; no endpoint returns a file of the bundle | PM03: no answer of the player daemon carries a PEM, a token or a path under `/etc/nebula`; the daemon runs as the player's user and cannot open `host.key` or `join.env` [#100] |
| 4 | a disabled button is the only thing between a role and a verb | the daemon checks the role; the page is not trusted | PM04, and C-tests of v0.4.0 for the host: the verb POSTed by hand is 403 [#103] |
| 5 | the web makes the persistent CA sign | the four verbs are not in the `console` role (unchanged) | the C-tests of v0.4.0; a new one that the disabled verbs are 403 [#103] |
| 6 | someone watches a game without a party | a view is a game client: no party, no path (NetworkPolicy) | WB01: a watcher with no party gets no frame (as DM's "a third player connects nothing") [#101] |
| 7 | someone is watched without knowing | the server announces spectators; the join text says games can be watched | WB02: the server log and a player's log name the spectator [#101] |
| 8 | a view shows more than the game (a screen, a desktop) | the frames come from the watcher's own spectator client, never from the watched machine | WB03: the watched player's node runs no new pod and no new listener [#101] |
| 9 | the picture or the keys leave the machine | between the tab and the local daemon, the loopback; between the pod and the daemon, the path of D7 (a port-forward goes through the host's API server, a `hostPort` stays on the node's loopback); the party carries only the game's UDP | WB04: no new listener on a Nebula or LAN address, and, if D7 is the port-forward, the frames of a player's view are named as passing through the host [#101, #102] |
| 10 | the page pulls code from the network | C06 unchanged; a vendored viewer is pinned and hashed | C06 extended to the new files [#102] |
| 11 | many views exhaust the server or the watcher | a cap on views per console; spectators count against the server's client limit | WB05 [#101] |
| 12 | a new public surface | none is added: the only change near the page server is the join text of D3 (`poc/page/page.py`) | the privacy gate of the public page, unchanged |
| 13 | the game image comes from somewhere new | built on the player's machine by `poc/doom/build.sh`; no registry | WB06: the player's node pulls nothing for the game pod (`imagePullPolicy: Never` kept) [#102] |
| 14 | the page's input reaches the player's real screen | the daemon injects only into the pod's virtual display (`DISPLAY=:99` fixed in the pod); it has no access to the player's own display | WB07: a key sent from the tab changes nothing outside the pod [#102] |
| 15 | a web page in the player's browser drives the player daemon (DNS rebinding, a cross-site POST) | the C03 check of v0.4.0 (Host and Origin must be the loopback address and port) in the player daemon too | PM05 [#100] |

## The pieces, in order

1. **#99** this RFC, decided.
2. **#103** host mode: `GET /verbs` (each verb, allowed or not for the caller, with why), disabled verbs on the page, the new read views.
3. **#100** player mode: `edge-mesh console`, its reads and the party verbs, on the same page.
4. **Spike** (inside #101): one spectator view of a live `poc-doom` match in a tab, both paths of D7; the numbers (CPU, bandwidth, frames
   per second, the latency of a key through the port-forward).
5. **#101** watching: the host's watcher identity (D6), views, the cap, the announcements, WB01 to WB05.
6. **#102** playing: the spike of A against B, then the chosen one; start and stop of the game server.

## Decisions asked

| # | Question | Proposed |
| :--- | :--- | :--- |
| D1 | Do the four CA verbs enter the web console? | **No.** Shown disabled with the command to type on the host |
| D2 | Does the host watch through a party, or by its admin power? | **Through a party**, accepted once by the server's owner |
| D3 | Do the join text and JOIN.md say that games can be watched and by whom? | **Yes**, before any view ships |
| D4 | May the page vendor a pinned noVNC if our own input path is not playable? | **Yes**, after the spike, under the public page's rule |
| D5 | Does player mode count down from the token's own expiry, with no new write by the engine? | **Yes**: the token's `exp` is the lease's end |
| D6 | Does the host get a watcher identity in the party model (a `watch-host` namespace the owner can accept, admitted by `party.py`)? | **Yes**, built in #101 before any host view; without it D2 cannot hold |
| D7 | How do a spectator's frames reach the watcher's console: port-forward through the host, or a loopback `hostPort` with a PSS exception? | **Decided after the spike**, with the numbers and threat 9's wording |

## Out of this milestone

Sound; WebRTC; a game built for the browser; watching from the public page; a player's console reached from another machine; any game
other than the deathmatch of poc/doom.
