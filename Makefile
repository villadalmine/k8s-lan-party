# k8s-lan-party - shortcuts. The checks CI runs are the ones `make pr-check` runs locally.
#   make pr-check                       # everything CI's verify job runs
#   make pr-check BASE=origin/main PR_BODY=body.md PR_TITLE="docs: x"

PYTHON   ?= python3
BASE     ?= origin/main
PR_BODY  ?=
PR_TITLE ?=
ONLY     ?=
NAME     ?=

.PHONY: regression features-check diagrams poc-lighthouse poc-cilium help labels-check test verify-ci pr-check install-hooks uninstall-hooks poc-join poc-host poc-weakness poc-down poc-ca poc-placement poc-kind poc-vm poc-revoke poc-admission poc-admission-auto poc-participants poc-participants-e2e poc-nat poc-bgp poc-golden poc-pool poc-party poc-page showcase showcase-build

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

features-check: ## docs/FEATURES.yml against the Makefile and the suites: every suite listed, every feature proven by steps that exist
	$(PYTHON) scripts/features.py validate

diagrams: ## the feature graph from docs/FEATURES.yml, then render docs/diagrams (NAME=x for one) with mermaid-cli, each SVG and PNG stamped with its source's hash
	$(PYTHON) scripts/features.py graph --write
	$(PYTHON) scripts/diagrams.py render $(NAME)

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

poc-bgp: ## issue #13: the gateway (Envoy Gateway, cert-manager TLS) and BGP only on own nodes, a test FRR router; remote nodes never announce; needs Incus
	poc/vm/poc-bgp.sh

poc-lighthouse: ## a lighthouse on a separate public server (a VM plays it): prepare/sign/deploy idempotent, a player joins through it, revoke reaches it; needs Incus
	poc/poc-lighthouse.sh

poc-revoke: ## issue #7: revocation in one command (mesh host revoke): idempotent, the negative test, the time to loss of access
	poc/poc-revoke.sh

poc-admission: ## admission with manual approval (docs/ADMISSION.md): request, approve, join, the lease ends, reap expels, the next one enters; needs Incus
	poc/poc-admission.sh

poc-admission-auto: ## the automatic pool (phase B1, docs/ADMISSION.md): one tick approves, expels at lease end and promotes the next; switch, panic, daily cap, notify hook; needs Incus
	poc/poc-admission-auto.sh

poc-pool: ## issues #9 and #11: a namespace and a kubeconfig per player (RBAC, quota, the error matrix rows 6-9 and 11), the idle kick, the lease end removes node, namespace and token; needs Incus
	poc/poc-pool.sh

poc-party: ## issue #75 part A: LAN party mode - two players who both agreed reach each other's namespace on the party's ports only; leave, off, end, kick and lease end close it; needs Incus
	poc/poc-party.sh

poc-participants: ## join by signed PR (issue #12), offline: cosign against a LOCAL TEST CA, the PR gate, issuing, encrypted delivery, removal; needs python3 with pyyaml and cryptography
	poc/poc-participants.sh

poc-participants-e2e: ## join by signed PR end to end: the participant's own keys, sync issues, open + join, removal revokes access; needs Incus
	poc/poc-participants-e2e.sh
poc-golden: ## issue #23: one check per golden rule against the game this machine hosts (yours or a stranger's); GOLDEN_ENV="<column>" writes the table in docs/POC-STATUS.md
	poc/poc-golden.sh
poc-nat: ## issue #15: symmetric NAT on both sides falls back to a Nebula relay on the lighthouse; direct and relayed paths measured; needs Incus
	poc/poc-nat.sh

poc-page: ## the public page (phase B2, docs/PUBLIC-PAGE.md): a visitor on another machine asks, waits, collects a sealed bundle, joins, is expelled; panic closes it; needs Incus (PUB_SSH/VIS_SSH: real machines)
	poc/poc-page.sh

# Issue #63: What's new on the public page. The recordings come from a real run only, through the filter; never edit a .cast by hand.
RELEASE ?= v0.2.0-poc
showcase: ## What's new: record a real poc-page run (needs Incus) as the player's and the host's screens, filter them, build RELEASE's .cast files
	rm -rf poc/.showcase && SHOWCASE=$(CURDIR)/poc/.showcase poc/poc-page.sh
	mkdir -p poc/page/whatsnew/$(RELEASE)/rec && cp poc/.showcase/player.tsv poc/.showcase/host.tsv poc/page/whatsnew/$(RELEASE)/rec/
	$(PYTHON) poc/page/whatsnew/showcase.py build poc/page/whatsnew/$(RELEASE)

showcase-build: ## What's new: rebuild every .cast from its committed source (no machines); poc/test_whatsnew.py checks they match
	for d in poc/page/whatsnew/v*/; do $(PYTHON) poc/page/whatsnew/showcase.py build "$${d%/}" || exit 1; done

# Issue #62: every suite of docs/FEATURES.yml, in its order, one at a time; the feature table goes to docs/POC-STATUS.md (poc/regression.sh).
regression: ## every suite in a fixed order, each with its own state (Incus: own project, UNDERLAY_NET=10.83.0); writes the feature table in docs/POC-STATUS.md; ONLY="poc-x poc-y" for a subset
	ONLY="$(ONLY)" poc/regression.sh
