"""The host console's reads are pure (issue #86): `mesh host players --read-only` burns no join token and leaves the node guard alone,
and `--json` prints one JSON document. Docker and nebula-cert are stubs that log every call; no cluster.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"
BASH = shutil.which("bash")

DOCKER = r'''#!/bin/sh
# docker stub: logs each call; "exec [-i] cp <command...>": a Ready node for alice, none for bob; a token list naming both
echo "$*" >>"$FAKE_LOG"
[ "$1" = exec ] || exit 0
shift; [ "$1" = -i ] && shift; shift
case "$1 $2 $3" in
  "kubectl get node") [ "$4" = alice ] && echo True || exit 1 ;;
  "k3s token list") echo "TOKEN TTL EXPIRES"; echo "tokali 7m x"; echo "tokbob 7m x" ;;
  "kubectl apply -f"|"kubectl create configmap") cat >/dev/null 2>&1; exit 0 ;;
esac
exit 0
'''

NEBULA_CERT = r'''#!/bin/sh
echo "$*" >>"$FAKE_LOG"
echo '[{"details": {"notAfter": "2030-01-01T10:00:00Z"}}]'
'''


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class PlayersRead(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_, cache, self.state = self.tmp / "bin", self.tmp / "cache", self.tmp / "state"
        for d in (bin_, cache):
            d.mkdir()
        for path, text in ((bin_ / "docker", DOCKER), (bin_ / "incus", "#!/bin/sh\nexit 0\n"), (cache / "nebula-cert", NEBULA_CERT)):
            path.write_text(text)
            path.chmod(0o755)
        for name, tok in (("cp", None), ("alice", "tokali"), ("bob", "tokbob")):
            b = self.state / "bundles" / name
            b.mkdir(parents=True)
            (b / "host.crt").write_text(f"certificate of {name}\n")
            if tok:
                (b / "join.env").write_text(f"K3S_TOKEN=K10abc::{tok}.secret\nNODE_NAME={name}\nNODE_IP=10.77.0.9\n")
        self.log = self.tmp / "calls.log"
        self.env = dict(os.environ, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", RUNTIME="docker", EMESH_STATE=str(self.state),
                        EMESH_CACHE=str(cache), FAKE_LOG=str(self.log))

    def mesh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(POC / "mesh"), "host", "players", *args], env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_read_only_json_burns_nothing_and_writes_nothing(self):
        before = snapshot(self.state)
        r = self.mesh("--json", "--read-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [{"name": "alice", "ready": "True", "cert_expires": "2030-01-01T10:00:00Z"},
                                                {"name": "bob", "ready": "absent", "cert_expires": "2030-01-01T10:00:00Z"}])
        self.assertNotIn("token delete", self.calls())
        self.assertNotIn("apply", self.calls())
        self.assertNotIn("create configmap", self.calls())
        self.assertEqual(snapshot(self.state), before)

    def test_without_read_only_it_still_burns_the_registered_node(self):
        r = self.mesh()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("alice", r.stdout)
        self.assertIn("bob          no", r.stdout)
        self.assertIn("k3s token delete tokali", self.calls())
        self.assertNotIn("token delete tokbob", self.calls())

    def test_unknown_option_is_refused(self):
        self.assertEqual(self.mesh("--burn").returncode, 2)


if __name__ == "__main__":
    unittest.main()
