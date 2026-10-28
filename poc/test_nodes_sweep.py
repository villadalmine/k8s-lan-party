"""The sweep of revoke.sh (nodes_sweep.py) keys on the bundle names the host issued, never on what a kubelet reports alone."""
import io
import json
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from nodes_sweep import main, strays

LIVE = {"cp": "10.77.0.2", "carol": "10.77.0.7"}     # bob (10.77.0.6) was just revoked


def node(name, *ips):
    return {"metadata": {"name": name},
            "status": {"addresses": [{"type": "InternalIP", "address": ip} for ip in ips] + [{"type": "Hostname", "address": name}]}}


class SweepTest(unittest.TestCase):
    def nodes(self, *items):
        return {"items": list(items)}

    def test_every_copy_goes_whatever_it_reports(self):
        got = strays(self.nodes(
            node("cp", "10.77.0.2"), node("carol", "10.77.0.7"),
            node("bob", "10.77.0.6"),                        # the revoked one
            node("bob2", "10.77.0.6"),                       # a cooperative copy
            node("bob3-dual", "10.77.0.6", "fd00::6"),       # dual stack: two InternalIPs
            node("bob4-lan", "192.0.2.44"),                  # a plain k3s agent: its LAN address
            node("bob5", ),                                  # no address at all
        ), LIVE, "10.77.0.6")
        self.assertEqual([n for n, _ in got], ["bob", "bob2", "bob3-dual", "bob4-lan", "bob5"])

    def test_a_live_name_with_the_revoked_or_a_foreign_address_is_a_copy(self):
        got = dict(strays(self.nodes(node("carol", "fd00::7", "10.77.0.6"), node("cp", "192.0.2.9")), LIVE, "10.77.0.6"))
        self.assertIn("revoked address", got["carol"])
        self.assertIn("not at its address 10.77.0.2", got["cp"])

    def test_live_nodes_stay(self):
        self.assertEqual(strays(self.nodes(node("cp", "10.77.0.2"), node("carol", "10.77.0.7"), node("carol-x")), dict(LIVE, **{"carol-x": "10.77.0.8"}),
                                "10.77.0.6"), [])

    def test_cli_and_a_bad_answer(self):
        out = io.StringIO()
        with redirect_stdout(out), unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(self.nodes(node("zz", "1.2.3.4"))))):
            self.assertEqual(main(["--gone-ip", "10.77.0.6", "cp=10.77.0.2"]), 0)
        self.assertEqual(out.getvalue(), "zz\tnot named after a live bundle\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), unittest.mock.patch("sys.stdin", io.StringIO("Error from server")):
            self.assertEqual(main(["--gone-ip", "10.77.0.6"]), 2)


if __name__ == "__main__":
    unittest.main()
