"""A suite that needs nodes without the trust taint pins TAINT=no, whatever the caller's shell exports (issue #59).

A `TAINT=yes` left in the shell (docs/TRY-IT.md recipes 1 and 2 export it) made poc-join's n1 and n2 register with `trust=<level>:NoSchedule`.
With the control plane tainted too, the study lab's pod, which tolerates nothing, had no node and stayed Pending, so J11 and J12 failed.
poc-pool has the same dependency: the participant's pods tolerate nothing, and trust taints are not supported with the pool (DESIGN.md 14).
Both suites now set their own CNI and TAINT, like poc-placement and poc-vm do. This test runs the head of each suite (everything up to and
including `. lib.sh`, before any container or VM is touched) with the wrong values exported and checks what the rest of the suite would see.
Docker and Incus are stubs.
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

# suite -> (CNI, TAINT, RUNTIME it must see); None = the caller's RUNTIME (docker in these tests)
PINNED = {
    "poc-join.sh": ("flannel", "no", None),   # both runtimes are proved (POC-STATUS: Dell Incus VMs and Docker)
    "poc-pool.sh": ("flannel", "no", "incus"),
}


def head_of(script: Path) -> str:
    """The lines of the suite up to and including the one that sources lib.sh (nothing after it runs)."""
    lines = script.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(". ") and "lib.sh" in line:
            return "\n".join(lines[: i + 1]) + "\n"
    raise AssertionError(f"{script.name} does not source lib.sh")


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class SuitesPinGame(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for tool in ("docker", "incus"):
            (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
            (stubs / tool).chmod(0o755)
        base = {k: v for k, v in os.environ.items() if k not in ("TAINT", "CNI", "RUNTIME")}
        self.env = dict(base, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", EMESH_STATE=str(self.tmp / "state"),
                        EMESH_CACHE=str(self.tmp / "cache"))

    def seen(self, suite: str, **env: str) -> str:
        code = head_of(POC / suite) + 'echo "CNI=$CNI TAINT=$TAINT RUNTIME=$RUNTIME"\n'
        # bash -c '<code>' <$0>: dirname "$0" is poc/, as when the suite runs
        p = subprocess.run([BASH, "-c", code, str(POC / suite)], env={**self.env, **env}, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip().splitlines()[-1]

    def test_a_tainted_shell_does_not_leak_into_the_suite(self) -> None:
        for suite, (cni, taint, runtime) in PINNED.items():
            with self.subTest(suite=suite):
                want = f"CNI={cni} TAINT={taint} RUNTIME={runtime or 'docker'}"
                self.assertEqual(self.seen(suite, TAINT="yes", CNI="cilium", RUNTIME="docker"), want)

    def test_a_clean_shell_gets_the_same_game(self) -> None:
        for suite, (cni, taint, runtime) in PINNED.items():
            with self.subTest(suite=suite):
                self.assertEqual(self.seen(suite), f"CNI={cni} TAINT={taint} RUNTIME={runtime or 'docker'}")

    def test_poc_join_keeps_the_callers_runtime(self) -> None:
        self.assertEqual(self.seen("poc-join.sh", TAINT="yes", RUNTIME="incus"), "CNI=flannel TAINT=no RUNTIME=incus")


if __name__ == "__main__":
    unittest.main()
