"""poc-join proves the default game and pins it: CNI=flannel and nodes without the trust taint, whatever the caller's shell exports (issue #59).

A `TAINT=yes` left in the shell (docs/TRY-IT.md recipe 2 exports it) made n1 and n2 register with `trust=<level>:NoSchedule`. With the control
plane tainted too, the study lab's pod, which tolerates nothing, had no node and stayed Pending, so J11 and J12 failed. The suite now sets its
own CNI and TAINT, like poc-placement and poc-vm do. This test runs the head of poc-join.sh (everything up to and including `. lib.sh`, before
any container or VM is touched) with the wrong values exported and checks what the rest of the suite would see. Docker and Incus are stubs.
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


def head_of(script: Path) -> str:
    """The lines of the suite up to and including the one that sources lib.sh (nothing after it runs)."""
    lines = script.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(". ") and "lib.sh" in line:
            return "\n".join(lines[: i + 1]) + "\n"
    raise AssertionError(f"{script.name} does not source lib.sh")


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class JoinPinsGame(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for tool in ("docker", "incus"):
            (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
            (stubs / tool).chmod(0o755)
        self.head = self.tmp / "head.sh"
        self.head.write_text(head_of(POC / "poc-join.sh") + 'echo "CNI=$CNI TAINT=$TAINT RUNTIME=$RUNTIME"\n', encoding="utf-8")
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", EMESH_STATE=str(self.tmp / "state"),
                        EMESH_CACHE=str(self.tmp / "cache"))

    def run_head(self, **env: str) -> str:
        # bash -c '<code>' <$0>: dirname "$0" is poc/, as when the suite runs
        p = subprocess.run([BASH, "-c", self.head.read_text(encoding="utf-8"), str(POC / "poc-join.sh")], env={**self.env, **env},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip().splitlines()[-1]

    def test_a_tainted_shell_does_not_leak_into_the_suite(self) -> None:
        self.assertEqual(self.run_head(TAINT="yes", CNI="cilium", RUNTIME="docker"), "CNI=flannel TAINT=no RUNTIME=docker")

    def test_a_clean_shell_gets_the_same_game(self) -> None:
        env = {k: v for k, v in self.env.items() if k not in ("TAINT", "CNI", "RUNTIME")}
        p = subprocess.run([BASH, "-c", self.head.read_text(encoding="utf-8"), str(POC / "poc-join.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().splitlines()[-1], "CNI=flannel TAINT=no RUNTIME=docker")

    def test_the_runtime_is_still_the_callers(self) -> None:
        # both runtimes are proved (POC-STATUS: Dell Incus VMs and Docker), so RUNTIME is not pinned
        self.assertEqual(self.run_head(TAINT="yes", RUNTIME="incus"), "CNI=flannel TAINT=no RUNTIME=incus")


if __name__ == "__main__":
    unittest.main()
