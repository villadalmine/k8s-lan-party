"""A game remembers how it was opened: `mesh host init` records RUNTIME, CNI and TAINT in .state/game.env (poc/game-env.sh).

Found on 2026-10-28 hosting a game on a cloud VM from the docs alone: CNI=cilium and TAINT=yes had to be repeated on every invite, so the
crontab line of HOST-YOUR-OWN.md (which carries only RUNTIME) would approve a flannel bundle, without the trust taint, into a Cilium game.
Docker and Incus are stubs that do nothing: the test touches no container, VM or network.
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
class GameEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for tool in ("docker", "incus"):
            (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
            (stubs / tool).chmod(0o755)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "game.env").write_text("GAME_RUNTIME=incus\nGAME_CNI=cilium\nGAME_TAINT=yes\n")
        base = {k: v for k, v in os.environ.items() if k not in ("RUNTIME", "CNI", "TAINT")}
        self.env = dict(base, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", EMESH_STATE=str(self.state))

    def bash(self, script: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], env={**self.env, **env}, capture_output=True, text=True, timeout=60)

    def test_later_commands_inherit_the_game(self) -> None:
        p = self.bash(f'. "{POC}/game-env.sh"; . "{POC}/lib.sh"; echo "$RUNTIME $CNI $TAINT"')
        self.assertEqual(p.stdout.strip(), "incus cilium yes", p.stderr)

    def test_a_variable_set_by_hand_wins(self) -> None:
        p = self.bash(f'. "{POC}/game-env.sh"; echo "$CNI"', CNI="flannel")
        self.assertEqual(p.stdout.strip(), "flannel")

    def test_invite_refuses_another_cni(self) -> None:
        p = self.bash(f'"{POC}/mesh" host invite alice --ttl 1h', CNI="flannel")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("this game runs CNI=cilium", p.stdout)
        self.assertFalse((self.state / "bundles" / "alice").exists(), "a refused invite left a bundle")

    def test_down_takes_the_game_runtime(self) -> None:
        # without RUNTIME, an Incus game must not take the Docker path (which deletes the state and leaves the VMs running)
        p = self.bash(f'"{POC}/down.sh"')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("VM flow removed", p.stdout)
        self.assertFalse(self.state.exists())

    def test_a_malformed_file_is_ignored_not_executed(self) -> None:
        # the file is read, never sourced: a value with shell syntax must not run, and the whole file is refused
        mark = self.tmp / "ran"
        (self.state / "game.env").write_text("GAME_RUNTIME=incus\nGAME_CNI=$(touch " + str(mark) + ")\nGAME_TAINT=yes\n")
        p = self.bash(f'. "{POC}/game-env.sh"; echo "[${{CNI:-unset}}]"')
        self.assertFalse(mark.exists(), "the value of game.env was executed")
        self.assertEqual(p.stdout.strip(), "[unset]")
        self.assertIn("ignored", p.stderr)

    def test_suites_do_not_read_it(self) -> None:
        # a suite sources lib.sh only and builds its own game: a recorded Cilium game must not leak into a flannel suite
        p = self.bash(f'. "{POC}/lib.sh"; echo "${{CNI:-unset}} ${{RUNTIME}}"')
        self.assertEqual(p.stdout.strip(), "unset docker")


if __name__ == "__main__":
    unittest.main()
