"""The host console's draft API (issue #85): poc/console/openapi.json and docs/CONSOLE.md say the same thing.

The RFC is decided in docs/CONSOLE.md; the daemon (#86-#89) will be tested against openapi.json. Until then this keeps the two in step:
every registry verb is in the doc's verb table with the same confirmation and roles, the console role never gets a verb that signs with the
persistent CA or hands out a key, and every route lives under /api/v1.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "poc" / "console" / "openapi.json"
DOC = ROOT / "docs" / "CONSOLE.md"
LOCAL_ONLY = {"invite", "bundle", "refresh"}          # a private key, the persistent CA, a running machine's config
DANGEROUS = {"approve", "auto-on", "panic", "panic-clear", "reap-all", "reap-one", "kick", "revoke", "party-end"}


def doc_rows() -> dict[str, list[tuple[str, str, str]]]:
    """API verb -> [(confirm, roles, API cell)] from the verb table of docs/CONSOLE.md (a cell like `kick`)."""
    rows = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0].startswith("`host "):
            m = re.fullmatch(r"`([a-z-]+)`(?: with .*)?", cells[3])
            if m:
                rows.setdefault(m.group(1), []).append((cells[4], cells[5], cells[3]))
    return rows


class ConsoleSpecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(SPEC.read_text(encoding="utf-8"))
        cls.actions = {a["verb"]: a for a in cls.spec["x-actions"]}

    def test_openapi_shape(self):
        self.assertEqual(self.spec["openapi"], "3.1.0")
        for path in self.spec["paths"]:
            self.assertTrue(path.startswith("/api/v1/"), path)
        self.assertEqual(sorted(self.spec["components"]["schemas"]["Verb"]["enum"]), sorted(self.actions))
        for server in self.spec["servers"]:
            self.assertRegex(server["url"], r"^http://127\.0\.0\.1:\d+$")   # loopback only, never a public address

    def test_every_verb_runs_the_engine_with_checked_args(self):
        for verb, a in self.actions.items():
            self.assertTrue(a["engine"].startswith("mesh host "), verb)
            self.assertTrue(set(a["required"]) <= set(a["params"]), verb)
            for p in re.findall(r"\{(\w+)\}", a["engine"]):
                self.assertIn(p, a["params"], f"{verb}: {p}")
            if verb == "panic":
                self.assertIsNone(a["timeout_seconds"])                 # panic is never killed half way
            else:
                self.assertGreater(a["timeout_seconds"], 0, verb)

    def test_every_engine_template_is_a_real_mesh_verb(self):
        mesh = (ROOT / "poc" / "mesh").read_text(encoding="utf-8")
        verbs = set(re.findall(r'"host ([a-z-]+)"', mesh))
        for verb, a in self.actions.items():
            word = a["engine"].split()[2]
            self.assertIn(word, verbs, f"{verb}: mesh host {word} is not a verb of poc/mesh")

    def test_console_role_never_signs_with_the_persistent_ca(self):
        for verb, a in self.actions.items():
            self.assertIn("local", a["roles"], verb)
            if verb in LOCAL_ONLY:
                self.assertEqual(a["roles"], ["local"], verb)
        for verb, a in self.actions.items():
            for never in ("init", "down", "play", "console"):
                self.assertNotIn(never, (verb, a["engine"].split()[2]))
        # approve with trust friends signs with the persistent CA: local only, whatever the verb's own roles are
        self.assertEqual(self.actions["approve"]["arg_roles"], {"trust": {"friends": ["local"]}})
        self.assertEqual(self.actions["approve"]["params"]["trust"]["default"], "public")
        for verb, a in self.actions.items():
            for arg, values in a["arg_roles"].items():
                self.assertIn(arg, a["params"], verb)
                for roles in values.values():
                    self.assertNotIn("console", roles, verb)

    def test_dangerous_verbs_are_confirmed(self):
        for verb, a in self.actions.items():
            self.assertEqual(a["confirm"] != "none", verb in DANGEROUS, verb)
        self.assertEqual(self.actions["kick"]["confirm"], "type:name")
        self.assertEqual(self.actions["panic"]["confirm"], "click")    # one click: panic has to be fast

    def test_doc_table_matches_the_registry(self):
        rows = doc_rows()
        self.assertEqual(sorted(rows), sorted(self.actions))
        self.assertEqual(len(rows["approve"]), 2)                   # public and friends
        words = {"none": "none", "click": "click", "type:name": "type the name", "type:all": "type `all`"}
        for verb, a in self.actions.items():
            for confirm, roles, cell in rows[verb]:
                self.assertEqual(confirm.split(" (")[0], words[a["confirm"]], verb)
                if "with `trust: friends`" in cell:                   # the row of an arg that narrows the roles (arg_roles)
                    self.assertEqual([r.strip() for r in roles.split(",")], a["arg_roles"]["trust"]["friends"], verb)
                    continue
                self.assertEqual([r.strip() for r in roles.split(",")], a["roles"], verb)


if __name__ == "__main__":
    unittest.main()
