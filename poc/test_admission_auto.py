"""Unit tests of the automatic pool (phase B1, docs/ADMISSION.md): the tick, the switch, panic, the event log and the notify
hook, the outbox and the metrics. A simulated clock (--now) and a fake mesh: no Docker, no network, no VM."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admission
from admission import AdmissionError
from pool import DAY

HERE = Path(__file__).resolve().parent
POSIX = os.name == "posix"
CFG = ("slots: 2\nlease_seconds: 3600\ncooldown_seconds: 3600\ndaily_leases_per_identity: 2\nidentity_ipv4_prefix: 32\n"
       "identity_ipv6_prefix: 64\nidentity_retention_seconds: 90000\nlease_warning_seconds: 300\nevents_max_lines: 5000\n"
       "trusted_proxies: [127.0.0.1]\n")
# The notify hook of the tests: it records its arguments, one JSON list per call, and exits with HOOK_EXIT.
HOOK = """import json, os, sys
with open(os.environ["HOOK_OUT"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("HOOK_EXIT", "0")))
"""
IP = {"ana": "198.51.100.10", "beto": "198.51.100.20", "caro": "2001:db8:c::1", "dani": "198.51.100.40"}


def fwd(p) -> str:
    return str(p).replace("\\", "/")   # a Windows path that shlex (POSIX rules) keeps whole


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = self.tmp / "pool.yaml"
        self.cfg.write_text(CFG)
        self.state = self.tmp / "admission"
        self.bundles = self.tmp / "bundles"
        self.bundles.mkdir()
        self.calls, self.fail_kick, self.fail_invite = [], set(), False
        self.seen = {}   # name -> what `mesh host activity` says: "active" (default), "idle: <why>" or None (the API did not answer)
        for fn in ("mesh_invite", "mesh_kick", "mesh_burn", "mesh_activity"):
            self.addCleanup(setattr, admission, fn, getattr(admission, fn))
        admission.mesh_activity = lambda mesh: self._activity
        admission.mesh_invite = lambda mesh: self._invite
        admission.mesh_kick = lambda mesh: self._kick
        admission.mesh_burn = lambda mesh: (lambda: self.calls.append(("burn",)))
        hook_dir = self.tmp / "dir with space"
        hook_dir.mkdir()
        (hook_dir / "hook.py").write_text(HOOK)
        self.hook_out = self.tmp / "hook.log"
        env = {"ADMISSION_NOTIFY_CMD": f'"{fwd(sys.executable)}" "{fwd(hook_dir / "hook.py")}"', "HOOK_OUT": str(self.hook_out)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("ADMISSION_NOTIFY_IP", "HOOK_EXIT"):
            os.environ.pop(k, None)

    def _invite(self, name, ttl, trust):
        if self.fail_invite:
            raise AdmissionError(1, f"{name}: the invite failed")
        self.calls.append(("invite", name, ttl, trust))
        d = self.bundles / name
        d.mkdir()
        (d / "config.yml").write_bytes(f"pki: {name}\n".encode())
        (d / "host.key").write_bytes(f"key of {name}\n".encode())
        return str(d)

    def _activity(self, name):
        self.calls.append(("activity", name))
        return self.seen.get(name, "active")

    def _kick(self, name):
        self.calls.append(("kick", name))
        return name not in self.fail_kick

    def cli(self, *args, now=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = admission.main(["--state", str(self.state), "--config", str(self.cfg), "--bundles", str(self.bundles),
                                   "--now", str(now), *args])
        return code, out.getvalue() + err.getvalue()

    def ok(self, *args, now=0):
        code, out = self.cli(*args, now=now)
        self.assertEqual(code, 0, out)
        return out

    def ask(self, *names, now=0):
        for n in names:
            self.ok("request", n, "--ip", IP[n.split("-")[0]], now=now)

    def events(self):
        p = self.state / "events.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def kinds(self):
        return [(e["type"], e["name"]) for e in self.events()]

    def hooked(self):
        return [json.loads(l) for l in self.hook_out.read_text().splitlines()] if self.hook_out.exists() else []

    def ledger(self):
        return json.loads((self.state / "state.json").read_text())

    def leases(self):
        return sorted(l["name"] for l in self.ledger()["pool"]["leases"])

    def invited(self):
        return [c[1] for c in self.calls if c[0] == "invite"]


class TickTest(Base):
    def test_auto_is_off_by_default_and_the_tick_only_expels(self):
        self.ask("ana", "beto", "caro")
        self.assertIn("tick: auto off | expelled 0 | approved 0", self.ok("auto", "--once", now=1))
        self.assertEqual(self.invited(), [])
        self.ok("approve", now=2)                                   # the host approves by hand: ana, until 3602
        self.assertIn("expelled 0 | approved 0", self.ok("auto", "--once", now=3601))
        self.assertIn("tick: auto off | expelled 1 | approved 0 | slots 0/2 used | 2 in the list (2 eligible now)",
                      self.ok("auto", "--once", now=3602))
        self.assertEqual(self.invited(), ["ana"])                   # beto and caro wait: auto is off
        self.assertEqual([c for c in self.calls if c[0] == "kick"], [("kick", "ana")])
        self.assertIn("auto: off | panic: off", self.ok("auto", "status", now=3603))

    def test_one_tick_approves_up_to_the_slots_in_fifo_order(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on", now=1)
        out = self.ok("auto", "--once", now=2)
        self.assertIn("tick: auto on | expelled 0 | approved 2 | slots 2/2 used | 1 in the list (1 eligible now)", out)
        self.assertEqual([c for c in self.calls if c[0] == "invite"], [("invite", "ana", 3600, "public"),
                                                                       ("invite", "beto", 3600, "public")])
        self.assertIn("1    caro", self.ok("waitlist", now=3))
        self.assertEqual(self.kinds(), [("request", "ana"), ("request", "beto"), ("request", "caro"), ("auto_on", ""),
                                        ("approved", "ana"), ("bundle_ready", "ana"), ("approved", "beto"),
                                        ("bundle_ready", "beto")])

    def test_the_tick_expels_then_promotes_the_next_in_one_pass(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=2)
        out = self.ok("auto", "--once", now=3602)
        self.assertIn("expelled 2 | approved 1 | slots 1/2 used | 0 in the list", out)
        self.assertEqual(self.leases(), ["caro"])
        self.assertEqual([c for c in self.calls if c[0] != "burn"][-3:], [("kick", "ana"), ("kick", "beto"),
                                                                         ("invite", "caro", 3600, "public")])
        self.assertEqual(self.kinds()[-4:], [("expelled", "ana"), ("expelled", "beto"), ("approved", "caro"),
                                             ("bundle_ready", "caro")])
        self.assertEqual(sorted(p.name for p in (self.state / "outbox").iterdir()), ["caro.tar.gz"])   # theirs are gone

    def test_a_tick_with_nothing_to_do_changes_nothing(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=2)
        files = ("state.json", "auto.json", "events.jsonl", "outbox/ana.tar.gz", "outbox/beto.tar.gz")
        before = {f: (self.state / f).read_bytes() for f in files}
        hooks, calls = len(self.hooked()), len(self.calls)
        for now in (2, 3, 1000):
            self.assertIn("expelled 0 | approved 0 | slots 2/2 used", self.ok("auto", "--once", now=now))
            self.assertEqual({f: (self.state / f).read_bytes() for f in files}, before, now)
        self.assertEqual(len(self.hooked()), hooks)
        self.assertEqual({c[0] for c in self.calls[calls:]}, {"burn"})   # no invite, no kick

    def test_the_daily_cap_and_the_cooldown_hold_inside_auto(self):
        self.ok("auto", "on")
        self.ask("ana")
        self.ok("auto", "--once", now=0)                            # ana: first lease of 198.51.100.10
        self.ok("auto", "--once", now=3600)                         # expelled, cooldown until 7200
        self.assertIn("ana-b: deferred until 1970-01-01T02:00:00Z (cooldown)", self.ok("request", "ana-b", "--ip", IP["ana"], now=3600))
        self.ask("beto", now=3601)
        self.assertIn("approved 1", self.ok("auto", "--once", now=3602))   # beto, not ana-b: the first ELIGIBLE one
        self.assertEqual(self.leases(), ["beto"])
        self.assertIn("approved 0", self.ok("auto", "--once", now=7199))
        self.assertIn("approved 1", self.ok("auto", "--once", now=7200))   # cooldown over: the identity's second lease
        self.assertEqual(self.leases(), ["ana-b", "beto"])
        code, out = self.cli("request", "ana-c", "--ip", IP["ana"], now=7300)   # the third in 24 h: refused, not listed
        self.assertEqual(code, 6, out)
        self.assertIn("ana-c: refused: the daily cap (2 leases in 24 h) is reached; eligible again at 1970-01-02T00:00:00Z", out)
        self.assertNotIn("ana-c", self.ok("waitlist", now=7301))
        refused = [e for e in self.events() if e["type"] == "refused"]
        self.assertEqual([(e["name"], e["why"], e["retry_at"]) for e in refused], [("ana-c", "daily-cap", DAY)])

    def test_a_listed_request_over_the_cap_is_never_approved_before_its_time(self):
        self.ask("ana")                                             # auto off: the host approves by hand twice
        self.ok("approve", now=0)
        self.ok("reap", now=3600)
        self.ok("request", "ana-b", "--ip", IP["ana"], now=7200)
        self.ok("approve", now=7200)
        self.ok("reap", now=10800)
        self.assertIn("(daily-cap)", self.ok("request", "ana-c", "--ip", IP["ana"], now=10800))   # listed while auto is off
        self.ok("auto", "on", now=10801)
        for now in (10801, 20000, DAY - 1):
            self.assertIn("approved 0", self.ok("auto", "--once", now=now))
        self.assertIn("approved 1", self.ok("auto", "--once", now=DAY))
        self.assertEqual(self.invited(), ["ana", "ana-b", "ana-c"])

    def test_an_invite_that_fails_records_nothing_and_the_next_tick_tries_again(self):
        self.ask("ana")
        self.ok("auto", "on")
        self.fail_invite = True
        code, out = self.cli("auto", "--once", now=1)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.leases(), [])
        self.assertEqual(self.kinds()[-1], ("invite_failed", "ana"))
        self.fail_invite = False
        self.assertIn("approved 1", self.ok("auto", "--once", now=2))
        self.assertEqual(self.leases(), ["ana"])

    def test_a_kick_that_fails_keeps_the_lease_and_its_place(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=0)
        self.fail_kick = {"ana"}
        code, out = self.cli("auto", "--once", now=3600)
        self.assertEqual(code, 1, out)
        self.assertIn("expelled 1 | approved 1 | slots 2/2 used", out)   # beto's place went to caro; ana's is still taken
        self.assertEqual(self.leases(), ["ana", "caro"])
        self.assertIn(("kick_failed", "ana"), self.kinds())
        self.fail_kick = set()
        self.assertIn("expelled 1", self.ok("auto", "--once", now=3660))
        self.assertEqual(self.leases(), ["caro"])

    def test_a_token_ttl_over_8_minutes_stops_approvals_but_never_the_expulsion(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=0)
        with mock.patch.dict(os.environ, {"TOKEN_TTL": "15m"}):
            code, out = self.cli("auto", "--once", now=3600)
        self.assertEqual(code, 2, out)
        self.assertIn("TOKEN_TTL=15m", out)
        self.assertIn("expelled 2 | approved 0", out)
        self.assertEqual(self.invited(), ["ana", "beto"])
        self.assertIn("approved 1", self.ok("auto", "--once", now=3601))   # default TOKEN_TTL again: caro

    def test_every_tick_burns_first_with_auto_off_on_and_in_panic(self):
        """`mesh host burn` also rewrites the node guard of the manual-admission change: the tick's timer is that every-minute refresh only if every tick burns,
        whatever the switch says, and before anything else."""
        def first_call_of_each_tick(*runs):
            for args in runs:
                n = len(self.calls)
                self.ok(*args[0], now=args[1])
                self.assertEqual(self.calls[n], ("burn",), args)
        self.ask("ana")
        first_call_of_each_tick((("auto", "--once"), 1))                  # auto off (the default)
        self.ok("auto", "on", now=2)
        first_call_of_each_tick((("auto", "--once"), 3), (("auto", "--once"), 3700))   # auto on: approves, then expels
        self.ok("panic", now=3701)
        first_call_of_each_tick((("auto", "--once"), 3702))               # panic on
        self.ok("panic", "--clear", now=3703)
        first_call_of_each_tick((("auto", "--once"), 3704))               # cleared, auto still off

    def test_expiring_is_recorded_once_per_lease(self):
        self.ask("ana")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=0)
        self.ok("auto", "--once", now=3299)
        self.assertNotIn("expiring", [k for k, _ in self.kinds()])
        self.ok("auto", "--once", now=3300)
        self.ok("auto", "--once", now=3400)
        self.assertEqual([k for k in self.kinds() if k[0] == "expiring"], [("expiring", "ana")])


class IdleTest(Base):
    """The idle kick (issue #11): a lease whose node is missing, NotReady or without a workload for idle_kick_seconds is expelled early."""

    def setUp(self):
        super().setUp()
        self.cfg.write_text(CFG + "idle_kick_seconds: 600\n")
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=0)                            # ana and beto until 3600; caro waits

    def test_a_node_that_never_came_is_expelled_after_the_idle_time_and_the_place_goes_on(self):
        self.seen = {"ana": "active", "beto": "idle: no node"}
        self.assertIn("expelled 0 | approved 0", self.ok("auto", "--once", now=599))
        out = self.ok("auto", "--once", now=600)
        self.assertIn("expelled 1 | approved 1 | slots 2/2 used | 0 in the list", out)
        self.assertEqual(self.leases(), ["ana", "caro"])
        ev = [e for e in self.events() if e["type"] == "expelled"]
        self.assertEqual([(e["name"], e["how"]) for e in ev], [("beto", "idle")])
        self.assertIn("beto: lease idle, node expelled, place freed", ev[0]["summary"])
        self.assertEqual(self.ledger()["history"][-1]["how"], "idle")

    def test_idle_counts_from_the_first_tick_that_sees_an_active_node_idle(self):
        self.seen = {"ana": "active", "beto": "active"}
        self.ok("auto", "--once", now=500)
        self.seen["ana"] = "idle: no workload"
        self.ok("auto", "--once", now=1000)                         # idle since 1000
        self.assertIn("expelled 0", self.ok("auto", "--once", now=1599))
        self.seen["ana"] = "active"                                  # back to work: the clock starts again
        self.ok("auto", "--once", now=1599)
        self.seen["ana"] = "idle: node not Ready"
        self.assertIn("expelled 0", self.ok("auto", "--once", now=2000))
        self.assertIn("expelled 1 | approved 1", self.ok("auto", "--once", now=2600))
        self.assertEqual(self.leases(), ["beto", "caro"])

    def test_an_unknown_never_expels(self):
        self.seen = {"ana": None, "beto": None}
        self.assertIn("expelled 0 | approved 0", self.ok("auto", "--once", now=3000))
        self.assertEqual(self.leases(), ["ana", "beto"])

    def test_a_tick_that_sees_the_same_thing_changes_nothing(self):
        self.seen = {"ana": "active", "beto": "idle: no node"}
        self.ok("auto", "--once", now=10)
        before = (self.state / "state.json").read_bytes()
        for now in (11, 200, 599):
            self.assertIn("expelled 0 | approved 0", self.ok("auto", "--once", now=now))
            self.assertEqual((self.state / "state.json").read_bytes(), before, now)

    def test_zero_switches_it_off_and_panic_does_not_ask(self):
        self.cfg.write_text(CFG + "idle_kick_seconds: 0\n")
        self.seen = {"ana": "idle: no node", "beto": "idle: no node"}
        self.ok("auto", "--once", now=3000)
        self.assertEqual(self.leases(), ["ana", "beto"])
        self.assertNotIn("activity", {c[0] for c in self.calls})
        self.cfg.write_text(CFG + "idle_kick_seconds: 600\n")
        self.ok("panic", now=3001)
        n = len(self.calls)
        self.ok("auto", "--once", now=3002)
        self.assertNotIn("activity", {c[0] for c in self.calls[n:]})

    def test_a_failed_idle_kick_keeps_the_lease_and_says_so(self):
        self.seen = {"beto": "idle: no node"}
        self.fail_kick = {"beto"}
        code, out = self.cli("auto", "--once", now=600)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.leases(), ["ana", "beto"])
        self.assertIn(("kick_failed", "beto"), self.kinds())


class RateTest(Base):
    """The per-identity request rate limit (issue #11): rate_max_requests in rate_window_seconds; a refused request is not listed."""

    def setUp(self):
        super().setUp()
        self.cfg.write_text(CFG + "rate_window_seconds: 600\nrate_max_requests: 3\n")

    def test_the_fourth_request_in_the_window_is_refused_and_not_listed(self):
        for now in (0, 10, 20):                                      # a repeat counts too
            self.ok("request", "ana", "--ip", IP["ana"], now=now)
        n = len(self.events())
        code, out = self.cli("request", "ana-b", "--ip", IP["ana"], now=30)
        self.assertEqual(code, 6, out)
        self.assertIn("ana-b: refused: too many requests from this identity (3 in 600 s); try again at 1970-01-01T00:10:00Z "
                      "[not added]", out)
        self.assertNotIn("ana-b", self.ok("waitlist", now=31))
        self.assertEqual(len(self.events()), n)                      # no event: the log is not the spammer's
        self.assertEqual(self.cli("request", "beto", "--ip", IP["beto"], now=40)[0], 0)   # another identity is not affected
        self.assertEqual(self.cli("request", "ana-b", "--ip", IP["ana"], now=599)[0], 6)  # refusals do not count, nor extend it
        self.assertIn("already in the list as ana", self.ok("request", "ana-b", "--ip", IP["ana"], now=600))   # the window moved on

    def test_x_forwarded_for_is_believed_only_from_the_trusted_proxy(self):
        self.ok("request", "ana", "--peer", "127.0.0.1", "--xff", "198.51.100.10", now=0)   # through the proxy: the client
        self.ok("request", "beto", "--peer", "203.0.113.5", "--xff", "198.51.100.10", now=0)   # anyone else: the socket
        out = self.ok("waitlist", now=1)
        self.assertRegex(out, r"ana +waiting +now +198\.51\.100\.10")
        self.assertRegex(out, r"beto +waiting +now +203\.0\.113\.5")
        for now in (2, 3):                                           # a spoofed header does not move the limit to someone else
            self.ok("request", "beto", "--peer", "203.0.113.5", "--xff", "198.51.100.10", now=now)
        self.assertEqual(self.cli("request", "beto", "--peer", "203.0.113.5", "--xff", "198.51.100.77", now=4)[0], 6)
        self.assertEqual(self.cli("request", "ana", "--peer", "127.0.0.1", "--xff", "198.51.100.10", now=5)[0], 0)


class SwitchTest(Base):
    def test_on_off_status_and_their_events_only_on_a_change(self):
        self.assertIn("auto: off | panic: off | slots 0/2 used | 0 in the list (0 eligible now) | last tick: never",
                      self.ok("auto", "status"))
        self.ok("auto", "on", now=5)
        self.ok("auto", "on", now=6)
        self.assertEqual(json.loads((self.state / "auto.json").read_text()), {"changed_at": 5, "enabled": True, "panic": False})
        self.ok("auto", "--once", now=7)
        self.assertIn("auto: on | panic: off | slots 0/2 used | 0 in the list (0 eligible now) | last tick: 1970-01-01T00:00:07Z",
                      self.ok("auto", "status", now=8))
        self.ok("auto", "off", now=9)
        self.ok("auto", "off", now=10)
        self.assertEqual(self.kinds(), [("auto_on", ""), ("auto_off", "")])

    def test_auto_takes_once_or_one_action(self):
        self.assertEqual(self.cli("auto")[0], 2)
        self.assertEqual(self.cli("auto", "on", "--once")[0], 2)

    def test_a_broken_switch_file_is_reported_and_left_alone(self):
        self.state.mkdir()
        (self.state / "auto.json").write_text('{"enabled": "yes"}')
        code, out = self.cli("auto", "--once")
        self.assertEqual(code, 1)
        self.assertIn("unreadable", out)
        self.assertEqual((self.state / "auto.json").read_text(), '{"enabled": "yes"}')

    def test_panic_expels_everyone_closes_admission_and_is_idempotent(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=1)
        out = self.ok("panic", now=10)
        self.assertIn("panic: on | expelled 2 | auto off | slots 0", out)
        self.assertEqual(self.leases(), [])
        self.assertEqual(list((self.state / "outbox").iterdir()), [])
        self.assertEqual([e["how"] for e in self.events() if e["type"] == "expelled"], ["panic", "panic"])
        for args in (("approve",), ("approve", "caro"), ("request", "dani", "--ip", IP["dani"]), ("auto", "on")):
            code, out = self.cli(*args, now=11)
            self.assertEqual(code, 7, args)
            self.assertIn("closed by panic", out)
        self.assertIn("tick: auto off (panic) | expelled 0 | approved 0 | slots 0/0 used", self.ok("auto", "--once", now=12))
        self.assertIn("auto: off | panic: on | slots 0/0 used", self.ok("auto", "status", now=12))
        self.assertIn("emesh_admission_slots_total 0", self.ok("metrics", now=12))
        n = len(self.events())
        self.assertIn("panic: on (already) | expelled 0", self.ok("panic", now=13))
        self.assertEqual(len(self.events()), n)                     # the second panic records nothing
        self.assertIn("panic: cleared", self.ok("panic", "--clear", now=14))
        self.assertIn("panic: was not on", self.ok("panic", "--clear", now=15))
        self.assertIn("approved 0", self.ok("auto", "--once", now=16))   # cleared, but auto stays off
        self.ok("auto", "on", now=17)
        self.assertIn("approved 1", self.ok("auto", "--once", now=18))   # caro, who kept her place
        self.assertEqual(self.leases(), ["caro"])
        self.assertEqual([k for k, _ in self.kinds()].count("panic"), 1)

    def test_panic_tries_a_failed_kick_again_on_every_tick(self):
        self.ask("ana")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=1)
        self.fail_kick = {"ana"}
        self.assertEqual(self.cli("panic", now=10)[0], 1)
        self.assertEqual(self.leases(), ["ana"])
        self.fail_kick = set()
        self.assertIn("expelled 1", self.ok("auto", "--once", now=11))   # in panic the tick expels every lease, ended or not
        self.assertEqual(self.leases(), [])


class EventsTest(Base):
    def test_the_log_and_the_hook_get_every_event_in_order_with_two_arguments(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=1)
        self.ok("auto", "--once", now=3400)
        self.ok("auto", "--once", now=3601)
        self.ok("panic", now=3700)
        events, hooked = self.events(), self.hooked()
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(hooked, [[e["type"], e["summary"]] for e in events])   # exactly two arguments each, spaces kept
        self.assertEqual([e["type"] for e in events],
                         ["request", "request", "request", "auto_on", "approved", "bundle_ready", "approved", "bundle_ready",
                          "expiring", "expiring", "expelled", "expelled", "approved", "bundle_ready", "panic", "expelled"])
        text = json.dumps(hooked)
        for secret in ("198.51.100", "2001:db8", "key of", str(self.tmp)):
            self.assertNotIn(secret, text)                          # no IP, no key, no local path in what leaves the host
        self.assertTrue(all(e["path"].endswith(".tar.gz") for e in events if e["type"] == "bundle_ready"))

    def test_the_identity_reaches_the_hook_only_when_asked(self):
        os.environ["ADMISSION_NOTIFY_IP"] = "1"
        self.ask("ana")
        self.assertEqual(self.hooked()[-1], ["request", "ana: waiting, position 1 of 1 [198.51.100.10]"])

    def test_hostile_text_never_reaches_a_shell(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        for bad in ('ana"; touch PWNED1', "ana;touch PWNED2", "$(touch PWNED3)", "`touch PWNED4`", "ana\ntouch PWNED5", "a'b"):
            code, _ = self.cli("request", bad, "--ip", IP["ana"])
            self.assertEqual(code, 2, bad)
        self.assertEqual(self.hooked(), [])
        self.assertFalse((self.state / "events.jsonl").exists())
        note = '"; touch PWNED6; $(touch PWNED7) `touch PWNED8` \'x\' \n touch PWNED9 | rm -rf /'
        self.ok("request", "ana", "--ip", IP["ana"], "--note", note)
        self.assertNotIn("PWNED", json.dumps(self.hooked()) + (self.state / "events.jsonl").read_text())
        self.assertNotIn("\n", self.ledger()["requests"][0]["note"])
        hostile = 'x"; touch PWNED10; $(touch PWNED11) `touch PWNED12` \'; touch PWNED13; \' | tee PWNED14'
        self.assertEqual(admission.notify([{"type": "request", "summary": hostile, "identity": ""}]), 0)
        self.assertEqual(self.hooked()[-1], ["request", hostile])     # one argument, verbatim
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("PWNED")), [])
        for line in (self.state / "events.jsonl").read_text().splitlines():
            self.assertNotIn("note", json.loads(line))

    def test_a_failing_or_missing_hook_breaks_nothing(self):
        os.environ["HOOK_EXIT"] = "3"
        self.ask("ana")
        self.ok("auto", "on")
        code, out = self.cli("auto", "--once", now=1)
        self.assertEqual(code, 0, out)
        self.assertIn("notify: the hook failed for 2 of 2 event(s)", out)
        self.assertEqual(self.leases(), ["ana"])
        os.environ["ADMISSION_NOTIFY_CMD"] = fwd(self.tmp / "no-such-hook")
        code, out = self.cli("auto", "--once", now=3601)
        self.assertEqual(code, 0, out)
        self.assertEqual([k for k, _ in self.kinds()][-1], "expelled")   # the event is in the log anyway

    def test_the_log_is_bounded_and_forgets_identities_after_the_retention(self):
        self.cfg.write_text(CFG.replace("events_max_lines: 5000", "events_max_lines: 4"))
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=1)
        self.assertEqual(len(self.events()), 4)
        self.assertEqual([e["seq"] for e in self.events()], [5, 6, 7, 8])
        self.assertIn('emesh_admission_events_total{type="request"} 3', self.ok("metrics", now=2))   # counters survive
        self.ok("reject", "caro", now=3)
        self.ok("auto", "--once", now=3601)                          # ana and beto expelled
        self.assertIn("198.51.100.10", (self.state / "events.jsonl").read_text())
        self.ok("auto", "--once", now=3601 + 90000)
        self.assertNotIn("198.51.100", (self.state / "events.jsonl").read_text())
        self.assertNotIn("2001:db8", (self.state / "events.jsonl").read_text())


class OutboxTest(Base):
    def setUp(self):
        super().setUp()
        self.ask("ana")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=100)
        self.box = self.state / "outbox"

    def test_the_bundle_is_complete_private_and_the_same_bytes_every_time(self):
        tgz = self.box / "ana.tar.gz"
        with tarfile.open(tgz) as t:
            self.assertEqual(t.getnames(), ["ana", "ana/config.yml", "ana/host.key"])
            self.assertEqual(t.extractfile("ana/host.key").read(), b"key of ana\n")
            self.assertTrue(all(m.mtime == 100 and m.uid == 0 and m.uname == "" for m in t.getmembers()))
        if POSIX:
            self.assertEqual(tgz.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.box.stat().st_mode & 0o777, 0o700)
        first = tgz.read_bytes()
        out = self.ok("bundle", "ana", now=200)
        self.assertIn("(unchanged)", out)
        self.assertIn("join: tar xzf ana.tar.gz && sudo edge-mesh join ./ana --accept-golden-rule", out)
        self.assertEqual(tgz.read_bytes(), first)
        tgz.unlink()
        self.assertIn("(written)", self.ok("bundle", "ana", now=300))
        self.assertEqual(tgz.read_bytes(), first)
        self.assertEqual([e["path"] for e in self.events() if e["type"] == "bundle_ready"], [str(tgz), str(tgz)])

    def test_bad_names_and_names_without_a_lease_are_refused(self):
        for bad in ("../x", "ana/../../etc", "/etc/passwd", "Ana"):
            self.assertEqual(self.cli("bundle", bad)[0], 2, bad)
        self.assertEqual(self.cli("bundle", "beto")[0], 3)
        self.assertEqual(sorted(p.name for p in self.box.iterdir()), ["ana.tar.gz"])

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_symbolic_links_are_never_followed(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.bundles / "ana" / "stolen").symlink_to(self.cfg)       # a link inside the bundle is left out
        (self.box / "ana.tar.gz").unlink()
        self.ok("bundle", "ana", now=200)
        with tarfile.open(self.box / "ana.tar.gz") as t:
            self.assertNotIn("ana/stolen", t.getnames())
        shutil.rmtree(self.box)
        self.box.symlink_to(elsewhere)                               # the outbox itself is a link: refused
        self.assertEqual(self.cli("bundle", "ana", now=300)[0], 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_the_bundle_is_deleted_when_the_lease_ends(self):
        self.ok("auto", "--once", now=3699)
        self.assertTrue((self.box / "ana.tar.gz").exists())
        self.ok("auto", "--once", now=3700)
        self.assertFalse((self.box / "ana.tar.gz").exists())


class MetricsTest(Base):
    def test_the_metrics_are_fixed_text_without_identities(self):
        self.ask("ana", "beto", "caro")
        self.ok("auto", "on")
        self.ok("auto", "--once", now=100)
        out = self.ok("metrics", now=1000)
        self.assertEqual(out, self.ok("metrics", now=1000))
        samples = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertEqual(samples, [
            "emesh_admission_slots_total 2", "emesh_admission_slots_used 2", "emesh_admission_waiting 1",
            "emesh_admission_leases_active 2", "emesh_admission_lease_seconds_remaining_min 2700",
            "emesh_admission_auto_enabled 1", "emesh_admission_panic 0",
            'emesh_admission_events_total{type="request"} 3', 'emesh_admission_events_total{type="refused"} 0',
            'emesh_admission_events_total{type="approved"} 2', 'emesh_admission_events_total{type="bundle_ready"} 2',
            'emesh_admission_events_total{type="expiring"} 0', 'emesh_admission_events_total{type="expelled"} 0',
            'emesh_admission_events_total{type="rejected"} 0', 'emesh_admission_events_total{type="kick_failed"} 0',
            'emesh_admission_events_total{type="invite_failed"} 0', 'emesh_admission_events_total{type="auto_on"} 1',
            'emesh_admission_events_total{type="auto_off"} 0', 'emesh_admission_events_total{type="panic"} 0',
            'emesh_admission_events_total{type="panic_cleared"} 0', "emesh_admission_last_tick_timestamp_seconds 100"])
        self.assertIn("# TYPE emesh_admission_events_total counter", out)
        for word in ("ana", "beto", "198.51", "2001:db8"):
            self.assertNotIn(word, out)


FAKE_MESH = """#!/bin/sh
# a fake `mesh` for the concurrency test: slow on purpose, so the ticks overlap
case "$2" in
  invite) sleep 0.2; mkdir "$FAKE_BUNDLES/$3" && echo "key" > "$FAKE_BUNDLES/$3/host.key" && echo "$3" >> "$FAKE_LOG"
          echo "bundle: $FAKE_BUNDLES/$3" ;;
  kick)   echo "kick $3" >> "$FAKE_LOG"; exit "${FAKE_KICK_EXIT:-0}" ;;
  burn)   echo "burned 0" ;;
esac
"""


@unittest.skipUnless(POSIX, "an executable fake mesh (sh)")
class ConcurrencyTest(unittest.TestCase):
    """Eight ticks at the same moment, as separate processes: never more leases than slots, never a cap broken."""

    def test_concurrent_ticks_never_exceed_the_slots_or_the_cap(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "pool.yaml").write_text(CFG)
        mesh, bundles, log = tmp / "mesh", tmp / "bundles", tmp / "invites.log"
        mesh.write_text(FAKE_MESH)
        mesh.chmod(0o755)
        bundles.mkdir()
        env = dict(os.environ, FAKE_BUNDLES=str(bundles), FAKE_LOG=str(log))
        env.pop("ADMISSION_NOTIFY_CMD", None)
        base = [sys.executable, str(HERE / "admission.py"), "--state", str(tmp / "admission"), "--config", str(tmp / "pool.yaml"),
                "--mesh", str(mesh), "--bundles", str(bundles)]

        def run(*args, now=0):
            return subprocess.run([*base, "--now", str(now), *args], env=env, capture_output=True, text=True, timeout=60)

        run("request", "capped", "--ip", "10.9.9.9")
        state = json.loads((tmp / "admission" / "state.json").read_text())
        state["pool"]["grants"] = {"10.9.9.9": [10, 20]}           # two leases in the last 24 h: over the cap
        (tmp / "admission" / "state.json").write_text(json.dumps(state))
        for i in range(5):
            run("request", f"p{i}", "--ip", f"10.0.{i}.1")
        run("auto", "on")
        procs = [subprocess.Popen([*base, "--now", "100", "auto", "--once"], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(8)]
        for p in procs:
            p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0)
        data = json.loads((tmp / "admission" / "state.json").read_text())
        self.assertEqual(sorted(l["name"] for l in data["pool"]["leases"]), ["p0", "p1"])
        self.assertEqual(log.read_text().split(), ["p0", "p1"])     # exactly two invites, the first two eligible
        approved = [json.loads(l)["name"] for l in (tmp / "admission" / "events.jsonl").read_text().splitlines()
                    if json.loads(l)["type"] == "approved"]
        self.assertEqual(approved, ["p0", "p1"])


    def test_kick_exit_4_is_a_retry_not_an_expulsion(self):
        """`mesh host kick` exits 4 while a Node is left, the API does not answer or the node guard was not updated (revoke.sh): the
        tick keeps the lease and its place, records kick_failed and expels on a later tick."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "pool.yaml").write_text(CFG)
        mesh, bundles, log = tmp / "mesh", tmp / "bundles", tmp / "mesh.log"
        mesh.write_text(FAKE_MESH)
        mesh.chmod(0o755)
        bundles.mkdir()
        env = dict(os.environ, FAKE_BUNDLES=str(bundles), FAKE_LOG=str(log))
        env.pop("ADMISSION_NOTIFY_CMD", None)
        base = [sys.executable, str(HERE / "admission.py"), "--state", str(tmp / "admission"), "--config", str(tmp / "pool.yaml"),
                "--mesh", str(mesh), "--bundles", str(bundles)]

        def run(*args, now=0, **extra):
            return subprocess.run([*base, "--now", str(now), *args], env={**env, **extra}, capture_output=True, text=True, timeout=60)

        run("request", "ana", "--ip", "10.0.0.1")
        run("request", "beto", "--ip", "10.0.0.2")
        run("auto", "on")
        run("request", "caro", "--ip", "10.0.0.3")
        self.assertEqual(run("auto", "--once", now=0).returncode, 0)
        r = run("auto", "--once", now=3600, FAKE_KICK_EXIT="4")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("expelled 0 | approved 0 | slots 2/2 used", r.stdout)       # both places stay taken: caro waits
        r = run("auto", "--once", now=3660)
        self.assertIn("expelled 2 | approved 1", r.stdout)
        kinds = [json.loads(l)["type"] for l in (tmp / "admission" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(kinds.count("kick_failed"), 2)


if __name__ == "__main__":
    unittest.main()
