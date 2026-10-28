"""The relay switch of issue #15 lands in the Nebula configs issue.sh writes, and only when the host turned it on.

`lighthouse-vps.sh sign --relay` remembers LH_RELAY=yes in the state's lighthouse.env; from then on the lighthouse's config says
`am_relay: true` and every other host (the control plane, each player) names the lighthouse as its relay. Without it no config has a relay
section, so a game that never asked for a relay behaves as before. `issue.sh` runs here with CONFIG_ONLY=1 (it rewrites only config.yml of an
existing bundle), so no CA, no container and no network is touched. The end-to-end proof is `make poc-nat`.
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
class RelayConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, True)
        for name in ("lh", "cp", "alice"):
            (self.state / "bundles" / name).mkdir(parents=True)
            (self.state / "bundles" / name / "host.crt").write_text("placeholder\n")

    def configs(self, lighthouse_env: str | None) -> dict[str, str]:
        if lighthouse_env is not None:
            (self.state / "lighthouse.env").write_text(lighthouse_env)
        env = dict(os.environ, EMESH_STATE=str(self.state), EMESH_CACHE=str(self.state / "cache"), CONFIG_ONLY="1")
        env.pop("LH_RELAY", None)
        out = {}
        for name in ("lh", "cp", "alice"):
            p = subprocess.run([BASH, str(POC / "issue.sh"), name, "x", "x", "x"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            out[name] = (self.state / "bundles" / name / "config.yml").read_text()
        return out

    def test_no_relay_by_default(self) -> None:
        for name, cfg in self.configs(None).items():
            self.assertNotIn("relay", cfg, name)

    def test_relay_off_writes_no_relay_section(self) -> None:
        for name, cfg in self.configs("LH_EXTERNAL=203.0.113.7\nLH_RELAY=no\n").items():
            self.assertNotIn("relay", cfg, name)

    def test_relay_on_lighthouse_relays_and_every_other_host_names_it(self) -> None:
        cfgs = self.configs("LH_EXTERNAL=203.0.113.7\nLH_RELAY=yes\n")
        self.assertIn("\nrelay: {am_relay: true}\n", cfgs["lh"])
        for name in ("cp", "alice"):
            self.assertIn('\nrelay: {relays: ["10.77.0.1"]}\n', cfgs[name], name)
            self.assertNotIn("am_relay", cfgs[name], name)
            self.assertIn('"10.77.0.1": ["203.0.113.7:4242"]', cfgs[name], name)
        # use_relays is left at Nebula's default (true): an explicit false anywhere would silently switch the fallback off
        for name, cfg in cfgs.items():
            self.assertNotIn("use_relays", cfg, name)


if __name__ == "__main__":
    unittest.main()
