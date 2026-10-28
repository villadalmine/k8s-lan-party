#!/usr/bin/env python3
"""Join by signed pull request (docs/DESIGN.md section 5.1, issue #12): persistent participants.

A participant adds participants/<login>.yaml (node name, resources, requested trust, their Nebula PUBLIC key and the public key the
bundle is encrypted to) and signs it with cosign keyless from the workflow .github/workflows/sign.yml in their own fork, which writes
participants/<login>.sigstore.json. Then:

  check     CI, on a pull request: every participant file the PR adds, changes or deletes. The signer (the certificate Fulcio issued
            to the workflow in <login>'s fork, and the numeric account id in it) must be the file's login and the PR author; the
            schema, the unique node name and the trust ceiling, friends (participants/policy.yaml read from the BASE branch, never from the PR)
            must hold. Only the maintainer touches the policy or other people's files.
  validate  the maintainer's host: the same checks on every file of a tree (main), without a PR author.
  sync      the maintainer's host, from a timer: re-verify main, revoke what is gone, changed or no longer verifies, issue what is new.
            Issuing signs the participant's own Nebula public key with the persistent CA (poc/issue.sh, IN_PUB: the private key never
            leaves their machine), signs the bundle with the issuer's cosign key and encrypts it to the participant's age or ssh-ed25519
            key: participants-outbox/<login>.age, handed over privately (never in a PR, an issue, a comment or an Actions artifact).
            The Nebula CA never leaves the maintainer's host: no GitHub secret, no Actions step holds it.
  open      the participant: decrypt the delivered file, verify the issuer's signature with the published key, unpack the bundle.
  lint      the schema of one file (the signing workflow runs it before it signs).

`--test-root <trusted_root.json>` verifies against a LOCAL TEST CA (poc/sigstore_test_ca.py) instead of the public Sigstore, with the
transparency log and SCT checks off: it is accepted only for a trusted root whose every authority is marked TEST ONLY, and only the PoC
fixture (make poc-participants) passes it. The real check (the workflow participants.yml) never does.

Exit codes: 0 fine, 1 refused or something left to do (one line per problem), 2 usage or a broken policy/tree (nothing was changed).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    yaml = None

POC = Path(__file__).resolve().parent
DIR = "participants"
POLICY = f"{DIR}/policy.yaml"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGN_WORKFLOW = ".github/workflows/sign.yml"
TEST_ORG = "k8s-lan-party TEST ONLY - not a Sigstore root"
LEVELS = {"public": 0, "friends": 1, "own": 2}
LOGIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}")      # GitHub: alphanumerics and single inner hyphens, 39 at most
NODE_RE = re.compile(r"[a-z][a-z0-9-]{1,19}")                           # the same rule as `mesh host invite` and admission.py
FILE_RE = re.compile(r"(?P<login>[a-z0-9-]{1,39})\.(?P<kind>yaml|sigstore\.json)")
NEBULA_PUB_RE = re.compile(r"-----BEGIN NEBULA X25519 PUBLIC KEY-----\n([A-Za-z0-9+/=]{40,48})\n-----END NEBULA X25519 PUBLIC KEY-----\n?")
AGE_RE = re.compile(r"age1[02-9ac-hj-np-z]{58}")
SSH_RE = re.compile(r"ssh-ed25519 (AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43})")   # no comment: a comment is often an e-mail address
FIELDS = {"apiVersion", "kind", "github", "github_id", "node", "trust", "resources", "nebula_public_key", "recipient"}
POLICY_FIELDS = {"apiVersion", "kind", "maintainers", "self_service_trust", "reserved_nodes", "cert_duration", "fork_name",
                 "nebula_net", "nebula_hosts"}
MAX_FILE = 4096
MAX_BUNDLE = 65536
BUNDLE_V03 = "application/vnd.dev.sigstore.bundle.v0.3+json"
# Fulcio's certificate extensions (github.com/sigstore/fulcio docs/oid-info.md); 1.8 onwards are DER UTF8String
OID_ISSUER_V2 = "1.3.6.1.4.1.57264.1.8"
OID_OWNER_ID = "1.3.6.1.4.1.57264.1.17"
BUNDLE_FILES = ("ca.crt", "config.yml", "host.crt", "join.env")   # what a participant's bundle holds (no host.key: it never leaves them)


class Refused(Exception):
    """A file or a policy that does not pass; the message says why."""


# --- reading YAML strictly ------------------------------------------------------------------------------------------------------------

def _loader():
    class Strict(yaml.SafeLoader):  # duplicate keys are refused: two parsers must never read one signed file two ways
        pass

    def mapping(loader, node, deep=False):
        keys = set()
        for k, _ in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in keys:
                raise Refused(f"duplicate key {key!r}")
            keys.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return Strict


def load_yaml(data: bytes, what: str):
    if yaml is None:
        raise Refused("PyYAML is missing (pip install pyyaml)")
    if len(data) > MAX_FILE:
        raise Refused(f"{what}: larger than {MAX_FILE} bytes")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Refused(f"{what}: only ASCII is accepted") from None
    try:
        return yaml.load(text, Loader=_loader())  # noqa: S506 - a SafeLoader subclass
    except Refused as e:
        raise Refused(f"{what}: {e}") from None
    except yaml.YAMLError as e:
        raise Refused(f"{what}: not valid YAML ({str(e).splitlines()[0]})") from None


def _int(v, lo: int, hi: int, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise Refused(f"{what} must be a whole number from {lo} to {hi}")
    return v


# --- the policy (maintainer-owned, read from the base branch) ------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    maintainers: tuple
    self_service_trust: str
    reserved_nodes: tuple
    cert_duration: str
    fork_name: str
    nebula_net: str
    nebula_hosts: tuple

    def ceiling(self, login: str) -> str:   # the same for every participant: there are no grants (own is never issued by PR)
        return self.self_service_trust

    def is_maintainer(self, login: str) -> bool:
        return login.lower() in self.maintainers


def load_policy(data: bytes | None) -> Policy:
    if data is None:
        raise Refused(f"{POLICY} is missing on the base branch: no participant file can be checked before it is on main")
    d = load_yaml(data, POLICY)
    if not isinstance(d, dict) or set(d) != POLICY_FIELDS:
        raise Refused(f"{POLICY}: the keys must be exactly {sorted(POLICY_FIELDS)}")
    if d["apiVersion"] != "k8s-lan-party/v1" or d["kind"] != "ParticipantPolicy":
        raise Refused(f"{POLICY}: apiVersion k8s-lan-party/v1, kind ParticipantPolicy")
    m = d["maintainers"]
    if not isinstance(m, list) or not m or not all(isinstance(x, str) and LOGIN_RE.fullmatch(x) for x in m):
        raise Refused(f"{POLICY}: maintainers is a non-empty list of lower-case GitHub logins")
    if d["self_service_trust"] not in ("friends",):
        raise Refused(f"{POLICY}: self_service_trust is friends: a pull request never issues own (own is the maintainer's own "
                      "machines, mesh host init) nor public (the automatic demo)")
    r = d["reserved_nodes"]
    if not isinstance(r, list) or not all(isinstance(x, str) for x in r):
        raise Refused(f"{POLICY}: reserved_nodes is a list of names")
    if not isinstance(d["cert_duration"], str) or not re.fullmatch(r"[1-9][0-9]{0,4}h", d["cert_duration"]):
        raise Refused(f"{POLICY}: cert_duration is hours, like 336h")
    if not isinstance(d["fork_name"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", d["fork_name"]):
        raise Refused(f"{POLICY}: fork_name is a repository name")
    if not isinstance(d["nebula_net"], str) or not re.fullmatch(r"(?:\d{1,3}\.){2}\d{1,3}", d["nebula_net"]):
        raise Refused(f"{POLICY}: nebula_net is the first three octets of the /24, like 10.77.0")
    h = d["nebula_hosts"]
    if not (isinstance(h, list) and len(h) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in h) and 5 <= h[0] <= h[1] <= 254):
        raise Refused(f"{POLICY}: nebula_hosts is [first, last] of the host numbers participants get, inside 5..254")
    return Policy(tuple(m), d["self_service_trust"], tuple(r), d["cert_duration"], d["fork_name"], d["nebula_net"], tuple(h))


# --- one participant file --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Participant:
    login: str
    github_id: int
    node: str
    trust: str
    cpu: int
    memory_gib: int
    nebula_public_key: str
    recipient: str
    sha256: str


def parse_participant(data: bytes, login: str, policy: Policy) -> Participant:
    """The schema of participants/<login>.yaml and the trust ceiling; raises Refused."""
    what = f"{DIR}/{login}.yaml"
    d = load_yaml(data, what)
    if not isinstance(d, dict):
        raise Refused(f"{what}: a mapping is expected")
    if set(d) != FIELDS:
        extra, missing = sorted(set(d) - FIELDS), sorted(FIELDS - set(d))
        raise Refused(f"{what}: the keys must be exactly {sorted(FIELDS)}" + (f"; unknown {extra}" if extra else "") +
                      (f"; missing {missing}" if missing else ""))
    if d["apiVersion"] != "k8s-lan-party/v1" or d["kind"] != "Participant":
        raise Refused(f"{what}: apiVersion k8s-lan-party/v1, kind Participant")
    if d["github"] != login:
        raise Refused(f"{what}: github must be {login!r}, the file's own name (lower case)")
    gid = _int(d["github_id"], 1, 2**53, f"{what}: github_id")
    node = d["node"]
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        raise Refused(f"{what}: node must be 2 to 20 characters, a-z, 0-9 and '-', starting with a letter")
    if node in policy.reserved_nodes:
        raise Refused(f"{what}: node {node!r} is reserved")
    trust = d["trust"]
    if trust not in LEVELS:
        raise Refused(f"{what}: trust must be friends")
    if trust == "public":
        raise Refused(f"{what}: trust public is the automatic demo (no pull request): ask for friends")
    ceiling = policy.ceiling(login)
    if LEVELS[trust] > LEVELS[ceiling]:
        # own puts a node in the groups own,control-plane, untainted, where the maintainer's private pods run (DESIGN.md section 4):
        # only the maintainer's own machines are own, joined with `mesh host init`, never a remote machine through a pull request
        raise Refused(f"{what}: trust {trust} is above what a participant may ask for ({ceiling}); own is only for the "
                      "maintainer's own machines, never through a pull request")
    res = d["resources"]
    if not isinstance(res, dict) or set(res) != {"cpu", "memory_gib"}:
        raise Refused(f"{what}: resources has exactly cpu and memory_gib")
    cpu = _int(res["cpu"], 1, 256, f"{what}: resources.cpu")
    mem = _int(res["memory_gib"], 1, 2048, f"{what}: resources.memory_gib")
    pub = d["nebula_public_key"]
    m = NEBULA_PUB_RE.fullmatch(pub) if isinstance(pub, str) else None
    try:
        ok = bool(m) and len(base64.b64decode(m.group(1), validate=True)) == 32
    except binascii.Error:
        ok = False
    if not ok:
        raise Refused(f"{what}: nebula_public_key is the output of `nebula-cert keygen -out-pub` (an X25519 PUBLIC key; keep the private one)")
    rcp = d["recipient"]
    if not isinstance(rcp, str) or not (AGE_RE.fullmatch(rcp) or _ssh_ed25519(rcp)):
        raise Refused(f"{what}: recipient is an age public key (age1...) or 'ssh-ed25519 AAAA...' without a comment")
    return Participant(login, gid, node, trust, cpu, mem, pub.strip() + "\n", rcp, hashlib.sha256(data).hexdigest())


def _ssh_ed25519(s: str) -> bool:
    m = SSH_RE.fullmatch(s)
    if not m:
        return False
    try:
        blob = base64.b64decode(m.group(1), validate=True)
    except binascii.Error:
        return False
    return len(blob) == 51 and blob[:15] == b"\x00\x00\x00\x0bssh-ed25519"


# --- the signature: cosign verify-blob, then the certificate's own claims -----------------------------------------------------------

def identity_regexp(login: str, fork_name: str) -> str:
    """The only certificate identity accepted for <login>: the signing workflow in <login>'s fork, on a branch (RE2 and Python re)."""
    return (r"^https://github\.com/(?i:" + re.escape(login) + r")/" + re.escape(fork_name) + r"/" + re.escape(SIGN_WORKFLOW) +
            r"@refs/heads/[A-Za-z0-9._/-]+$")


def leaf_certificate(bundle) -> bytes:
    """The one certificate of a v0.3 bundle: the same one cosign verifies. A bundle that also carries a chain or a key is refused, so
    cosign and this code can never read two different certificates out of one file."""
    if not isinstance(bundle, dict) or bundle.get("mediaType") != BUNDLE_V03:
        raise Refused("the signature is not a Sigstore bundle v0.3 (cosign 3 `sign-blob --bundle`)")
    vm = bundle.get("verificationMaterial")
    if not isinstance(vm, dict) or set(vm) & {"x509CertificateChain", "publicKey"}:
        raise Refused("the signature must carry exactly one Fulcio certificate (a keyless signature, no chain, no key)")
    raw = (vm.get("certificate") or {}).get("rawBytes") if isinstance(vm.get("certificate"), dict) else None
    if not isinstance(raw, str):
        raise Refused("the signature carries no Fulcio certificate (a keyless signature is required, not a key)")
    try:
        return base64.b64decode(raw, validate=True)
    except binascii.Error:
        raise Refused("the certificate in the signature is not base64") from None


def certificate_claims(der: bytes) -> dict:
    """SAN URIs, the OIDC issuer and the account id of the repository owner, from a Fulcio certificate."""
    from cryptography import x509
    from cryptography.x509.oid import ObjectIdentifier

    cert = x509.load_der_x509_certificate(der)

    def utf8(oid: str) -> str | None:
        try:
            v = cert.extensions.get_extension_for_oid(ObjectIdentifier(oid)).value.value
        except x509.ExtensionNotFound:
            return None
        if len(v) < 2 or v[0] != 0x0C:   # DER UTF8String, short or long length form
            return None
        n, off = v[1], 2
        if n & 0x80:
            k = n & 0x7F
            n, off = int.from_bytes(v[2:2 + k], "big"), 2 + k
        return v[off:off + n].decode("utf-8", "replace") if off + n == len(v) else None

    try:
        uris = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        uris = []
    return {"uris": list(uris), "issuer": utf8(OID_ISSUER_V2), "owner_id": utf8(OID_OWNER_ID)}


def check_test_root(path: str) -> None:
    """--test-root is accepted only for a trusted root made by poc/sigstore_test_ca.py: every authority marked TEST ONLY, no logs."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"--test-root {path}: cannot be read ({e})") from None
    cas = d.get("certificateAuthorities") or []
    if not cas or any((c.get("subject") or {}).get("organization") != TEST_ORG for c in cas) or d.get("tlogs") or d.get("ctlogs"):
        raise SystemExit(f"--test-root {path}: not a local TEST root (poc/sigstore_test_ca.py); refusing to skip the transparency log")


class Verifier:
    """verify(login, github_id, yaml_bytes, bundle_bytes) -> list of problems (empty: signed by that account's signing workflow)."""

    def __init__(self, fork_name: str, cosign: str = "cosign", test_root: str | None = None):
        self.fork_name, self.cosign, self.test_root = fork_name, cosign, test_root
        if test_root:
            check_test_root(test_root)

    def __call__(self, login: str, github_id: int, blob: bytes, bundle_bytes: bytes | None) -> list[str]:
        what = f"{DIR}/{login}.yaml"
        if bundle_bytes is None:
            return [f"{what}: not signed: {DIR}/{login}.sigstore.json is missing (run the 'Sign my participant file' workflow in your fork)"]
        if len(bundle_bytes) > MAX_BUNDLE:
            return [f"{DIR}/{login}.sigstore.json: larger than {MAX_BUNDLE} bytes"]
        try:
            der = leaf_certificate(json.loads(bundle_bytes))
        except Refused as e:
            return [f"{DIR}/{login}.sigstore.json: {e}"]
        except ValueError:
            return [f"{DIR}/{login}.sigstore.json: not JSON"]
        rx = identity_regexp(login, self.fork_name)
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "blob").write_bytes(blob)
            (Path(t) / "bundle.json").write_bytes(bundle_bytes)
            cmd = [self.cosign, "verify-blob", "--bundle", f"{t}/bundle.json", "--certificate-identity-regexp", rx,
                   "--certificate-oidc-issuer", OIDC_ISSUER]
            if self.test_root:   # the PoC fixture only (check_test_root): a local CA, no transparency log, no SCT
                cmd += ["--trusted-root", self.test_root, "--insecure-ignore-tlog", "--insecure-ignore-sct"]
            try:
                r = subprocess.run(cmd + [f"{t}/blob"], capture_output=True, text=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired) as e:
                return [f"{what}: cosign did not run ({e})"]
        if r.returncode != 0:
            err = [ln for ln in r.stderr.splitlines() if ln.startswith("Error:")]
            return [f"{what}: the signature does not verify for {login}: {(err[-1][7:] if err else r.stderr.strip()[-300:])}"]
        try:
            c = certificate_claims(der)
        except ValueError as e:
            return [f"{what}: the certificate cannot be read ({e})"]
        problems = []
        if len(c["uris"]) != 1 or not re.fullmatch(rx[1:-1], c["uris"][0]):
            problems.append(f"{what}: the certificate names {c['uris']}, not the signing workflow of {login}'s fork")
        if c["issuer"] != OIDC_ISSUER:
            problems.append(f"{what}: the certificate's OIDC issuer is {c['issuer']!r}, not GitHub Actions")
        if c["owner_id"] != str(github_id):
            problems.append(f"{what}: signed from the GitHub account with id {c['owner_id']}, but the file says github_id {github_id} "
                            "(a renamed or re-registered login does not inherit a participant)")
        return problems


# --- trees: a git commit (CI, the maintainer's clone) or a dict (unit tests) ---------------------------------------------------------

class GitTree:
    def __init__(self, repo: str, ref: str):
        self.repo = repo
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True, text=True)
        if r.returncode != 0:
            raise Refused(f"no such commit: {ref}")
        self.sha = r.stdout.strip()

    def entries(self) -> dict:
        """{name: mode} of what is directly under participants/ (a sub-directory shows with mode 040000)."""
        r = subprocess.run(["git", "-C", self.repo, "ls-tree", "-z", self.sha, f"{DIR}/"], capture_output=True, check=True)
        out = {}
        for rec in r.stdout.split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                out[path.decode()[len(DIR) + 1:]] = meta.split()[0].decode()
        return out

    def read(self, path: str) -> bytes | None:
        r = subprocess.run(["git", "-C", self.repo, "cat-file", "blob", f"{self.sha}:{path}"], capture_output=True)
        return r.stdout if r.returncode == 0 else None


class DictTree:
    def __init__(self, files: dict):
        self.files = {k: (v.encode() if isinstance(v, str) else v) for k, v in files.items()}

    def entries(self) -> dict:
        out = {}
        for p in self.files:
            if p.startswith(DIR + "/"):
                rest = p[len(DIR) + 1:]
                out[rest.split("/")[0]] = "040000" if "/" in rest else "100644"
        return out

    def read(self, path: str) -> bytes | None:
        return self.files.get(path)


def changed_paths(repo: str, base: str, head: str) -> list[tuple[str, str]]:
    mb = subprocess.run(["git", "-C", repo, "merge-base", base, head], capture_output=True, text=True, check=True).stdout.strip()
    r = subprocess.run(["git", "-C", repo, "diff", "--no-renames", "--name-status", "-z", mb, head], capture_output=True, check=True)
    parts = [p.decode() for p in r.stdout.split(b"\0") if p]
    return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


# --- the checks ----------------------------------------------------------------------------------------------------------------------

def _uniqueness(found: dict, policy: Policy) -> dict:
    """{login: [problems]} for every file that shares a node name, a Nebula key or a recipient with another one."""
    out: dict = {}
    for field, label in (("node", "node name"), ("nebula_public_key", "Nebula public key"), ("recipient", "recipient")):
        seen: dict = {}
        for login in sorted(found):
            seen.setdefault(getattr(found[login], field), []).append(login)
        for value, logins in seen.items():
            if len(logins) > 1:
                for login in logins:
                    others = ", ".join(x for x in logins if x != login)
                    shown = f" {value!r}" if field == "node" else ""
                    out.setdefault(login, []).append(f"{DIR}/{login}.yaml: the {label}{shown} is already {others}'s: it must be unique")
    return out


def _parse_tree(tree, policy: Policy) -> tuple[dict, dict]:
    """Every participant file of a tree: ({login: Participant}, {login: [problems]}) without the signatures."""
    found, problems = {}, {}
    entries = tree.entries()
    for name in sorted(entries):
        m = FILE_RE.fullmatch(name)
        if name in ("README.md", "policy.yaml", "issuer.pub"):
            continue
        if not m or not LOGIN_RE.fullmatch(m["login"]) or entries[name] != "100644":
            problems.setdefault(name, []).append(f"{DIR}/{name}: not a participant file (participants/<login>.yaml and "
                                                 "<login>.sigstore.json, regular files, lower case)")
            continue
        login = m["login"]
        if m["kind"] == "sigstore.json":
            if f"{login}.yaml" not in entries:
                problems.setdefault(login, []).append(f"{DIR}/{name}: a signature without its {login}.yaml")
            continue
        try:
            found[login] = parse_participant(tree.read(f"{DIR}/{name}"), login, policy)
        except Refused as e:
            problems.setdefault(login, []).append(str(e))
    for login, ps in _uniqueness(found, policy).items():
        problems.setdefault(login, []).extend(ps)
    return found, problems


def check_pr(changes: list, base, head, author: str, author_id: int, verify) -> list[str]:
    """The pull request gate. `changes` is [(status, path)] between the merge base and the head."""
    touched = sorted({p for _, p in changes if p.startswith(DIR + "/")})
    if not touched:
        return []
    try:
        policy = load_policy(base.read(POLICY))   # the BASE branch's policy: a PR cannot raise its own ceiling
    except Refused as e:
        if base.read(POLICY) is None and all(p[len(DIR) + 1:] in ("README.md", "policy.yaml", "issuer.pub") for p in touched):
            return []   # the pull request that brings the policy itself: there is no participant file to judge yet
        return [str(e)]
    author = author.lower()
    maint = policy.is_maintainer(author)
    problems, logins = [], set()
    for path in touched:
        name = path[len(DIR) + 1:]
        m = FILE_RE.fullmatch(name)
        if name in ("README.md", "policy.yaml", "issuer.pub"):
            if not maint:
                problems.append(f"{path}: only the maintainer changes it")
        elif "/" in name or not m or not LOGIN_RE.fullmatch(m["login"]):
            problems.append(f"{path}: not a participant file (participants/<login>.yaml and <login>.sigstore.json, lower case)")
        else:
            logins.add(m["login"])
    others = sorted({p for _, p in changes if not p.startswith(DIR + "/")})
    if logins and others and not maint:
        problems.append(f"a join pull request changes only participants/{author}.yaml and its .sigstore.json; move {others[0]}"
                        f"{' and ' + str(len(others) - 1) + ' more' if len(others) > 1 else ''} to another pull request")
    found, tree_problems = _parse_tree(head, policy)
    head_entries = head.entries()
    for login in sorted(logins):
        y, s = f"{DIR}/{login}.yaml", f"{DIR}/{login}.sigstore.json"
        data, sig = head.read(y), head.read(s)
        if data is None:   # the file goes: a removal (or a stray signature)
            if f"{login}.yaml" in head_entries:   # still in the tree but not a readable file (a gitlink, a directory)
                problems.append(f"{y}: must be a regular file")
                continue
            if sig is not None:
                problems.append(f"{s}: a signature without its {login}.yaml (delete both)")
            if not maint:
                if author != login:
                    problems.append(f"{y}: only {login} or the maintainer may remove it")
                else:   # the login alone is not the participant: a renamed login re-registered by somebody else has another id
                    was_id = None
                    with contextlib.suppress(Refused):
                        was = load_yaml(base.read(y) or b"", y)
                        was_id = was.get("github_id") if isinstance(was, dict) else None
                    if was_id != author_id:
                        problems.append(f"{y}: only {login}'s account (id {was_id}) or the maintainer may remove it; "
                                        f"the PR author's id is {author_id}")
            continue
        if author != login:
            problems.append(f"{y}: only {login} may add or change it (the PR author is {author})")
            continue
        if head_entries.get(f"{login}.yaml") != "100644" or (sig is not None and head_entries.get(f"{login}.sigstore.json") != "100644"):
            problems.append(f"{y}: must be a regular file (not a link)")
            continue
        if login in tree_problems:
            problems.extend(tree_problems[login])
            continue
        p = found[login]
        if p.github_id != author_id:
            problems.append(f"{y}: github_id {p.github_id} is not the PR author's account id ({author_id})")
            continue
        old = base.read(y)
        if old is not None:
            with contextlib.suppress(Refused):
                was = load_yaml(old, y)
                if isinstance(was, dict) and was.get("github_id") != p.github_id:
                    problems.append(f"{y}: github_id changed ({was.get('github_id')} -> {p.github_id}): a login does not move to another account")
                    continue
        problems.extend(verify(login, p.github_id, data, sig))
    return problems


def sig_sha256(tree, login: str) -> str:
    return hashlib.sha256(tree.read(f"{DIR}/{login}.sigstore.json") or b"").hexdigest()


def validate_tree(tree, verify, verified: frozenset = frozenset()) -> tuple[Policy, dict, list[str]]:
    """The issuing side: (policy, {login: Participant} that pass everything, problems). Raises Refused when the policy itself is broken.
    The schema, the ceiling and the uniqueness are checked on every file, every time (a policy change applies at once). The signature
    is verified for every file that was not verified before in exactly these bytes (`verified`: (login, file sha256, signature sha256)
    from the ledger): the same bytes cannot stop verifying, so an unreachable Sigstore never reads as "every participant is gone"."""
    policy = load_policy(tree.read(POLICY))
    found, problems = _parse_tree(tree, policy)
    valid = {}
    for login in sorted(found):
        if login in problems:
            continue
        if (login, found[login].sha256, sig_sha256(tree, login)) in verified:
            valid[login] = found[login]
            continue
        ps = verify(login, found[login].github_id, tree.read(f"{DIR}/{login}.yaml"), tree.read(f"{DIR}/{login}.sigstore.json"))
        if ps:
            problems[login] = ps
        else:
            valid[login] = found[login]
    return policy, valid, [p for k in sorted(problems) for p in problems[k]]


# --- the issuing side: sync --------------------------------------------------------------------------------------------------------

def cert_identity(p: Participant) -> dict:
    """What the issued certificate and its delivery depend on: a change here revokes the old certificate and issues a new one."""
    return {"node": p.node, "trust": p.trust, "pub_sha256": hashlib.sha256(p.nebula_public_key.encode()).hexdigest(), "recipient": p.recipient}


def plan(valid: dict, ledger: dict) -> tuple[list, list, list, list]:
    """(revoke, issue, update, unchanged) logins. A file that is gone or no longer passes is revoked; one whose node, trust, Nebula key
    or recipient changed is revoked and issued again (under its new node name: a name is never issued twice); a change elsewhere
    (resources) only updates the ledger."""
    have = ledger["participants"]
    same = {x for x in valid if x in have and all(have[x].get(k) == v for k, v in cert_identity(valid[x]).items())}
    revoke = sorted(x for x in have if x not in same)
    issue = sorted(x for x in valid if x not in same)
    update = sorted(x for x in same if have[x]["sha256"] != valid[x].sha256)
    unchanged = sorted(x for x in same if have[x]["sha256"] == valid[x].sha256)
    return revoke, issue, update, unchanged


def _ledger_load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"participants": {}, "retired_nodes": []}


def _ledger_save(path: Path, led: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _run(cmd: list, env: dict | None = None, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=data, capture_output=True, env=env, timeout=600)


def _tar(files: dict) -> bytes:
    """A deterministic tar: the given names in order, regular files, mode 0600, mtime 0, no owner."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(files[name]), 0o600, 0
            t.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


def _untar(data: bytes, allowed: tuple, limit: int = 1 << 20) -> dict:
    """{name: bytes} of a tar holding exactly the allowed names as regular files, nothing else (no links, no paths)."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
        for m in t.getmembers():
            if m.name not in allowed or not m.isreg() or m.size > limit or m.name in out:
                raise Refused(f"unexpected member in the bundle: {m.name!r}")
            out[m.name] = t.extractfile(m).read()
    if set(out) != set(allowed):
        raise Refused(f"the bundle must hold {list(allowed)}, it holds {sorted(out)}")
    return out


def deliver(bundle_dir: Path, recipient: str, out: Path, issuer_key: str, cosign: str, age: str, signing_config: str | None) -> None:
    """Pack the bundle, sign it with the issuer's cosign key, encrypt both to the participant: <out>, mode 0600."""
    inner = _tar({n: (bundle_dir / n).read_bytes() for n in BUNDLE_FILES})
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "bundle.tar").write_bytes(inner)
        cmd = [cosign, "sign-blob", "--yes", "--key", issuer_key, "--bundle", f"{t}/bundle.tar.sigstore.json"]
        if signing_config:
            cmd += ["--signing-config", signing_config]
        r = _run(cmd + [f"{t}/bundle.tar"])
        if r.returncode != 0:
            raise Refused(f"cosign sign-blob failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
        outer = _tar({"bundle.tar": inner, "bundle.tar.sigstore.json": (Path(t) / "bundle.tar.sigstore.json").read_bytes()})
    tmp = out.with_suffix(".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    r = _run([age, "--encrypt", "--recipient", recipient, "--output", str(tmp)], data=outer)
    if r.returncode != 0:
        tmp.unlink()
        raise Refused(f"age failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)


def _fingerprint(nebula_cert: str, crt: Path) -> str:
    r = _run([nebula_cert, "print", "-json", "-path", str(crt)])
    if r.returncode != 0:
        raise Refused(f"nebula-cert print failed on {crt}")
    return json.loads(r.stdout)[0]["fingerprint"]


def _used_ips(bundles: Path) -> set:
    used = set()
    for env in bundles.glob("*/join.env"):
        for line in env.read_text().splitlines():
            if line.startswith("NODE_IP="):
                used.add(line[8:].strip())
    return used


def sync(a) -> int:
    state = Path(a.state)
    pdir = state / "participants"
    outbox = state / "participants-outbox"
    pdir.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(mode=0o700, exist_ok=True)
    os.chmod(outbox, 0o700)
    lock = open(pdir / "lock", "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
    except ImportError:  # pragma: no cover - not Linux/macOS
        pass
    ledger_path = pdir / "ledger.json"
    led = _ledger_load(ledger_path)
    verified = frozenset((k, v["sha256"], v["sig_sha256"]) for k, v in led["participants"].items())
    try:
        tree = GitTree(a.repo, a.ref)
        verify = Verifier(_policy_fork(tree), a.cosign, a.test_root)
        policy, valid, problems = validate_tree(tree, verify, verified)
    except Refused as e:
        print(f"sync: nothing done: {e}")
        return 2
    revoke, issue, update, unchanged = plan(valid, led)
    for p in problems:
        print(f"not valid: {p}")
    bundles = state / "bundles"
    env = dict(os.environ, EMESH_STATE=str(state))
    n_issued = n_revoked = 0
    unrevoked = set()   # logins whose old certificate could not be revoked: nothing new is issued to them until it is
    for login in revoke:
        e = led["participants"][login]
        why = "its file changed" if login in valid else "its file is gone from main or does not pass any more"
        r = _run(shlex.split(a.revoke_cmd) + [e["node"]], env=env)
        if r.returncode != 0:
            problems.append(f"revoke {login} (node {e['node']}) failed (exit {r.returncode}): it stays issued; the next sync tries again")
            print(f"revoke failed: {login} (node {e['node']}): {r.stdout.decode(errors='replace').strip()[-200:]}")
            unrevoked.add(login)
            continue
        with contextlib.suppress(FileNotFoundError):
            (outbox / f"{login}.age").unlink()
        led["participants"].pop(login)
        led["retired_nodes"] = sorted(set(led["retired_nodes"]) | {e["node"]})
        _ledger_save(ledger_path, led)
        n_revoked += 1
        print(f"revoked {login} (node {e['node']}): {why}")
    for login in issue:
        p = valid[login]
        if login in unrevoked:   # issuing now would overwrite the ledger entry of the certificate that is still valid
            print(f"not issued: {login}: the old certificate (node {led['participants'][login]['node']}) is not revoked yet")
            continue
        if p.node in led["retired_nodes"] or (bundles / p.node).exists():
            problems.append(f"{DIR}/{login}.yaml: node name {p.node!r} was issued on this host before: pick another one")
            print(f"not issued: {login}: node name {p.node!r} was used before")
            continue
        used = _used_ips(bundles) | {e["ip"] for e in led["participants"].values()}
        lo, hi = policy.nebula_hosts
        ip = next((f"{policy.nebula_net}.{i}" for i in range(hi, lo - 1, -1) if f"{policy.nebula_net}.{i}" not in used), None)
        if ip is None:
            problems.append(f"{login}: no free Nebula address in {policy.nebula_net}.{lo}-{hi}")
            continue
        pub = pdir / f"{login}.pub"
        pub.write_text(p.nebula_public_key)
        if p.trust != "friends":   # parse_participant refuses anything else; a second lock where the CA is used
            problems.append(f"{login}: trust {p.trust} is never issued through a pull request")
            continue
        ienv = dict(env, IN_PUB=str(pub), TAINT="yes")   # the trust=friends:NoSchedule taint from registration (DESIGN.md section 4)
        r = _run([a.issue_cmd, p.node, ip, p.trust, policy.cert_duration], env=ienv)
        if r.returncode != 0:
            problems.append(f"issue {login} failed (exit {r.returncode}): {r.stdout.decode(errors='replace').strip()[-200:]}")
            print(f"not issued: {login}: poc/issue.sh failed")
            shutil.rmtree(bundles / p.node, ignore_errors=True)
            continue
        g = _run(shlex.split(a.guard_cmd), env=env)   # the node guard admits the new name (mesh host burn rewrites it)
        try:
            fp = _fingerprint(a.nebula_cert, bundles / p.node / "host.crt")
            deliver(bundles / p.node, p.recipient, outbox / f"{login}.age", a.issuer_key, a.cosign, a.age, a.signing_config)
        except Refused as e:
            problems.append(f"deliver {login}: {e}; revoking what was issued")
            _run(shlex.split(a.revoke_cmd) + [p.node], env=env)
            led["retired_nodes"] = sorted(set(led["retired_nodes"]) | {p.node})
            _ledger_save(ledger_path, led)
            continue
        led["participants"][login] = dict(cert_identity(p), ip=ip, sha256=p.sha256, sig_sha256=sig_sha256(tree, login), fingerprint=fp,
                                          github_id=p.github_id)
        _ledger_save(ledger_path, led)
        n_issued += 1
        note = "" if g.returncode == 0 else " (the node guard was not refreshed: run mesh host burn)"
        print(f"issued {login} (node {p.node}, {ip}, {p.trust}): participants-outbox/{login}.age{note}")
    for login in update:   # the same certificate still fits (only the resources changed): record the new file
        led["participants"][login].update(sha256=valid[login].sha256, sig_sha256=sig_sha256(tree, login))
        _ledger_save(ledger_path, led)
        print(f"updated {login} (node {led['participants'][login]['node']}): same certificate")
    for login in unchanged:
        print(f"unchanged {login} (node {led['participants'][login]['node']})")
    print(f"sync: issued {n_issued}, revoked {n_revoked}, unchanged {len(unchanged)}, problems {len(problems)}")
    return 1 if problems else 0


def _policy_fork(tree) -> str:
    return load_policy(tree.read(POLICY)).fork_name


# --- the participant's side: open the delivered bundle ---------------------------------------------------------------------------

def open_bundle(a) -> int:
    r = _run([a.age, "--decrypt", "--identity", a.identity, a.file])
    if r.returncode != 0:
        print(f"open: cannot decrypt {a.file} with {a.identity}: it was not encrypted to this key")
        return 1
    try:
        outer = _untar(r.stdout, ("bundle.tar", "bundle.tar.sigstore.json"))
    except (Refused, tarfile.TarError) as e:
        print(f"open: {e}")
        return 1
    with tempfile.TemporaryDirectory() as t:
        for n, b in outer.items():
            (Path(t) / n).write_bytes(b)
        cmd = [a.cosign, "verify-blob", "--key", a.issuer_pub, "--bundle", f"{t}/bundle.tar.sigstore.json"]
        if a.offline_test:   # the PoC fixture: its test issuer key signs without a transparency log
            cmd.append("--insecure-ignore-tlog")
        v = _run(cmd + [f"{t}/bundle.tar"])
    if v.returncode != 0:
        print(f"open: REFUSED: the bundle is not signed by the issuer key {a.issuer_pub} (anybody can encrypt to your public key; "
              "only the maintainer can sign)")
        return 1
    try:
        files = _untar(outer["bundle.tar"], BUNDLE_FILES)
    except (Refused, tarfile.TarError) as e:
        print(f"open: {e}")
        return 1
    out = Path(a.out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    for n, b in files.items():
        fd = os.open(out / n, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b)
    if a.key:
        shutil.copyfile(a.key, out / "host.key")
        os.chmod(out / "host.key", 0o600)
    print(f"bundle verified (signed by {a.issuer_pub}) and unpacked into {out}" +
          ("" if a.key else "; copy your host.key (the private half of nebula_public_key) into it") +
          f"; then: sudo edge-mesh join {out} --accept-golden-rule")
    return 0


# --- command line ------------------------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="CI: the participant files a pull request touches")
    c.add_argument("--repo", default=".")
    c.add_argument("--base", required=True, help="the base branch, e.g. origin/main (its policy.yaml is the one applied)")
    c.add_argument("--head", default="HEAD")
    c.add_argument("--author", required=True, help="the PR author's login")
    c.add_argument("--author-id", required=True, type=int, help="the PR author's numeric account id")
    v = sub.add_parser("validate", help="the issuing side: every participant file of a commit")
    v.add_argument("--repo", default=".")
    v.add_argument("--ref", default="HEAD")
    s = sub.add_parser("sync", help="the issuing side: revoke what is gone, issue what is new (from a timer)")
    s.add_argument("--repo", required=True, help="a clone of the repository, fetched (the timer runs git fetch first)")
    s.add_argument("--ref", default="origin/main")
    s.add_argument("--state", default=os.environ.get("EMESH_STATE", str(POC / ".state")))
    s.add_argument("--issuer-key", required=True, help="the issuer's cosign private key (COSIGN_PASSWORD in the environment)")
    s.add_argument("--signing-config", help="a cosign signing config (the PoC: one without a transparency log, offline)")
    s.add_argument("--issue-cmd", default=str(POC / "issue.sh"))
    s.add_argument("--revoke-cmd", default=f"{POC / 'mesh'} host revoke")
    s.add_argument("--guard-cmd", default=f"{POC / 'mesh'} host burn")
    s.add_argument("--nebula-cert", default=str(Path(os.environ.get("EMESH_CACHE", POC / ".cache")) / "nebula-cert"))
    s.add_argument("--age", default="age")
    o = sub.add_parser("open", help="the participant: decrypt, verify the issuer's signature, unpack")
    o.add_argument("file")
    o.add_argument("--identity", required=True, help="your age identity file or ssh-ed25519 private key")
    o.add_argument("--issuer-pub", required=True, help="the issuer's public key, from the repository (participants/issuer.pub)")
    o.add_argument("--out", required=True)
    o.add_argument("--key", help="your Nebula host.key, copied into the bundle")
    o.add_argument("--age", default="age")
    o.add_argument("--offline-test", action="store_true", help="the PoC fixture only: its test issuer key has no transparency log entry")
    lt = sub.add_parser("lint", help="the schema of one participant file (before signing)")
    lt.add_argument("file")
    lt.add_argument("--policy", default=str(POC.parent / POLICY))
    for p in (c, v, s, o):
        p.add_argument("--cosign", default="cosign")
    for p in (c, v, s):
        p.add_argument("--test-root", help="the PoC fixture only: a LOCAL TEST trusted root (poc/sigstore_test_ca.py)")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        try:
            base, head = GitTree(a.repo, a.base), GitTree(a.repo, a.head)
            changes = changed_paths(a.repo, a.base, a.head)
        except Refused as e:
            print(f"FAIL {e}")
            return 1
        try:
            fork = load_policy(base.read(POLICY)).fork_name
        except Refused:
            fork = ""   # check_pr reports the policy itself (or lets the pull request that brings it pass)
        problems = check_pr(changes, base, head, a.author, a.author_id, Verifier(fork, a.cosign, a.test_root))
        n = len({p for _, p in changes if p.startswith(DIR + "/")})
        for p in problems:
            print(f"FAIL {p}")
        if not problems:
            print(f"OK {n} file(s) under participants/ checked" if n else "OK no participant file changed")
        return 1 if problems else 0
    if a.cmd == "validate":
        try:
            tree = GitTree(a.repo, a.ref)
            _, valid, problems = validate_tree(tree, Verifier(_policy_fork(tree), a.cosign, a.test_root))
        except Refused as e:
            print(f"FAIL {e}")
            return 2
        for login in sorted(valid):
            print(f"OK {login} (node {valid[login].node}, {valid[login].trust})")
        for p in problems:
            print(f"FAIL {p}")
        return 1 if problems else 0
    if a.cmd == "sync":
        return sync(a)
    if a.cmd == "open":
        return open_bundle(a)
    if a.cmd == "lint":
        f = Path(a.file)
        try:
            p = parse_participant(f.read_bytes(), f.name[:-5] if f.name.endswith(".yaml") else f.name, load_policy(Path(a.policy).read_bytes()))
        except (Refused, OSError) as e:
            print(f"FAIL {e}")
            return 1
        print(f"OK {f}: node {p.node}, trust {p.trust}, github_id {p.github_id}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
