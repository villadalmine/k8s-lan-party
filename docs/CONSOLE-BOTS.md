# Driving the host console from a bot

Your own automation (an infra agent, a chat bot, a cron job) uses **the same API the host console's web page uses**: the same routes, the
same checks, the same audit. Use it as it is: a bot needs no script of its own on the host and no `mesh` in a shell. This page shows the
calls, with the live page's switch (#119) as the worked example: see whether the house match is on the air, take it off, put it back.

**Off the air is not down.** `live-off` only stops the house match from reaching the public page (the page says offline). The match, the
players, their games and the whole demo keep running. Removing things is `mesh host house down` or `mesh host down`, typed on the host;
neither is in the API.

## How a bot reaches the console

The console is **never public** ([CONSOLE.md](CONSOLE.md)). A bot reaches it one of two ways:

| Where the bot runs | How | Role | What it may run |
| :--- | :--- | :--- | :--- |
| on the host, as the same user | the unix socket `$STATE/console/api.sock` (`curl --unix-socket`) | `local` | the whole registry |
| elsewhere | an SSH tunnel to the host's loopback port (same port both ends), `Authorization: Bearer <token>` | `console` | the registry minus the local-only verbs |

**The token.** The web listener's bearer token comes from `mesh host console url` on the host (printed once, in a link's `#t=` fragment).
Treat it like a password:
- keep it in the bot's secret store, never in a prompt, a chat, a log or a model's output;
- it is the same token as the web page's; `mesh host console rotate-token` on the host kills it at once;
- 10 wrong tokens in 10 minutes lock the web listener for 10 minutes.

A narrower role for bots is #50's decision. Until then, a bot that only reads or switches the live page still holds a `console` token.

## Read

```sh
B=http://127.0.0.1:8090/api/v1            # through the tunnel; on the host, the socket is: curl --unix-socket "$STATE/console/api.sock" http://console/api/v1/...
H="Authorization: Bearer $CONSOLE_TOKEN"  # from the bot's secret store
curl -fsS -H "$H" "$B/health"             # {"ok": true, "role": "console", "version": ...}
curl -fsS -H "$H" "$B/live"               # {"switch": "on", "streaming": true}
curl -fsS -H "$H" "$B/panel"              # {"up": true, "fields": {"nodes_total": 3, ...}}: counts only (PANEL.md)
curl -fsS -H "$H" "$B/status"             # places, waiting, auto, panic
curl -fsS -H "$H" "$B/page"               # the public page as the puller last saw it
curl -fsS -H "$H" "$B/verbs"              # every verb, whether this role may run it, and why not
```

Every read is a pure read on the host: it changes nothing and is safe to poll (keep it to one call every few seconds).

## Act: a verb without a confirmation (`live-off`)

A `POST` answers **202 with a job** (`queued`, then `running`); poll it until it is `done`, `failed` or `timeout`.

```sh
curl -fsS -H "$H" -H 'Content-Type: application/json' -d '{"args": {}}' "$B/actions/live-off"
# {"id": "0123456789abcdef", "verb": "live-off", "state": "queued", ...}
curl -fsS -H "$H" "$B/jobs/0123456789abcdef"
# {"state": "done", "exit": 0, "output": "live: off - the page says offline; the house match and the demo keep running ..."}
```

## Act: a verb with a confirmation (`live-on`)

Verbs that need a person's yes (`click`, `type the name`) answer the first `POST` with **200 and a plan**: what the verb will do, and a
single-use `confirm` token valid for 60 seconds. Send it back to run the verb. A bot that confirms on its own is taking that decision.
It should do so only where its owner has said it may, and it should show the plan it accepted in its own log.

```sh
curl -fsS -H "$H" -H 'Content-Type: application/json' -d '{"args": {}}' "$B/actions/live-on"
# 200 {"verb": "live-on", "will": ["the house match goes back on the public page ...", "players in the house's parties are broadcast ..."],
#      "confirm": "<single use>", "expires_in": 60, "type_to_confirm": null}
curl -fsS -H "$H" -H 'Content-Type: application/json' -d '{"args": {}, "confirm": "<single use>"}' "$B/actions/live-on"
# 202 {"id": "...", "state": "queued"}  -> poll /jobs/{id}
```

A verb with `type_to_confirm` (for example `kick`) also needs `"typed": "<that value>"` in the second body. If the state changed between
the plan and the confirmation, the answer is **409 plan-changed**: ask for a new plan.

## Python

```python
import json, os, time, urllib.request

B = "http://127.0.0.1:8090/api/v1"
TOKEN = os.environ["CONSOLE_TOKEN"]          # from the bot's secret store, never printed

def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read() or b"null")

def run(verb, args=None, confirm=False):
    status, out = call("POST", f"/actions/{verb}", {"args": args or {}})
    if status == 200:                          # a plan: the verb wants a yes
        if not confirm:
            return {"plan": out["will"]}
        status, out = call("POST", f"/actions/{verb}", {"args": args or {}, "confirm": out["confirm"]})
    while out.get("state") in ("queued", "running"):
        time.sleep(2)
        _, out = call("GET", f"/jobs/{out['id']}")
    return out

print(call("GET", "/live")[1])                 # {'switch': 'on', 'streaming': True}
print(run("live-off")["state"])                # done
print(run("live-on"))                          # {'plan': [...]}: shown to its owner first
```

## What a bot cannot do through the API

- **Create or destroy:** `init`, `down`, `house init`, `house down`, `watch init` and `watch down` are not in the API: they are typed on
  the host only (`GET /verbs` lists them as `allowed: false`, with the command; a POST answers 404).
- **The persistent CA or a private key:** the local-only verbs (`invite`, `bundle`, `refresh`, `approve --trust friends`) need the socket,
  on the host.
- **Turn the live page on without a confirmation:** `live-on` always asks for the plan first.

Every job a bot starts is in the console's audit log (`GET /audit`), with `role: console` and `source: web`, like the page's. Reads and
the first POST of a confirmed verb (the plan) change nothing and leave no line.
