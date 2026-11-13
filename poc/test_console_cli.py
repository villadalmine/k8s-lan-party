"""The CLI as a client of the console (issue #88, docs/CONSOLE.md): the same stdout, stderr and exit code through the API as direct, for
every registry verb; the tick and panic never go through it; nothing sent means direct; busy and a plan that keeps changing exit 75; a
direct registry verb writes one audit line and its nested calls none. The daemon runs on a real unix socket with the fake engine of
poc/test_console_actions.py; `poc/mesh` itself runs where it needs no cluster (the admission ledger, the dispatch).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
sys.path.insert(0, str(HERE))
import actions  # noqa: E402
import mutate  # noqa: E402
from test_console import UnixHTTP  # noqa: E402
from test_console_actions import LINUX, DaemonCase  # noqa: E402

CLIENT = HERE / "console" / "client.py"
BASH = shutil.which("bash")


def sample(verb: str) -> dict:
    a = actions.REGISTRY[verb]
    return {k: ("bob" if k == "b" else "ana") for k in a.required}


@unittest.skipUnless(LINUX and BASH, "the daemon needs Linux (SO_PEERCRED) and bash")
class CliTest(DaemonCase):
    def client(self, words: list[str], env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLIENT), "run", "--socket", str(self.sock), "--mesh", str(self.mesh), "--", *words],
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})

    def direct(self, words: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.mesh), *words], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env={**os.environ, "EMESH_DIRECT": "1", "EMESH_AUDITED": "1"})

    def test_the_same_stdout_stderr_and_exit_code_for_every_verb(self):
        verbs = [v for v in actions.REGISTRY if v not in ("tick", "panic")]
        p = unittest.mock.patch.object(mutate, "RATE_PER_MINUTE", 1000)   # 34 calls, some of them two (plan, confirm), in seconds
        p.start()
        self.addCleanup(p.stop)
        self.assertGreater(len(verbs), 15)
        for code in ("0", "3"):
            os.environ["FAKE_EXIT"] = code
            for verb in verbs:
                with self.subTest(verb=verb, exit=code):
                    words = actions.argv(verb, sample(verb), "local")
                    via, direct = self.client(words), self.direct(words)
                    self.assertEqual((via.stdout, via.stderr, via.returncode), (direct.stdout, direct.stderr, direct.returncode))
                    self.assertEqual(via.returncode, int(code))
        self.assertEqual(len(self.audit()), 2 * len(verbs))   # every call through the API is a job of the daemon

    def test_the_local_role_sees_what_the_engine_printed(self):
        os.environ["FAKE_PLANT"] = str(self.state) + "/bundles/ana"
        r = self.client(["host", "kick", "ana"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(self.state) + "/bundles/ana", r.stdout)
        self.assertNotIn("<state>", r.stdout)
        self.assertNotIn(str(self.state), (self.state / "console" / "audit.jsonl").read_text())   # the audit stays redacted

    def test_the_tick_and_panic_never_go_through_the_api(self):
        for words in (["host", "auto", "--once"], ["host", "panic"]):
            r = self.client(words, {"EMESH_AUDITED": "1"})   # the fake engine wants the marker that mesh itself would set
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations(), [["host", "auto", "--once"], ["host", "panic"]])
        self.assertFalse((self.state / "console" / "audit.jsonl").exists())   # no job of the daemon

    def test_what_is_not_exactly_a_registry_call_runs_direct(self):
        for words in (["host", "invite", "ana", "--pub", "key.pub"], ["host", "request", "dani", "--note", "-x"], ["host", "kick", "cp"]):
            r = self.client(words, {"EMESH_AUDITED": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.mutations()), 3)
        self.assertFalse((self.state / "console" / "audit.jsonl").exists())

    def test_c15_a_socket_with_no_daemon_behind_it_runs_direct(self):
        stale = self.tmp / "stale.sock"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(stale))
        s.close()   # the file stays, nobody listens: a daemon that was killed
        r = subprocess.run([sys.executable, str(CLIENT), "run", "--socket", str(stale), "--mesh", str(self.mesh), "--", "host", "kick", "ana"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "EMESH_AUDITED": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("done kick ana", r.stdout)
        self.assertEqual(self.mutations(), [["host", "kick", "ana"]])

    def test_busy_exits_75_and_runs_nothing(self):
        os.environ.update(FAKE_SLEEP="2", FAKE_SLEEP_ONLY="burn")
        for _ in range(1 + mutate.QUEUE_MAX):
            self.assertEqual(self.post("burn", {"args": {}})[0], 202)
        r = self.client(["host", "reap"])
        self.assertEqual(r.returncode, 75)
        self.assertIn("busy", r.stderr)
        self.assertNotIn(["host", "reap"], self.mutations())

    def test_a_job_that_waits_says_so_once(self):
        os.environ.update(FAKE_SLEEP="2", FAKE_SLEEP_ONLY="burn")
        self.post("burn", {"args": {}})
        r = self.client(["host", "kick", "ana"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("waiting for kick (job "), 1)
        self.assertTrue(r.stderr.endswith("note kick\n"), r.stderr)
        self.assertEqual(r.stdout, "done kick ana \n")

    def test_a_plan_that_keeps_changing_exits_75_after_three_tries(self):
        n = iter(range(100))
        with unittest.mock.patch.object(mutate, "plan", lambda verb, args, reads: [f"plan {next(n)}"]):
            r = self.client(["host", "kick", "ana"])
        self.assertEqual(r.returncode, 75)
        self.assertIn("kept changing", r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_a_plan_that_changed_once_is_asked_again(self):
        seen = []

        def plan(verb, args, reads):
            seen.append(1)
            return ["first"] if len(seen) == 1 else ["second"]   # the plan shown, then a new state from the confirm on
        with unittest.mock.patch.object(mutate, "plan", plan):
            r = self.client(["host", "kick", "ana"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations(), [["host", "kick", "ana"]])

    def test_only_the_local_role_sees_raw_output(self):
        j = self.post("burn", {"args": {}})[1]
        self.job(j["id"])
        console = self.srv.RequestHandlerClass.console
        self.assertIn("stdout", console.raw_job(j, "local"))
        self.assertIsNone(console.raw_job(j, "console"))

    def test_a_slow_plan_is_waited_for(self):
        """The daemon's plan reads wait for the ledger's lock (60 s each at most): the CLI never gives up on a POST, only on a GET of a
        job, so a command it reported as failed never runs anyway. The GET timeout is shrunk to 1 s; the second plan takes 3 s."""
        seen = []

        def slow(verb, args, reads):
            seen.append(1)
            if len(seen) == 2:
                time.sleep(3)
            return ["the same plan"]
        code = (f"import sys; sys.path.insert(0, {str(HERE / 'console')!r}); import client; client.READ_TIMEOUT = 1; "
                f"sys.exit(client.main(sys.argv[1:]))")
        with unittest.mock.patch.object(mutate, "plan", slow):
            r = subprocess.run([sys.executable, "-c", code, "run", "--socket", str(self.sock), "--mesh", str(self.mesh), "--",
                                "host", "kick", "ana"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations(), [["host", "kick", "ana"]])

    def test_a_lost_answer_after_the_job_was_sent_says_it_may_have_run(self):
        for words in (["host", "kick", "ana"], ["host", "burn"]):   # after a confirmation; and a verb whose one POST starts the job
            with self.subTest(words=words):
                self.lost_answer(words)

    def lost_answer(self, words):
        open(self.log, "w").close()
        console = self.srv.RequestHandlerClass.console
        real = console.act

        def die_after(verb, body, *a):   # the job is submitted, then the daemon is gone before it answers
            out = real(verb, body, *a)
            if out[0] == 202:
                raise SystemExit   # the handler thread dies: the connection closes with no answer
            return out
        console.act = die_after
        self.addCleanup(setattr, console, "act", real)
        r = self.client(words)
        console.act = real
        self.assertEqual(r.returncode, 1)
        self.assertIn("it may have run", r.stderr)
        end = time.monotonic() + 10
        while not self.mutations() and time.monotonic() < end:
            time.sleep(0.1)
        time.sleep(0.5)
        self.assertEqual(self.mutations(), [words])   # it did run, once: no direct run after it

    def test_a_listener_of_another_role_gets_no_raw_output(self):
        """The handler's role, not a constant, decides: a listener of the console role (#89) built on the same handler is redacted."""
        os.environ["FAKE_PLANT"] = str(self.state) + "/bundles/ana"
        self.srv.RequestHandlerClass.role = "console"
        status, j = self.post("burn", {"args": {}})
        self.assertEqual(status, 202)
        for _ in range(100):
            c = UnixHTTP(str(self.sock))
            c.request("GET", f"/api/v1/jobs/{j['id']}")
            v = json.loads(c.getresponse().read())
            c.close()
            if v["state"] == "done":
                break
            time.sleep(0.1)
        self.assertIn("<state>/bundles/ana", v["stdout"])
        self.assertNotIn(str(self.state), json.dumps(v))

    def test_a_refusal_exits_2(self):
        self.srv.RequestHandlerClass.console.runner.rate = lambda role: (_ for _ in ()).throw(actions.Refused(403, "role", "not for you"))
        r = self.client(["host", "kick", "ana"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("not for you", r.stderr)


@unittest.skipUnless(LINUX and BASH, "needs Linux and bash")
class MeshTest(DaemonCase):
    """`poc/mesh` itself: the dispatch to the daemon, and the direct path's lock and audit (admission verbs: no cluster needed)."""

    def run_mesh(self, *words: str, **env: str) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items() if k not in ("EMESH_DIRECT", "EMESH_AUDITED", "EMESH_LOCK_HELD")}
        return subprocess.run([BASH, str(HERE / "mesh"), *words], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                              env={**base, "EMESH_STATE": str(self.state), "EMESH_CONSOLE_SOCK": str(self.sock), "RUNTIME": "docker",
                                   "POOL_CONFIG": str(HERE / "pool.yaml"), **env})

    def test_a_registry_verb_goes_to_the_daemon(self):
        r = self.run_mesh("host", "kick", "ana")
        self.assertEqual((r.returncode, r.stdout), (0, "done kick ana \n"), r.stderr)
        self.assertEqual(self.audit()[0]["source"], "socket")

    def test_emesh_direct_skips_the_daemon_and_writes_one_direct_line(self):
        r = self.run_mesh("host", "reject", "zed", EMESH_DIRECT="1")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)   # not on the list: the engine's own answer
        self.assertEqual(self.mutations(), [])
        line, = self.audit()
        self.assertEqual((line["source"], line["role"], line["verb"], line["args"], line["exit"]),
                         ("direct", "local", "reject", {"name": "zed"}, 3))

    def test_c11_a_direct_verb_waits_for_the_engine_lock(self):
        holder = subprocess.Popen([BASH, "-c", f'. "{HERE}/lib.sh"; mesh_lock; sleep 2'], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env={**{k: v for k, v in os.environ.items() if not k.startswith("EMESH_")}, "EMESH_STATE": str(self.state)})
        self.addCleanup(holder.wait, 10)
        end = time.monotonic() + 5
        while time.monotonic() < end and not (self.state / "mesh.lock.owner").exists():
            time.sleep(0.05)
        t0 = time.monotonic()
        r = self.run_mesh("host", "request", "dani", EMESH_DIRECT="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGreater(time.monotonic() - t0, 1.2)   # it waited for the holder

    def test_ctrl_c_on_a_direct_verb_still_leaves_its_audit_line(self):
        import fcntl
        import signal
        adm = self.state / "admission"
        adm.mkdir(parents=True, exist_ok=True)
        with open(adm / "lock", "a+") as ledger:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)   # the verb gets the engine's lock, then waits for the ledger: half done
            base = {k: v for k, v in os.environ.items() if not k.startswith("EMESH_")}
            p = subprocess.Popen([BASH, str(HERE / "mesh"), "host", "request", "dani"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True,
                                 env={**base, "EMESH_STATE": str(self.state), "EMESH_DIRECT": "1", "RUNTIME": "docker",
                                      "POOL_CONFIG": str(HERE / "pool.yaml")})
            end = time.monotonic() + 10
            while not (self.state / "mesh.lock.owner").exists() and time.monotonic() < end:
                time.sleep(0.05)
            time.sleep(1)   # admission.py is waiting for the ledger now
            os.killpg(p.pid, signal.SIGINT)   # what Ctrl-C sends to the terminal's foreground group
            p.communicate(timeout=30)
        self.assertNotEqual(p.returncode, 0)
        line, = self.audit()
        self.assertEqual((line["source"], line["verb"]), ("direct", "request"))
        self.assertNotEqual(line["exit"], 0)

    def test_a_read_is_never_sent_nor_audited(self):
        r = self.run_mesh("host", "waitlist")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.state / "console" / "audit.jsonl").exists())

    def test_a_direct_tick_keeps_its_nested_calls_direct(self):
        """The tick never goes through the API, and neither do the `mesh host burn` it runs inside: sent to the daemon, the burn would
        wait for the tick's own lock (and the daemon's queue) instead of running."""
        r = self.run_mesh("host", "auto", "--once")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("burn", r.stdout + r.stderr)
        self.assertEqual(self.mutations(), [])   # the fake engine behind the daemon saw nothing
        self.assertFalse((self.state / "console" / "audit.jsonl").exists())   # the tick is not audited, nor its burn

    def test_without_the_daemon_the_cli_works(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self.sock.exists():
            self.sock.unlink()
        r = self.run_mesh("host", "request", "dani", "--note", "hello")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(l["source"], l["verb"]) for l in self.audit()], [("direct", "request")])


if __name__ == "__main__":
    unittest.main()
