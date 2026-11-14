"""The host console's daemon and registry (docs/CONSOLE.md, issue #86): a real server on a unix socket in a temporary directory, the
engine replaced by a fake `mesh` that logs every call and answers canned JSON. No cluster, no network.

The C.. names are the threat table's rows in docs/CONSOLE.md.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "console"))
import actions  # noqa: E402
import server  # noqa: E402

SPEC = json.loads((HERE / "console" / "openapi.json").read_text(encoding="utf-8"))
LINUX = hasattr(socket, "SO_PEERCRED")

PEM_KEY = "-----BEGIN NEBULA X25519 PRIVATE KEY-----\nAAAAsecretsecretsecret\n-----END NEBULA X25519 PRIVATE KEY-----"
JOIN_TOKEN = "K10deadbeefcafe0123::server:abcdef0123456789"
SA_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJl"
CLAIM_CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-Z"
LH_RSH = "ssh -i /root/.ssh/lh-key admin@203.0.113.99"
PULLER = "puller-token-0123456789abcdef0123456789abcdef"

FAKE_MESH = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\n")
assert os.environ.get("EMESH_DIRECT") == "1", "the daemon runs the engine direct"
plant = os.environ.get("FAKE_PLANT", "")
state = os.environ["FAKE_STATE"]
key = " ".join(a[:3])
if key == "host admission-status --json":
    print(json.dumps({"auto": False, "panic": False, "slots_total": 2, "slots_used": 1, "waiting": 1, "eligible": 1, "now": 5,
                      "lease_seconds": 3600, "leases": [{"name": "ana", "end": 99}], "requests": [{"name": "beto"}]}))
elif key == "host waitlist --json":
    print(json.dumps([{"position": 1, "name": "beto", "state": "waiting", "eligible_at": 0, "why": "", "identity": "203.0.113.0/24",
                       "note": plant, "requested_at": 1}]))
elif key == "host leases --json":
    print(json.dumps([{"name": "ana", "trust": "public", "start": 1, "end": 99, "left_seconds": 94, "identity": "198.51.100.0/24"}]))
elif key == "host players --json":
    print(json.dumps([{"name": "ana", "ready": "True", "cert_expires": "2030-01-01T00:00:00Z"}]))
elif key == "host party list":
    print(json.dumps([{"player": "ana", "peer": "beto", "status": "waiting " + plant}]))
elif key == "host admission-metrics --read-only":
    print("emesh_admission_waiting 1\n# " + plant)
elif a[:2] == ["host", "activity"]:
    if a[2] == "ana":
        print("active")
    elif a[2] == "beto":
        print("idle: no workload since " + state + "/bundles/beto"); sys.exit(1)
    else:
        print("unknown: the API did not answer"); sys.exit(2)
elif a[:2] == ["host", "broken"]:
    sys.exit(3)
else:
    print("unexpected", a, file=sys.stderr); sys.exit(9)
'''


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=20)
        self.unix = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        self.sock.connect(self.unix)


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
            for p in sorted(root.rglob("*")) if not p.is_socket()}


class RegistryTest(unittest.TestCase):
    """The registry (poc/console/actions.py) is the spec's x-actions; argv() refuses before anything runs (C07, C08)."""

    def test_registry_is_the_spec(self):
        spec = {a["verb"]: a for a in SPEC["x-actions"]}
        self.assertEqual(sorted(spec), sorted(actions.REGISTRY))
        for verb, a in actions.REGISTRY.items():
            s = spec[verb]
            self.assertEqual((a.engine, a.params, list(a.required), a.confirm, list(a.roles), a.timeout_seconds, a.summary),
                             (s["engine"], s["params"], s["required"], s["confirm"], s["roles"], s["timeout_seconds"], s["summary"]), verb)
            self.assertEqual({k: {v: list(r) for v, r in m.items()} for k, m in a.arg_roles.items()}, s["arg_roles"], verb)

    def test_argv(self):
        self.assertEqual(actions.argv("kick", {"name": "ana"}, "console"), ["host", "kick", "ana"])
        self.assertEqual(actions.argv("approve", {}, "console"), ["host", "approve", "--trust", "public"])
        self.assertEqual(actions.argv("approve", {"name": "ana", "trust": "friends"}, "local"),
                         ["host", "approve", "ana", "--trust", "friends"])
        self.assertEqual(actions.argv("request", {"name": "ana", "note": "from the forum"}, "local"),
                         ["host", "request", "ana", "--note", "from the forum"])
        self.assertEqual(actions.argv("invite", {"name": "ana"}, "local"), ["host", "invite", "ana", "--ttl", "2h", "--trust", "friends"])
        self.assertEqual(actions.argv("party-end", {"a": "ana", "b": "beto"}, "console"), ["host", "party", "end", "ana", "beto"])
        self.assertEqual(actions.argv("panic", {}, "console"), ["host", "panic"])

    def test_c07_hostile_values_never_reach_the_engine(self):
        for bad in ("ana;id", "$(id)", "`id`", "ana\nbeto", "../etc", "--all", "Ana", "a", "x" * 21, "ana beto", "",
                    "cp", "lh", "n1", "n2"):   # the control plane, the lighthouse, the PoC's own nodes: never a player
            with self.assertRaises(actions.Refused, msg=bad) as e:
                actions.argv("kick", {"name": bad}, "local")
            self.assertEqual(e.exception.status, 400)
        for args in ({}, {"name": "ana", "extra": "x"}, {"name": 5}, {"name": ["ana"]}, ["ana"]):
            with self.assertRaises(actions.Refused, msg=repr(args)):
                actions.argv("kick", args, "local")
        for note in ("line\nbreak", "-starts-like-a-flag", "x" * 201, "bell\x07"):
            with self.assertRaises(actions.Refused, msg=note):
                actions.argv("request", {"name": "ana", "note": note}, "local")
        with self.assertRaises(actions.Refused):
            actions.argv("invite", {"name": "ana", "ttl": "2h;id"}, "local")
        for verb, args in (("revoke", {"name": "cp"}), ("reap-one", {"name": "lh"}), ("party-end", {"a": "cp", "b": "ana"})):
            with self.assertRaises(actions.Refused, msg=verb):
                actions.argv(verb, args, "local")
        self.assertEqual(actions.argv("kick", {"name": "cpu"}, "local"), ["host", "kick", "cpu"])   # only the exact names
        self.assertEqual(actions.argv("refresh", {"name": "cp"}, "local"), ["host", "refresh", "cp"])

    def test_c08_console_never_reaches_the_persistent_ca_or_a_key(self):
        for verb, args in (("invite", {"name": "ana"}), ("bundle", {"name": "ana"}), ("refresh", {"name": "cp"}),
                           ("approve", {"trust": "friends"}), ("approve", {"name": "ana", "trust": "friends"})):
            with self.assertRaises(actions.Refused, msg=verb) as e:
                actions.argv(verb, args, "console")
            self.assertEqual(e.exception.status, 403)
        for verb in ("init", "down", "play", "console", "nuke"):
            with self.assertRaises(actions.Refused) as e:
                actions.argv(verb, {}, "local")
            self.assertEqual(e.exception.status, 404)
        self.assertEqual({a["verb"] for a in actions.describe("console")} & {"invite", "bundle", "refresh"}, set())
        # GET /verbs shows them to the console role as not allowed, with where to type them; init and down are never anybody's
        shown = {v["verb"]: v for v in actions.verbs("console")}
        self.assertEqual({k for k, v in shown.items() if not v["allowed"]}, {"invite", "bundle", "refresh", "init", "down"})
        self.assertEqual(shown["invite"]["command"], "mesh host invite <name> --ttl <ttl> --trust <trust>")
        self.assertTrue(all(v["why"] for v in shown.values() if not v["allowed"]) and not any(v["why"] for v in shown.values() if v["allowed"]))
        local = {v["verb"]: v["allowed"] for v in actions.verbs("local")}
        self.assertEqual({k for k, ok in local.items() if not ok}, {"init", "down"})
        self.assertEqual([c["value"] for v in actions.verbs("console") for c in v["closed_values"]], ["friends"])
        self.assertEqual([c for v in actions.verbs("local") for c in v["closed_values"]], [])
        self.assertEqual(set(shown) - {"init", "down"}, set(actions.REGISTRY))


@unittest.skipUnless(LINUX, "the daemon needs Linux (SO_PEERCRED)")
class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="c"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        (self.state / "admission").mkdir(parents=True)
        (self.state / "admission" / "last-tick").write_text("1234\n")
        (self.state / "admission" / "events.jsonl").write_text(
            "".join(json.dumps({"seq": i, "ts": 100 * i, "type": "request", "name": f"p{i}", "summary": f"p{i} asked"}) + "\n"
                    for i in range(1, 8)) + "not json\n")
        # the form lighthouse-vps.sh writes: printf 'LH_RSH=%q' (backslash-escaped spaces), not a quoted string
        q = subprocess.run(["bash", "-c", 'printf "LH_RSH=%q\\n" "$1"', "_", LH_RSH], capture_output=True, text=True, check=True).stdout
        self.assertIn("\\ ", q)
        (self.state / "lighthouse.env").write_text("LH_EXTERNAL=203.0.113.99\n" + q)
        (self.state / "page").mkdir()
        (self.state / "page" / "puller.token").write_text(PULLER + "\n")
        self.mesh = self.tmp / "mesh"
        self.mesh.write_text(FAKE_MESH)
        self.mesh.chmod(0o755)
        self.log = self.tmp / "calls"
        env = {"FAKE_LOG": str(self.log), "FAKE_STATE": str(self.state), "FAKE_PLANT": ""}
        self.env = unittest.mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sock = self.state / "console" / "api.sock"
        self.start()

    def start(self):
        self.srv = server.make_server(self.state, self.mesh, self.sock)
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        c = UnixHTTP(str(self.sock))
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def get(self, path: str):
        status, data, r = self.call("GET", "/api/v1/" + path)
        return status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data.decode())

    def engine_calls(self) -> list:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_reads(self):
        self.assertEqual(self.get("health"), (200, {"ok": True, "role": "local", "version": server.VERSION}))
        status, s = self.get("status")
        self.assertEqual(status, 200)
        self.assertNotIn("requests", s)
        self.assertEqual((s["slots_used"], s["last_tick"]), (1, 1234))
        self.assertEqual(self.get("requests")[1][0]["name"], "beto")
        self.assertEqual(self.get("leases")[1][0]["left_seconds"], 94)
        self.assertEqual(self.get("players")[1][0]["ready"], "True")
        self.assertEqual(self.get("parties")[1][0]["peer"], "beto")
        self.assertIn("emesh_admission_waiting 1", self.get("metrics")[1])
        self.assertEqual(self.get("players/ana/activity")[1], {"name": "ana", "state": "active", "why": None})
        self.assertEqual(self.get("players/beto/activity")[1]["state"], "idle")
        self.assertEqual(self.get("players/carol/activity")[1]["state"], "unknown")
        self.assertEqual({a["verb"] for a in self.get("actions")[1]}, set(actions.REGISTRY))
        self.assertEqual([e["seq"] for e in self.get("events?limit=3")[1]], [5, 6, 7])
        self.assertEqual([e["seq"] for e in self.get("events?since=600")[1]], [6, 7])
        self.assertEqual(self.get("audit")[1], [])

    def test_every_route_is_in_the_spec_and_answers(self):
        spec_paths = set(SPEC["paths"])
        for route in server.ROUTES:
            self.assertIn(f"/api/v1/{route}", spec_paths)
        for p in spec_paths:
            if "get" in SPEC["paths"][p]:
                path = p.replace("{name}", "ana").replace("{id}", "0123456789abcdef")
                status, _ = self.get(path[len("/api/v1/"):])
                self.assertIn(status, (200, 404) if "{id}" in p else (200,), p)   # no job exists before #87

    def test_the_spec_lists_every_status_a_get_can_answer(self):
        for p, item in SPEC["paths"].items():
            if "get" in item:
                self.assertTrue({"400", "404", "405", "500", "502", "503", "504"} <= set(item["get"]["responses"]), p)

    def test_c14_a_get_changes_no_file_and_runs_only_pure_reads(self):
        before = snapshot(self.state)
        for p in server.ROUTES + ("players/ana/activity", "jobs/0123456789abcdef"):   # every GET route, so a new one is covered
            self.get(p)
            self.call("HEAD", "/api/v1/" + p)
        self.assertEqual(snapshot(self.state), before)
        pure = [args for args, _ in server.READS.values()]
        for call in self.engine_calls():
            self.assertTrue(call in pure or call[:2] == ["host", "activity"], call)
            self.assertNotIn(call[1], {"burn", "reap", "kick", "revoke", "approve", "auto", "panic", "invite", "bundle", "refresh"})
        self.assertIn(["host", "players", "--json", "--read-only"], self.engine_calls())
        self.assertIn(["host", "party", "list", "--read-only", "--json"], self.engine_calls())

    def test_c13_no_secret_in_any_response(self):
        plant = " ".join((PEM_KEY, JOIN_TOKEN, SA_TOKEN, CLAIM_CODE, LH_RSH, PULLER, str(self.state) + "/ca/ca.key", "/home/alice/x"))
        os.environ["FAKE_PLANT"] = plant
        with open(self.state / "admission" / "events.jsonl", "a") as f:
            f.write(json.dumps({"seq": 99, "ts": 9999, "type": "bundle_ready", "summary": plant, "path": plant}) + "\n")
        seen = ""
        for p in ("requests", "parties", "metrics", "events", "players/beto/activity", "status"):
            status, data, _ = self.call("GET", "/api/v1/" + p)
            self.assertEqual(status, 200, p)
            seen += data.decode()
        for secret in ("BEGIN NEBULA", "AAAAsecret", "K10deadbeef", "abcdef0123456789", SA_TOKEN[:20], CLAIM_CODE, LH_RSH,
                       "lh-key", PULLER, str(self.state), "/home/alice"):
            self.assertNotIn(secret, seen, secret)
        for mark in ("<pem>", "<token>", "<code>", "<LH_RSH>", "<state>/ca/ca.key", "~/x"):
            self.assertIn(mark, seen, mark)

    def test_methods_and_a_mutation_asks_first(self):
        status, data, _ = self.call("POST", "/api/v1/actions/kick", b'{"args": {"name": "ana"}}', {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(data)["type_to_confirm"]), (200, "ana"))   # a plan: kick needs a confirmation (#87)
        self.assertEqual(self.call("POST", "/api/v1/actions/nuke", b"{}")[0], 404)
        self.assertEqual(self.call("POST", "/api/v1/status", b"{}")[0], 405)
        self.assertEqual(self.call("GET", "/api/v1/actions/kick")[0], 405)
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.call(m, "/api/v1/status")[0], 405, m)
        self.assertEqual([c for c in self.engine_calls() if c[:2] != ["host", "leases"]], [])   # the plan reads leases, runs nothing
        status, _, r = self.call("GET", "/api/v1/health")
        self.assertEqual(r.getheader("Content-Security-Policy").split(";")[0], "default-src 'none'")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_bad_paths_and_queries(self):
        for p in ("/", "/api/v2/status", "/api/v1/nope", "/api/v1/../v1/status", "/api/v1//status", "/api/v1/players/ana",
                  "/api/v1/status\\x"):
            self.assertEqual(self.call("GET", p)[0], 404, p)
        for p in ("players/Ana/activity", "players/a;b/activity", "jobs/xyz", "events?limit=0", "events?limit=501", "events?since=-1",
                  "events?limit=1&limit=2", "events?a=1&b=2&c=3&d=4&e=5"):
            self.assertEqual(self.get(p)[0], 400, p)

    def test_engine_failure_is_502_and_says_nothing_of_it(self):
        with unittest.mock.patch.dict(server.READS, {"leases": (["host", "broken"], "json")}):
            status, body = self.get("leases")
        self.assertEqual((status, body["error"]), (502, "engine"))

    def test_c02_another_uid_is_closed_before_a_byte(self):
        with unittest.mock.patch.object(server, "peer_uid", return_value=os.getuid() + 1):
            c = UnixHTTP(str(self.sock))
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError, OSError)):
                c.request("GET", "/api/v1/health")
                c.getresponse()
            c.close()
        self.assertEqual(self.get("health")[0], 200)

    def test_socket_files(self):
        self.assertEqual(stat.S_IMODE(self.sock.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.sock.stat().st_mode), 0o600)
        with self.assertRaises(SystemExit):   # a second daemon on a live socket
            server.make_server(self.state, self.mesh, self.sock)
        with self.assertRaises(SystemExit):
            server.socket_path(self.tmp / ("x" * 120), None)

    def test_the_socket_directory_is_never_loosened_or_invented(self):
        other = self.tmp / "shared"
        other.mkdir(mode=0o755)
        os.chmod(other, 0o755)
        with self.assertRaises(SystemExit) as e:   # an existing directory that is not 0700 is refused, not chmod'ed
            server.make_server(self.state, self.mesh, other / "api.sock")
        self.assertEqual(e.exception.code, 2)
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)
        with self.assertRaises(SystemExit):        # a state directory that does not exist is not created
            server.make_server(self.tmp / "nope", self.mesh, self.tmp / "nope" / "console" / "api.sock")
        self.assertFalse((self.tmp / "nope").exists())

    def test_the_state_dir_is_redacted_as_the_engine_names_it(self):
        link = self.tmp / "via-link"
        link.symlink_to(self.state)
        r = server.Redactor(self.state, link)
        self.assertEqual(r.text(f"{link}/bundles/ana and {self.state}/ca"), "<state>/bundles/ana and <state>/ca")
        self.assertEqual(r.text("code ABCDEFGHIJKLMNOPQRSTUVWXYZ typed raw"), "code <code> typed raw")

    def test_short_or_embedded_lh_rsh_is_not_a_secret(self):
        (self.state / "lighthouse.env").write_text("LH_RSH=false\n")   # what poc-nat.sh writes during its suite
        r = server.Redactor(self.state)
        self.assertEqual(r.text("falsely, a false start"), "falsely, a false start")
        (self.state / "lighthouse.env").write_text("LH_RSH=ssh\\ lh-box\n")
        r = server.Redactor(self.state)
        self.assertEqual(r.text('run --ssh "ssh lh-box" now; xssh lh-boxy'), 'run --ssh "<LH_RSH>" now; xssh lh-boxy')

    def test_a_stale_socket_is_replaced(self):
        self.srv.shutdown()
        self.srv.server_close()   # the file stays, nobody answers
        self.assertTrue(self.sock.exists())
        self.start()
        self.assertEqual(self.get("health")[0], 200)

    def test_c16_connections_are_bounded(self):
        self.srv.slots = threading.BoundedSemaphore(2)
        idle = []
        for _ in range(2):   # two clients that connect and send nothing hold both slots
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(self.sock))
            idle.append(c)
            self.addCleanup(c.close)
        self.assertEqual(self.until(503), 503)   # the accept loop hands both idle clients a slot, then refuses the third
        for c in idle:
            c.close()
        self.assertEqual(self.until(200), 200)   # their handlers see EOF and free the slots

    def until(self, want: int, seconds: float = 10) -> int:
        """Poll /health until it answers `want` (no fixed sleep: a loaded CI runner is slow), or return the last answer."""
        import time
        end, status = time.monotonic() + seconds, None
        while time.monotonic() < end:
            try:
                status = self.call("GET", "/api/v1/health")[0]
            except OSError:
                status = None
            if status == want:
                break
            time.sleep(0.05)
        return status

    def test_c17_odd_inputs_never_5xx(self):
        rng = random.Random(86)
        chars = "abc/?&=%.;-_0123456789$`\\\"'<> \x7f"
        for _ in range(150):
            path = "/api/v1/" + "".join(rng.choice(chars) for _ in range(rng.randint(0, 30)))
            method = rng.choice(["GET", "POST", "HEAD"])
            try:
                status = self.call(method, path.replace(" ", "%20"), b"{" if method == "POST" else None)[0]
            except (http.client.HTTPException, OSError):   # a path http.client itself refuses, or a closed connection: not a 5xx
                continue
            self.assertLess(status, 500, path)


if __name__ == "__main__":
    unittest.main()
