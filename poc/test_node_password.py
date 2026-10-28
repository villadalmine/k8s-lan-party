"""The node password secret the host pre-creates for a bundle (node_password.py): k3s's own hash format, the right secret."""
import base64
import io
import json
import unittest
import unittest.mock
from contextlib import redirect_stdout

from node_password import create_hash, main, secret, verify


class NodePasswordTest(unittest.TestCase):
    def test_hash_format_is_the_k3s_one(self):
        h = create_hash("hello world", salt=bytes.fromhex("0011223344556677"))
        version, salt, n, r, p, enc = h.lstrip("$").split(":")
        self.assertEqual((h[0], version, salt, n, r, p), ("$", "1", "0011223344556677", "15", "8", "1"))
        self.assertNotIn("=", enc)                                     # base64 without padding (RawStdEncoding)
        self.assertEqual(len(base64.b64decode(enc + "=" * (-len(enc) % 4))), 64)

    def test_only_the_bundle_password_verifies(self):
        h = create_hash("the bundle's password")
        self.assertTrue(verify(h, "the bundle's password"))
        self.assertFalse(verify(h, "another visitor's password"))

    def test_secret(self):
        s = secret("Carol", "pw")
        self.assertEqual((s["metadata"]["name"], s["metadata"]["namespace"], s["type"], s["immutable"]),
                         ("carol.node-password.k3s", "kube-system", "k3s.cattle.io/node-password", True))
        self.assertTrue(verify(base64.b64decode(s["data"]["hash"]).decode(), "pw"))

    def test_cli(self):
        out = io.StringIO()
        with redirect_stdout(out), unittest.mock.patch("sys.stdin", io.StringIO("pw\n")):
            self.assertEqual(main(["secret", "bob"]), 0)
        self.assertEqual(json.loads(out.getvalue())["metadata"]["name"], "bob.node-password.k3s")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["new"]), 0)
        with unittest.mock.patch("sys.stdin", io.StringIO("")), unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["secret", "bob"]), 2)


if __name__ == "__main__":
    unittest.main()
