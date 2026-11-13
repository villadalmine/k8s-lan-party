"""The host console's mutations (docs/CONSOLE.md; issue #87): plans and confirm tokens, jobs, the queue, timeouts, the audit log and
the notify hook. A real daemon on a unix socket; the engine is a fake `mesh` that logs every call, can sleep, ignore SIGTERM and leave a
child behind. No cluster, no network. C.. names are the threat table's rows in docs/CONSOLE.md.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
import actions  # noqa: E402
import mutate  # noqa: E402
import server  # noqa: E402
from test_console import UnixHTTP  # noqa: E402

LINUX = hasattr(socket, "SO_PEERCRED")
SECRET = "-----BEGIN NEBULA X25519 PRIVATE KEY-----\nAAAAsecret\n-----END NEBULA X25519 PRIVATE KEY-----"

FAKE_MESH = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\n")
assert os.environ.get("EMESH_DIRECT") == "1"
k = " ".join(a[:3])
leases = json.load(open(os.environ["FAKE_LEASES"]))
if k == "host leases --json":
    print(json.dumps(leases)); sys.exit(0)
if k == "host admission-status --json":
    print(json.dumps({"slots_total": 2, "slots_used": len(leases), "lease_seconds": 3600, "leases": leases, "requests": []})); sys.exit(0)
if k == "host waitlist --json":
    print(json.dumps([{"name": "beto", "state": "waiting"}])); sys.exit(0)
if k.startswith("host players") or k.startswith("host party list"):
    print("[]"); sys.exit(0)
# a mutation
assert os.environ.get("EMESH_AUDITED") == "1", "the daemon audits; the engine must not write a second line"
if os.environ.get("FAKE_TERM_IGNORE"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("FAKE_CHILD"):   # a grandchild in the same process group, as kubectl under revoke.sh would be
    c = subprocess.Popen(["sleep", "60"])
    open(os.environ["FAKE_CHILD"], "w").write(str(c.pid))
if not os.environ.get("FAKE_SLEEP_ONLY") or a[1] == os.environ["FAKE_SLEEP_ONLY"]:
    time.sleep(float(os.environ.get("FAKE_SLEEP") or 0))
print("done " + " ".join(a[1:]) + " " + os.environ.get("FAKE_PLANT", ""))
sys.exit(int(os.environ.get("FAKE_EXIT") or 0))
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie is gone for our purpose
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


@unittest.skipUnless(LINUX, "the daemon needs Linux (SO_PEERCRED)")
class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ca"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        (self.state / "admission").mkdir(parents=True)
        self.mesh = self.tmp / "mesh"
        self.mesh.write_text(FAKE_MESH)
        self.mesh.chmod(0o755)
        self.log = self.tmp / "calls"
        self.leases = self.tmp / "leases.json"
        self.set_leases([{"name": "ana", "end": 3600, "trust": "public", "start": 0, "left_seconds": 1, "identity": "x"}])
        env = {"FAKE_LOG": str(self.log), "FAKE_LEASES": str(self.leases)}
        for k in ("FAKE_SLEEP", "FAKE_SLEEP_ONLY", "FAKE_EXIT", "FAKE_TERM_IGNORE", "FAKE_CHILD", "FAKE_PLANT", "ADMISSION_NOTIFY_CMD"):
            env[k] = ""
        p = unittest.mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.sock = self.state / "console" / "api.sock"
        self.srv = server.make_server(self.state, self.mesh, self.sock)
        self.console = self.srv.RequestHandlerClass.console
        self.console.runner.kill_grace = 1
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def set_leases(self, ls):
        self.leases.write_text(json.dumps(ls))

    def post(self, verb: str, body) -> tuple[int, dict]:
        raw = json.dumps(body).encode()
        c = UnixHTTP(str(self.sock))
        c.request("POST", f"/api/v1/actions/{verb}", body=raw, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        return r.status, out

    def job(self, job_id: str, wait: float = 15) -> dict:
        end = time.monotonic() + wait
        while True:
            c = UnixHTTP(str(self.sock))
            c.request("GET", f"/api/v1/jobs/{job_id}")
            j = json.loads(c.getresponse().read())
            c.close()
            if j["state"] not in ("queued", "running") or time.monotonic() > end:
                return j
            time.sleep(0.05)

    def mutations(self) -> list:
        calls = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        return [c for c in calls if not any(f in c for f in ("--json", "--read-only"))]

    def audit(self) -> list:
        p = self.state / "console" / "audit.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def confirm(self, verb, args, typed=None):
        status, plan = self.post(verb, {"args": args})
        self.assertEqual(status, 200, plan)
        body = {"args": args, "confirm": plan["confirm"]}
        if typed is not None:
            body["typed"] = typed
        return plan, self.post(verb, body)

    def test_a_plain_verb_is_a_job_with_its_output_and_one_audit_line(self):
        status, j = self.post("burn", {"args": {}})
        self.assertEqual((status, j["state"] in ("queued", "running", "done")), (202, True))
        j = self.job(j["id"])
        self.assertEqual((j["state"], j["exit"]), ("done", 0))
        self.assertIn("done burn", j["output"])
        self.assertEqual(self.mutations(), [["host", "burn"]])
        [line] = self.audit()
        self.assertEqual((line["verb"], line["source"], line["role"], line["exit"], line["confirmed"], line["job"]),
                         ("burn", "socket", "local", 0, False, j["id"]))
        self.assertEqual(oct((self.state / "console" / "audit.jsonl").stat().st_mode & 0o777), "0o600")

    def test_c09_a_dangerous_verb_without_confirmation_runs_nothing(self):
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        self.assertEqual(status, 200)
        self.assertEqual(plan["type_to_confirm"], "ana")
        self.assertEqual(plan["expires_in"], 60)
        self.assertIn("kick ana", plan["will"][0])
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.audit(), [])

    def test_c09_c10_wrong_reused_mismatched_expired_tokens_and_typing_run_nothing(self):
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        tok = plan["confirm"]
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": "x" * 43, "typed": "ana"})[0], 410)
        self.assertEqual(self.post("revoke", {"args": {"name": "ana"}, "confirm": tok, "typed": "ana"})[0], 410)   # other verb: spent
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        self.assertEqual(self.post("kick", {"args": {"name": "beto"}, "confirm": plan["confirm"], "typed": "beto"})[0], 410)
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "anna"})[0], 400)  # kept
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        with unittest.mock.patch.object(self.console.plans, "clock", lambda: time.monotonic() + 61):
            self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"})[0], 410)
        self.assertEqual(self.mutations(), [])
        _, (status, j) = self.confirm("kick", {"name": "ana"}, "ana")
        self.assertEqual(status, 202)
        self.assertEqual(self.job(j["id"])["exit"], 0)
        self.assertEqual(self.mutations(), [["host", "kick", "ana"]])
        self.assertTrue(self.audit()[0]["confirmed"])

    def test_reap_all_wants_the_word_all(self):
        status, plan = self.post("reap-all", {"args": {}})
        self.assertEqual(plan["type_to_confirm"], "all")
        self.assertEqual(self.post("reap-all", {"args": {}, "confirm": plan["confirm"]})[0], 400)
        _, (status, j) = self.confirm("reap-all", {}, "all")
        self.assertEqual(self.job(j["id"])["exit"], 0)
        self.assertEqual(self.mutations(), [["host", "reap", "--all"]])

    def test_the_plan_changed_since_it_was_shown(self):
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        self.set_leases([])   # ana's lease ended meanwhile
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"})[0], 409)
        self.assertEqual(self.mutations(), [])

    def test_a_queued_job_checks_its_plan_again_when_it_starts(self):
        os.environ["FAKE_SLEEP"] = "1.5"
        status, first = self.post("burn", {"args": {}})
        _, (status, j) = self.confirm("kick", {"name": "ana"}, "ana")
        self.assertEqual((status, j["state"]), (202, "queued"))
        self.set_leases([])   # while it waits, the state changes
        j = self.job(j["id"])
        self.assertEqual((j["state"], j["exit"], j["error"]), ("failed", 75, "plan-changed"))
        self.assertNotIn(["host", "kick", "ana"], self.mutations())

    def test_c11_one_at_a_time_and_409_when_the_queue_is_full(self):
        os.environ["FAKE_SLEEP"] = "0.6"
        ids = [self.post("burn", {"args": {}})[1]["id"] for _ in range(5)]   # one running, four waiting
        status, out = self.post("burn", {"args": {}})
        self.assertEqual((status, out["error"]), (409, "busy"))
        jobs = [self.job(i, 20) for i in ids]
        self.assertTrue(all(j["exit"] == 0 for j in jobs))
        spans = sorted((j["started"], j["ended"]) for j in jobs)
        self.assertEqual(len(self.mutations()), 5)
        self.assertEqual(len(self.audit()), 5)
        for (s1, e1), (s2, _) in zip(spans, spans[1:]):   # serialized: each starts after the previous one ended
            self.assertLessEqual(e1, s2)

    def test_c12b_panic_skips_the_queue_and_has_no_timeout(self):
        os.environ.update(FAKE_SLEEP="3", FAKE_SLEEP_ONLY="burn")
        slow = self.post("burn", {"args": {}})[1]["id"]
        time.sleep(0.2)
        _, (status, p) = self.confirm("panic", {})
        self.assertEqual(status, 202)
        self.assertEqual(actions.REGISTRY["panic"].timeout_seconds, None)
        p = self.job(p["id"], 1.5)
        self.assertEqual((p["state"], p["exit"]), ("done", 0))            # finished...
        self.assertEqual(self.console.runner.get(slow).state, "running")  # ...while the burn still runs: it did not wait
        self.job(slow)
        self.assertIn(["host", "panic"], self.mutations())

    def test_c12_a_timeout_kills_the_process_group(self):
        child = self.tmp / "child.pid"
        os.environ.update(FAKE_SLEEP="30", FAKE_TERM_IGNORE="1", FAKE_CHILD=str(child))
        short = dataclasses.replace(actions.REGISTRY["burn"], timeout_seconds=1)
        with unittest.mock.patch.dict(actions.REGISTRY, {"burn": short}):
            j = self.post("burn", {"args": {}})[1]
            j = self.job(j["id"], 20)
        self.assertEqual((j["state"], j["exit"]), ("timeout", 124))
        pid = int(child.read_text())
        end = time.monotonic() + 5
        while alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(alive(pid), "the grandchild survived: the process group was not killed")
        self.assertEqual(self.audit()[0]["exit"], 124)

    def test_c07_hostile_args_through_http_never_reach_the_engine(self):
        for args in ({"name": "ana;id"}, {"name": "$(id)"}, {"name": "--all"}, {"name": "cp"}, {"name": "ana", "x": 1}, {"name": 5}):
            self.assertEqual(self.post("kick", {"args": args})[0], 400, args)
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "extra": 1})[0], 400)
        self.assertEqual(self.post("nuke", {"args": {}})[0], 404)
        c = UnixHTTP(str(self.sock))   # not JSON: 415; no body rules bypassed
        c.request("POST", "/api/v1/actions/burn", body=b"name=ana", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(c.getresponse().status, 415)
        c.close()
        self.assertEqual(self.mutations(), [])

    def test_c08_the_console_role_through_act(self):
        for verb, args in (("invite", {"name": "ana"}), ("bundle", {"name": "ana"}), ("approve", {"trust": "friends"})):
            with self.assertRaises(actions.Refused) as e:
                self.console.act(verb, {"args": args}, "console", "tok:x", "web", 1000)
            self.assertEqual(e.exception.status, 403)
        self.assertEqual(self.mutations(), [])

    def test_c13_no_secret_in_job_output_or_audit_and_notify_for_the_console_role(self):
        os.environ["FAKE_PLANT"] = SECRET + " " + str(self.state) + "/ca/ca.key"
        hook = self.tmp / "hook.py"
        out = self.tmp / "hook.out"
        hook.write_text(f"import sys, json\nopen({str(out)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        os.environ["ADMISSION_NOTIFY_CMD"] = f"{sys.executable} {hook}"
        status, j = self.console.act("request", {"args": {"name": "dani", "note": "hi"}}, "console", "tok:x", "web", 1000)
        self.assertEqual(status, 202)
        j = self.job(j["id"])
        self.assertNotIn("AAAAsecret", j["output"])
        self.assertIn("<pem>", j["output"])
        self.assertIn("<state>/ca/ca.key", j["output"])
        text = (self.state / "console" / "audit.jsonl").read_text()
        self.assertNotIn("AAAAsecret", text)
        self.assertEqual(self.audit()[0]["role"], "console")
        end = time.monotonic() + 5
        while not out.exists() and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(json.loads(out.read_text().splitlines()[0]), ["console_action", "request dani: exit 0"])   # no note
        os.environ["ADMISSION_NOTIFY_CMD"] = f"{sys.executable} {hook}"   # a local job does not notify
        j = self.job(self.post("burn", {"args": {}})[1]["id"])
        time.sleep(0.3)
        self.assertEqual(len(out.read_text().splitlines()), 1)

    def test_c10_a_token_is_bound_to_the_credential_it_was_issued_to(self):
        status, plan = self.console.act("kick", {"args": {"name": "ana"}}, "local", "uid:1000", "socket", 1000)
        with self.assertRaises(actions.Refused) as e:
            self.console.act("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"}, "local", "uid:1001", "socket", 1001)
        self.assertEqual(e.exception.status, 410)
        self.assertEqual(self.mutations(), [])

    def test_a_typo_does_not_spend_the_token(self):
        status, plan = self.post("kick", {"args": {"name": "ana"}})
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "anna"})[0], 400)
        status, j = self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"})
        self.assertEqual(status, 202)
        self.job(j["id"])
        self.assertEqual(self.post("kick", {"args": {"name": "ana"}, "confirm": plan["confirm"], "typed": "ana"})[0], 410)   # spent
        self.assertEqual(self.mutations(), [["host", "kick", "ana"]])

    def test_one_panic_at_a_time(self):
        os.environ.update(FAKE_SLEEP="1.5", FAKE_SLEEP_ONLY="panic")
        _, (status, first) = self.confirm("panic", {})
        _, (status, second) = self.confirm("panic", {})
        self.assertEqual(second["id"], first["id"])
        self.job(first["id"])
        self.assertEqual(self.mutations(), [["host", "panic"]])

    def test_a_finished_job_is_seen_whole(self):
        """The state is written last: whoever sees done or failed also sees the output, the end time and the audit line."""
        os.environ["FAKE_SLEEP"] = "0.3"
        j = self.post("burn", {"args": {}})[1]
        end = time.monotonic() + 10
        while time.monotonic() < end:
            c = UnixHTTP(str(self.sock))
            c.request("GET", f"/api/v1/jobs/{j['id']}")
            v = json.loads(c.getresponse().read())
            c.close()
            if v["state"] not in ("queued", "running"):
                self.assertIsNotNone(v["ended"])
                self.assertIn("done burn", v["output"])
                self.assertEqual(len(self.audit()), 1)
                break
        else:
            self.fail("the job never finished")

    def test_an_audit_that_cannot_be_written_never_stops_the_queue(self):
        self.job(self.post("burn", {"args": {}})[1]["id"])   # the audit file exists
        audit = self.state / "console" / "audit.jsonl"
        audit.chmod(0o400)
        self.addCleanup(audit.chmod, 0o600)
        if os.access(audit, os.W_OK):
            self.skipTest("running as root: a 0400 file stays writable")
        jobs = [self.job(self.post("burn", {"args": {}})[1]["id"]) for _ in range(2)]
        self.assertEqual([(j["state"], j["exit"], j["error"]) for j in jobs], [("done", 0, "audit")] * 2)
        audit.chmod(0o600)
        self.assertEqual(self.job(self.post("burn", {"args": {}})[1]["id"])["error"], None)   # and it recovers

    def test_a_job_that_breaks_the_runner_ends_and_the_queue_moves_on(self):
        real = self.console.runner._run
        calls = []

        def broken_once(job):
            calls.append(job.id)
            if len(calls) == 1:
                raise RuntimeError("a bug in the daemon")
            return real(job)
        self.console.runner._run = broken_once
        jobs = [self.post("burn", {"args": {}})[1] for _ in range(2)]
        first, second = (self.job(j["id"]) for j in jobs)
        self.assertEqual((first["state"], first["error"]), ("failed", "internal"))
        self.assertIsNotNone(first["ended"])
        self.assertEqual((second["state"], second["exit"]), ("done", 0))

    def test_the_notify_line_goes_through_the_redactor(self):
        sent = []
        self.console.runner.notify = sent.append
        with unittest.mock.patch.object(mutate, "notify_summary", lambda job: SECRET):
            j = self.console.act("burn", {"args": {}}, "console", "tok:x", "web", 1000)[1]
            self.job(j["id"])
        self.assertEqual(len(sent), 1)
        self.assertNotIn("AAAAsecret", sent[0])

    def test_rate_limit(self):
        with unittest.mock.patch.object(mutate, "RATE_PER_MINUTE", 3):
            for _ in range(3):
                self.assertEqual(self.post("auto-off", {"args": {}})[0], 202)
            self.assertEqual(self.post("auto-off", {"args": {}})[0], 429)

    def test_audit_rotates_at_its_size(self):
        with unittest.mock.patch.object(mutate, "AUDIT_MAX", 600):
            for _ in range(6):
                self.job(self.post("burn", {"args": {}})[1]["id"])
        d = self.state / "console"
        self.assertTrue((d / "audit.jsonl.1").exists())
        self.assertLessEqual((d / "audit.jsonl").stat().st_size, 600)
        lines = (d / "audit.jsonl").read_text().splitlines() + (d / "audit.jsonl.1").read_text().splitlines()
        self.assertTrue(all(json.loads(l)["verb"] == "burn" for l in lines))


if __name__ == "__main__":
    unittest.main()
