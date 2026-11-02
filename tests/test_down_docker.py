"""`poc/down.sh` in the Docker flow leaves no container and no network of the game (issue #36).

Found on 2026-10-28: after `mesh play alice` and `mesh host down`, the container `alice` was still running and the network `emesh` could not be
removed, because down.sh removed only `lh cp n1 n2`. Here `docker` is a stub with a small state (containers, their networks, a network that
cannot be removed while a container is on it, as the real one): no Docker, no container, nothing on the machine is touched. The real thing is the
step H11 of `make poc-host` (Docker flow)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"
BASH = shutil.which("bash")

STUB = r'''#!/usr/bin/env python3
import json, sys
f = "%(state)s"
d = json.load(open(f))
a = sys.argv[1:]
import os
if os.path.exists(f + ".down") and a[:1] in (["ps"], ["network"]) and a[:2] != ["network", "rm"]:
    print("Cannot connect to the Docker daemon", file=sys.stderr); sys.exit(1)   # Docker unreachable: every query fails
def save(): json.dump(d, open(f, "w"))
if a[:2] == ["ps", "-a"] and "--format" in a:
    print("\n".join(c["name"] for c in d["containers"]))
elif a[:2] == ["network", "ls"]:
    print("\n".join(d["networks"]))
elif a[:2] == ["ps", "-aq"]:
    net = a[3].split("=", 1)[1] if len(a) > 3 and a[2] == "--filter" else None
    print("\n".join(c["name"] for c in d["containers"] if net is None or net in c["nets"]))
elif a[:2] == ["rm", "-f"]:
    d["containers"] = [c for c in d["containers"] if c["name"] not in a[2:]]; save()
elif a[:2] == ["network", "rm"]:
    if any(a[2] in c["nets"] for c in d["containers"]):
        print("Error: network has active endpoints", file=sys.stderr); sys.exit(1)
    d["networks"] = [n for n in d["networks"] if n != a[2]]; save()
elif a[:2] == ["network", "inspect"]:
    sys.exit(0 if a[2] in d["networks"] else 1)
else:
    sys.exit(0)
'''


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class DownDocker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dstate = self.tmp / "docker.json"
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "docker").write_text(STUB % {"state": self.dstate})
        (bin_ / "docker").chmod(0o755)
        self.state = self.tmp / "state"
        (self.state / "ca").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("RUNTIME", "CNI", "TAINT")}
        self.env.update(PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", EMESH_STATE=str(self.state), EMESH_CACHE=str(self.tmp / "cache"))

    def game(self, names: list[str]) -> None:
        cs = [{"name": n, "nets": ["emesh"]} for n in names] + [{"name": "fwl", "nets": []}, {"name": "someone-else", "nets": ["bridge"]}]
        self.dstate.write_text(json.dumps({"containers": cs, "networks": ["emesh", "bridge"]}))

    def docker_state(self) -> dict:
        return json.loads(self.dstate.read_text())

    def run_sh(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], env=self.env, capture_output=True, text=True, timeout=60)

    def test_down_removes_every_container_on_the_network_and_the_network(self) -> None:
        self.game(["lh", "cp", "n1", "n2", "alice", "bob-2"])
        p = self.run_sh(f'RUNTIME=docker "{POC}/down.sh"')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = self.docker_state()
        self.assertEqual([c["name"] for c in d["containers"]], ["someone-else"])   # only what is not ours is left
        self.assertEqual(d["networks"], ["bridge"])
        self.assertFalse(self.state.exists())

    def clean(self) -> str:
        return self.run_sh(f'. "{POC}/lib.sh"; RUNTIME=docker; docker_clean && echo clean || echo not-clean').stdout.strip()

    def test_the_suite_check_sees_a_leftover_and_a_clean_host(self) -> None:
        self.game(["lh", "cp", "alice"])
        self.assertEqual(self.clean(), "not-clean")
        self.run_sh(f'RUNTIME=docker "{POC}/down.sh"')
        self.assertEqual(self.clean(), "clean")
        # the old script's blind spot: only a player and the network are left
        self.dstate.write_text(json.dumps({"containers": [{"name": "alice", "nets": ["emesh"]}], "networks": ["emesh"]}))
        self.assertEqual(self.clean(), "not-clean")

    def test_a_failed_docker_query_is_not_clean(self) -> None:
        self.game(["lh", "cp", "alice"])
        self.run_sh(f'RUNTIME=docker "{POC}/down.sh"')
        self.assertEqual(self.clean(), "clean")
        Path(str(self.dstate) + ".down").write_text("")   # Docker stops answering: nothing can be verified
        self.assertEqual(self.clean(), "not-clean")
        self.game(["lh", "cp", "alice"])                  # and with leftovers it is still not clean
        self.assertEqual(self.clean(), "not-clean")

    def test_down_twice_is_safe(self) -> None:
        self.game(["lh", "cp", "alice"])
        for _ in range(2):
            self.assertEqual(self.run_sh(f'RUNTIME=docker "{POC}/down.sh"').returncode, 0)
        self.assertEqual(self.docker_state()["networks"], ["bridge"])


if __name__ == "__main__":
    unittest.main()
