# The panel: what the proofs of concept are doing, live

Issue [#119](https://github.com/villadalmine/k8s-lan-party/issues/119), milestone v0.8.0. The owner asked for a read-only dashboard next to
the live match, with real data only ("que sea de verdad, no quiero maquetar nada falso"), that shows the platform under the game so it is
"not just a game", and the same panel when someone runs the project on their own, limited by a profile or a config of theirs.

## What it shows

The live page (`/live`) has a section **What is running right now**: groups of numbers, each next to the idea it shows. The idea's text is
the feature's learn block of [FEATURES.yml](FEATURES.yml) (concept, analogy, where it is used for real), the same text as
[LEARN.md](LEARN.md).

| Group | Fields | Feature |
| :--- | :--- | :--- |
| The mesh | `nodes_total`, `nodes_ready`, `player_nodes` | `join`, `pool` |
| Encryption between pods | `wireguard`, `node_encryption`, `mutual_auth` | `encrypted-pod-network`, `mutual-auth` |
| The pool | `player_namespaces`, `player_pods_running`, `pods_running` | `pool`, `placement` |
| What the machines are doing | `cpu_used_millicores`, `cpu_total_millicores`, `memory_used_mib`, `memory_total_mib` | `placement` |
| Parties | `parties_active` (pairs of players), `parties_waiting` | `party` |
| Games | `games_running`, `house_open`, `house_players` | `doom`, `live-page` |
| Admission, since the host began | `admitted_total`, `expelled_total`, `refused_total` | `admission-auto` |

The one list is `poc/page/panelspec.py`: the host sends only these fields and the page accepts only these, each an integer or a yes/no.

## Real numbers or nothing

- Every value is read from the cluster when the puller runs (`mesh host panel`, `poc/panel.py`): `kubectl get` of nodes, pods, namespaces
  and `cilium-config`; the kubelets' own `stats/summary` through the API server (k3s here runs without metrics-server); and the host's
  read-only reads (`party list --read-only --json`, `admission-metrics --read-only`). Nothing is written.
- The whole read has a budget of 45 s (`--budget`): a read that would pass it is not started. With `publish: no` the puller reads
  nothing at all (`--if-published`), so the panel never slows the puller's report.
- The encryption fields come from `cilium-config`, the configuration Cilium runs with; no such ConfigMap means no Cilium (flannel), so
  "no"; a failed read of it is not a "no", it leaves them out.
- A read that fails leaves its fields out, and the page shows them as **not live**. CPU in use is left out if one ready node does not
  answer: a sum without it would be wrong.
- The page shows the panel only while the host reports (the public page's 5 minutes). A silent host shows **nothing**, never its last
  numbers. A report without a panel clears the last one.

## Counts and states only

No field is a name, an address, a key or a token, and none is a string: a string could carry a name. The page checks every report against
the allowlist and refuses a report with anything else (400 `bad-report`). This adds **no public write and no credential**: the panel
travels inside the report the puller already sends, with the puller's token, outbound from the host
([PUBLIC-PAGE.md](PUBLIC-PAGE.md)). The page has one new read, `GET /api/v1/panel`, and the visitor's browser draws it with text nodes
only.

## Profiles: where it runs decides what it shows

`poc/panel.yaml` (or `PANEL_CONFIG`) is the operator's choice:

```yaml
profile: public        # public | self-hosted | host
off: []                # fields to leave out, e.g. [cpu_used_millicores, memory_used_mib]
publish: no            # yes: the puller sends it to the public page
```

- A profile is an allowlist; `off` can only take fields away. Nothing in the config adds a field.
- An unknown key, profile or field is refused (`panel.py check`, exit 2): a typo never shows more than was meant.
- **`publish` is `no` in the repository.** The host turns it on. The `host` profile is never published (the config refuses it, and the
  puller drops it).
- Today the three profiles allow the same fields, all safe for the public. The profiles are the place where host-only fields go later
  (for example per-player rows in the host's own console), without a change of the page's rules.

The host console reads the same panel: `GET /api/v1/panel` ([CONSOLE.md](CONSOLE.md)), and a bot can read it as in
[CONSOLE-BOTS.md](CONSOLE-BOTS.md).

## Threats and their tests

| # | Threat | Answer | Test |
| :- | :--- | :--- | :--- |
| PN01 | the panel leaks a player's name or address | integers and yes/no only, from a fixed list; the page refuses anything else | `poc/test_panel.py` |
| PN02 | a stolen puller token puts lies or a script on the page | it can send only those numbers; the browser writes text nodes only (no HTML) | `poc/test_panel.py` |
| PN03 | old numbers shown as live | shown only while the host reports; a report without a panel clears it | `poc/test_panel.py` |
| PN04 | the panel changes the cluster | `kubectl get`, `stats/summary` and `--read-only` reads only | `poc/test_panel.py` (the fake refuses anything else) |
| PN05 | a config shows more than meant | profiles are allowlists, `off` only narrows, unknown names are refused, `host` is never published | `poc/test_panel.py` |
| PN06 | a value is guessed | a failed read leaves its field out; the page says "not live" | `poc/test_panel.py` |

Known gap: DM39 proves `mesh host panel` on a real cluster; the path puller, report, page and `/live` is proven by `poc/test_panel.py`
against the real page code, not on the lab (poc-doom runs no puller).
