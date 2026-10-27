"""Two games on one machine keep separate state: EMESH_STATE picks the state directory, and down.sh removes only its own.

Every PoC suite starts with down.sh, which removes the state (CA keys, bundles, blocklist). Before EMESH_STATE the path was fixed, so a
suite run from the same tree deleted the CA of a second game kept next to it (docs/HOST-YOUR-OWN.md, "A second game"). Docker and Incus
are replaced by stubs that do nothing: the test touches no container, VM or network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for tool in ("docker", "incus"):
            stub = stubs / tool
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        self.games = {}
        for game in ("one", "two"):
            state = self.tmp / game
            (state / "ca").mkdir(parents=True)
            (state / "ca" / "ca.key").write_text(f"key of {game}\n")
            self.games[game] = state
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}")

    def run_bash(self, *args: str, **env: str) -> str:
        p = subprocess.run([BASH, *args], env={**self.env, **env}, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_lib_uses_emesh_state(self) -> None:
        out = self.run_bash("-c", f'. "{POC}/lib.sh"; echo "$STATE"', EMESH_STATE=str(self.games["one"]))
        self.assertEqual(out.strip(), str(self.games["one"]))

    def test_default_stays_in_the_tree(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "EMESH_STATE"}
        p = subprocess.run([BASH, "-c", f'. "{POC}/lib.sh"; echo "$STATE"'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.stdout.strip(), str(POC / ".state"))

    def test_down_removes_only_its_own_state(self) -> None:
        for runtime in ("docker", "incus"):
            with self.subTest(runtime=runtime):
                (self.games["one"] / "ca").mkdir(parents=True, exist_ok=True)
                self.run_bash(str(POC / "down.sh"), RUNTIME=runtime, EMESH_STATE=str(self.games["one"]))
                self.assertFalse(self.games["one"].exists(), "down.sh left its own state behind")
                self.assertEqual((self.games["two"] / "ca" / "ca.key").read_text(), "key of two\n",
                                 "down.sh of game one touched the CA of game two")


if __name__ == "__main__":
    unittest.main()
