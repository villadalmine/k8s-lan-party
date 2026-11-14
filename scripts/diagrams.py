#!/usr/bin/env python3
"""docs/diagrams (issue #62): each <name>.mmd has a <name>.svg and .png carrying the source's SHA-256 (an SVG comment, a PNG tEXt chunk).
    scripts/diagrams.py check               every SVG and PNG carries the hash of its .mmd (Python only)
    scripts/diagrams.py render [NAME ...]   render with mermaid-cli (MMDC, default `mmdc`) and stamp the hash (all when no name)
A <name>.json next to a .mmd is that diagram's mermaid config (features.json: the ELK layout) and part of its hash. mermaid-cli 11.4.2 is
the version the committed images were rendered with (it ships the ELK layout); MMDC="npx -y @mermaid-js/mermaid-cli@11.4.2" pins it.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import struct
import subprocess
import sys
import zlib
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
KEY = b"source-sha256"
SVG_MARK = re.compile(r"\n?<!-- source-sha256: ([0-9a-f]{64}) -->\n?")


def source_hash(mmd: Path) -> str:
    """The .mmd, and the diagram's own mermaid config when it has one (<name>.json): a changed layout is a changed source too."""
    cfg = mmd.with_suffix(".json")
    return hashlib.sha256(mmd.read_bytes() + (b"\0" + cfg.read_bytes() if cfg.is_file() else b"")).hexdigest()


def chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    out, i = [], 8
    while i < len(png):
        n, kind = struct.unpack(">I4s", png[i:i + 8])
        out.append((kind, png[i + 8:i + 8 + n]))
        i += 12 + n
    return out


def png_hash(png: Path) -> str | None:
    return next((d[len(KEY) + 1:].decode() for k, d in chunks(png.read_bytes()) if k == b"tEXt" and d.startswith(KEY + b"\0")), None)


def svg_hash(svg: Path) -> str | None:
    m = SVG_MARK.search(svg.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def stamp(mmd: Path) -> None:
    """Write the source's hash into its SVG and PNG, replacing an older one."""
    h = source_hash(mmd)
    svg, png = mmd.with_suffix(".svg"), mmd.with_suffix(".png")
    svg.write_text(SVG_MARK.sub("", svg.read_text(encoding="utf-8")).rstrip("\n") + f"\n<!-- source-sha256: {h} -->\n", encoding="utf-8")
    raw = png.read_bytes()
    body = b"".join(struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d)) for k, d in chunks(raw)
                    if not (k == b"tEXt" and d.startswith(KEY + b"\0")))
    text = KEY + b"\0" + h.encode()
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text))
    ihdr_end = 8 + 12 + struct.unpack(">I", raw[8:12])[0]   # right after IHDR, which must come first
    png.write_bytes(raw[:8] + body[:ihdr_end - 8] + chunk + body[ihdr_end - 8:])


def stale(directory: Path = DIR) -> list[str]:
    problems = []
    for mmd in sorted(directory.glob("*.mmd")):
        h = source_hash(mmd)
        for out, read in ((mmd.with_suffix(".svg"), svg_hash), (mmd.with_suffix(".png"), png_hash)):
            if not out.is_file():
                problems.append(f"{out.name}: missing; run scripts/diagrams.py render {mmd.stem}")
            elif read(out) != h:
                problems.append(f"{out.name}: not rendered from the current {mmd.name}; run scripts/diagrams.py render {mmd.stem}")
    return problems


def render(names: list[str], directory: Path = DIR) -> None:
    env = os.environ.get("MMDC", "mmdc")   # a path (spaces allowed), or a command such as `npx -y @mermaid-js/mermaid-cli@11.4.2`
    mmdc = [env] if Path(env).is_file() else shlex.split(env)
    for mmd in [directory / f"{n}.mmd" for n in names] or sorted(directory.glob("*.mmd")):
        # a diagram's own mermaid config, when it has one (features.json: the ELK layout, wide instead of a 13000 px column)
        config = ["-c", str(mmd.with_suffix(".json"))] if mmd.with_suffix(".json").is_file() else []
        for suffix, extra in ((".svg", []), (".png", ["-b", "white", "-s", "2"])):
            subprocess.run([*mmdc, "-q", "-i", str(mmd), "-o", str(mmd.with_suffix(suffix)), *config, *extra], check=True)
        stamp(mmd)
        print(f"rendered {mmd.stem}: svg, png, {source_hash(mmd)[:12]}")


def main() -> int:
    if sys.argv[1:2] == ["render"]:
        render(sys.argv[2:])
        return 0
    problems = stale()
    print("\n".join(problems) or "docs/diagrams: every SVG and PNG carries the hash of its .mmd")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
