#!/usr/bin/env python3
"""The diagrams under docs/diagrams (issue #62): every <name>.mmd is rendered to <name>.svg and <name>.png, and both carry the SHA-256 of
the source (the SVG as a comment, the PNG as a tEXt chunk). tests/test_diagrams.py fails when a .mmd changed and its SVG or PNG was not
rendered again. Checking needs nothing but Python; rendering needs mermaid-cli (MMDC, default `mmdc`).

    scripts/diagrams.py check               every .mmd: its SVG and PNG carry its hash
    scripts/diagrams.py render [NAME ...]   render with mermaid-cli and stamp the hash (all diagrams when no name is given)
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import subprocess
import sys
import zlib
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
KEY = b"source-sha256"
SVG_MARK = re.compile(r"\n?<!-- source-sha256: ([0-9a-f]{64}) -->\n?")


def source_hash(mmd: Path) -> str:
    return hashlib.sha256(mmd.read_bytes()).hexdigest()


def chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    out, i = [], 8
    while i < len(png):
        n, kind = struct.unpack(">I4s", png[i:i + 8])
        out.append((kind, png[i + 8:i + 8 + n]))
        i += 12 + n
    return out


def png_hash(png: Path) -> str | None:
    for kind, data in chunks(png.read_bytes()):
        if kind == b"tEXt" and data.startswith(KEY + b"\0"):
            return data[len(KEY) + 1:].decode()
    return None


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
    mmdc = os.environ.get("MMDC", "mmdc")
    for mmd in [directory / f"{n}.mmd" for n in names] or sorted(directory.glob("*.mmd")):
        for suffix, extra in ((".svg", []), (".png", ["-b", "white", "-s", "2"])):
            subprocess.run([mmdc, "-q", "-i", str(mmd), "-o", str(mmd.with_suffix(suffix)), *extra], check=True)
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
