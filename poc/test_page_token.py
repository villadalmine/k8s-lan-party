"""page-server.sh and the puller's token (issue #47): the file may hold CRLF (Git Bash on Windows writes it), the hash the server keeps must
be the hash of what the puller sends (Python's `.strip()`), and `verify` must send that same token and fail unless the page accepts it.
The ssh command is a stub that records the script page-server.sh would run on the server: no server, no network, nothing installed."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "page"
BASH = shutil.which("bash")
TOKEN = "abcDEF0123_-xyz" + "456789ABCDEFghijklmnopqrstuv"   # a test fixture, not a secret  # gitleaks:allow


@unittest.skipUnless(BASH and os.name == "posix", "needs bash on a POSIX system")
class PullerToken(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sent = self.tmp / "sent"
        fake = self.tmp / "fakessh"   # called as: fakessh bash -s  (the script arrives on stdin)
        fake.write_text(f'#!/bin/sh\ncat >>"{self.sent}"\necho "-----" >>"{self.sent}"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fake = fake
        self.tokfile = self.tmp / "puller.token"
        self.env = dict(os.environ, EMESH_STATE=str(self.tmp / "state"), EMESH_CACHE=str(self.tmp / "cache"))

    def run_ps(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(PAGE / "page-server.sh"), *args, "--ssh", str(self.fake), "--token-file", str(self.tokfile)],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def puller_reads(self) -> str:   # exactly what puller.py's call() does
        return self.tokfile.read_text().strip()

    def sent_hashes(self) -> list[str]:
        return re.findall(r'!= "([0-9a-f]{64})"', self.sent.read_text())

    def test_crlf_token_hashes_like_the_puller_and_the_file_is_rewritten(self) -> None:
        self.tokfile.write_bytes(TOKEN.encode() + b"\r\n")
        p = self.run_ps("puller-token")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.sent_hashes(), [hashlib.sha256(self.puller_reads().encode()).hexdigest()])
        self.assertEqual(self.sent_hashes(), [hashlib.sha256(TOKEN.encode()).hexdigest()])
        self.assertEqual(self.tokfile.read_bytes(), TOKEN.encode() + b"\n")
        self.assertIn("normalized", p.stdout)
        self.assertEqual(stat.S_IMODE(self.tokfile.stat().st_mode), 0o600)

    def test_clean_file_is_left_alone_and_a_second_run_changes_nothing_here(self) -> None:
        self.tokfile.write_bytes(TOKEN.encode() + b"\n")
        for _ in range(2):
            p = self.run_ps("puller-token")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertNotIn("changed:", p.stdout)
        self.assertEqual(self.tokfile.read_bytes(), TOKEN.encode() + b"\n")

    def test_no_file_makes_a_token_the_puller_reads_back(self) -> None:
        p = self.run_ps("puller-token")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.sent_hashes(), [hashlib.sha256(self.puller_reads().encode()).hexdigest()])
        self.assertEqual(len(self.puller_reads()), 43)

    def test_a_token_with_odd_characters_is_refused(self) -> None:
        self.tokfile.write_bytes(b"it's a token\r\n")
        p = self.run_ps("puller-token")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.sent.exists())

    def test_an_interior_space_is_refused_not_removed(self) -> None:
        """The puller strips only the ends: `abc DEF` would be sent with its space and get 401, so neither the hash nor verify may use `abcDEF`."""
        self.tokfile.write_bytes(b"abcDEF0123_-xyz extra\r\n")
        p = self.run_ps("puller-token")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.sent.exists())
        self.assertEqual(self.tokfile.read_bytes(), b"abcDEF0123_-xyz extra\r\n")   # not rewritten into a different credential
        self.run_ps("verify", "--site", "https://203.0.113.7")
        script = self.sent.read_text()
        self.assertIn("no puller token", script)
        self.assertNotIn("abcDEF0123_-xyzextra", script)
        self.assertNotIn("Authorization: Bearer %s\\n' 'abc", script)

    def test_verify_sends_the_clean_token_and_demands_a_200(self) -> None:
        self.tokfile.write_bytes(TOKEN.encode() + b"\r\n")
        p = self.run_ps("verify", "--site", "https://203.0.113.7")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        script = self.sent.read_text()
        self.assertIn(f"'{TOKEN}'", script)
        self.assertNotIn("\r", script)
        self.assertIn("/api/v1/puller/intake", script)
        self.assertRegex(script, r'\[ "\\?\$s" = 200 \] && ok "the puller\'s token is accepted')

    def test_verify_fails_without_a_token_instead_of_trusting_the_hash(self) -> None:
        p = self.run_ps("verify", "--site", "https://203.0.113.7")
        self.assertIn("no puller token", self.sent.read_text())
        self.assertNotIn(TOKEN, self.sent.read_text())
        self.assertEqual(p.returncode, 0)   # the stub ssh exits 0: the FAIL is decided by the script it sends, which says `no`


if __name__ == "__main__":
    unittest.main()
