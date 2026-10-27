"""Every script under poc/ that starts with a shebang is committed executable.

A clone made on Linux gets the modes git recorded, so a script committed as 100644 fails with "Permission denied" the first time someone runs the
command from the README (found on 2026-10-27 deploying the demo with `git archive`; before that every run chmod'ed the files by hand).
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tracked_modes():
    out = subprocess.run(["git", "ls-files", "-s", "poc"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        yield meta.split()[0], path


class PocExecBits(unittest.TestCase):
    def test_scripts_with_a_shebang_are_executable_in_git(self):
        bad = []
        for mode, path in tracked_modes():
            with open(ROOT / path, "rb") as f:
                head = f.read(2)
            if head == b"#!" and mode != "100755":
                bad.append(f"{mode} {path}")
        self.assertEqual(bad, [], "committed without the executable bit; run: git update-index --chmod=+x <file>")


if __name__ == "__main__":
    unittest.main()
