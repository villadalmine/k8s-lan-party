"""The engine's lock and the outermost layer of `poc/mesh` (issue #88, docs/CONSOLE.md "Jobs, locks and timeouts"): every registry verb is
classified as one; two mutations never run at once; a nested `mesh` does not wait for its own parent (C12c); a marker set by hand or left
behind is not trusted; a killed holder frees the lock; a process that outlives the command does not keep it. bash and lib.sh, no cluster.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
sys.path.insert(0, str(HERE))
import actions  # noqa: E402

BASH = shutil.which("bash")


@unittest.skipUnless(BASH and os.name == "posix", "needs bash")
class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ml"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("EMESH_")}
        self.env.update(EMESH_STATE=str(self.state), EMESH_CACHE=str(self.tmp / "cache"))
        self.marks = self.tmp / "marks"

    def bash(self, script: str, **env: str) -> subprocess.Popen:
        return subprocess.Popen([BASH, "-c", f'. "{HERE}/lib.sh"; {script}'], env={**self.env, **env}, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, start_new_session=True)

    def run_bash(self, script: str, timeout: float = 30, **env: str) -> subprocess.CompletedProcess:
        p = self.bash(script, **env)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            raise
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def critical(self, tag: str, seconds: float = 0.6) -> str:
        """Take the lock, then write `<tag> in`, wait, `<tag> out`: overlapping sections show as two `in` in a row."""
        return (f'mesh_lock; echo "{tag} in" >>"{self.marks}"; sleep {seconds}; '
                f'echo "{tag} out $(python3 -c "import time; print(time.time())")" >>"{self.marks}"')

    def sections(self) -> list[str]:
        return self.marks.read_text().split("\n")[:-1]

    def assertSerial(self):
        lines = self.sections()
        for i in range(0, len(lines), 2):
            self.assertEqual(lines[i].split()[0], lines[i + 1].split()[0], lines)
            self.assertEqual((lines[i].split()[1], lines[i + 1].split()[1]), ("in", "out"), lines)

    def test_every_registry_verb_is_classified_as_one(self):
        for verb, a in actions.REGISTRY.items():
            words = actions.argv(verb, {k: ("bob" if k == "b" else "ana") for k in a.required}, "local")
            r = subprocess.run([BASH, "-c", f'. "{HERE}/lib.sh"; verb_class "$@"', "x", *words], env=self.env, capture_output=True,
                               text=True, timeout=10)
            self.assertEqual(r.stdout.split(), ["registry", verb], (verb, words))
        for words, want in ((["host", "players"], "mutate"), (["host", "players", "--json", "--read-only"], "read"),
                            (["host", "party"], "mutate"), (["host", "party", "list", "--read-only", "--json"], "read"),
                            (["host", "waitlist"], "read"), (["host", "auto", "status"], "read"), (["host", "activity", "ana"], "read"),
                            (["host", "init"], "free"), (["host", "console"], "free"), (["play", "ana"], "free")):
            r = subprocess.run([BASH, "-c", f'. "{HERE}/lib.sh"; verb_class "$@"', "x", *words], env=self.env, capture_output=True,
                               text=True, timeout=10)
            self.assertEqual(r.stdout.split()[0], want, words)
        r = subprocess.run([BASH, "-c", f'. "{HERE}/lib.sh"; verb_class "$@"', "x", "host", "reap", "--name=ana"], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout.split(), ["registry", "reap-one"])   # argparse's --name=X form

    def test_c11_two_mutations_never_run_at_once(self):
        ps = [self.bash(self.critical(t)) for t in ("a", "b", "c")]
        for p in ps:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(len(self.sections()), 6)
        self.assertSerial()

    def test_c12c_a_nested_mesh_does_not_wait_for_its_parent(self):
        nested = f'bash -c \'. "{HERE}/lib.sh"; mesh_lock; echo nested >>"{self.marks}"\''
        r = self.run_bash(f'mesh_lock; python3 -c "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1], shell=True))" "{nested}"',
                          timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sections(), ["nested"])

    def test_c12c_in_a_subshell_too(self):
        """mesh_lock inside a subshell: the holder serves the subshell, so a nested call trusts it instead of waiting for it forever."""
        nested = f'bash -c \'. "{HERE}/lib.sh"; mesh_lock; echo nested >>"{self.marks}"\''
        r = self.run_bash(f'( mesh_lock; {nested} )', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sections(), ["nested"])

    def test_a_marker_set_by_hand_is_not_trusted(self):
        """While another command holds the lock: a marker naming init, this test (an ancestor that does not hold the lock), the holder
        itself (it holds it, but is not an ancestor) or nonsense is refused, and the call waits its turn. One holder per marker."""
        for n, marker in enumerate(("1", str(os.getpid()), "HOLDER", "nonsense")):
            with self.subTest(marker=marker):
                self.marks.unlink(missing_ok=True)
                holder = self.bash(self.critical(f"a{n}", 1.5))
                time.sleep(0.5)   # the holder has the lock
                r = self.run_bash(self.critical(f"b{n}", 0.1), EMESH_LOCK_HELD=str(holder.pid) if marker == "HOLDER" else marker)
                self.assertEqual(r.returncode, 0, r.stderr)
                holder.communicate(timeout=30)
                self.assertSerial()

    def test_a_stale_owner_file_is_not_trusted(self):
        """Nobody holds the lock, and the owner file left behind names an ancestor of both callers: they still take turns."""
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "mesh.lock").touch()
        (self.state / "mesh.lock.owner").write_text(f"{os.getpid()} 1\n")   # even naming a live "holder" (a reused pid)
        ps = [self.bash(self.critical(t), EMESH_LOCK_HELD=str(os.getpid())) for t in ("a", "b")]
        for p in ps:
            p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0)
        self.assertSerial()

    def test_an_owner_file_of_a_holder_that_is_gone_is_not_trusted(self):
        """The moment between a holder taking the lock and rewriting the owner file: the lock is held, and the file still names the last
        holder's pid (killed, so it could not remove it) with a served pid that is our ancestor. Not trusted: the call waits."""
        import meshlock
        lock = self.state / "mesh.lock"
        self.state.mkdir(parents=True, exist_ok=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        __import__("fcntl").flock(fd, __import__("fcntl").LOCK_EX)   # a new holder has the lock, the file is the old one's
        meshlock.owner_path(lock).write_text(f"{os.getpid()} {dead.pid}\n")
        self.assertFalse(meshlock.held_by(lock, os.getpid()))
        meshlock.owner_path(lock).write_text(f"{os.getpid()} {os.getpid()}\n")   # the same file, its holder alive: trusted
        self.assertTrue(meshlock.held_by(lock, os.getpid()))
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        meshlock.owner_path(lock).write_text(f"{os.getpid()} {child.pid}\n")   # a holder that is our child: trusted
        self.assertTrue(meshlock.held_by(lock, os.getpid()))
        meshlock.owner_path(lock).write_text(f"{os.getpid()} 1\n")   # alive, but not ours: a dead holder's pid reused
        self.assertFalse(meshlock.held_by(lock, os.getpid()))
        meshlock.owner_path(lock).write_text(f"{os.getpid()}\n")   # the old format, or a file written by hand
        self.assertFalse(meshlock.held_by(lock, os.getpid()))

    def test_a_holder_removes_the_owner_file_before_it_lets_go(self):
        r = self.run_bash(f'mesh_lock; echo a >>"{self.marks}"', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        end = time.monotonic() + 5
        while (self.state / "mesh.lock.owner").exists() and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse((self.state / "mesh.lock.owner").exists())

    def test_a_killed_holder_frees_the_lock(self):
        p = self.bash(self.critical("a", 60))
        time.sleep(0.5)
        os.killpg(p.pid, signal.SIGKILL)   # what the console daemon does at a job's timeout
        p.communicate(timeout=10)
        t0 = time.monotonic()
        r = self.run_bash(self.critical("b", 0.1), timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_process_that_outlives_the_command_does_not_keep_the_lock(self):
        r = self.run_bash(f'mesh_lock; (sleep 30 </dev/null >/dev/null 2>&1 &); echo a >>"{self.marks}"', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        t0 = time.monotonic()
        r = self.run_bash(f'mesh_lock; echo b >>"{self.marks}"', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.sections(), ["a", "b"])

    def test_the_audit_lock_is_not_the_engine_lock(self):
        """A daemon job's audit line never waits for a direct command that holds the engine's lock (a reap-all: up to 900 s)."""
        holder = self.bash(self.critical("a", 3))
        time.sleep(0.5)
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, str(HERE / "console" / "client.py"), "audit", "--state", str(self.state), "--verb", "kick",
                            "--started", str(time.time()), "--exit", "0", "--", "host", "kick", "ana"], env=self.env, capture_output=True,
                           text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 2)
        holder.communicate(timeout=30)

    def test_c12b_panic_closes_at_once_and_expels_once_the_lock_is_free(self):
        adm = self.state / "admission"
        adm.mkdir(parents=True)
        holder = self.bash(self.critical("a", 3))   # a long pass of somebody else
        time.sleep(0.5)
        p = subprocess.Popen([sys.executable, str(HERE / "admission.py"), "--state", str(adm), "--config", str(HERE / "pool.yaml"),
                              "--mesh", "/bin/false", f"--engine-lock={self.state / 'mesh.lock'}", "panic"], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        end = time.monotonic() + 5
        while time.monotonic() < end and not (adm / "auto.json").exists():
            time.sleep(0.05)
        self.assertTrue(json.loads((adm / "auto.json").read_text())["panic"])   # closed now
        time.sleep(1.5)
        self.assertIsNone(p.poll())   # the expulsions still wait for the lock (the holder keeps it 3 s)
        out, err = p.communicate(timeout=30)
        holder.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("panic: on", out)
        held_until = float(self.sections()[-1].split()[2])   # "a out <time>": the holder's last moment with the lock
        self.assertGreaterEqual((adm / "state.json").stat().st_mtime, held_until)   # the panic's pass wrote after it


if __name__ == "__main__":
    unittest.main()
