"""poc/doom/build.sh: every file the Containerfile copies from the build context is in the image's hash and in the tarball sent to a
build VM (a file left out of the tarball failed the build only on machines without a container engine)."""
import re
import unittest
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "poc" / "doom"


class BuildInputs(unittest.TestCase):
    def test_copied_files_are_hashed_and_sent(self):
        copied = set()
        for line in (D / "Containerfile").read_text().splitlines():
            if line.startswith("COPY ") and "--from=" not in line:
                copied |= set(line.split()[1:-1])
        self.assertIn("play-client.sh", copied)
        build = (D / "build.sh").read_text()
        hashed = set(re.findall(r'"\$D/([\w.-]+)"', re.search(r"^HASH=.*$", build, re.M).group(0)))
        sent = set(re.search(r'tar cf - -C "\$D" ([^|]+)\|', build).group(1).split())
        self.assertLessEqual(copied, hashed)
        self.assertLessEqual(copied | {"Containerfile"}, sent)
        for f in copied:
            self.assertTrue((D / f).is_file(), f)


if __name__ == "__main__":
    unittest.main()
