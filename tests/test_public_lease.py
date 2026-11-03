"""Issues #68 and #69: the game poc-vm hands to poc-golden (make regression) is a golden one.

#69: a public node's certificate lives the pool's lease (pool.yaml lease_seconds), in the Docker flow (up.sh) and in the VM flow (vm/up.sh)
alike, read from pool.yaml and never a duration typed twice. #68: poc-vm removes every test pod it creates (web and cli of checks-cilium.sh,
the SPIRE twins) before the game is kept, so no unlabelled pod is left on a shared node for G2.3 to find.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"


class PublicLease(unittest.TestCase):
    def lease(self, text):
        with tempfile.TemporaryDirectory() as t:
            pool = Path(t) / "pool.yaml"
            if text is not None:
                pool.write_text(text)
            return subprocess.run(["bash", "-c", '. "$1/pool-lease.sh"; lease_seconds', "-", str(POC)],
                                  env={**os.environ, "POOL_CONFIG": str(pool)}, capture_output=True, text=True)

    def test_the_lease_is_a_positive_integer_or_nothing_is_issued(self):
        self.assertEqual(self.lease("slots: 2\nlease_seconds: 3600         # 1 hour\n").stdout, "3600\n")
        for bad in ("lease_seconds: 0\n", "slots: 2\n", "lease_seconds: 1h\n", None):
            r = self.lease(bad)   # 0 is "until the CA expires" for nebula-cert: it must fail closed
            self.assertEqual((r.returncode, r.stdout), (2, ""), bad)
            self.assertIn("lease_seconds must be a positive integer", r.stderr)

    def test_both_flows_issue_the_public_node_for_the_pool_lease(self):
        for script in ("up.sh", "vm/up.sh"):
            text = (POC / script).read_text(encoding="utf-8")
            self.assertIn('. "$POC/pool-lease.sh"', text, script)
            self.assertIn('LEASE="$(lease_seconds)s" || exit 2', text, script)
            public = [ln for ln in text.splitlines() if re.search(r"public", ln) and ("issue.sh" in ln or '"n2 ' in ln)]
            self.assertTrue(public and all("$LEASE" in ln for ln in public), f"{script}: {public}")


class TestPodsRemoved(unittest.TestCase):
    def test_poc_vm_deletes_every_pod_checks_cilium_creates(self):
        checks = (POC / "checks-cilium.sh").read_text(encoding="utf-8")
        created = set(re.findall(r"kind: Pod\nmetadata: \{name: ([\w-]+)", checks)) | set(re.findall(r'"(auth-\w+) \$AUTH', checks))
        self.assertEqual(created, {"web", "cli", "auth-ok", "auth-no"})
        delete = next(ln for ln in (POC / "vm/poc-vm.sh").read_text(encoding="utf-8").splitlines() if ln.startswith("k delete pod"))
        self.assertTrue(created <= set(delete.split()), delete)


if __name__ == "__main__":
    unittest.main()
