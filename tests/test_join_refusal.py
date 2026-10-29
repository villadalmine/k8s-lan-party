"""A joiner without the privileges `edge-mesh join` needs gets a clear refusal, never a half-started node (issue #14).

What a joiner needs (docs/JOIN.md, "Who can join"): root, because Nebula creates a network device and k3s mounts; and `/dev/net/tun`, which a
VM, privileged Docker and WSL2 have and plain Docker has not. Checked here with the real script:

- the golden rule not accepted: exit 3, before anything else;
- not root: exit 6 (this test runs as a normal user; as root it drops to nobody with setpriv);
- root in a plain (unprivileged) Docker container: no `/dev/net/tun`, exit 4, and nothing started;
- the positive control: the same container given `/dev/net/tun` passes that check and stops at the next one (no lighthouse, exit 5), so the
  exit 4 above comes from the missing device and not from something else in the container.

The Docker cases need Docker; they are skipped without it on a laptop, and fail instead of skipping in CI (CI=true), so the refusal is
tested on every pull request. The image is pinned in poc/stack.env (ALPINE_IMAGE).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"
EDGE_MESH = POC / "edge-mesh"
SH = shutil.which("sh")
DOCKER = shutil.which("docker")


def stack_env(key: str) -> str:
    for line in (POC / "stack.env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].split()[0]
    raise KeyError(key)


def docker_works() -> bool:
    if not DOCKER:
        return False
    return subprocess.run([DOCKER, "info"], capture_output=True, timeout=60).returncode == 0


def make_bundle(root: Path) -> Path:
    b = root / "bundle"
    b.mkdir()
    for f in ("ca.crt", "host.crt", "host.key", "config.yml"):
        (b / f).write_text("not a real one\n")
    # 192.0.2.1 (TEST-NET-1) never answers: a run that gets past the device check stops at the lighthouse (exit 5)
    (b / "join.env").write_text(
        "K3S_URL=https://192.0.2.2:6443\nK3S_TOKEN=x\nNODE_NAME=t\nNODE_IP=192.0.2.3\nTRUST=public\nLH_NEB=192.0.2.1\n"
    )
    for p in [b, *b.iterdir()]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    root.chmod(0o755)
    return b


@unittest.skipUnless(SH and os.name == "posix", "needs a POSIX sh")
class Refusal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = make_bundle(self.tmp)

    def join(self, *extra: str, prefix: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        return subprocess.run([*prefix, SH, str(EDGE_MESH), "join", str(self.bundle), *extra], capture_output=True, text=True, timeout=60)

    def test_golden_rule_not_accepted_exit_3(self) -> None:
        p = self.join()
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("refusing to join: golden rule not accepted", p.stdout)

    def test_not_root_exit_6(self) -> None:
        prefix: tuple[str, ...] = ()
        if os.geteuid() == 0:
            setpriv = shutil.which("setpriv")
            if not setpriv:
                self.skipTest("running as root and no setpriv to drop to nobody")
            prefix = (setpriv, "--reuid=65534", "--regid=65534", "--clear-groups")
        p = self.join("--accept-golden-rule", prefix=prefix)
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        self.assertIn("refusing to join: run as root (sudo edge-mesh join ...)", p.stdout)


@unittest.skipUnless(SH and os.name == "posix", "needs a POSIX sh")
class RefusalInDocker(unittest.TestCase):
    def setUp(self) -> None:
        if not docker_works():
            if os.environ.get("CI") == "true":
                self.fail("CI must test the refusal in a real container, and Docker does not answer here")
            self.skipTest("no Docker here (CI runs this case)")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = make_bundle(self.tmp)
        self.image = stack_env("ALPINE_IMAGE")
        if subprocess.run([DOCKER, "image", "inspect", self.image], capture_output=True, timeout=60).returncode != 0:
            for _ in range(3):   # a registry hiccup is not a refusal: try the pull three times, then say why
                pull = subprocess.run([DOCKER, "pull", self.image], capture_output=True, text=True, timeout=300)
                if pull.returncode == 0:
                    break
            else:
                self.fail(f"cannot pull {self.image}: {pull.stderr.strip()[-300:]}")

    def run_in(self, *docker_args: str) -> subprocess.CompletedProcess:
        cmd = [DOCKER, "run", "--rm", "--network", "none", *docker_args,
               "-v", f"{EDGE_MESH}:/usr/local/bin/edge-mesh:ro", "-v", f"{self.bundle}:/bundle:ro", self.image,
               "sh", "-c", "sh /usr/local/bin/edge-mesh join /bundle --accept-golden-rule; rc=$?; "
               "pidof nebula k3s >/dev/null && echo 'STARTED SOMETHING'; exit $rc"]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def test_plain_docker_has_no_tun_exit_4(self) -> None:
        p = self.run_in()
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("refusing to join: no /dev/net/tun (needs a privileged Linux: VM, privileged Docker or WSL2)", p.stdout)
        self.assertNotIn("STARTED SOMETHING", p.stdout)

    def test_positive_control_with_tun_passes_the_check(self) -> None:
        p = self.run_in("--cap-add", "NET_ADMIN", "--device", "/dev/net/tun")
        self.assertNotIn("no /dev/net/tun", p.stdout)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("cannot reach the lighthouse", p.stdout)


if __name__ == "__main__":
    unittest.main()
