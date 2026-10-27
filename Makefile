# edge-mesh-lab - shortcuts. The checks CI runs are the ones `make pr-check` runs locally.
#   make pr-check                       # everything CI's verify job runs
#   make pr-check BASE=origin/main PR_BODY=body.md PR_TITLE="docs: x"

PYTHON   ?= python3
BASE     ?= origin/main
PR_BODY  ?=
PR_TITLE ?=

.PHONY: poc-lighthouse poc-cilium help labels-check test verify-ci pr-check install-hooks uninstall-hooks poc-join poc-host poc-weakness poc-down poc-ca poc-placement poc-kind poc-vm poc-revoke

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

labels-check: ## labels are code: every label named anywhere exists in .github/labels.yml
	$(PYTHON) scripts/check_labels.py

test: ## unit tests of the gates (tests/)
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m unittest discover -s poc -v

# The single definition of "what CI's verify job runs": pr_check.py reads it from `make -n verify-ci`.
verify-ci: ## what the CI job `verify` runs
	$(PYTHON) scripts/check_labels.py
	$(PYTHON) -m unittest discover -s tests
	$(PYTHON) -m unittest discover -s poc

pr-check: ## the checks CI runs on a PR, locally, one line each (BASE= PR_BODY=draft.md PR_TITLE=)
	@$(PYTHON) scripts/pr_check.py --base $(BASE) $(if $(PR_BODY),--body-file $(PR_BODY)) $(if $(PR_TITLE),--title "$(PR_TITLE)")

install-hooks: ## OPTIONAL: run `make pr-check` before every `git push` (this clone only; undo: make uninstall-hooks)
	git config --local core.hooksPath .githooks
	git config --local pr-check.prepush true
	@echo "OK: git push now runs make pr-check (.githooks/pre-push). Undo: make uninstall-hooks"

uninstall-hooks: ## switch the pre-push check off again
	-git config --local --unset pr-check.prepush
	@echo "OK: git push no longer runs make pr-check"

# The proof of concept (poc/README.md): needs Docker. Every step prints PASS or FAIL; the output is the same on every run.
poc-join: ## step 1: a local CA, lighthouse, control plane and two nodes join; a study lab runs on them (about 4 min)
	poc/poc-join.sh

poc-host: ## host your own game: mesh host init, invite, players, kick, down
	poc/poc-host.sh

poc-weakness: ## what one control plane costs: flapping, tolerance, lossy link, control plane down
	poc/poc-weakness.sh

poc-down: ## remove every PoC container, network and the state
	poc/down.sh

poc-cilium: ## step 2: Cilium (tunnel + WireGuard + mutual auth) over Nebula; needs Docker on a Linux kernel with BPF (WSL2, Linux)
	poc/poc-cilium.sh

poc-ca: ## phase B3: two CAs (persistent and pool, the pool constrained to group public); pulling the pool CA cuts only the pool
	poc/poc-ca.sh

poc-placement: ## phase C, the golden rule: Kyverno trust-based placement; a private pod on someone else's node is rejected
	poc/poc-placement.sh

poc-kind: ## step 2 on kind (no Nebula): Cilium + WireGuard + hardened SPIRE; needs kind, helm, kubectl (WSL2, Linux)
	poc/kind-cilium.sh

poc-vm: ## step 2 on Incus VMs (Linux host with Incus): the same checks with distro kernels; vm/run-all.sh runs step 1, the CAs and placement
	poc/vm/poc-vm.sh

poc-lighthouse: ## a lighthouse on a separate public server (a VM plays it): prepare/sign/deploy idempotent, a player joins through it, revoke reaches it; needs Incus
	poc/poc-lighthouse.sh

poc-revoke: ## issue #7: revocation in one command (mesh host revoke): idempotent, the negative test, the time to loss of access
	poc/poc-revoke.sh
