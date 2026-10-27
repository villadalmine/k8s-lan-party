# Security policy

This project is an MVP (phase 1) and handles identities and join credentials, so security reports are welcome and taken seriously.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Use GitHub's private reporting: the **Security** tab of this repository,
then **Report a vulnerability** (a private advisory only the maintainer sees). Include what you found, how to reproduce it, and
what it lets an attacker do. If the Security tab is not available, open an issue that says only "security contact please"
and nothing else, and the maintainer will reach out.

You can expect a first answer within 3 days and a decision on severity and a fix plan within 7.

## What is in scope

The join flow (bundles, join tokens and their burning, Nebula certificates), the blocklist and revocation, the placement policy (trust levels,
Kyverno), the PoC scripts, and the CI gates.

## What is a known, accepted limit (not a vulnerability)

**What runs on someone else's machine can be seen by its owner.** No encryption protects a pod from the owner of the node it runs
on. That is the [golden rule](docs/GOLDEN-RULES.md); the defence is placement by trust, not hiding. A report that shows the
placement being bypassed is in scope.

## Supported versions

Only the latest commit on `main` while the project is pre-1.0.
