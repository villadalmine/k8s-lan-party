# edge-mesh-lab - shortcuts. The checks CI runs are the ones `make pr-check` runs locally.
#   make pr-check                       # everything CI's verify job runs
#   make pr-check BASE=origin/main PR_BODY=body.md PR_TITLE="docs: x"

PYTHON   ?= python3
BASE     ?= origin/main
PR_BODY  ?=
PR_TITLE ?=

.PHONY: help labels-check test verify-ci pr-check install-hooks uninstall-hooks

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

labels-check: ## labels are code: every label named anywhere exists in .github/labels.yml
	$(PYTHON) scripts/check_labels.py

test: ## unit tests of the gates (tests/)
	$(PYTHON) -m unittest discover -s tests -v

# The single definition of "what CI's verify job runs": pr_check.py reads it from `make -n verify-ci`.
verify-ci: ## what the CI job `verify` runs
	$(PYTHON) scripts/check_labels.py
	$(PYTHON) -m unittest discover -s tests

pr-check: ## the checks CI runs on a PR, locally, one line each (BASE= PR_BODY=draft.md PR_TITLE=)
	@$(PYTHON) scripts/pr_check.py --base $(BASE) $(if $(PR_BODY),--body-file $(PR_BODY)) $(if $(PR_TITLE),--title "$(PR_TITLE)")

install-hooks: ## OPTIONAL: run `make pr-check` before every `git push` (this clone only; undo: make uninstall-hooks)
	git config --local core.hooksPath .githooks
	git config --local pr-check.prepush true
	@echo "OK: git push now runs make pr-check (.githooks/pre-push). Undo: make uninstall-hooks"

uninstall-hooks: ## switch the pre-push check off again
	-git config --local --unset pr-check.prepush
	@echo "OK: git push no longer runs make pr-check"
