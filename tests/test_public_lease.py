"""Issues #68 and #69: the game poc-vm hands to poc-golden (make regression) is a golden one.

#69: a public node's certificate lives the pool's lease (pool.yaml lease_seconds), in the Docker flow (up.sh) and in the VM flow (vm/up.sh)
alike, read from pool.yaml and never a duration typed twice. #68: poc-vm removes every test pod it creates (web and cli of checks-cilium.sh,
the SPIRE twins) before the game is kept, so no unlabelled pod is left on a shared node for G2.3 to find.
"""
import re
import unittest
from pathlib import Path

POC = Path(__file__).resolve().parent.parent / "poc"


class PublicLease(unittest.TestCase):
    def test_both_flows_issue_the_public_node_for_the_pool_lease(self):
        for script in ("up.sh", "vm/up.sh"):
            text = (POC / script).read_text(encoding="utf-8")
            self.assertIn("lease_seconds", text, script)
            public = [ln for ln in text.splitlines() if "public" in ln and "issue.sh" in ln or re.search(r'"n2 \S+ public', ln)]
            self.assertTrue(public, script)
            for ln in public:
                self.assertNotRegex(ln, r"public\s+\d+[smh]\b", f"{script}: a typed duration for the public node: {ln}")


class TestPodsRemoved(unittest.TestCase):
    def test_poc_vm_deletes_every_pod_checks_cilium_creates(self):
        checks = (POC / "checks-cilium.sh").read_text(encoding="utf-8")
        created = set(re.findall(r"kind: Pod\nmetadata: \{name: ([\w-]+)", checks)) | set(re.findall(r'"(auth-\w+) \$AUTH', checks))
        self.assertEqual(created, {"web", "cli", "auth-ok", "auth-no"})
        delete = next(ln for ln in (POC / "vm/poc-vm.sh").read_text(encoding="utf-8").splitlines() if ln.startswith("k delete pod"))
        self.assertTrue(created <= set(delete.split()), delete)


if __name__ == "__main__":
    unittest.main()
