"""A suite that needs nodes without the trust taint pins TAINT=no, whatever the caller's shell exports (issue #59).

A `TAINT=yes` left in the shell (docs/TRY-IT.md recipes 1 and 2 export it) made poc-join's n1 and n2 register with `trust=<level>:NoSchedule`.
With the control plane tainted too, the study lab's pod, which tolerates nothing, had no node and stayed Pending, so J11 and J12 failed.
poc-pool has the same dependency: the participant's pods tolerate nothing, and trust taints are not supported with the pool (DESIGN.md 14).
Both suites now set their own CNI and TAINT, like poc-placement and poc-vm do. This test runs the head of each suite (everything up to and
including `. lib.sh`, before any container or VM is touched) with the wrong values exported and checks what the rest of the suite would see.
Docker and Incus are stubs. TryItNoteMatchesTheSuites checks the note of docs/TRY-IT.md recipes 1 and 2 against what each suite exports,
so the list of suites that pin CNI and TAINT cannot drift from the scripts (review of PR #64).
"""
from __future__ import annotations

import os
import re
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
        # ${X:-unset}: the suite runs under set -u, and a suite that does not set CNI must show that, not die
        code = head_of(POC / suite) + 'echo "CNI=${CNI:-unset} TAINT=${TAINT:-unset} RUNTIME=${RUNTIME:-unset}"\n'
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

    def test_poc_party_pins_taint_and_lets_the_caller_pick_the_cni(self) -> None:
        # poc-party proves the party's NetworkPolicies on both CNIs (flannel by default, CNI=cilium on purpose); TAINT is always no
        self.assertEqual(self.seen("poc-party.sh"), "CNI=flannel TAINT=no RUNTIME=incus")
        self.assertEqual(self.seen("poc-party.sh", TAINT="yes", CNI="cilium", RUNTIME="docker"), "CNI=cilium TAINT=no RUNTIME=incus")

    def test_poc_doom_pins_taint_and_runs_cilium_unless_the_caller_picks_flannel(self) -> None:
        # the Doom demo's CNI is Cilium (the demo's own); CNI=flannel also runs; TAINT is always no
        self.assertEqual(self.seen("poc-doom.sh"), "CNI=cilium TAINT=no RUNTIME=incus")
        self.assertEqual(self.seen("poc-doom.sh", TAINT="yes", CNI="flannel", RUNTIME="docker"), "CNI=flannel TAINT=no RUNTIME=incus")

    def test_poc_join_keeps_the_callers_runtime(self) -> None:
        self.assertEqual(self.seen("poc-join.sh", TAINT="yes", RUNTIME="incus"), "CNI=flannel TAINT=no RUNTIME=incus")


def suites() -> dict[str, str]:
    """Every PoC suite (poc/poc-*.sh, poc/vm/poc-*.sh, poc/vm/run-all.sh) by name, with its text."""
    files = sorted(POC.glob("poc-*.sh")) + sorted((POC / "vm").glob("poc-*.sh")) + [POC / "vm" / "run-all.sh"]
    return {str(f.relative_to(POC).with_suffix("")): f.read_text(encoding="utf-8") for f in files}


def exported(text: str, var: str) -> str | None:
    """The value a suite exports for var on an `export ...` line, or None."""
    m = re.search(rf"(?m)^export [^#\n]*\b{var}=(\S+)", text)
    return m.group(1) if m else None


def delegate(text: str) -> str | None:
    """A suite that is only `exec .../poc-X.sh` (vm/poc-placement.sh): the name of the suite it runs."""
    m = re.search(r'(?m)^exec "\$\(dirname "\$0"\)/\.\./(poc-[a-z-]+)\.sh"', text)
    return m.group(1) if m else None


class TryItNoteMatchesTheSuites(unittest.TestCase):
    """docs/TRY-IT.md recipes 1 and 2 say which suites pin CNI and TAINT; the note is checked against the scripts so it cannot drift."""

    NOTE = re.compile(r"((?:`poc-[a-z-]+`(?:, | and )?)+) pin their own CNI and TAINT; the other suites inherit TAINT \(the Incus ones pin "
                      r"`CNI=flannel`\), and none of those depends on TAINT \(issue #59\)")

    def test_the_note_names_exactly_the_suites_that_pin_taint(self) -> None:
        notes = self.NOTE.findall((POC.parent / "docs" / "TRY-IT.md").read_text(encoding="utf-8"))
        self.assertEqual(len(notes), 2, "the note is expected once in recipe 1 and once in recipe 2")
        pinning = {n.split("/")[-1] for n, t in suites().items() if exported(t, "TAINT")}
        for note in notes:
            self.assertEqual(set(re.findall(r"`(poc-[a-z-]+)`", note)), pinning)

    def test_every_other_incus_suite_pins_cni_flannel(self) -> None:
        all_suites = suites()
        for name, text in all_suites.items():
            if exported(text, "TAINT") or exported(text, "RUNTIME") != "incus":
                continue
            with self.subTest(suite=name):
                target = delegate(text)
                cni = exported(all_suites[target], "CNI") if target else exported(text, "CNI")
                self.assertEqual(cni, "flannel")


if __name__ == "__main__":
    unittest.main()
