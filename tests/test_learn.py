"""Learn mode (issue #77): every `learn` block is valid, docs/LEARN.md and poc/page/whatsnew/learn.json are what docs/FEATURES.yml generates
(sealed with the hash of the learn data, like the diagrams), and the rule that asks a feature to explain itself holds. No network, no machines."""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import features  # noqa: E402
import learn  # noqa: E402

CAT = features.load()
FILES = features.tracked()


def problems(cat):
    return [p for p in features.validate(cat, files=FILES) if "learn" in p or "user_facing" in p or "layer" in p or "releases" in p]


class Rule(unittest.TestCase):
    def test_the_catalog_has_no_learn_problem(self):
        self.assertEqual(problems(CAT), [])

    def test_every_user_facing_or_release_feature_has_learn(self):
        need = [f["id"] for f in CAT["features"] if f.get("user_facing") or f.get("releases")]
        self.assertGreaterEqual(len(need), 15)
        for f in CAT["features"]:
            if f["id"] in need:
                self.assertTrue(f.get("learn"), f["id"])

    def test_the_releases_of_the_issue_are_covered(self):
        by = {f["id"]: f for f in CAT["features"]}
        for fid, v in (("party", "v0.3.0-poc"), ("doom", "v0.3.0-poc"), ("cilium-guard", "v0.2.0-poc"), ("spire-scope", "v0.2.0-poc"),
                       ("public-page", "v0.2.1-poc")):
            self.assertIn(v, by[fid]["releases"], fid)

    def test_a_user_facing_feature_without_learn_fails(self):
        cat = copy.deepcopy(CAT)
        f = next(x for x in cat["features"] if x["id"] == "party")
        del f["learn"]
        self.assertIn("feature party: needs a 'learn' block (it is user_facing or a release page shows it)", problems(cat))

    def test_a_feature_a_release_shows_needs_learn_even_if_not_user_facing(self):
        cat = copy.deepcopy(CAT)
        f = next(x for x in cat["features"] if x["id"] == "cilium-guard")
        self.assertFalse(f["user_facing"])
        del f["learn"]
        self.assertTrue(any("cilium-guard: needs a 'learn' block" in p for p in problems(cat)))
        f["releases"] = []   # not shown, not user facing: no learn needed
        self.assertEqual(problems(cat), [])

    def test_bad_blocks_fail(self):
        for change, want in ((lambda f: f["learn"].pop("analogy"), "must hold exactly"),
                             (lambda f: f["learn"].update(concept=""), "learn.concept"),
                             (lambda f: f["learn"].update(real_world="a | b"), "learn.real_world"),
                             (lambda f: f["learn"].update(seen_in=" x"), "learn.seen_in"),
                             (lambda f: f.update(layer="nope"), "'layer'"),
                             (lambda f: f.pop("user_facing"), "'user_facing'"),
                             (lambda f: f.update(releases=["v9.9.9-poc"]), "releases: v9.9.9-poc")):
            cat = copy.deepcopy(CAT)
            change(next(x for x in cat["features"] if x["id"] == "party"))
            self.assertTrue(any(want in p for p in problems(cat)), want)


class Generated(unittest.TestCase):
    def test_the_generated_files_are_what_the_catalog_generates(self):
        self.assertEqual(learn.stale(CAT), [])

    def test_a_changed_sentence_makes_them_stale(self):
        cat = copy.deepcopy(CAT)
        next(x for x in cat["features"] if x["id"] == "party")["learn"]["analogy"] = "something else"
        self.assertEqual(len(learn.stale(cat)), 2)

    def test_the_seal_is_the_hash_of_the_learn_data(self):
        seal = re.search(r"learn-source-sha256: ([0-9a-f]{64})", learn.DOC.read_text()).group(1)
        self.assertEqual(seal, learn.seal(learn.rows(CAT)))
        self.assertEqual(learn.PAGE.read_text(), learn.page_json(CAT))   # learn.json has no hash inside: it is compared whole

    def test_one_table_per_layer_and_one_row_per_feature_with_its_proof(self):
        text = learn.DOC.read_text()
        for lid, title in features.LAYERS.items():
            if any(f["layer"] == lid and f.get("learn") for f in CAT["features"]):
                self.assertIn(f"## {title}\n", text)
        for f in CAT["features"]:
            if f.get("learn"):
                row = next(r for r in text.splitlines() if r.startswith(f"| `{f['id']}` |"))
                self.assertIn(f["learn"]["concept"], row)
                self.assertIn(f"`{f['proven_by'][0]['suite']}`", row)

    def test_same_catalog_same_bytes(self):
        self.assertEqual(learn.markdown(CAT), learn.markdown(copy.deepcopy(CAT)))
        self.assertEqual(learn.page_json(CAT), learn.page_json(copy.deepcopy(CAT)))

    def test_readme_and_try_it_link_it(self):
        for doc in ("README.md", "docs/TRY-IT.md"):
            self.assertRegex((ROOT / doc).read_text(), r"\]\((docs/)?LEARN\.md\)", doc)


if __name__ == "__main__":
    unittest.main()
