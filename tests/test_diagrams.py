"""Diagrams that cannot go stale (issue #62): every docs/diagrams/*.mmd has an SVG and a PNG carrying the SHA-256 of the source, and the
feature graph (docs/diagrams/features.mmd) is exactly what scripts/features.py generates from docs/FEATURES.yml. Without mermaid-cli only
the hashes are compared; with it (MMDC or `mmdc` on PATH) the feature graph is also rendered, so a source mermaid refuses fails here."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import diagrams  # noqa: E402
import features  # noqa: E402

MMDC = os.environ.get("MMDC") or shutil.which("mmdc")


class Diagrams(unittest.TestCase):
    def test_every_svg_and_png_was_rendered_from_its_current_source(self):
        self.assertEqual(diagrams.stale(), [])

    def test_the_feature_graph_is_generated_from_the_catalog(self):
        self.assertEqual(features.GRAPH.read_text(encoding="utf-8"), features.graph(features.load()),
                         "docs/diagrams/features.mmd is stale: scripts/features.py graph --write, then scripts/diagrams.py render features")

    def test_a_changed_source_without_a_new_render_fails_and_a_stamp_fixes_it(self):
        with tempfile.TemporaryDirectory() as t:
            for f in diagrams.DIR.glob("who-sees-what.*"):
                shutil.copy(f, t)
            mmd = Path(t) / "who-sees-what.mmd"
            mmd.write_text(mmd.read_text(encoding="utf-8") + "  extra --> node\n", encoding="utf-8")
            self.assertEqual([p.split(":")[0] for p in diagrams.stale(Path(t))], ["who-sees-what.svg", "who-sees-what.png"])
            diagrams.stamp(mmd)
            self.assertEqual(diagrams.stale(Path(t)), [])
            self.assertEqual(diagrams.png_hash(mmd.with_suffix(".png")), diagrams.source_hash(mmd))
            (Path(t) / "who-sees-what.png").unlink()
            self.assertIn("who-sees-what.png: missing", diagrams.stale(Path(t))[0])

    @unittest.skipUnless(MMDC, "mermaid-cli not installed: the hashes are checked, nothing is rendered")
    def test_the_feature_graph_renders(self):
        with tempfile.TemporaryDirectory() as t:
            subprocess.run([MMDC, "-q", "-i", str(features.GRAPH), "-o", str(Path(t) / "f.svg")], check=True, timeout=300)
            self.assertIn("<svg", (Path(t) / "f.svg").read_text(encoding="utf-8")[:200])


if __name__ == "__main__":
    unittest.main()
