"""Unit tests of the manual admission ledger (admission.py) with a simulated clock. No Docker, no network."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import admission
from admission import AdmissionError, Ledger, clean_text, locked_state, token_ttl_seconds
from pool import DAY, Pool, client_ip

HERE = Path(__file__).resolve().parent
CFG = {"slots": 2, "lease_seconds": 3600, "cooldown_seconds": 3600, "daily_leases_per_identity": 2,
       "rate_window_seconds": 600, "rate_max_requests": 5, "identity_ipv4_prefix": 32, "identity_ipv6_prefix": 64,
       "trusted_proxies": ["127.0.0.1"]}


class FakeMesh:
    def __init__(self, fail_invite=False, fail_kick=False):
        self.invited, self.kicked = [], []
        self.fail_invite, self.fail_kick = fail_invite, fail_kick

    def invite(self, name, ttl, trust):
        if self.fail_invite:
            raise AdmissionError(1, "invite failed")
        self.invited.append((name, ttl, trust))
        return f"/state/bundles/{name}"

    def kick(self, name):
        self.kicked.append(name)
        return not self.fail_kick


def ledger(state, now, cfg=CFG):
    return Ledger(cfg, state, now)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.state, self.mesh = {}, FakeMesh()

    def at(self, now):
        led = ledger(self.state, now)
        return led

    def save(self, led):
        self.state = json.loads(json.dumps(led.dump()))   # through JSON, as on disk

    def approve(self, now, name=None, trust="public"):
        led = self.at(now)
        lease = led.approve(led.pick(name), trust, self.mesh.invite)
        self.save(led)
        return lease

    def reap(self, now, everyone=False):
        led, n = self.at(now), 0
        for lease in led.due(everyone):
            if self.mesh.kick(lease["name"]):
                led.finish(lease)
                n += 1
        self.save(led)
        return n

    def request(self, now, name, ident, note=""):
        led = self.at(now)
        req, what = led.request(name, ident, note)
        out = (req["state"], led.position(req), what, req["eligible_at"], req["why"])
        self.save(led)
        return out

    def test_fifo_and_slots(self):
        for i, n in enumerate(["ana", "beto", "caro"]):
            self.assertEqual(self.request(i, n, f"10.0.0.{i}")[:2], ("waiting", i + 1))
        self.assertEqual(self.approve(10)["name"], "ana")
        self.assertEqual(self.approve(11)["name"], "beto")
        with self.assertRaises(AdmissionError) as e:
            self.approve(12)
        self.assertEqual(e.exception.code, 5)
        self.assertIn("ana", str(e.exception))          # says which lease ends next
        self.assertEqual(self.mesh.invited, [("ana", 3600, "public"), ("beto", 3600, "public")])
        self.assertEqual(self.request(13, "caro", "10.0.0.2")[:3], ("waiting", 1, "already in the list"))

    def test_lease_end_frees_the_place_for_the_next_in_line(self):
        self.request(0, "ana", "10.0.0.1")
        self.request(0, "beto", "10.0.0.2")
        self.request(0, "caro", "10.0.0.3")
        self.approve(1)
        self.approve(2)
        self.assertEqual(self.reap(3600), 0)               # ana ends at 3601
        self.assertEqual(self.reap(3601), 1)
        self.assertEqual(self.mesh.kicked, ["ana"])
        self.assertEqual(self.approve(3602)["name"], "caro")

    def test_reap_is_idempotent(self):
        self.request(0, "ana", "10.0.0.1")
        self.approve(0)
        self.assertEqual(self.reap(3600), 1)
        self.assertEqual(self.reap(3600), 0)
        self.assertEqual(self.reap(9999), 0)
        self.assertEqual(self.mesh.kicked, ["ana"])
        led = self.at(9999)
        self.assertEqual(led.find("ana")["state"], "expired")
        self.assertEqual([h["how"] for h in led.history], ["expired"])

    def test_cooldown_defers_and_approve_skips_to_the_next(self):
        self.request(0, "ana", "10.0.0.1")
        self.approve(0)
        self.reap(3600)
        state, pos, _, when, why = self.request(3700, "ana-2", "10.0.0.1")
        self.assertEqual((state, pos, when, why), ("deferred", 1, 7200, "cooldown"))
        self.request(3701, "beto", "10.0.0.2")
        with self.assertRaises(AdmissionError) as e:
            self.approve(3702, "ana-2")
        self.assertEqual(e.exception.code, 6)
        self.assertEqual(self.approve(3703)["name"], "beto")   # first ELIGIBLE, not first in line
        self.assertEqual(self.approve(7200)["name"], "ana-2")  # cooldown over: waiting again

    def test_daily_cap_two_per_identity_in_24h(self):
        t = 0
        for name in ("ana", "ana-b"):
            self.request(t, name, "10.0.0.1")
            lease = self.approve(t)
            self.reap(lease["end"])
            t = lease["end"] + 3600
        state, _, _, when, why = self.request(t, "ana-c", "10.0.0.1")
        self.assertEqual((state, when, why), ("deferred", DAY, "daily-cap"))
        with self.assertRaises(AdmissionError) as e:
            self.approve(DAY - 1)
        self.assertEqual(e.exception.code, 4)
        self.assertIn("ana-c is eligible at", str(e.exception))
        self.assertEqual(self.approve(DAY)["name"], "ana-c")

    def test_approve_with_nobody_eligible_changes_nothing(self):
        with self.assertRaises(AdmissionError) as e:
            self.approve(0)
        self.assertEqual(e.exception.code, 4)
        self.assertEqual(self.mesh.invited, [])

    def test_invite_failure_records_nothing(self):
        self.request(0, "ana", "10.0.0.1")
        led = self.at(1)
        with self.assertRaises(AdmissionError):
            led.approve(led.pick(), "public", FakeMesh(fail_invite=True).invite)
        led = self.at(2)                       # what was saved before
        self.assertEqual(led.find("ana")["state"], "waiting")
        self.assertEqual(led.pool.leases, [])

    def test_kick_failure_keeps_the_lease(self):
        self.request(0, "ana", "10.0.0.1")
        self.approve(0)
        self.mesh.fail_kick = True
        self.assertEqual(self.reap(4000), 0)
        self.assertEqual(len(self.at(4000).pool.leases), 1)
        self.mesh.fail_kick = False
        self.assertEqual(self.reap(4001), 1)

    def test_reap_all_is_the_abuse_switch(self):
        self.request(0, "ana", "10.0.0.1")
        self.request(0, "beto", "10.0.0.2")
        self.approve(0)
        self.approve(0)
        self.assertEqual(self.reap(60, everyone=True), 2)
        self.assertEqual([h["how"] for h in self.at(60).history], ["ended early", "ended early"])

    def test_one_place_in_the_list_per_identity(self):
        self.request(0, "ana", "10.0.0.1")
        _, pos, what, _, _ = self.request(1, "otra", "10.0.0.1")
        self.assertEqual((pos, what), (1, "this identity is already in the list as ana; not added"))
        self.assertIsNone(self.at(2).find("otra"))

    def test_a_name_is_used_once(self):
        self.request(0, "ana", "10.0.0.1")
        self.approve(0)
        self.reap(3600)
        with self.assertRaises(AdmissionError) as e:
            self.request(DAY, "ana", "10.0.0.9")
        self.assertEqual(e.exception.code, 3)
        self.request(2, "caro", "10.0.0.3")
        with self.assertRaises(AdmissionError) as e:
            self.request(3, "caro", "10.0.0.4")     # same name, another identity
        self.assertEqual(e.exception.code, 3)

    def test_reject(self):
        self.request(0, "ana", "10.0.0.1")
        self.request(0, "beto", "10.0.0.2")
        led = self.at(1)
        self.assertEqual(led.reject("ana", "spam"), "ana: rejected")
        self.assertEqual(led.reject("ana"), "ana: already rejected")
        self.save(led)
        self.assertEqual(self.approve(2)["name"], "beto")
        with self.assertRaises(AdmissionError):
            self.at(3).reject("beto")               # has a lease: reap --name, not reject

    def test_status_line(self):
        self.request(0, "ana", "10.0.0.1")
        self.request(0, "beto", "10.0.0.2")
        self.approve(0)
        self.assertEqual(self.at(600).status_line(),
                         "admission: slots 1/2 used | 1 in the list (1 eligible now) | next to end: ana in 50m00s")

    def test_invalid_names(self):
        for bad in ("Alice", "a", "1abc", "-ab", "a;rm", "a b", "../x", "x" * 21, "lh", "cp", "n1", "", "ana\n"):
            with self.assertRaises(AdmissionError, msg=bad) as e:
                self.request(0, bad, "10.0.0.1")
            self.assertEqual(e.exception.code, 2)
        self.assertEqual(self.request(0, "a1-b" + "c" * 16, "10.0.0.1")[0], "waiting")   # 20 characters

    def test_a_name_with_a_bundle_is_taken_and_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            bundles = Path(d)
            led = Ledger(CFG, {}, 0, bundles)
            led.request("ana", "10.0.0.1")
            led.request("beto", "10.0.0.2")
            (bundles / "ana").mkdir()          # ana was invited by hand after asking
            self.assertEqual(led.pick()["name"], "beto")
            with self.assertRaises(AdmissionError) as e:
                led.pick("ana")
            self.assertEqual(e.exception.code, 3)
            (bundles / "caro").mkdir()
            with self.assertRaises(AdmissionError) as e:
                led.request("caro", "10.0.0.3")
            self.assertEqual(e.exception.code, 3)

    def test_notes_and_reasons_lose_control_characters(self):
        self.assertEqual(clean_text("hi\x1b[31mred\x1b[0m\nnext\u202eevil\x00"), "hi[31mred[0mnextevil")
        self.assertEqual(len(clean_text("x" * 500)), 200)
        _, _, _, _, _ = self.request(0, "ana", "10.0.0.1", "line\nbreak\x07")
        self.assertEqual(self.at(1).find("ana")["note"], "linebreak")


class RetentionTest(unittest.TestCase):
    """No identity outlives the retention once its lease or request closed (simulated clock)."""

    def test_identities_are_forgotten_after_the_retention(self):
        mesh = FakeMesh()
        state = {}
        def step(now, fn):
            nonlocal state
            led = Ledger(CFG, state, now)
            out = fn(led)
            state = json.loads(json.dumps(led.dump()))
            return out
        ips = ["198.51.100.1", "198.51.100.2", "2001:db8:7::/64", "198.51.100.4"]
        step(0, lambda l: [l.request(n, ip) for n, ip in zip(["ana", "beto", "caro", "dani"], ips)])
        step(1, lambda l: l.approve(l.pick(), "public", mesh.invite))
        step(2, lambda l: l.approve(l.pick(), "public", mesh.invite))
        step(3, lambda l: l.reject("caro", "spam"))
        step(3602, lambda l: [l.finish(x) for x in l.due()])     # ana and beto end
        last = 3602
        retention = DAY + CFG["cooldown_seconds"]
        text = json.dumps(state)
        self.assertIn("198.51.100.1", text)                       # still needed: cooldown, daily cap
        step(last + retention - 1, lambda l: None)
        self.assertIn("198.51.100.1", json.dumps(state))          # one second before the retention
        step(last + retention, lambda l: None)
        text = json.dumps(state)
        for ip in ips[:3]:
            self.assertNotIn(ip, text, ip)
        self.assertIn("198.51.100.4", text)                       # dani is still waiting: an open request keeps it
        self.assertNotIn("spam", text)
        self.assertEqual([r["name"] for r in state["requests"]], ["ana", "beto", "caro", "dani"])   # names stay
        with self.assertRaises(AdmissionError):
            step(last + retention, lambda l: l.request("ana", "203.0.113.9"))                     # a name is used once

    def test_the_caps_still_hold_inside_the_retention(self):
        p = Pool(CFG)
        p.grant("1.1.1.1", 0)
        p.reap("lease-1", 3600)
        p.prune(3601)
        self.assertEqual(p.eligible_at("1.1.1.1", 3601), (7200, "cooldown"))
        p.prune(7200)
        self.assertEqual(p.cooldown, {})
        self.assertEqual(p.grants, {"1.1.1.1": [0]})              # the daily cap still counts it
        p.prune(DAY)
        self.assertEqual(p.grants, {})

    def test_retention_is_never_shorter_than_the_rules(self):
        self.assertEqual(Pool(dict(CFG, identity_retention_seconds=60)).retention(), DAY)


class ProxyTest(unittest.TestCase):
    def test_a_trusted_proxy_seen_on_a_dual_stack_socket(self):
        self.assertEqual(client_ip("::ffff:127.0.0.1", "203.0.113.7", ["127.0.0.1"]), "203.0.113.7")
        self.assertEqual(client_ip("127.0.0.1", "::ffff:203.0.113.7", ["::ffff:127.0.0.1"]), "203.0.113.7")


class TokenTtlTest(unittest.TestCase):
    """A join token must die before k3s may drop the node password of an unused name (10 min): 1 to 8 minutes, like lib.sh ttl_seconds."""

    def test_accepted(self):
        for value, seconds in (("8m", 480), ("5m", 300), ("90s", 90), ("60", 60), ("480s", 480)):
            self.assertEqual(token_ttl_seconds(value), seconds, value)

    def test_refused(self):
        for value in ("9m", "10m", "15m", "1h", "481s", "30s", "", "8 m", "8min", "-5m", "8m;id"):
            with self.assertRaises(AdmissionError, msg=value) as e:
                token_ttl_seconds(value)
            self.assertEqual(e.exception.code, 2)


class CliTest(unittest.TestCase):
    """The command line on a real state file: identity from --ip/--peer/--xff, exit codes, the file, concurrency."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = Path(self.dir.name) / "pool.yaml"
        self.cfg.write_text("slots: 2\nlease_seconds: 3600\ncooldown_seconds: 3600\ndaily_leases_per_identity: 2\n"
                            "identity_ipv4_prefix: 24\nidentity_ipv6_prefix: 64\ntrusted_proxies: [127.0.0.1]\n")
        self.state = Path(self.dir.name) / "admission"

    def cli(self, *args, now=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = admission.main(["--state", str(self.state), "--config", str(self.cfg), "--now", str(now), *args])
        return code, out.getvalue() + err.getvalue()

    def test_identity_from_ip_xff_and_peer(self):
        self.assertEqual(self.cli("request", "ana", "--ip", "203.0.113.7")[0], 0)
        self.assertEqual(self.cli("request", "beto", "--ip", "2001:db8:1:2::77")[0], 0)
        self.cli("request", "caro", "--peer", "127.0.0.1", "--xff", "198.51.100.9")       # trusted proxy: believed
        self.cli("request", "dani", "--peer", "192.0.2.5", "--xff", "198.51.100.200")     # untrusted: the socket peer
        self.cli("request", "eva")                                                          # no IP: the name
        code, out = self.cli("waitlist")
        for ident in ("203.0.113.0/24", "2001:db8:1:2::/64", "198.51.100.0/24", "192.0.2.0/24", "name:eva"):
            self.assertIn(ident, out)
        code, out = self.cli("request", "fede", "--ip", "203.0.113.200")                  # same /24 as ana
        self.assertIn("already in the list as ana", out)

    def test_bad_input_exits_2_and_writes_nothing(self):
        self.assertEqual(self.cli("request", "Bad;Name", "--ip", "203.0.113.7")[0], 2)
        self.assertEqual(self.cli("request", "ana", "--ip", "not-an-ip")[0], 2)
        self.assertFalse((self.state / "state.json").exists())

    def test_request_prints_position_and_deferral(self):
        self.assertEqual(self.cli("request", "ana", "--ip", "10.1.1.1", "--note", "from the forum")[1],
                         "ana: waiting, position 1 of 1 [added]\n")
        self.assertEqual(self.cli("request", "beto", "--ip", "10.2.2.2")[1], "beto: waiting, position 2 of 2 [added]\n")

    def test_approve_refuses_a_token_ttl_over_the_cap_and_changes_nothing(self):
        self.cli("request", "ana", "--ip", "10.1.1.1")
        before = (self.state / "state.json").read_text()
        with unittest.mock.patch.dict(os.environ, {"TOKEN_TTL": "15m"}):
            code, out = self.cli("approve", now=100)
        self.assertEqual(code, 2)
        self.assertIn("1 to 8 minutes", out)
        self.assertEqual((self.state / "state.json").read_text(), before)

    def test_approve_and_reap_through_the_cli(self):
        calls = []
        self.addCleanup(setattr, admission, "mesh_invite", admission.mesh_invite)
        self.addCleanup(setattr, admission, "mesh_kick", admission.mesh_kick)
        self.addCleanup(setattr, admission, "mesh_burn", admission.mesh_burn)
        admission.mesh_burn = lambda mesh: (lambda: calls.append(("burn",)))
        admission.mesh_invite = lambda mesh: (lambda n, ttl, t: calls.append(("invite", n, ttl, t)) or f"/b/{n}")
        admission.mesh_kick = lambda mesh: (lambda n: calls.append(("kick", n)) or True)
        self.cli("request", "ana", "--ip", "10.1.1.1")
        code, out = self.cli("approve", now=100)
        self.assertEqual(code, 0)
        self.assertIn("approved ana (public) until 1970-01-01T01:01:40Z; bundle: /b/ana", out)
        self.assertEqual(self.cli("approve", now=101)[0], 4)
        leases = self.cli("leases", now=200)[1].splitlines()
        self.assertEqual(leases[1].split(), ["ana", "public", "1970-01-01T01:01:40Z", "58m20s", "10.1.1.0/24"])
        self.assertEqual(self.cli("reap", now=3699)[1], "reaped 0\n")
        self.assertEqual(self.cli("reap", now=3700)[1], "reaped ana: kicked, place freed\nreaped 1\n")
        self.assertEqual(self.cli("reap", now=3701)[1], "reaped 0\n")
        self.assertEqual(calls, [("invite", "ana", 3600, "public"), ("burn",), ("burn",), ("kick", "ana"), ("burn",)])
        code, out = self.cli("reap", "--name", "nobody", now=3702)
        self.assertEqual(code, 3)
        self.assertIn("slots 0/2 used", self.cli("status", now=3702)[1])

    def test_state_file_is_json_and_no_temp_file_is_left(self):
        self.cli("request", "ana", "--ip", "10.1.1.1")
        data = json.loads((self.state / "state.json").read_text())
        self.assertEqual(data["requests"][0]["name"], "ana")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["lock", "state.json"])

    def test_an_exception_inside_the_lock_writes_nothing(self):
        self.cli("request", "ana", "--ip", "10.1.1.1")
        before = (self.state / "state.json").read_text()
        with self.assertRaises(RuntimeError):
            with locked_state(self.state) as s:
                s["requests"] = []
                raise RuntimeError("boom")
        self.assertEqual((self.state / "state.json").read_text(), before)

    def test_a_corrupt_ledger_is_reported_and_left_alone(self):
        self.cli("request", "ana", "--ip", "10.1.1.1")
        f = self.state / "state.json"
        f.write_text(f.read_text()[:40])
        broken = f.read_text()
        code, out = self.cli("request", "beto", "--ip", "10.2.2.2")
        self.assertEqual(code, 1)
        self.assertIn("unreadable", out)
        self.assertIn("nothing was changed", out)
        self.assertEqual(f.read_text(), broken)

    def test_concurrent_requests_lose_nothing(self):
        n = 12
        procs = [subprocess.Popen([sys.executable, str(HERE / "admission.py"), "--state", str(self.state),
                                   "--config", str(self.cfg), "--now", "0", "request", f"p{i:02d}", "--ip", f"10.{i}.0.1"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(n)]
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        data = json.loads((self.state / "state.json").read_text())
        self.assertEqual(sorted(r["name"] for r in data["requests"]), [f"p{i:02d}" for i in range(n)])
        self.assertEqual(len({r["id"] for r in data["requests"]}), n)
        self.assertEqual(data["next_request"], n + 1)


if __name__ == "__main__":
    unittest.main()
