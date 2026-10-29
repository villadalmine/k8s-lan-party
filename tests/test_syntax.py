"""Every shell script parses and every Python file compiles (a merge once duplicated a whole `case` in poc/mesh: every mesh command was
unparseable and no check noticed). `bash -n` / `sh -n` per the shebang, and `compile()` for Python: nothing is executed, nothing is written."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    return [f for f in out if f and (ROOT / f).is_file()]


class Syntax(unittest.TestCase):
    def test_shell_scripts_parse(self):
        shells = {"bash": shutil.which("bash"), "sh": shutil.which("sh")}
        bad = []
        for f in tracked():
            p = ROOT / f
            try:
                first = p.read_bytes().split(b"\n", 1)[0].decode("utf-8", "replace")
            except OSError:
                continue
            if f.endswith(".sh") or first.startswith("#!") and ("bash" in first or "/sh" in first or "env sh" in first):
                kind = "sh" if ("/sh" in first or "env sh" in first) and "bash" not in first else "bash"
                exe = shells[kind]
                if not exe:
                    continue
                r = subprocess.run([exe, "-n", str(p)], capture_output=True, text=True)
                if r.returncode != 0:
                    bad.append(f"{f}: {r.stderr.strip()[:200]}")
        self.assertEqual(bad, [], "shell syntax errors")

    def test_python_files_compile(self):
        bad = []
        for f in tracked():
            p = ROOT / f
            first = p.read_bytes().split(b"\n", 1)[0].decode("utf-8", "replace") if p.suffix != ".py" else ""
            if p.suffix == ".py" or first.startswith("#!") and "python" in first:
                try:
                    compile(p.read_text(encoding="utf-8"), f, "exec")
                except (SyntaxError, ValueError) as e:
                    bad.append(f"{f}: {e}")
        self.assertEqual(bad, [], "python syntax errors")


if __name__ == "__main__":
    unittest.main()
