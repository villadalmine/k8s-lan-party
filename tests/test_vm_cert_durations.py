"""poc/vm/up.sh: a throw-away control plane and lighthouse get certificates of a day; PERSIST=1 gives both 180 days; the *_CERT_DURATION
variables win either way (week.sh sets them to the demo's length)."""
import subprocess
import unittest
from pathlib import Path

UP = Path(__file__).resolve().parent.parent / "poc" / "vm" / "up.sh"


def durations(env: dict) -> tuple[str, str]:
    lines = [l for l in UP.read_text().splitlines() if l.startswith(("CP_CERT=", "LH_CERT="))]
    assert len(lines) == 2, lines
    out = subprocess.run(["bash", "-c", "\n".join(lines) + '\necho "$CP_CERT $LH_CERT"'], env={"PATH": "/usr/bin:/bin", **env},
                         capture_output=True, text=True, check=True).stdout.split()
    return out[0], out[1]


class CertDurations(unittest.TestCase):
    def test_a_day_by_default(self):
        self.assertEqual(durations({}), ("24h", "24h"))

    def test_persist_gives_both_a_season(self):
        self.assertEqual(durations({"PERSIST": "1"}), ("4320h", "4320h"))

    def test_the_variables_win(self):
        self.assertEqual(durations({"PERSIST": "1", "CP_CERT_DURATION": "216h", "LH_CERT_DURATION": "216h"}), ("216h", "216h"))
        self.assertEqual(durations({"LH_CERT_DURATION": "48h"}), ("24h", "48h"))


if __name__ == "__main__":
    unittest.main()
