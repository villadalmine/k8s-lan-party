"""Images pinned by digest stay pinned (issue #20, PR #84): a later "looked up live" refresh must not drop the digest and go back to a tag alone.

AGNHOST_IMAGE (poc/stack.env) was pinned by tag only until 2026-11-11 (a finding of the #83 review); DOOM_DEBIAN_IMAGE (poc/doom/doom.env) was pinned
by digest from the start. Each is `<repository>:<tag>@sha256:<64 hex>`: the tag says which release it is, the digest is what is pulled. Extend PINNED
when another image gets its digest (PAUSE_IMAGE is still a tag).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = {"AGNHOST_IMAGE": ROOT / "poc" / "stack.env", "DOOM_DEBIAN_IMAGE": ROOT / "poc" / "doom" / "doom.env"}
REF = re.compile(r"^[^@\s:]+(?::\d+)?(?:/[^@\s:]+)*:[^@\s:/]+@sha256:[0-9a-f]{64}$")


def value(path: Path, name: str):
    m = re.search(rf"(?m)^{name}=(\S+)$", path.read_text(encoding="utf-8"))
    return m and m.group(1)


class ImagePins(unittest.TestCase):
    def test_pinned_images_carry_tag_and_digest(self):
        for name, path in PINNED.items():
            with self.subTest(name=name):
                ref = value(path, name)
                self.assertIsNotNone(ref, f"{name} not found in {path.relative_to(ROOT)}")
                self.assertRegex(ref, REF, f"{name} must be <repository>:<tag>@sha256:<digest>, not a tag alone")

    def test_the_pattern_refuses_a_tag_alone(self):
        self.assertNotRegex("registry.k8s.io/e2e-test-images/agnhost:2.66.1", REF)
        self.assertNotRegex("registry.k8s.io/e2e-test-images/agnhost@sha256:" + "0" * 64, REF)
        self.assertRegex("registry.k8s.io/e2e-test-images/agnhost:2.66.1@sha256:" + "a" * 64, REF)


if __name__ == "__main__":
    unittest.main()
