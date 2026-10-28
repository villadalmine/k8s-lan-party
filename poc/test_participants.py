"""Join by signed PR (poc/participants.py): the schema, the policy, the pull request gate, the issuer's plan and the bundle packing.

Pure logic: trees are dicts and the signature verifier is a stub, so this runs in `make verify-ci` with no cosign. The real cosign
verification against a local TEST CA, the issuing, the encrypted delivery and the revocation run in `make poc-participants`.
"""
from __future__ import annotations

import base64
import io
import tarfile
import unittest

import participants as P

POLICY = """apiVersion: k8s-lan-party/v1
kind: ParticipantPolicy
maintainers: [maint]
self_service_trust: friends
reserved_nodes: [lh, cp]
cert_duration: 336h
fork_name: k8s-lan-party
nebula_net: 10.77.0
nebula_hosts: [200, 250]
"""


def pub(n: int) -> str:
    return ("-----BEGIN NEBULA X25519 PUBLIC KEY-----\n" + base64.b64encode(bytes([n]) * 32).decode() +
            "\n-----END NEBULA X25519 PUBLIC KEY-----\n")


def age(n: int) -> str:
    return "age1" + "qpzry9x8gf2tvdw0s3jn54khce6mua7l"[n % 32] * 58


def doc(login: str, gid: int, node: str, trust: str = "friends", n: int = 1, extra: str = "", recipient: str | None = None) -> str:
    key = "\n".join("  " + ln for ln in pub(n).splitlines())
    return (f"apiVersion: k8s-lan-party/v1\nkind: Participant\ngithub: {login}\ngithub_id: {gid}\nnode: {node}\ntrust: {trust}\n"
            f"resources: {{cpu: 2, memory_gib: 4}}\nnebula_public_key: |\n{key}\nrecipient: {recipient or age(n)}\n{extra}")


def policy(extra: str = "") -> bytes:
    return (POLICY + extra).encode()


class Verify:
    """The stub verifier: everything verifies except the logins in `bad`; the calls are recorded."""

    def __init__(self, bad=()):
        self.bad, self.calls = set(bad), []

    def __call__(self, login, gid, blob, sig):
        self.calls.append(login)
        if sig is None:
            return [f"{login}: not signed"]
        return [f"{login}: bad signature"] if login in self.bad else []


def tree(files: dict, with_policy: bool = True) -> P.DictTree:
    f = {"participants/policy.yaml": policy()} if with_policy else {}
    f.update(files)
    return P.DictTree(f)


def signed(login: str, gid: int, node: str, **kw) -> dict:
    return {f"participants/{login}.yaml": doc(login, gid, node, **kw), f"participants/{login}.sigstore.json": "{}"}


class Schema(unittest.TestCase):
    def setUp(self):
        self.pol = P.load_policy(policy())

    def parse(self, text, login="alice", pol=None):
        return P.parse_participant(text.encode() if isinstance(text, str) else text, login, pol or self.pol)

    def refused(self, text, fragment, login="alice", pol=None):
        with self.assertRaises(P.Refused) as e:
            self.parse(text, login, pol)
        self.assertIn(fragment, str(e.exception))

    def test_valid(self):
        p = self.parse(doc("alice", 1001, "alice-pc"))
        self.assertEqual((p.login, p.github_id, p.node, p.trust, p.cpu, p.memory_gib), ("alice", 1001, "alice-pc", "friends", 2, 4))

    def test_keys_exact(self):
        self.refused(doc("alice", 1001, "alice-pc", extra="note: hi\n"), "unknown ['note']")
        self.refused(doc("alice", 1001, "alice-pc").replace("trust: friends\n", ""), "missing ['trust']")

    def test_duplicate_key_refused(self):  # two YAML parsers must never read one signed file two ways
        self.refused(doc("alice", 1001, "alice-pc", extra="trust: own\n"), "duplicate key 'trust'")

    def test_ascii_and_size(self):
        self.refused(doc("alice", 1001, "alice-pc") + "# café\n", "only ASCII")
        self.refused(doc("alice", 1001, "alice-pc") + "#" * 5000 + "\n", "larger than")

    def test_login_is_the_file_name(self):
        self.refused(doc("bob", 1002, "bob-pc"), "github must be 'alice'")

    def test_node_name(self):
        self.refused(doc("alice", 1001, "Alice_PC"), "node must be")
        self.refused(doc("alice", 1001, "cp"), "reserved")

    def test_github_id(self):
        self.refused(doc("alice", 0, "alice-pc"), "github_id")
        self.refused(doc("alice", "true", "alice-pc"), "github_id")

    def test_trust_ceiling(self):
        # own = the groups own,control-plane, untainted, where the maintainer's private pods run: never through a pull request
        self.refused(doc("alice", 1001, "alice-pc", trust="own"), "own is only for the maintainer's own machines")
        self.refused(doc("alice", 1001, "alice-pc", trust="public"), "automatic demo")
        self.refused(doc("alice", 1001, "alice-pc", trust="root"), "trust must be friends")

    def test_nebula_public_key(self):
        bad = doc("alice", 1001, "alice-pc").replace("PUBLIC KEY", "PRIVATE KEY")
        self.refused(bad, "nebula_public_key")
        short = doc("alice", 1001, "alice-pc").replace(base64.b64encode(bytes([1]) * 32).decode(), base64.b64encode(b"x" * 16).decode())
        self.refused(short, "nebula_public_key")

    def test_recipient(self):
        ssh = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32).decode()
        self.assertEqual(self.parse(doc("alice", 1001, "alice-pc", recipient=ssh)).recipient, ssh)
        self.refused(doc("alice", 1001, "alice-pc", recipient=ssh + " alice@example.org"), "without a comment")
        self.refused(doc("alice", 1001, "alice-pc", recipient="age1short"), "recipient")


class PolicyFile(unittest.TestCase):
    def test_missing_policy(self):
        with self.assertRaises(P.Refused) as e:
            P.load_policy(None)
        self.assertIn("missing on the base branch", str(e.exception))

    def test_self_service_is_friends(self):
        with self.assertRaises(P.Refused):
            P.load_policy(POLICY.replace("self_service_trust: friends", "self_service_trust: own").encode())

    def test_no_grants(self):  # there is no way to raise one participant above friends: a grants key is refused
        with self.assertRaises(P.Refused):
            P.load_policy(policy("grants: {alice: own}\n"))


class Identity(unittest.TestCase):
    def test_regexp(self):
        import re
        rx = re.compile(P.identity_regexp("alice", "k8s-lan-party"))
        ok = "https://github.com/alice/k8s-lan-party/.github/workflows/sign.yml@refs/heads/join"
        self.assertTrue(rx.match(ok))
        self.assertTrue(rx.match(ok.replace("alice", "Alice")))   # GitHub logins are case-insensitive
        for bad in (ok.replace("alice", "bob"), ok.replace("alice", "alicex"), ok.replace("sign.yml", "other.yml"),
                    ok.replace("k8s-lan-party", "other"), ok.replace("refs/heads/join", "refs/tags/v1"), ok + " x",
                    ok.replace("https://github.com/", "https://github.com.evil/")):
            self.assertFalse(rx.match(bad), bad)


class Bundle(unittest.TestCase):
    """What is refused before cosign runs (the cosign path itself: make poc-participants)."""

    def verify(self, bundle: bytes | None):
        return P.Verifier("k8s-lan-party", cosign="/nonexistent/cosign")("alice", 1001, b"x", bundle)

    def test_refused_before_cosign(self):
        cert = {"certificate": {"rawBytes": base64.b64encode(b"der").decode()}}
        cases = {
            None: "not signed",
            b"not json": "not JSON",
            b"[]": "not a Sigstore bundle v0.3",
            b'{"mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.1"}': "not a Sigstore bundle v0.3",
            P.json.dumps({"mediaType": P.BUNDLE_V03, "verificationMaterial": {"publicKey": {"hint": "k"}}}).encode(): "exactly one Fulcio",
            P.json.dumps({"mediaType": P.BUNDLE_V03, "verificationMaterial": dict(cert, x509CertificateChain={})}).encode(): "exactly one Fulcio",
            P.json.dumps({"mediaType": P.BUNDLE_V03, "verificationMaterial": {}}).encode(): "no Fulcio certificate",
            b"{" + b" " * P.MAX_BUNDLE + b"}": "larger than",
        }
        for bundle, want in cases.items():
            p = self.verify(bundle)
            self.assertEqual(len(p), 1, (bundle[:60] if bundle else bundle, p))
            self.assertIn(want, p[0])


class CheckPR(unittest.TestCase):
    def check(self, changes, base_files, head_files, author="alice", author_id=1001, verify=None):
        return P.check_pr(changes, tree(base_files), tree(head_files), author, author_id, verify or Verify())

    def add(self, login):
        return [("A", f"participants/{login}.yaml"), ("A", f"participants/{login}.sigstore.json")]

    def test_nothing_to_check(self):
        self.assertEqual(self.check([("M", "README.md")], {}, {}), [])

    def test_valid_pr_passes(self):
        v = Verify()
        self.assertEqual(self.check(self.add("alice"), {}, signed("alice", 1001, "alice-pc"), verify=v), [])
        self.assertEqual(v.calls, ["alice"])

    def test_bad_signature_fails(self):
        self.assertEqual(self.check(self.add("alice"), {}, signed("alice", 1001, "alice-pc"), verify=Verify(bad={"alice"})),
                         ["alice: bad signature"])

    def test_unsigned_fails(self):
        p = self.check([("A", "participants/alice.yaml")], {}, {"participants/alice.yaml": doc("alice", 1001, "alice-pc")})
        self.assertEqual(p, ["alice: not signed"])

    def test_someone_elses_file_fails(self):  # replaying alice's signed file from another account
        p = self.check(self.add("alice"), {}, signed("alice", 1001, "alice-pc"), author="mallory", author_id=1003)
        self.assertTrue(any("only alice may add or change it" in x for x in p), p)

    def test_maintainer_cannot_add_someone(self):
        p = self.check(self.add("alice"), {}, signed("alice", 1001, "alice-pc"), author="maint", author_id=1)
        self.assertTrue(any("only alice may add" in x for x in p), p)

    def test_account_id_must_be_the_authors(self):
        p = self.check(self.add("alice"), {}, signed("alice", 4242, "alice-pc"))
        self.assertTrue(any("not the PR author's account id" in x for x in p), p)

    def test_account_id_cannot_move(self):  # a login that was renamed and registered again by somebody else
        base = signed("alice", 1001, "alice-pc")
        head = signed("alice", 2002, "alice-pc", n=2)
        p = self.check([("M", "participants/alice.yaml"), ("M", "participants/alice.sigstore.json")], base, head, author_id=2002)
        self.assertTrue(any("github_id changed" in x for x in p), p)

    def test_trust_above_ceiling_fails_without_maintainer(self):
        p = self.check(self.add("alice"), {}, signed("alice", 1001, "alice-pc", trust="own"))
        self.assertTrue(any("own is only for the maintainer's own machines" in x for x in p), p)

    def test_policy_edit_in_the_pr_itself_does_not_count(self):  # the base policy decides, and only the maintainer touches it
        head = P.DictTree({"participants/policy.yaml": policy().replace(b"[maint]", b"[maint, alice]"),
                           **{k: v.encode() for k, v in signed("alice", 1001, "alice-pc", trust="own").items()}})
        ch = self.add("alice") + [("M", "participants/policy.yaml")]
        p = P.check_pr(ch, tree({}), head, "alice", 1001, Verify())
        self.assertTrue(any("policy.yaml: only the maintainer" in x for x in p), p)
        self.assertTrue(any("own is only for the maintainer's own machines" in x for x in p), p)

    def test_node_name_unique(self):
        base = signed("bob", 1002, "shared", n=2)
        p = self.check(self.add("alice"), base, {**base, **signed("alice", 1001, "shared")})
        self.assertTrue(any("node name 'shared' is already bob's" in x for x in p), p)

    def test_same_key_or_recipient_refused(self):
        base = signed("bob", 1002, "bob-pc", n=1)
        p = self.check(self.add("alice"), base, {**base, **signed("alice", 1001, "alice-pc", n=1)})
        self.assertTrue(any("Nebula public key is already bob's" in x for x in p), p)
        self.assertTrue(any("recipient is already bob's" in x for x in p), p)

    def test_join_pr_only_touches_its_files(self):
        p = self.check(self.add("alice") + [("M", "poc/mesh")], {}, signed("alice", 1001, "alice-pc"))
        self.assertTrue(any("move poc/mesh to another pull request" in x for x in p), p)

    def test_removal(self):
        base = signed("alice", 1001, "alice-pc")
        gone = [("D", "participants/alice.yaml"), ("D", "participants/alice.sigstore.json")]
        self.assertEqual(self.check(gone, base, {}), [])                                   # alice leaves
        self.assertEqual(self.check(gone, base, {}, author="maint", author_id=1), [])        # the maintainer removes her
        p = self.check(gone, base, {}, author="mallory", author_id=1003)
        self.assertTrue(any("only alice or the maintainer may remove it" in x for x in p), p)
        # review finding 1: the login alone is not the participant; a re-registered login (another account id) cannot remove her
        p = self.check(gone, base, {}, author="alice", author_id=9999)
        self.assertTrue(any("only alice's account (id 1001) or the maintainer may remove it" in x for x in p), p)
        self.assertEqual(self.check(gone, base, {}, author="Alice", author_id=1001), [])      # logins are case-insensitive
        p = self.check(gone[:1], base, {"participants/alice.sigstore.json": "{}"})
        self.assertTrue(any("a signature without its alice.yaml" in x for x in p), p)

    def test_gitlink_is_not_a_removal(self):  # review finding 6: an entry in the tree that is not a readable blob
        class Gitlink(P.DictTree):
            def entries(self):
                return {**super().entries(), "alice.yaml": "160000"}
        p = P.check_pr([("T", "participants/alice.yaml")], tree(signed("alice", 1001, "alice-pc")),
                       Gitlink({"participants/policy.yaml": policy()}), "alice", 1001, Verify())
        self.assertEqual(p, ["participants/alice.yaml: must be a regular file"])

    def test_stray_files(self):
        p = self.check([("A", "participants/notes.txt"), ("A", "participants/x/alice.yaml")], {}, {})
        self.assertEqual(len(p), 2, p)

    def test_bootstrap_brings_the_policy(self):  # the first pull request adds policy.yaml and the README, no participant file
        ch = [("A", "participants/policy.yaml"), ("A", "participants/README.md"), ("A", "poc/participants.py")]
        self.assertEqual(P.check_pr(ch, tree({}, with_policy=False), tree({"participants/README.md": "x"}), "anyone", 7, Verify()), [])

    def test_no_policy_on_base(self):
        p = P.check_pr(self.add("alice"), tree({}, with_policy=False), tree(signed("alice", 1001, "alice-pc")), "alice", 1001, Verify())
        self.assertEqual(len(p), 1)
        self.assertIn("missing on the base branch", p[0])


class Issuer(unittest.TestCase):
    def test_validate_skips_only_already_verified_bytes(self):
        t = tree({**signed("alice", 1001, "alice-pc"), **signed("bob", 1002, "bob-pc", n=2)})
        v = Verify()
        _, valid, problems = P.validate_tree(t, v)
        self.assertEqual((sorted(valid), problems, v.calls), (["alice", "bob"], [], ["alice", "bob"]))
        seen = frozenset({("alice", valid["alice"].sha256, P.sig_sha256(t, "alice"))})
        v2 = Verify(bad={"alice", "bob"})
        _, valid2, problems2 = P.validate_tree(t, v2, seen)
        self.assertEqual((sorted(valid2), v2.calls), (["alice"], ["bob"]))   # alice's bytes were verified before; bob's are checked

    def test_policy_change_applies_to_issued_files(self):  # an own file on main is never valid: the schema runs every time
        t = tree(signed("alice", 1001, "alice-pc", trust="own"))
        _, valid, problems = P.validate_tree(t, Verify())
        self.assertEqual(valid, {})
        self.assertTrue(problems)

    def test_duplicates_on_main_are_both_invalid(self):
        t = tree({**signed("alice", 1001, "same"), **signed("bob", 1002, "same", n=2)})
        _, valid, problems = P.validate_tree(t, Verify())
        self.assertEqual((valid, len(problems)), ({}, 2))

    def test_plan(self):
        pol = P.load_policy(policy())
        a = P.parse_participant(doc("alice", 1001, "alice-pc").encode(), "alice", pol)
        b = P.parse_participant(doc("bob", 1002, "bob-pc", n=2).encode(), "bob", pol)
        b2 = P.parse_participant(doc("bob", 1002, "bob-pc", n=2).replace("cpu: 2", "cpu: 3").encode(), "bob", pol)
        c = P.parse_participant(doc("carol", 1004, "carol-pc", n=3).encode(), "carol", pol)
        c2 = P.parse_participant(doc("carol", 1004, "carol-new", n=4).encode(), "carol", pol)
        led = {"participants": {x.login: dict(P.cert_identity(x), sha256=x.sha256) for x in (a, b, c)},
               "retired_nodes": []}
        led["participants"]["gone"] = {"node": "gone-pc", "sha256": "x"}
        revoke, issue, update, unchanged = P.plan({"alice": a, "bob": b2, "carol": c2}, led)
        self.assertEqual((revoke, issue, update, unchanged), (["carol", "gone"], ["carol"], ["bob"], ["alice"]))
        self.assertEqual(P.plan({}, {"participants": {}, "retired_nodes": []}), ([], [], [], []))


class Packing(unittest.TestCase):
    def test_tar_is_deterministic(self):
        f = {"b": b"2", "a": b"1"}
        self.assertEqual(P._tar(f), P._tar(dict(reversed(list(f.items())))))
        self.assertEqual(P._untar(P._tar(f), ("a", "b")), f)

    def test_untar_refuses_extra_missing_and_links(self):
        with self.assertRaises(P.Refused):
            P._untar(P._tar({"a": b"1", "evil": b"x"}), ("a",))
        with self.assertRaises(P.Refused):
            P._untar(P._tar({"a": b"1"}), ("a", "b"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo("a")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
            t.addfile(info)
        with self.assertRaises(P.Refused):
            P._untar(buf.getvalue(), ("a",))


if __name__ == "__main__":
    unittest.main()
