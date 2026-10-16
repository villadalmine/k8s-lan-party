#!/usr/bin/env python3
"""Run, on your machine, the checks CI runs on a pull request.

    make pr-check
    make pr-check BASE=origin/main PR_BODY=body.md PR_TITLE="docs: x"

Adapted from study-cybercirujas (scripts/pr_check.py). One line per check (OK / FAIL / WARN), a
summary, exit 1 if anything FAILed. Nothing is duplicated here: the content checks are read from
`make -n verify-ci`, so this stays equal to CI by construction. Everything that needs the network
stays in CI (listed at the end).
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The key shapes CI refuses (verify.yml, step "Secrets").
SECRET = (r"\b(sk-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{32,}|AIza[A-Za-z0-9_-]{30,}|"
          r"gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
CI_ONLY = ("where-it-ran: the cited run (needs the pushed head)",)

results: list[str] = []


def run(cmd, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return p.returncode, (p.stdout + p.stderr).strip()


def report(status: str, name: str, secs: float = 0.0, detail: str = "") -> None:
    results.append(status)
    print(f"{status:<4}  {name}" + (f"  ({secs:.1f}s)" if secs else ""), flush=True)
    if detail and status != "OK":
        for line in detail.splitlines()[-12:]:
            print(f"        {line}")


def check(name: str, cmd) -> None:
    start = time.time()
    code, out = run(cmd)
    report("OK" if code == 0 else "FAIL", name, time.time() - start, out)


def verify_ci_commands() -> list[str]:
    """The recipe of `make verify-ci`, one command per entry (the single definition CI uses)."""
    code, out = run(["make", "--no-print-directory", "-n", "verify-ci"])
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def label(cmd: str) -> str:
    parts = shlex.split(cmd)
    if "unittest" in parts:
        return "unit tests (tests/)"
    return " ".join([Path(parts[1]).name] + parts[2:]) if len(parts) > 1 else cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--body-file", help="your draft PR description (checked with check_pr.py)")
    ap.add_argument("--title", help="your PR title (default: the last commit subject)")
    args = ap.parse_args()
    t0 = time.time()

    # 1. CRLF in a tracked text file changes every hash a check relies on.
    _, eol = run(["git", "ls-files", "--eol"])
    crlf = [line.partition("\t")[2] for line in eol.splitlines()
            if "i/crlf" in line.partition("\t")[0] or "w/crlf" in line.partition("\t")[0]]
    if crlf:
        report("WARN", f"line endings: {len(crlf)} tracked text file(s) have CRLF "
               "(git config core.autocrlf false, then re-checkout)", 0, "\n".join(crlf[:5]))
    else:
        report("OK", "line endings (no CRLF in tracked text files)")

    # 2. CI job `verify`: the content checks and the unit tests, command by command.
    cmds = verify_ci_commands()
    if not cmds:
        report("FAIL", "could not read `make -n verify-ci`")
    for cmd in cmds:
        check(label(cmd), cmd.replace("python3", shlex.quote(sys.executable), 1) if cmd.startswith("python3") else cmd)

    # 3. CI step "Secrets".
    start = time.time()
    code, out = run(f"git grep -nE {shlex.quote(SECRET)} -- . ':!scripts/pr_check.py' ':!.github/workflows/verify.yml'")
    report("FAIL" if code == 0 else "OK", "secrets (key-shaped strings)", time.time() - start, out)

    # 4. CI job `pr-format`: the description (needs a title and a body), and DCO on every commit.
    title = args.title or run(["git", "log", "-1", "--format=%s"])[1]
    cmd = [sys.executable, "scripts/check_pr.py", "--base", args.base, "--title", title]
    if args.body_file:
        check("PR description and DCO (check_pr.py)", cmd + ["--body-file", args.body_file])
    else:
        # No draft body: only the commits' DCO can be checked.
        start = time.time()
        sys.path.insert(0, str(REPO / "scripts"))
        import check_pr  # noqa: WPS433
        problems = check_pr.dco(check_pr.commits_of(args.base))
        report("FAIL" if problems else "OK", "DCO sign-off on every commit of the branch",
               time.time() - start, "\n".join(problems))
        report("WARN", "PR description not checked: pass PR_BODY=draft.md PR_TITLE=\"...\"")

    bad = results.count("FAIL")
    print(f"\n{results.count('OK')} OK, {results.count('WARN')} WARN, {bad} FAIL in {time.time() - t0:.0f}s")
    print("CI only: " + "; ".join(CI_ONLY))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
