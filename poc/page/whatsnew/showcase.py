#!/usr/bin/env python3
"""The recordings of "What's new" (docs/PUBLIC-PAGE.md, issue #63): real runs turned into asciicast v2 files, by script only.

    showcase.py stamp RAW SHOWN [--timed]   stdin -> RAW: the shown command, then each line with its time (--timed: lines are "epoch<TAB>text")
    showcase.py note RAW TEXT               one line of the story (a chapter title) at this time
    showcase.py redact RAW TSV [--strip P]  the filter: RAW (a run's screen, may hold secrets) -> TSV (committed), P removed from paths
    showcase.py from-text TXT DIR           a text transcript with UTC times (the AWS run) -> DIR/<stem>-player.tsv and DIR/<stem>-host.tsv
    showcase.py cast TSV CAST TITLE         TSV -> an asciicast v2 file; same TSV, same bytes
    showcase.py build DIR                   DIR/rec/<stem>.txt -> TSV, then every DIR/rec/<name>.tsv -> DIR/<name>.cast (titles: DIR.json)
    showcase.py scan FILE...                exit 1 and name the line when a file holds what the filter removes (a leak)

A TSV line is "<seconds>\\t<kind>\\t<text>", kind cmd | out | note. Nothing here is edited by hand: `make showcase` records a real
`poc-page` run and builds the casts; tests in poc/test_whatsnew.py rebuild them and scan every committed file.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
import time
from pathlib import Path

ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "10.77.0.0/24",                                                      # the game's Nebula mesh (stack.env MESH_CIDR)
    "10.42.0.0/16", "10.43.0.0/16",                                      # k3s pods and services
    "127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]   # loopback, any, documentation
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]*\d)")
# what may never reach a recording: (name, pattern, replacement). The order matters: the longest shapes first.
SECRETS = [
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(-----END [A-Z0-9 ]+-----|$)"), "<pem>"),
    ("k3s-token", re.compile(r"K10[0-9a-f]{8,}(::[\w.:-]+)?"), "<k3s-token>"),
    ("jwt", re.compile(r"eyJ[\w-]{8,}(\.[\w-]*){0,2}"), "<jwt>"),
    ("bearer", re.compile(r"(?i)(bearer|token[=:])\s*[\w.~+/=-]{8,}"), r"\1 <token>"),
    ("claim-code", re.compile(r"\b(?!X{5}-X{5})[A-Z2-7]{5}(-[A-Z2-7]{1,5}){3,}\b"), "<claim-code>"),   # XXXXX-XXXXX-...: already masked
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    ("fingerprint", re.compile(r"\b[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){7,}\b"), "<fingerprint>"),
    ("hex", re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<hex>"),
    ("base64", re.compile(r"(?<![\w+/-])(?=[\w+/-]*[A-Z])(?=[\w+/-]*[a-z])(?=[\w+/-]*\d)[\w+/-]{40,}={0,2}"), "<base64>"),   # mixed: not a path
    ("ipv6", re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}(?![\w:])"), "<ipv6>"),
    ("nebula-index", re.compile(r"((?:local|remote|initiator|responder)Index)=\d+"), r"\1=<n>"),
]
GAP, IDLE = 2.0, 60.0   # in a cast a pause longer than GAP plays as GAP; one longer than IDLE also prints how long it really was
COLS, ROWS = 110, 30
PROMPT = {"player": "\x1b[1;32mplayer$\x1b[0m ", "host": "\x1b[1;36mhost$\x1b[0m "}


def public_ip(s: str) -> bool:
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    return not any(a in n for n in ALLOWED_NETS)


def redact_line(s: str, strip: list[str] = ()) -> str:
    for p in strip:
        s = s.replace(p, "")
    for _, rx, rep in SECRETS:
        s = rx.sub(rep, s)
    return IPV4.sub(lambda m: "<ip>" if public_ip(m.group(1)) else m.group(1), s)


def leaks(s: str) -> list[str]:
    """The names of the patterns found in s (the filter's own list, plus any address outside ALLOWED_NETS)."""
    found = [name for name, rx, _ in SECRETS if rx.search(s.replace("<", " ").replace(">", " "))]
    return found + [f"ip {m.group(1)}" for m in IPV4.finditer(s) if public_ip(m.group(1))]


def stamp(raw: str, shown: str, timed: bool) -> int:
    now, rows = time.time(), []
    for ln in sys.stdin:
        ln = ln.rstrip("\n")
        t, tab, text = ln.partition("\t") if timed else ("", "", ln)
        try:
            at = float(t) if tab else (rows[-1][0] if timed and rows else time.time())
        except ValueError:
            at, text = (rows[-1][0] if rows else now), ln
        rows.append((at, text if tab or not timed else ln))
    t0 = rows[0][0] - 0.5 if timed and rows else now
    with open(raw, "a") as f:
        f.write(f"{t0:.3f}\tcmd\t{shown}\n" + "".join(f"{t:.3f}\tout\t{x}\n" for t, x in rows))
    try:   # the caller may stop reading (grep -q): the screen is already written
        sys.stdout.write("".join(x + "\n" for _, x in rows))
        sys.stdout.flush()
    except BrokenPipeError:
        sys.stdout = None
    return 0


def read_tsv(path: str) -> list[tuple[float, str, str]]:
    out = []
    for ln in Path(path).read_text().splitlines():
        t, kind, text = ln.split("\t", 2)
        out.append((float(t), kind, text))
    return out


def write_tsv(rows, path: str) -> None:
    Path(path).write_text("".join(f"{t:.3f}\t{k}\t{x}\n" for t, k, x in rows))


def redact(raw: str, tsv: str, strip: list[str]) -> None:
    rows = sorted(read_tsv(raw), key=lambda r: r[0])   # stable: same time keeps the order written
    if not rows:
        raise SystemExit(f"{raw}: empty")
    t0 = rows[0][0]
    write_tsv([(t - t0, k, redact_line(x, strip)) for t, k, x in rows], tsv)


def from_text(txt: str, outdir: str, stem: str = "aws") -> None:
    """The AWS transcript: sections '==== N. VISITOR|HOST|Watching ... ====' go to the player or the host; a line that carries a time
    sets the clock, every other line comes 0.4 s after the one before; lines before the first section go to both."""
    sides: dict[str, list] = {"player": [], "host": []}
    clock, day, side = 0.0, None, None
    iso = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})Z")
    hms = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")
    for ln in Path(txt).read_text().splitlines():
        m, h = iso.search(ln), hms.match(ln)
        if m and (day is None or ln.strip().startswith(("2026", "## "))):
            t = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
            if day is None:   # the first time: the lines before it come just before it
                for rows in sides.values():
                    rows[:] = [(x + t - clock - 0.4, k, y) for x, k, y in rows]
                clock = t - 0.4
            day = day or m.group(1)
            clock = max(clock, t)
        elif h and day:
            clock = max(clock, int(h.group(1)) * 3600 + int(h.group(2)) * 60 + int(h.group(3)))
        else:
            clock += 0.4
        sec = re.match(r"^=+ \d+\. (\S+)", ln)
        if sec:
            side = "player" if sec.group(1) in ("VISITOR:", "AWS") else "host"
        kind = "note" if sec or side is None else ("cmd" if ln.startswith("$ ") else "out")
        text = ln[2:] if kind == "cmd" else ln
        for s in ([side] if side else ["player", "host"]):
            sides[s].append((clock, kind, redact_line(text)))
    for s, rows in sides.items():
        t0 = rows[0][0]
        write_tsv([(t - t0, k, x) for t, k, x in rows], f"{outdir}/{stem}-{s}.tsv")


def annotate(rows: list, ann: dict) -> list:
    """The notes and the shortening of annotations.json, applied to a screen: new 'note' rows before the lines they explain, and long
    runs of one kind of line kept to their first lines plus one that says how many were left out. The real lines are never edited."""
    out, seen, last_note = [], [], -10 ** 9
    rules = [(re.compile(r["match"]), re.compile(r["after"]) if r.get("after") else None, r["note"]) for r in ann.get("notes", [])]
    for t, kind, text in rows:
        for rx, after, note in rules:
            if rx.search(text):
                if (after is None or any(after.search(x) for x in seen)) and len(out) - last_note > ann.get("window", 3):
                    out.append((t, "note", note))
                    last_note = len(out) - 1
                break
        out.append((t, kind, text))
        seen.append(text)
    for c in ann.get("collapse", []):
        rx, kept, i = re.compile(c["match"]), [], 0
        while i < len(out):
            j = i
            while j < len(out) and out[j][1] == "out" and rx.search(out[j][2]):
                j += 1
            if j - i > c["min"]:
                kept += out[i:i + c["keep"]] + [(out[i + c["keep"]][0], "out", c["say"].format(n=j - i - c["keep"]))]
                i = j
            else:
                kept.append(out[i])
                i += 1
        out = kept
    return out


def cast(tsv: str, out: str, title: str, ann: dict | None = None) -> None:
    side = "host" if "host" in Path(tsv).stem else "player"
    ev, clock, last = [], 0.0, None
    for t, kind, text in annotate(read_tsv(tsv), ann or {}):
        if last is not None:
            gap = t - last
            clock += min(max(gap, 0.0), GAP)
            if gap > IDLE:
                ev.append((clock, f"\x1b[2m   ... {int(gap // 60)} min {int(gap % 60)} s later\x1b[0m\r\n"))
                clock += 0.5
        last = t
        if kind == "cmd":
            ev.append((clock, PROMPT[side]))
            for i, ch in enumerate(text):   # typed, 25 ms a key
                ev.append((clock + 0.3 + i * 0.025, ch))
            clock += 0.3 + len(text) * 0.025 + 0.3
            ev.append((clock, "\r\n"))
        elif kind == "note":
            ev.append((clock, f"\x1b[1;33m# {text}\x1b[0m\r\n" if text.strip() else "\r\n"))
        else:
            ev.append((clock, text + "\r\n"))
    head = {"version": 2, "width": COLS, "height": ROWS, "title": title, "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}
    body = "".join(json.dumps([round(t, 3), "o", d]) + "\n" for t, d in ev)
    Path(out).write_text(json.dumps(head, sort_keys=True) + "\n" + body)


def build(d: str) -> None:
    rel = Path(d)
    data = json.loads((rel.parent / f"{rel.name}.json").read_text())
    titles = {c["file"]: c["title"] for c in data.get("casts", [])}
    notes = rel.parent / "annotations.json"   # the expected errors and long runs, for every release (docs/PUBLIC-PAGE.md)
    ann = json.loads(notes.read_text()) if notes.is_file() else {}
    for txt in sorted((rel / "rec").glob("*.txt")):   # a text transcript (the AWS run): first its two TSV files
        from_text(str(txt), str(rel / "rec"), txt.stem)
    for tsv in sorted((rel / "rec").glob("*.tsv")):
        name = f"{tsv.stem}.cast"
        cast(str(tsv), str(rel / name), titles.get(name, tsv.stem), ann)
        print(f"cast: {rel / name}")


def scan(files: list[str]) -> int:
    bad = 0
    for f in files:
        for n, ln in enumerate(Path(f).read_text().splitlines(), 1):
            text = ln
            if f.endswith(".cast") and n > 1:
                text = json.loads(ln)[2]
            for what in leaks(text):
                print(f"LEAK {f}:{n}: {what}")
                bad += 1
    return 1 if bad else 0


def main(a: list[str]) -> int:
    cmd = a[0] if a else ""
    if cmd == "stamp":
        return stamp(a[1], a[2], "--timed" in a)
    if cmd == "note":
        with open(a[1], "a") as f:
            f.write(f"{time.time():.3f}\tnote\t{a[2]}\n")
        return 0
    if cmd == "redact":
        redact(a[1], a[2], [a[i + 1] for i, x in enumerate(a) if x == "--strip"])
        return scan([a[2]])
    if cmd == "from-text":
        from_text(a[1], a[2], Path(a[1]).stem)
        return 0
    if cmd == "cast":
        cast(a[1], a[2], a[3])
        return 0
    if cmd == "build":
        build(a[1])
        return 0
    if cmd == "scan":
        return scan(a[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
