"""A player's machine gets only some of poc/ (docs/JOIN.md, and poc/doom/week.sh for the week's demo): every module the player console
imports, and doom/pod.sh, must be in both lists (a missing one failed only on a real machine: ModuleNotFoundError)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POC = ROOT / "poc"
DIRS = [POC, POC / "console", POC / "page"]


def closure() -> set[str]:
    seen, todo = set(), [POC / "console" / "player.py"]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        for n in ast.walk(ast.parse(f.read_text())):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else (
                [n.module] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else [])
            for m in mods:
                for d in DIRS:
                    g = d / (m.split(".")[0] + ".py")
                    if g.exists():
                        todo.append(g)
                        break
    return {str(f.relative_to(POC)) for f in seen if f.parent != POC / "console"} | {"doom/pod.sh"}


class PlayerConsoleFiles(unittest.TestCase):
    def test_join_installs_every_module(self):
        join = (ROOT / "docs" / "JOIN.md").read_text()
        for f in sorted(closure()):
            self.assertIn(Path(f).name, join, f)

    def test_week_sends_every_module(self):
        week = (POC / "doom" / "week.sh").read_text()
        for f in sorted(closure()):
            self.assertIn(f, week, f)


if __name__ == "__main__":
    unittest.main()
