#!/usr/bin/env python3
"""The feature catalog (docs/FEATURES.yml, issue #62): what the project does, which suite steps prove it, what it touches.

    scripts/features.py validate            every Makefile suite is in the catalog, every feature names suite steps that exist
    scripts/features.py suites              the suites in the order `make regression` runs them: id, runtimes, script, on_game_of
    scripts/features.py table --runs DIR --commit SHA --date YYYY-MM-DD [--env TEXT] [--write docs/POC-STATUS.md]
                                            the feature x result table of one `make regression` run (poc/regression.sh)

No network, no clock: the same catalog, scripts and run outputs give the same table, byte for byte.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "docs" / "FEATURES.yml"
RUNTIMES = ("docker", "incus", "kind", "none")
# `step <id> "..."` in every suite; `check <rule> <id>` and `depends <rule> <id>` in poc-golden.sh
STEP_DEF = re.compile(r"^\s*(?:step\s+([A-Za-z]+[0-9][0-9A-Za-z]*)|(?:check|depends)\s+\d+\s+(G[0-9]+\.[0-9]+))\s+\"")
STEP_OUT = re.compile(r"^(PASS|FAIL|SKIP|DEP)\s+(\S+)\s")
BEGIN, END = "<!-- regression:begin", "<!-- regression:end -->"


def load(path: Path = CATALOG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def makefile_suites(path: Path = ROOT / "Makefile") -> dict[str, str]:
    """`poc-<x>:` target -> the script its recipe runs."""
    out, target = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(poc-[a-z0-9-]+):", line)
        if m:
            target = m.group(1)
        elif target and line.startswith("\t"):
            out[target] = line.split()[0]
            target = None
    return out


def script_steps(script: str) -> list[str]:
    """The step ids a suite defines, in the order of the source."""
    ids = []
    for line in (ROOT / script).read_text(encoding="utf-8").splitlines():
        m = STEP_DEF.match(line)
        if m and (m.group(1) or m.group(2)) not in ids:
            ids.append(m.group(1) or m.group(2))
    return ids


def expand(specs: list[str], ordered: list[str]) -> list[str]:
    """['J01-J08', 'J13'] -> the ids between both ends, in source order. ValueError on an id the suite does not define."""
    out = []
    for spec in specs:
        first, _, last = str(spec).partition("-")
        last = last or first
        for end in (first, last):
            if end not in ordered:
                raise ValueError(f"step {end} is not defined by the suite")
        i, j = ordered.index(first), ordered.index(last)
        if j < i:
            raise ValueError(f"range {spec} runs backwards")
        out += [s for s in ordered[i:j + 1] if s not in out]
    return out


def tracked() -> list[str]:
    res = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return res.stdout.splitlines()


def validate(cat: dict, make: dict[str, str] | None = None, files: list[str] | None = None) -> list[str]:
    """Every problem of the catalog against the Makefile, the suites' sources and the tracked files."""
    make = makefile_suites() if make is None else make
    files = tracked() if files is None else files
    problems, suites = [], {}
    for s in cat.get("suites") or []:
        sid = s.get("id")
        if sid in suites:
            problems.append(f"suite {sid}: listed twice")
        suites[sid] = s
        if sid not in make:
            problems.append(f"suite {sid}: no such target in the Makefile")
        elif make[sid] != s.get("script"):
            problems.append(f"suite {sid}: the Makefile runs {make[sid]}, the catalog says {s.get('script')}")
        bad = [r for r in s.get("runtime") or [] if r not in RUNTIMES]
        if not s.get("runtime") or bad:
            problems.append(f"suite {sid}: runtime must be a list of {', '.join(RUNTIMES)}")
    problems += [f"suite {t}: in the Makefile but not in docs/FEATURES.yml" for t in make if t not in suites]
    cited, seen = set(), set()
    for f in cat.get("features") or []:
        fid = f.get("id") or "?"
        if fid in seen:
            problems.append(f"feature {fid}: listed twice")
        seen.add(fid)
        for key in ("description", "proven_by", "paths", "docs"):
            if not f.get(key):
                problems.append(f"feature {fid}: '{key}' is empty")
        for proof in f.get("proven_by") or []:
            sid = proof.get("suite")
            if sid not in suites:
                problems.append(f"feature {fid}: suite {sid} is not in the catalog")
                continue
            cited.add(sid)
            if not proof.get("steps"):
                problems.append(f"feature {fid}: {sid} names no step")
                continue
            try:
                expand(proof["steps"], script_steps(suites[sid]["script"]))
            except ValueError as exc:
                problems.append(f"feature {fid}: {sid} {exc}")
        for glob in f.get("paths") or []:
            if not any(fnmatch.fnmatch(p, glob) for p in files):
                problems.append(f"feature {fid}: path {glob} matches no tracked file")
        for doc in (f.get("docs") or []) + (f.get("diagrams") or []):
            if doc not in files:
                problems.append(f"feature {fid}: {doc} is not a tracked file")
    for sid, s in suites.items():
        if sid not in cited and s.get("role") != "teardown":
            problems.append(f"suite {sid}: no feature names one of its steps")
    return problems


# --- one run of `make regression` -> the table ------------------------------------------------------------------

def read_run(runs: Path, sid: str) -> tuple[str, str, dict[str, str], str]:
    """(runtime, status, step -> PASS|FAIL|SKIP|DEP, RESULT line) of one suite; status is RAN, TIMEOUT, SKIP(<why>) or NOT RUN."""
    st = runs / f"{sid}.status"
    if not st.is_file():
        return "-", "NOT RUN", {}, ""
    runtime, _, status = st.read_text(encoding="utf-8").strip().partition("\t")
    steps, result = {}, ""
    out = runs / f"{sid}.out"
    for line in out.read_text(encoding="utf-8", errors="replace").splitlines() if out.is_file() else []:
        m = STEP_OUT.match(line)
        if m:
            steps.setdefault(m.group(2), m.group(1))
        elif line.startswith("RESULT "):
            result = line.strip()
    return runtime, status, steps, result


def proof_result(status: str, steps: dict[str, str], wanted: list[str]) -> str:
    if status not in ("RAN", "TIMEOUT"):
        return status
    got = [steps.get(s, "FAIL") for s in wanted]   # a step the run never printed did not pass
    if "FAIL" in got:
        return "FAIL"
    return "PASS" if all(g == "PASS" for g in got) else "SKIP"


def feature_result(results: list[str]) -> str:
    if "FAIL" in results:
        return "FAIL"
    if all(r == "PASS" for r in results):
        return "PASS"
    return "PARTIAL" if "PASS" in results else results[0]


def table(cat: dict, runs: Path, commit: str, date: str, env: str = "") -> str:
    suites = {s["id"]: s for s in cat["suites"]}
    runs_of = {sid: read_run(runs, sid) for sid in suites}
    lines = [f"Commit `{commit}`, {date}{', ' + env if env else ''}.", "",
             "| Suite | Runtime | Result | Steps |", "| :--- | :--- | :--- | :--- |"]
    for sid, (runtime, status, steps, result) in runs_of.items():
        if status == "RAN" and suites[sid].get("role") == "teardown":
            lines.append(f"| `{sid}` | {runtime} | done (teardown) | - |")
        elif status in ("RAN", "TIMEOUT"):
            fails = [k for k, v in steps.items() if v == "FAIL"]
            res = (result[len("RESULT "):] if result else "no RESULT line") + (" (timeout)" if status == "TIMEOUT" else "")
            counts = ", ".join(f"{sum(v == k for v in steps.values())} {k}" for k in ("PASS", "FAIL", "SKIP", "DEP")
                               if any(v == k for v in steps.values())) or "none"
            lines.append(f"| `{sid}` | {runtime} | {res} | {counts}{' (' + ', '.join(fails) + ')' if fails else ''} |")
        else:
            lines.append(f"| `{sid}` | {runtime} | {status} | - |")
    lines += ["", "| Feature | Result | Proven by |", "| :--- | :--- | :--- |"]
    for f in cat["features"]:
        parts, results = [], []
        for proof in f["proven_by"]:
            _, status, steps, _ = runs_of[proof["suite"]]
            r = proof_result(status, steps, expand(proof["steps"], script_steps(suites[proof["suite"]]["script"])))
            results.append(r)
            parts.append(f"`{proof['suite']}` {', '.join(map(str, proof['steps']))}: {r}")
        lines.append(f"| `{f['id']}` | {feature_result(results)} | {'; '.join(parts)} |")
    return "\n".join(lines) + "\n"


def write_between(path: Path, text: str) -> None:
    doc = path.read_text(encoding="utf-8")
    i, j = doc.find(BEGIN), doc.find(END)
    if i < 0 or j < i:
        raise SystemExit(f"{path}: no '{BEGIN} ... {END}' markers")
    i = doc.index("\n", i) + 1
    path.write_text(doc[:i] + text + doc[j:], encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("validate", "suites", "table"))
    p.add_argument("--runs", type=Path)
    p.add_argument("--commit", default="")
    p.add_argument("--date", default="")
    p.add_argument("--env", default="")
    p.add_argument("--write", type=Path)
    a = p.parse_args()
    cat = load()
    if a.command == "validate":
        problems = validate(cat)
        print("\n".join(problems) or f"docs/FEATURES.yml OK: {len(cat['suites'])} suites, {len(cat['features'])} features")
        return 1 if problems else 0
    if a.command == "suites":
        for s in cat["suites"]:
            print("\t".join([s["id"], ",".join(s["runtime"]), s["script"], s.get("on_game_of", "-"), s.get("role", "-")]))
        return 0
    text = table(cat, a.runs, a.commit, a.date, a.env)
    if a.write:
        write_between(a.write, text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
