"""The "What's new" section of the public page (issue #63; how to add a release: CONTRIBUTING.md "What's new"): one JSON file per
release in poc/page/whatsnew/, its recordings next to it. Built once when the page starts: no state, no request ever reaches a file name.

    whatsnew/<version>.json        version, date, title, release, summary, issues; the newest ones also story, casts, features
    whatsnew/<version>/<name>.cast the recordings (poc/page/whatsnew/showcase.py, from real runs only)
    whatsnew/<version>/rec/        their filtered sources; never installed on the server, never served
The page loads with strict=False (a recording that `make showcase` has not made yet is left out); poc/test_whatsnew.py with strict=True.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

REPO = "https://github.com/villadalmine/k8s-lan-party"
VERSION_RE = re.compile(r"v\d+\.\d+\.\d+(-[a-z0-9]+)?")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}\.cast")
# the player runs a terminal emulator compiled to WebAssembly: only this page lets the browser compile it (no eval of JavaScript)
CSP = ("default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")


class NewsError(ValueError):
    pass


def _text(f: Path, obj: dict, key: str, where: str = "", optional: bool = False) -> None:
    """A key the HTML prints: a non-empty string (an optional one may be missing), or NewsError naming the file and the key."""
    if optional and key not in obj:
        return
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise NewsError(f"{f.name}: {where}{key!r} must be a non-empty string")


def load(d: Path, strict: bool = True) -> list[dict]:
    """Every release file, newest first, checked: a bad one stops the page from starting (and make test fails). strict=False (the
    page): a listed recording not made yet is left out, so the page of `make showcase` can run before its own recordings exist."""
    out = []
    for f in sorted(d.glob("v*.json")):
        r = json.loads(f.read_text())
        v = r.get("version", "")
        if not VERSION_RE.fullmatch(v) or f.stem != v:
            raise NewsError(f"{f.name}: version must be the file name, like v1.2.3-poc")
        if not r.get("release", "").startswith(f"{REPO}/releases/tag/{v}"):
            raise NewsError(f"{f.name}: release must be {REPO}/releases/tag/{v}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("date", "")) or not all(isinstance(i, int) for i in r.get("issues", [])):
            raise NewsError(f"{f.name}: date is YYYY-MM-DD, issues are numbers")
        for k in ("title", "summary"):
            _text(f, r, k)
        _text(f, r, "aws_title", optional=True)
        for side, s in (r.get("story") or {}).items():
            if side not in ("player", "host") or not isinstance(s, dict):
                raise NewsError(f"{f.name}: story: {side!r} must be player or host, with title and steps")
            _text(f, s, "title", f"story.{side}: ")
            if not isinstance(s.get("steps"), list) or not all(isinstance(x, str) and x.strip() for x in s["steps"]):
                raise NewsError(f"{f.name}: story.{side}: 'steps' must be a list of non-empty strings")
        for c in r.get("casts", []):
            for k in ("file", "title"):
                _text(f, c, k, "cast: ")
            for k in ("caption", "run"):
                _text(f, c, k, "cast: ", optional=True)
            if c.get("side") not in ("player", "host"):
                raise NewsError(f"{f.name}: cast {c.get('file')!r}: 'side' must be player or host")
            if not NAME_RE.fullmatch(c.get("file", "")):
                raise NewsError(f"{f.name}: cast {c.get('file')!r} is not a name like player.cast")
            if strict and not (d / v / c["file"]).is_file():
                raise NewsError(f"{f.name}: cast {c['file']!r} is not a file in whatsnew/{v}/ (make showcase RELEASE={v})")
        r["casts"] = [c for c in r.get("casts", []) if (d / v / c["file"]).is_file()]
        for c in r.get("features", []):
            for k in ("name", "what", "why", "command"):
                _text(f, c, k, f"feature {c.get('name')!r}: ")
            if not all(isinstance(i, int) for i in c.get("issues", [])):
                raise NewsError(f"{f.name}: feature {c.get('name')!r}: issues are numbers")
        out.append(r)
    known = {r["version"] for r in out}
    for r in out:
        if "recordings_from" in r and (r["recordings_from"] not in known or r["recordings_from"] == r["version"]):
            raise NewsError(f"{r['version']}.json: recordings_from must name another release in whatsnew/")
    return sorted(out, key=lambda r: (r["date"], [int(x) for x in re.findall(r"\d+", r["version"])]), reverse=True)


def casts(d: Path, releases: list[dict]) -> dict[str, bytes]:
    return {f"/whats-new/{r['version']}/{c['file']}": (d / r["version"] / c["file"]).read_bytes()
            for r in releases for c in r.get("casts", [])}


def _issues(nums) -> str:
    return " ".join(f'<a href="{REPO}/issues/{n}">#{n}</a>' for n in nums)


def _cast(r: dict, c: dict) -> str:
    e = html.escape
    return (f'<figure><div class=cast data-cast="/whats-new/{e(r["version"])}/{e(c["file"])}"></div>'
            f'<figcaption><b>{e(c["title"])}</b> {e(c.get("caption", ""))} '
            f'<a href="/whats-new/{e(r["version"])}/{e(c["file"])}">.cast</a></figcaption></figure>')


def _release(r: dict, by: dict) -> str:
    e, parts = html.escape, []
    parts.append(f'<section id="{e(r["version"])}"><h2>{e(r["version"])}: {e(r["title"])}</h2>'
                 f'<p class=meta>{e(r["date"])} &middot; <a href="{e(r["release"])}">release notes on GitHub</a> &middot; '
                 f'{_issues(r.get("issues", []))}</p><p>{e(r["summary"])}</p>')
    story = r.get("story") or {}
    if story:
        parts.append("<div class=sides>")
        for side in ("player", "host"):
            s = story.get(side) or {}
            steps = "".join(f"<li>{e(x)}</li>" for x in s.get("steps", []))
            screens = "".join(_cast(r, c) for c in r.get("casts", []) if c.get("side") == side and not c.get("run"))
            parts.append(f'<div class=side><h3>{e(s.get("title", side))}</h3><ol>{steps}</ol>{screens}</div>')
        parts.append("</div>")
    aws = [c for c in r.get("casts", []) if c.get("run")]
    if aws:
        parts.append(f'<h3>{e(r.get("aws_title", "The same game on a real cloud"))}</h3><div class=sides>'
                     + "".join(f"<div class=side>{_cast(r, c)}</div>" for c in aws) + "</div>")
    src = by.get(r.get("recordings_from", ""))
    if src:   # a release with no run of its own shows the recordings it changed, and says whose they are
        parts.append(f'<h3>The recordings of <a href="/whats-new/{e(src["version"])}">{e(src["version"])}</a>, rebuilt by this release</h3>'
                     '<div class=sides>' + "".join(f"<div class=side>{_cast(src, c)}</div>" for c in src.get("casts", [])) + "</div>")
    feats = r.get("features", [])
    if feats:
        parts.append("<h3>New power-ups</h3><div class=cards>")
        for f in feats:
            parts.append(f'<article class=card><h4>{e(f["name"])}</h4><p><b>What:</b> {e(f["what"])}</p><p><b>Why:</b> {e(f["why"])}</p>'
                         f'<pre>{e(f["command"])}</pre><p class=meta>{_issues(f.get("issues", []))}</p></article>')
        parts.append("</div>")
    return "".join(parts) + "</section>"


def _doc(title: str, body: str) -> str:
    return ('<!doctype html>\n<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><link rel=stylesheet href=/static/page.css>'
            '<link rel=stylesheet href=/static/asciinema-player.css></head><body><main class=wide>' + body
            + '</main><script src=/static/asciinema-player.min.js></script><script src=/static/whatsnew.js></script></body></html>\n')


def pages(releases: list[dict]) -> dict[str, str]:
    """/whats-new (the index: the newest release in full, then every release with a link to its page) and /whats-new/<version> for
    each one, with older / index / newer. The paths come from the checked versions (VERSION_RE), never from a request."""
    e, by = html.escape, {r["version"]: r for r in releases}
    rows = "".join(f'<tr><td><a href="/whats-new/{e(r["version"])}">{e(r["version"])}</a></td><td>{e(r["date"])}</td>'
                   f'<td>{e(r["title"])}</td><td><a href="{e(r["release"])}">GitHub</a></td><td>{_issues(r.get("issues", []))}</td></tr>'
                   for r in releases)
    table = ('<section id=history><h2>Every release</h2><table><tr><th>Page</th><th>Date</th><th>What</th><th>Notes</th><th>Issues</th></tr>'
             + rows + '</table></section>')
    out = {"/whats-new": _doc("k8s-lan-party: what's new", '<p><a href="/">&larr; ask for a place</a></p><h1>What\'s new</h1>'
                              '<p>Every release, told as a game: the player who lends a machine for an hour, and the host who runs the pool. '
                              'The recordings are real runs, played back here (no video, no tracker: plain text your browser replays). '
                              'Each release has its own page below.</p>' + (_release(releases[0], by) if releases else "") + table)}
    for i, r in enumerate(releases):
        newer, older = (releases[i - 1] if i else None), (releases[i + 1] if i + 1 < len(releases) else None)
        nav = " &middot; ".join(x for x in (
            f'<a href="/whats-new/{e(older["version"])}">&larr; {e(older["version"])}</a>' if older else "",
            '<a href="/whats-new">all releases</a>',
            f'<a href="/whats-new/{e(newer["version"])}">{e(newer["version"])} &rarr;</a>' if newer else "") if x)
        out[f"/whats-new/{r['version']}"] = _doc(f"k8s-lan-party {r['version']}", f'<nav class=meta>{nav}</nav>' + _release(r, by)
                                                 + f'<nav class=meta>{nav}</nav>')
    return out
