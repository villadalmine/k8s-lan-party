"""The MTU is set once (issue #8): poc/stack.env holds the only number, the Nebula MTU, and the pod MTU is derived from it.

Before 2026-10-28 the Nebula MTU (1300) sat in issue.sh and the pod MTU (1150) in stack.env and kind-cilium.sh, each a number of its own; changing the
first left the others silently wrong. The suites (poc-vm V11, poc-cilium C10) measure the chain on live nodes; this test keeps a second number from
coming back in the files.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "poc" / "stack.env"


def tracked_poc_files():
    out = subprocess.run(["git", "ls-files", "poc"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.splitlines() if (ROOT / p).is_file()]


FORMULA = "pod_mtu_from() { echo $(( ($1 - 50 - 80) / 50 * 50 )); }"   # VXLAN 50, WireGuard 80 (worst case), rounded down to 50


class MtuOnce(unittest.TestCase):
    def test_stack_env_has_one_mtu_number(self):
        text = STACK.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^NEBULA_MTU=\d+$")
        self.assertRegex(text, r'(?m)^POD_MTU=\$\(pod_mtu_from "\$NEBULA_MTU"\)$', "POD_MTU must be derived from NEBULA_MTU, not written as a number")

    def test_pod_mtu_is_derived(self):
        text = STACK.read_text(encoding="utf-8")
        self.assertIn(FORMULA, text, "the one formula changed: update this test and the overheads in docs/POC-STATUS.md together")
        nebula = int(re.search(r"(?m)^NEBULA_MTU=(\d+)$", text).group(1))
        self.assertGreaterEqual((nebula - 50 - 80) // 50 * 50, 1000, "a Nebula MTU this low leaves pods under 1000 bytes (df_ping refuses that)")

    def test_no_second_number_in_the_scripts(self):
        bad = []
        for f in tracked_poc_files():
            if f == STACK:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                # `mtu: 1300`, `MTU=1150`, `--mtu=1150`, `ip link set x mtu 1150`, `--set MTU=1150`: any MTU followed by a number
                if re.search(r"\bPOD_MTU=\d|\bNEBULA_MTU=\d|\bmtu\b\W{0,3}\d{3,4}\b", line, re.I):
                    bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "an MTU number outside poc/stack.env")


if __name__ == "__main__":
    unittest.main()
