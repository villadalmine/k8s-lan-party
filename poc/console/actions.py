"""The host console's action registry (docs/CONSOLE.md, issue #85): the only `mesh` verbs the API may run, with typed parameters.

Each entry is mirrored in `x-actions` of poc/console/openapi.json (poc/test_console.py keeps them equal). `argv()` turns a verb, its
arguments and the caller's role into the argument list of `poc/mesh` (never a shell): an unknown verb, an unknown or missing argument, a
value that does not match, or a role that may not run it is refused before anything runs. The engine checks everything again.

Python standard library only. Mutations run from #87; this module is what #86's `GET /api/v1/actions` describes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# a player's name: admission.py's rule, minus its RESERVED names (the lighthouse, the control plane, the PoC's own nodes): `kick cp`
# would drain and blocklist the control plane. refresh takes lh and cp on purpose, with its own pattern, and is local only.
NAME = {"type": "string", "pattern": "^(?!(?:lh|cp|n1|n2)$)[a-z][a-z0-9-]{1,19}$"}
LOCAL, BOTH = ("local",), ("local", "console")
CONFIRMS = ("none", "click", "type:name", "type:all")


class Refused(Exception):
    """A call the registry does not allow: the HTTP status and a machine word (400 bad args, 403 role, 404 no such verb)."""

    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


@dataclass(frozen=True)
class Action:
    verb: str
    engine: str                      # "mesh host ..." with {param} and [optional --flag {param}] parts
    params: dict
    required: tuple = ()
    confirm: str = "none"
    roles: tuple = BOTH
    timeout_seconds: int | None = 120
    summary: str = ""
    arg_roles: dict = field(default_factory=dict)   # {param: {value: roles}}: a value that narrows who may run the verb


REGISTRY: dict[str, Action] = {a.verb: a for a in (
    Action("request", "mesh host request {name} [--ip {ip}] [--note {note}]",
           {"name": NAME, "ip": {"type": "string", "maxLength": 45, "pattern": "^[0-9A-Fa-f.:]+$"}, "note": {"type": "string", "maxLength": 200}},
           ("name",), timeout_seconds=60, summary="put someone on the waiting list"),
    Action("reject", "mesh host reject {name} [--reason {reason}]", {"name": NAME, "reason": {"type": "string", "maxLength": 200}},
           ("name",), timeout_seconds=60, summary="take a request off the list"),
    Action("approve", "mesh host approve [{name}] --trust {trust}", {"name": NAME, "trust": {"enum": ["public", "friends"], "default": "public"}},
           confirm="click", timeout_seconds=300, arg_roles={"trust": {"friends": LOCAL}},
           summary="approve the named request or the first eligible one; trust friends (the persistent CA) is local only"),
    Action("tick", "mesh host auto --once", {}, timeout_seconds=600, summary="one tick now, the same as the timer's"),
    Action("auto-on", "mesh host auto on", {}, confirm="click", summary="open automatic admission"),
    Action("auto-off", "mesh host auto off", {}, summary="stop automatic approvals (expiry keeps running)"),
    Action("panic", "mesh host panic", {}, confirm="click", timeout_seconds=None, summary="auto off, slots 0, every active lease expelled"),
    Action("panic-clear", "mesh host panic --clear", {}, confirm="click", summary="reopen admission (auto stays off)"),
    Action("burn", "mesh host burn", {}, summary="burn the join token of every registered player, refresh the node guard"),
    Action("reap", "mesh host reap", {}, timeout_seconds=600, summary="expel every lease that ended"),
    Action("reap-all", "mesh host reap --all", {}, confirm="type:all", timeout_seconds=900, summary="end every lease now"),
    Action("reap-one", "mesh host reap --name {name}", {"name": NAME}, ("name",), "type:name", timeout_seconds=300, summary="end this lease now"),
    Action("kick", "mesh host kick {name}", {"name": NAME}, ("name",), "type:name", timeout_seconds=300,
           summary="token, cordon, drain, revoke, machine, namespace"),
    Action("revoke", "mesh host revoke {name}", {"name": NAME}, ("name",), "type:name", timeout_seconds=300,
           summary="blocklist the certificate everywhere, delete the node"),
    Action("party-reconcile", "mesh host party reconcile", {}, summary="open the parties both sides agreed to, close the rest"),
    Action("party-end", "mesh host party end {a} {b}", {"a": NAME, "b": NAME}, ("a", "b"), "click",
           summary="end a party: both consents and its NetworkPolicies removed"),
    Action("invite", "mesh host invite {name} --ttl {ttl} --trust {trust}",
           {"name": NAME, "ttl": {"type": "string", "pattern": "^([0-9]+[hms]){1,3}$", "default": "2h"},
            "trust": {"enum": ["friends", "public"], "default": "friends"}}, ("name",), roles=LOCAL, timeout_seconds=300,
           summary="a bundle for one player, outside the queue"),
    Action("refresh", "mesh host refresh {name}", {"name": {"type": "string", "pattern": "^(lh|cp|[a-z][a-z0-9-]{1,19})$"}}, ("name",),
           roles=LOCAL, summary="rewrite and apply a machine's Nebula config"),
    Action("bundle", "mesh host bundle {name}", {"name": NAME}, ("name",), roles=LOCAL, timeout_seconds=60,
           summary="pack the approved bundle into the outbox again"),
)}

CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_value(verb: str, key: str, schema: dict, value) -> str:
    if not isinstance(value, str):
        raise Refused(400, "bad-args", f"{verb}: {key} must be a string")
    if "enum" in schema and value not in schema["enum"]:
        raise Refused(400, "bad-args", f"{verb}: {key} must be one of {', '.join(schema['enum'])}")
    if len(value) > schema.get("maxLength", 64) or CONTROL.search(value):
        raise Refused(400, "bad-args", f"{verb}: {key} is too long or has a control character")
    if "pattern" in schema and not re.fullmatch(schema["pattern"].strip("^$"), value):
        raise Refused(400, "bad-args", f"{verb}: {key} does not match {schema['pattern']}")
    if value.startswith("-"):   # never a flag of the engine, whatever a pattern allows
        raise Refused(400, "bad-args", f"{verb}: {key} cannot start with '-'")
    return value


def argv(verb: str, args: dict, role: str) -> list[str]:
    """The argument list after `poc/mesh` for this call, or Refused. Defaults fill missing optional values that have one."""
    a = REGISTRY.get(verb)
    if a is None:
        raise Refused(404, "no-such-verb", "not a verb of the registry")
    if role not in a.roles:
        raise Refused(403, "role", f"{verb} is not open to the {role} role")
    if not isinstance(args, dict):
        raise Refused(400, "bad-args", "args must be an object")
    extra = set(args) - set(a.params)
    if extra:
        raise Refused(400, "bad-args", f"{verb} takes no {', '.join(sorted(extra))}")
    vals = {k: check_value(verb, k, a.params[k], v) for k, v in args.items()}
    for k in a.required:
        if k not in vals:
            raise Refused(400, "bad-args", f"{verb} needs {k}")
    for k, schema in a.params.items():
        if k not in vals and "default" in schema:
            vals[k] = schema["default"]
    for k, narrowed in a.arg_roles.items():
        if role not in narrowed.get(vals.get(k), (role,)):
            raise Refused(403, "role", f"{verb} with {k} {vals[k]} is not open to the {role} role")
    out: list[str] = []
    for part in re.findall(r"\[[^\]]*\]|\S+", a.engine)[1:]:   # without the leading "mesh"
        optional = part.startswith("[")
        words = part.strip("[]").split()
        keys = [w[1:-1] for w in words if w.startswith("{")]
        if optional and not all(k in vals for k in keys):
            continue
        out += [vals[w[1:-1]] if w.startswith("{") else w for w in words]
    return out


def template(a: Action) -> tuple[list[str], list[tuple[str, bool]], dict[str, str]]:
    """An engine string as (literal words, positional {param}s with whether optional, {--flag: param})."""
    parts = re.findall(r"\[[^\]]*\]|\S+", a.engine)[1:]   # without the leading "mesh"
    words: list[str] = []
    for part in parts:
        if part.startswith("["):
            words += ["[", *part.strip("[]").split(), "]"]
        else:
            words.append(part)
    literal: list[str] = []
    positional: list[tuple[str, bool]] = []
    flags: dict[str, str] = {}
    optional, i = False, 0
    while i < len(words):
        w = words[i]
        if w in ("[", "]"):
            optional = w == "["
        elif w.startswith("--") and i + 1 < len(words) and words[i + 1].startswith("{"):
            flags[w] = words[i + 1][1:-1]
            i += 1
        elif w.startswith("{"):
            positional.append((w[1:-1], optional))
        elif not positional and not flags:
            literal.append(w)
        else:
            raise ValueError(f"{a.verb}: a literal word after a parameter")
        i += 1
    return literal, positional, flags


def parse(words: list[str]) -> tuple[str, dict] | None:
    """The inverse of argv(): a `mesh` argument list as (verb, args), or None when it is not exactly a registry call (an unknown flag, a
    missing or extra word, a value the registry refuses for the local role). Defaults are not filled in: argv() adds them again."""
    found = sorted(((template(a), a) for a in REGISTRY.values()), key=lambda t: -len(t[0][0]))
    for (literal, positional, flags), a in found:
        if words[:len(literal)] != literal:
            continue
        args: dict[str, str] = {}
        todo = list(positional)
        rest, i, ok = words[len(literal):], 0, True
        while ok and i < len(rest):
            w = rest[i]
            if w.startswith("--"):
                ok = w in flags and i + 1 < len(rest) and flags[w] not in args
                if ok:
                    args[flags[w]] = rest[i + 1]
                i += 2
            else:
                ok = bool(todo)
                if ok:
                    args[todo.pop(0)[0]] = w
                i += 1
        if not ok:
            continue
        try:
            argv(a.verb, args, "local")   # a missing required name, a bad value: refused here
        except Refused:
            continue
        return a.verb, args
    return None


def describe(role: str) -> list[dict]:
    """GET /api/v1/actions: the entries this role may run (arg_roles shown, so a client can grey out what it may not choose)."""
    return [{"verb": a.verb, "summary": a.summary, "params": a.params, "required": list(a.required), "confirm": a.confirm,
             "timeout_seconds": a.timeout_seconds, "arg_roles": {k: {v: list(r) for v, r in m.items()} for k, m in a.arg_roles.items()}}
            for a in REGISTRY.values() if role in a.roles]


# The host's verbs that are never in the registry (docs/CONSOLE.md, the verb table): shown by GET /verbs so that a client can say where they
# are typed, never run through the API.
HOST_ONLY = (
    ("init", "mesh host init", "create the CA, the lighthouse and the control plane", "the daemon lives in the state this creates"),
    ("down", "mesh host down", "remove everything, the state too", "it would delete the daemon's own socket and audit log"),
)
WHY_LOCAL = "it signs with the persistent CA, or writes a private key or a running machine's configuration: typed on the host only"


def command(a: Action) -> str:
    """The engine string as a person types it: `mesh host kick <name>`."""
    return re.sub(r"\{(\w+)\}", r"<\1>", a.engine)


def closed(a: Action, role: str) -> list[dict]:
    """The argument values of an allowed verb that this role may still not send (approve --trust friends for the web)."""
    if role not in a.roles:
        return []
    return [{"param": k, "value": v, "command": re.sub(r"<" + k + r">", v, command(a)), "why": WHY_LOCAL}
            for k, m in a.arg_roles.items() for v, roles in m.items() if role not in roles]


def verbs(role: str) -> list[dict]:
    """GET /api/v1/verbs: every registry verb, plus init and down, with who may run it. `allowed` is for the caller's role; a client draws what is not
    allowed as disabled, with `why` and the `command` to type on the host. It is a description: the check is argv()'s, at each POST."""
    out = [{"verb": a.verb, "summary": a.summary, "command": command(a), "roles": list(a.roles), "allowed": role in a.roles,
            "why": None if role in a.roles else WHY_LOCAL, "closed_values": closed(a, role)} for a in REGISTRY.values()]
    out += [{"verb": verb, "summary": summary, "command": cmd, "roles": [], "allowed": False, "why": why, "closed_values": []}
            for verb, cmd, summary, why in HOST_ONLY]
    return out
