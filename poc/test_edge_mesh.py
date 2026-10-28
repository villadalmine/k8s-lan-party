"""`edge-mesh kubectl`: the error matrix's messages and exit codes (DESIGN.md section 14.1, rows 6-9 and 11) with a stub kubectl and a
stub `ip`, so every row is checked without a cluster. `make poc-pool` checks the same rows against a real one."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SH = shutil.which("sh")
KUBECONFIG = "apiVersion: v1\nkind: Config\ncontexts:\n- name: alice\n  context:\n    cluster: edge-mesh\n    namespace: play-alice\n"
RBAC = ('Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:play-alice:user" cannot list resource "pods" '
        'in API group "" in the namespace "play-bob"')
QUOTA = ('Error from server (Forbidden): error when creating "STDIN": pods "big" is forbidden: exceeded quota: pool, requested: '
         'limits.cpu=1, used: limits.cpu=200m, limited: limits.cpu=1')
PSA = ('Error from server (Forbidden): error when creating "STDIN": pods "root" is forbidden: violates PodSecurity "restricted:latest": '
       'privileged')


@unittest.skipUnless(SH and os.name == "posix", "needs sh on Linux or macOS")
class KubectlWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        b = self.tmp / "bin"
        b.mkdir()
        (b / "ip").write_text('#!/bin/sh\n[ "$STUB_NEBULA" = up ] && echo "    inet 10.77.0.5/24 scope global nebula1"\nexit 0\n')
        (b / "kubectl").write_text('#!/bin/sh\necho "kubectl $* with $KUBECONFIG"\n[ -z "$STUB_ERR" ] || echo "$STUB_ERR" >&2\n'
                                   'exit "${STUB_RC:-0}"\n')
        for f in b.iterdir():
            f.chmod(0o755)
        self.kc = self.tmp / "kubeconfig"
        self.kc.write_text(KUBECONFIG)

    def run_it(self, err="", rc=0, nebula="up", kc=None):
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "KUBECONFIG": str(kc or self.kc), "STUB_NEBULA": nebula,
               "STUB_ERR": err, "STUB_RC": str(rc)}
        r = subprocess.run([SH, str(HERE / "edge-mesh"), "kubectl", "get", "pods"], env=env, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def test_success_passes_through(self):
        code, out, err = self.run_it()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"kubectl get pods with {self.kc}\n")

    def test_row_6_nebula_down(self):
        code, out, err = self.run_it(nebula="down")
        self.assertEqual(code, 10)
        self.assertEqual(out, "")   # kubectl never ran: it would only time out
        self.assertIn("edge-mesh: Nebula is down (nebula1 has no address), so the cluster cannot be reached; run: sudo edge-mesh join", err)

    def test_row_7_ca_mismatch(self):
        code, _, err = self.run_it("Unable to connect to the server: tls: failed to verify certificate: x509: certificate signed by "
                                   "unknown authority", 1)
        self.assertEqual(code, 11)
        self.assertIn("x509: certificate signed by unknown authority", err)   # kubectl's own line stays
        self.assertIn("edge-mesh: the API's certificate is not the one your bundle pins (x509)", err)

    def test_row_8_token_ended(self):
        code, _, err = self.run_it("error: You must be logged in to the server (Unauthorized)", 1)
        self.assertEqual(code, 12)
        self.assertIn("edge-mesh: 401 Unauthorized: your access ended with your lease", err)

    def test_row_9_forbidden_outside_the_namespace(self):
        code, _, err = self.run_it(RBAC, 1)
        self.assertEqual(code, 13)
        self.assertIn("edge-mesh: forbidden (403): you may only work in your namespace play-alice", err)

    def test_row_11_quota_is_not_mistaken_for_rbac(self):
        code, _, err = self.run_it(QUOTA, 1)
        self.assertEqual(code, 14)
        self.assertIn("edge-mesh: exceeded quota in play-alice: lower the requests or delete something", err)
        self.assertNotIn("403", err)

    def test_a_policy_denial_keeps_kubectl_s_message_and_exit_code(self):
        code, _, err = self.run_it(PSA, 1)
        self.assertEqual(code, 1)
        self.assertIn('violates PodSecurity "restricted:latest"', err)
        self.assertNotIn("edge-mesh:", err)

    def test_no_kubeconfig(self):
        code, _, err = self.run_it(kc=self.tmp / "missing")
        self.assertEqual(code, 2)
        self.assertIn("edge-mesh: no kubeconfig at", err)


if __name__ == "__main__":
    unittest.main()
