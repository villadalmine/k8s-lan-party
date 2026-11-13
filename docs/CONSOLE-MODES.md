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
| Host mode with every verb | **Yes, for four verbs** (`invite`, `bundle`, `refresh`, `approve --trust friends`: they sign with the persistent CA or hand out a private key). Not for the rest | every verb is **on the page**; those four are shown disabled with the command to type on the host. The reads that are missing (activity, metrics, pool, the public page's state) become views |
| Player mode | **No**, if it runs on the player's own machine with the player's own credentials | `edge-mesh console`: a daemon on the player's loopback that talks to the cluster with the kubeconfig the bundle already gave. It gets no new power: RBAC decides, as it does for `edge-mesh kubectl` today |
| Watch the game, a view per player | **No**, if watching is a party like any other | a spectator client of the game (Odamex has one) per watched player, next to the watcher, sending frames to the watcher's own console. The host watches through a party the server's owner accepted, and is seen in the game as a spectator |
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
| Mutations | the registry of v0.4.0 | `party invite`, `party accept`, `party leave`, `play`, `watch` |
| Never | the four CA verbs; `init`, `down`; the lighthouse and page-server scripts | anything of the host; anything of another player; the bundle's private key in any answer |

**The two daemons never talk to each other.** A player's console has no address of the host's console and no token for it; the host's
console still binds the loopback of the host. Player mode adds no listener on the host and nothing a player can reach that they could not
reach before.

**What a role cannot do is shown, not hidden.** `GET /actions` already says, per argument value, which roles may send it, and the page
disables the others (`(on the host only)`). #103 extends that to whole verbs: each daemon serves the full list with a `roles` field, and
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
| start and stop the game server | does not exist | **new verb**, with a plan (#102) | the server is a pod in the hosting player's namespace: the host may stop it (it is the host's cluster), starting one in a player's namespace is that player's, from player mode |

**Decision asked (D1):** keep the four CA verbs out of the web. The alternative is a second factor typed on the host for each signature,
which is the CLI with more steps.

## Watching: a party, a spectator, a view per player

Today the deathmatch (poc/doom, v0.3.0) is an Odamex server in the hosting player's namespace, reached only through a party with that
player; `poc/doom/client.sh` is a player on a virtual screen (Xvfb, 320x200) that can already record its picture (ffmpeg). An Odamex
client enters as a **spectator** and stays one until it types `join` (client.sh does exactly that). That a spectator can follow one
player's eyes and change whom is Odamex's documented behaviour, **to be confirmed in the spike** with this build.

- **A view is a spectator client.** One per watched player, each on its own virtual screen, each following one player. They run next to
  the watcher (the host's views on the host, a player's on the player's machine) and their frames go to the watcher's own console over
  the loopback. Nothing of the picture crosses the mesh: only the game's own UDP does, as for any player.
- **Watching is a party.** A spectator is a client of the server, and the server is reachable only through a party both sides accepted.
  So the host watches when the server's owner has a party with the host's watcher, and a player watches the games they could play. No
  new rule, no bypass: the NetworkPolicies of parties already enforce it (poc-doom DM steps prove a third player connects nothing).
- **The watched see the watcher.** The server announces a client that connects and the scoreboard lists spectators (**to be confirmed in
  the spike**; if this build does not, the watcher's daemon says it in the game's chat). Nobody is watched without a line saying who came in.
- **It is the game, not the machine.** A view shows what the game server sends about a player (position, what they see in the map). It
  is never a capture of that player's screen, desktop or keys.
- **Cost.** One game client per view: CPU on the watcher's side (a software renderer at 320x200), and the server's bandwidth per
  client. Four players watched are four clients. To be measured in the spike; the page offers one view at a time and tiles on request.

**Decisions asked (D2, D3):**
- D2: the host watches **through a party** (the server's owner accepts it once), not by its cluster-admin power. The golden rule already
  tells players that the owner of the machine can see what runs on it, so the host *could* look; asking keeps "pairwise, by consent" true
  for the one case where the host is a participant.
- D3: the join text and JOIN.md say that a game can be watched by whoever has a party with its server, and that spectators are announced.

## Playing in the browser

The browser cannot be the game: Odamex speaks UDP, a tab cannot, and a Doom built for the browser would not speak Odamex's protocol
without a proxy that turns WebSockets into UDP (a new network path into a party: refused here).

So the game client stays a native process, on the **player's own machine**, on a virtual screen, exactly as the robots run today, and the
player's console shows that screen in the tab and sends the tab's keys and mouse back, all over the loopback.

| Way to carry the screen and the input | For | Against |
| :--- | :--- | :--- |
| **A. Our own**: frames as MJPEG or PNG over HTTP, input as small POSTs, injected with `xdotool` (already in the image) | no third-party code; reuses C06 (no inline script, no CDN) | latency of a POST per key; mouse look needs care; audio is another stream |
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
- "Time left" is not in the bundle: the lease is the host's record. The player's token dies with the lease (row 8 of the matrix), so the
  daemon can say *ended*, not *minutes left*. Either the host writes the lease's end into a ConfigMap in the player's namespace at
  approve (a new write for the engine, readable only by that player), or player mode shows no countdown. **Decision asked (D5).**
- The game image must be on the player's machine to play or watch. It is a public image pinned by digest (doom.env); pulling it is the
  player's choice, from the page, with its size shown first.

## Threats and their tests

In the style of CONSOLE.md. `P..` are unit tests of the player daemon, `W..` steps of the game suites; the issue that builds each is in
brackets.

| # | Threat | What stops it | Test |
| :--- | :--- | :--- | :--- |
| 1 | a player's console reaches the host's console, or another player's | neither has the other's address or token; both bind the loopback only and refuse any other address at start | P01 (the daemon refuses a non-loopback address), the K01 of v0.4.0 unchanged [#100] |
| 2 | player mode is a way around RBAC | it holds only the player's kubeconfig; every call is the API's to refuse | P02: a read outside the namespace is a 403 shown as "outside your namespace" [#100] |
| 3 | the bundle's private key, the k3s token or the kubeconfig leak through the player's API | the same redaction as the host's; no endpoint returns a file of the bundle | P03 [#100] |
| 4 | a disabled button is the only thing between a role and a verb | the daemon checks the role; the page is not trusted | P04, and C-tests of v0.4.0 for the host: the verb POSTed by hand is 403 [#103] |
| 5 | the web makes the persistent CA sign | the four verbs are not in the `console` role (unchanged) | the C-tests of v0.4.0; a new one that the disabled verbs are 403 [#103] |
| 6 | someone watches a game without a party | a view is a game client: no party, no path (NetworkPolicy) | W01: a watcher with no party gets no frame (as DM's "a third player connects nothing") [#101] |
| 7 | someone is watched without knowing | the server announces spectators; the join text says games can be watched | W02: the server log and a player's log name the spectator [#101] |
| 8 | a view shows more than the game (a screen, a desktop) | the frames come from the watcher's own spectator client, never from the watched machine | by construction; W03: the watched machine runs nothing new [#101] |
| 9 | the picture or the keys leave the machine | frames and input travel on the loopback, between the tab and the local daemon; the mesh carries only the game's UDP | W04: no new listener on a Nebula or LAN address [#102] |
| 10 | the page pulls code from the network | C06 unchanged; a vendored viewer is pinned and hashed | C06 extended to the new files [#102] |
| 11 | many views exhaust the server or the watcher | a cap on views per console; spectators count against the server's client limit | W05 [#101] |
| 12 | a new public surface | none is added: nothing here touches the page server | the privacy gate of the public page, unchanged |

## The pieces, in order

1. **#99** this RFC, decided.
2. **#103** host mode: the role per verb in `/actions`, disabled verbs on the page, the new read views.
3. **#100** player mode: `edge-mesh console`, its reads and the party verbs, on the same page.
4. **Spike** (inside #101): one spectator view of a live `poc-doom` match in a tab; the numbers (CPU, bandwidth, frames per second).
5. **#101** watching: views, the cap, the announcements, W01 to W05.
6. **#102** playing: the spike of A against B, then the chosen one; start and stop of the game server.

## Decisions asked

| # | Question | Proposed |
| :--- | :--- | :--- |
| D1 | Do the four CA verbs enter the web console? | **No.** Shown disabled with the command to type on the host |
| D2 | Does the host watch through a party, or by its admin power? | **Through a party**, accepted once by the server's owner |
| D3 | Do the join text and JOIN.md say that games can be watched and by whom? | **Yes**, before any view ships |
| D4 | May the page vendor a pinned noVNC if our own input path is not playable? | **Yes**, after the spike, under the public page's rule |
| D5 | Does the host publish each player's lease end in that player's namespace, so player mode can count down? | **Yes**: one ConfigMap written at approve and removed at the lease's end |

## Out of this milestone

Sound; WebRTC; a game built for the browser; watching from the public page; a player's console reached from another machine; any game
other than the deathmatch of poc/doom.
