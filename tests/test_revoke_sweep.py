"""revoke.sh sweeps every Node a copy of the bundle could have registered, and never reports success it cannot confirm.

Docker, kubectl and nebula-cert are stubs: the Node list is a JSON file the stub `kubectl` reads and edits. Exit 4 is the "not
confirmed" answer that `mesh host reap` turns into "keep the lease and try again": a Node that stays, an API that does not answer,
a bundle without NODE_IP.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"
BASH = shutil.which("bash")

DOCKER = r'''#!/bin/sh
# docker stub: "exec [-i] <container> <command...>"; kubectl get/delete nodes against $FAKE_NODES, the rest succeeds
echo "$*" >>"$FAKE_LOG"
[ "$1" = exec ] || exit 0
shift; [ "$1" = -i ] && shift; shift
case "$1 $2 $3" in
  "kubectl get nodes") [ -z "$FAKE_API_DOWN" ] || exit 1
    if [ "$5" = name ]; then python3 -c 'import json,sys; [print("node/" + i["metadata"]["name"]) for i in json.load(open(sys.argv[1]))["items"]]' "$FAKE_NODES"
    else cat "$FAKE_NODES"; fi ;;
  "kubectl delete node") [ -z "$FAKE_API_DOWN" ] || exit 1; [ -n "$FAKE_STICKY" ] && exit 0
    python3 - "$FAKE_NODES" "$4" <<'PY'
import json, sys
p, name = sys.argv[1], sys.argv[2]
d = json.load(open(p)); d["items"] = [i for i in d["items"] if i["metadata"]["name"] != name]; json.dump(d, open(p, "w"))
PY
    ;;
  "kubectl apply -f") cat >/dev/null ;;
  "k3s token list") echo "TOKEN TTL EXPIRES"; for t in $FAKE_TOKENS; do echo "$t 7m 2030-01-01T00:00:00Z"; done ;;
  "kubectl get secrets") [ -z "$FAKE_API_DOWN" ] || exit 1; for s in $FAKE_SECRETS; do echo "secret/$s"; done ;;
esac
exit 0
'''

NEBULA_CERT = r'''#!/bin/sh
# nebula-cert stub: "print -json -path <file>" -> a fingerprint made of the file's content
while [ $# -gt 0 ]; do [ "$1" = -path ] && f=$2; shift; done
python3 -c 'import hashlib,json,sys; print(json.dumps([{"fingerprint": hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()}]))' "$f"
'''


def node(name: str, *ips: str) -> dict:
    return {"metadata": {"name": name}, "status": {"addresses": [{"type": "InternalIP", "address": ip} for ip in ips]}}


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on Linux or macOS")
class RevokeSweep(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_, cache, self.state = self.tmp / "bin", self.tmp / "cache", self.tmp / "state"
        for d in (bin_, cache):
            d.mkdir()
        for path, text in ((bin_ / "docker", DOCKER), (bin_ / "incus", "#!/bin/sh\nexit 0\n"), (cache / "nebula-cert", NEBULA_CERT)):
            path.write_text(text)
            path.chmod(0o755)
        for name, ip in (("cp", None), ("bob", "10.77.0.6"), ("carol", "10.77.0.7")):
            b = self.state / "bundles" / name
            b.mkdir(parents=True)
            (b / "host.crt").write_text(f"certificate of {name}\n")
            (b / "config.yml").write_text("pki:\n  blocklist: []\n")
            if ip:
                (b / "join.env").write_text(f"K3S_TOKEN=K10abc::tok{name[:3]}.secret\nNODE_NAME={name}\nNODE_IP={ip}\n")
        self.nodes = self.tmp / "nodes.json"
        self.nodes.write_text(json.dumps({"items": [
            node("cp", "10.77.0.2"), node("carol", "10.77.0.7"), node("bob", "10.77.0.6"),
            node("bob2", "10.77.0.6"), node("bob3-dual", "10.77.0.6", "fd00::6"), node("bob4-lan", "192.0.2.44")]}))
        self.log = self.tmp / "calls.log"
        self.env = dict(os.environ, PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", RUNTIME="docker", EMESH_STATE=str(self.state),
                        EMESH_CACHE=str(cache), FAKE_NODES=str(self.nodes), FAKE_LOG=str(self.log), REVOKE_WAIT="2")

    def revoke(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(POC / "revoke.sh"), "bob"], env={**self.env, **env}, capture_output=True, text=True,
                              timeout=120)

    def left(self) -> list[str]:
        return sorted(i["metadata"]["name"] for i in json.loads(self.nodes.read_text())["items"])

    def test_every_copy_goes_and_the_guard_drops_the_name(self) -> None:
        p = self.revoke()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.left(), ["carol", "cp"])
        for copy in ("bob2", "bob3-dual", "bob4-lan"):
            self.assertIn(f"node {copy} deleted: not named after a live bundle", p.stdout)
        guard = [line for line in self.log.read_text().splitlines() if "create configmap edge-mesh-nodes" in line]
        self.assertTrue(guard and "--from-literal=carol=10.77.0.7" in guard[-1] and "--from-literal=cp=" in guard[-1]
                        and "bob" not in guard[-1], guard)

    def test_a_node_that_stays_is_exit_4(self) -> None:
        p = self.revoke(FAKE_STICKY="1")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("Nodes still registered: bob bob2 bob3-dual bob4-lan", p.stdout)
        self.assertNotIn("revoked bob", p.stdout)

    def test_an_api_that_does_not_answer_is_exit_4(self) -> None:
        p = self.revoke(FAKE_API_DOWN="1")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertNotIn("revoked bob", p.stdout)

    def test_no_node_ip_is_exit_4(self) -> None:
        env = self.state / "bundles" / "bob" / "join.env"
        env.write_text("NODE_NAME=bob\n")
        p = self.revoke()
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("no NODE_IP", p.stdout)

    def lib(self, script: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", f'. "{POC}/lib.sh"; {script}'], env={**self.env, **env}, capture_output=True, text=True,
                              timeout=60)

    def test_the_guard_admits_an_unjoined_name_only_while_its_token_works(self) -> None:
        b = self.state / "bundles" / "dave"
        b.mkdir()
        (b / "host.crt").write_text("certificate of dave")
        (b / "join.env").write_text("".join(f"{line}{chr(10)}" for line in
                                            ("K3S_TOKEN=K10abc::tokdav.secret", "NODE_NAME=dave", "NODE_IP=10.77.0.8")))
        pw = "dave.node-password.k3s"
        both = self.lib("guard_names", FAKE_TOKENS="tokdav", FAKE_SECRETS=pw).stdout.split()
        no_token = self.lib("guard_names", FAKE_TOKENS="", FAKE_SECRETS=pw).stdout.split()
        no_password = self.lib("guard_names", FAKE_TOKENS="tokdav", FAKE_SECRETS="").stdout.split()   # k3s dropped it
        self.assertIn("dave=10.77.0.8", both)
        self.assertNotIn("dave=10.77.0.8", no_token)
        self.assertNotIn("dave=10.77.0.8", no_password)
        self.assertIn("carol=10.77.0.7", no_password)      # a name with a Node stays, token and password or not
        self.assertEqual(self.lib("guard_names", FAKE_API_DOWN="1").returncode, 2)

    def test_token_ttl_is_capped_at_8_minutes(self) -> None:
        for value, want in (("8m", "480"), ("90s", "90"), ("5m", "300")):
            self.assertEqual(self.lib(f"ttl_seconds {value}").stdout.strip(), want, value)
        for value in ("9m", "10m", "15m", "1h", "30s", "x", "8m1"):
            self.assertEqual(self.lib(f"ttl_seconds '{value}'").returncode, 2, value)

    def without_flock(self, holder: str) -> subprocess.CompletedProcess:
        """with_guard_lock on the symlink lock, which some holder already took: '<pid>:<epoch>'."""
        self.state.mkdir(parents=True, exist_ok=True)
        os.symlink(holder, self.state / "guard.lock.pid")
        return self.lib("with_guard_lock echo ran", EMESH_NO_FLOCK="1", GUARD_LOCK_TRIES="3")

    def test_a_stale_lock_is_taken_over_without_flock(self) -> None:
        dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()   # a pid that has exited
        p = self.without_flock(f"{dead}:{int(time.time()) - 120}")                                       # gone, two minutes ago
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "ran"), p.stderr)
        self.assertFalse(os.path.lexists(self.state / "guard.lock.pid"))
        self.assertFalse(os.path.exists(self.state / "guard.lock.takeover"))                            # the takeover lock is released

    def test_a_young_or_live_lock_is_never_taken_over(self) -> None:
        dead = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
        for holder in (f"{dead}:{int(time.time())}", f"{os.getpid()}:{int(time.time()) - 120}"):          # just taken; alive
            with self.subTest(holder=holder):
                p = self.without_flock(holder)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn("is held by pid", p.stdout)
                self.assertEqual(os.readlink(self.state / "guard.lock.pid"), holder)
                os.unlink(self.state / "guard.lock.pid")

    def test_one_guard_writer_at_a_time(self) -> None:
        """Mutual exclusion by order, not by time: each writer marks that it tried, then, inside the lock, writes in-<name>, waits until BOTH
        writers have tried (so the other one is surely waiting at the lock, or inside it if the lock did not hold) and writes out-<name>. With a
        working lock the file can only read in-x, out-x, in-y, out-y; no timing is involved. On the CI runner this check caught the
        first mkdir-based lock letting both writers in (in-a, in-b, out-b, out-a); the symlink lock replaced it."""
        script = (
            'mark=$(mktemp -d); order="$mark/order"; : >"$order"; '
            'writer() { touch "$mark/tried-$1"; with_guard_lock sh -c \''
            'echo "in-$0" >>"$1"; i=0; until [ -e "$2/tried-a" ] && [ -e "$2/tried-b" ]; do i=$((i+1)); [ $i -lt 1200 ] || exit 1; sleep 0.05; done; '
            'echo "out-$0" >>"$1"\' "$1" "$order" "$mark"; }; '
            'writer a & writer b; wait; cat "$order"; rm -rf "$mark"')
        for env in ({}, {"EMESH_NO_FLOCK": "1"}):
            for _ in range(10):
                lines = self.lib(script, **env).stdout.split()
                self.assertIn(lines, (["in-a", "out-a", "in-b", "out-b"], ["in-b", "out-b", "in-a", "out-a"]), (env, lines))

    def test_the_guard_refresh_age(self) -> None:
        self.assertGreater(int(self.lib("guard_refresh_age").stdout), 10 ** 9)          # never refreshed: a huge age, so invite notes it
        p = self.lib("node_guard && guard_refresh_age")                                   # the stubbed API takes the write
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLessEqual(int(p.stdout.split()[-1]), 5)


if __name__ == "__main__":
    unittest.main()
