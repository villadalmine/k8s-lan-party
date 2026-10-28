"""poc/golden.py: the golden rule table (two environments fill one table, idempotently), the placement audit, the certificate line."""
import json
import unittest

import golden

DOC = """# Status

before
<!-- golden:begin old note -->
<!-- golden:end -->
after
"""
RUN_A = [("1", "G1.1", "PASS", "the join says rule 1"), ("2", "G2.10", "FAIL", "tenth"), ("2", "G2.2", "DEP #9", "per user")]
RUN_B = [("1", "G1.1", "PASS", "the join says rule 1"), ("2", "G2.2", "SKIP", "per user")]


class Table(unittest.TestCase):
    def test_first_run_fills_one_column_in_rule_order(self):
        out = golden.update(DOC, "lab", RUN_A)
        rows = [line for line in out.splitlines() if line.startswith("| ") and "G" in line.split("|")[2]]
        self.assertEqual([r.split("|")[2].strip() for r in rows], ["G1.1", "G2.2", "G2.10"])
        self.assertIn("| Rule | Check | What it proves | lab |", out)
        self.assertIn("| 2 | G2.2 | per user | DEP #9 |", out)
        self.assertTrue(out.startswith("# Status\n\nbefore\n") and out.endswith("after\n"))

    def test_second_environment_keeps_the_first(self):
        out = golden.update(golden.update(DOC, "lab", RUN_A), "stranger", RUN_B)
        self.assertIn("| Rule | Check | What it proves | lab | stranger |", out)
        self.assertIn("| 2 | G2.2 | per user | DEP #9 | SKIP |", out)
        self.assertIn("| 2 | G2.10 | tenth | FAIL | - |", out)

    def test_idempotent(self):
        once = golden.update(DOC, "lab", RUN_A)
        self.assertEqual(golden.update(once, "lab", RUN_A), once)

    def test_refuses_a_doc_without_markers_and_a_pipe_in_the_label(self):
        with self.assertRaises(ValueError):
            golden.update("# nothing\n", "lab", RUN_A)
        with self.assertRaises(ValueError):
            golden.update(DOC, "a|b", RUN_A)


class Audit(unittest.TestCase):
    def pod(self, ns, name, node, labels=None, phase="Running"):
        return {"metadata": {"namespace": ns, "name": name, "labels": labels}, "spec": {"nodeName": node}, "status": {"phase": phase}}

    def test_private_off_own_is_reported_and_the_rest_is_not(self):
        pods = {"items": [
            self.pod("lab", "a", "alice"),                                              # unlabelled = private, on friends: bad
            self.pod("lab", "b", "alice", {"edge-mesh/sensitivity": "private"}),        # private on friends: bad
            self.pod("lab", "c", "alice", {"edge-mesh/sensitivity": "shareable"}),      # shareable: fine
            self.pod("lab", "d", "cp"),                                                 # private on own: fine
            self.pod("kube-system", "e", "alice"),                                      # system namespace: excluded
            self.pod("lab", "f", "alice", phase="Succeeded"),                           # finished: excluded
            self.pod("lab", "g", "ghost"),                                              # node without a trust label: bad
        ]}
        bad = golden.audit(pods, {"cp": "own", "alice": "friends"})
        self.assertEqual(bad, ["lab/a (private) on alice (friends)", "lab/b (private) on alice (friends)",
                               "lab/g (private) on ghost (no trust label)"])


class Listeners(unittest.TestCase):
    PROC = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:192B 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1
   1: 02004D0A:192B 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1
   2: 02004D0A:192B 0500A8C0:C350 01 00000000:00000000 00:00000000 00000000     0        0 3 1
   3: 00000000:2710 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4 1
   0: 00000000000000000000000001000000:192B 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 5 1
"""

    def test_listening_sockets_of_one_port(self):
        self.assertEqual(golden.listeners(self.PROC, 6443), ["127.0.0.1", "10.77.0.2", "::1"])
        self.assertEqual(golden.listeners(self.PROC, 10000), ["0.0.0.0"])


class Cert(unittest.TestCase):
    def test_line(self):
        printed = json.dumps([{"fingerprint": "f1", "details": {"issuer": "ca1", "notBefore": "2026-10-28T10:00:00Z",
                                                                "notAfter": "2026-10-28T11:00:00Z"}}])
        self.assertEqual(golden.cert_line(printed), "f1 ca1 3600")

    def test_a_ca_is_its_own_issuer(self):
        printed = json.dumps([{"fingerprint": "ca1", "details": {"issuer": "", "notBefore": "2026-10-28T10:00:00Z",
                                                                 "notAfter": "2027-10-28T10:00:00Z"}}])
        self.assertEqual(golden.cert_line(printed).split()[1], "ca1")


if __name__ == "__main__":
    unittest.main()
