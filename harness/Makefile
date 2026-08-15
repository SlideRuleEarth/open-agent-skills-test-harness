# Makefile for harness — install and test the eval harness.
#
# Two audiences, two targets:
#   make install   end users  -> isolated CLI on your PATH (pipx)
#   make dev       contributors -> local .venv with an editable install
# Run these from inside harness/.

PYTHON ?= python3
VENV   ?= .venv
# Distribution / CLI name used with pipx — MUST match [project].name in pyproject.toml.
DIST   ?= agentskill-evals

.PHONY: help install dev selftest lint verify restricted mutation clean uninstall

help: ## That's me!
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "# harness — install & test"
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "#----target--------------------description------------------------------------------------"
	@grep -E '^[a-zA-Z_-].+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo

install: ## End users: install the CLI in an isolated env, on your PATH (needs pipx — see README)
	@command -v pipx >/dev/null 2>&1 || { \
		echo "error: 'pipx' is required for 'make install' but is not on your PATH."; \
		echo "  install it, then re-run 'make install':"; \
		echo "    brew install pipx                     # macOS (Homebrew) — simplest"; \
		echo "    python3 -m pip install --user pipx && python3 -m pipx ensurepath"; \
		echo "  (conda/mambaforge: 'conda install pipx' in base can clash with conda's own deps —"; \
		echo "   use brew/pip above, or a dedicated env: conda create -n pipx -c conda-forge pipx)"; \
		echo "  or skip pipx and use an isolated venv instead:  make dev"; \
		exit 1; \
	}
	@pipx uninstall $(DIST) >/dev/null 2>&1 || true
	pipx install .
	@echo "installed — try:  agentskill-evals list-agents-configured-models --skills-root .."
	@echo "(re-run 'make install' anytime to pick up source changes; or 'make dev' for a live editable install)"

dev: ## Contributors: create $(VENV)/ and editable-install with extras (incl. the pinned Ruff)
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[schema,dev]"
	@echo "activate with:  . $(VENV)/bin/activate"

selftest: ## Run the parser self-test (no agent CLIs or third-party deps required)
	$(PYTHON) -m agentskill_evals selftest

# Runs from $(VENV) on purpose, never a Ruff found on PATH: pyproject pins
# `required-version` to the exact build the `dev` extra installs, so an ambient Ruff would
# either refuse to run or — worse, if it happened to match — quietly become the thing the
# "zero findings" contract depends on. The tree passes at zero, so any output here is a
# regression introduced by the change in front of you, not pre-existing debt.
lint: ## Lint the Python with the pinned Ruff and the shell with the pinned ShellCheck (run `make dev` first)
	@test -x $(VENV)/bin/ruff || { \
	  echo "lint: $(VENV)/bin/ruff is missing — run 'make dev' first (it installs the"; \
	  echo "      exact Ruff version pyproject.toml's required-version pins; a different"; \
	  echo "      build reports different findings for the same code)."; \
	  exit 1; }
	$(VENV)/bin/ruff check .
# FAILS rather than skips when ShellCheck is absent, for the reason the Ruff guard above does:
# `make lint` is an explicit request to lint, and a target that prints success without having
# checked the shell is the same fail-OPEN shape this script spent six review rounds on. (The
# pre-push hook makes the opposite call on purpose — an incidental gate should not block a
# push over a missing optional tool.) `make dev` installs it; there is no system package to
# find, and no ambient shellcheck is used even if one is on PATH.
	@test -x $(VENV)/bin/shellcheck || { \
	  echo "lint: $(VENV)/bin/shellcheck is missing — run 'make dev' first (it installs the"; \
	  echo "      exact ShellCheck the lint contract is written against; a different build"; \
	  echo "      reports different findings for the same script)."; \
	  exit 1; }
	$(VENV)/bin/shellcheck tools/*.sh
# A parse check under every shell present, which ShellCheck does NOT do: it reads the script,
# it does not hand it to `dash` and ask. A comment ending in a backslash would swallow the
# `trap` on the next line under a shell that continued it — silently, in the direction that
# removes a handler.
	@for sh in /bin/sh /bin/dash /bin/zsh /bin/ksh /bin/bash; do \
	  [ -x "$$sh" ] || continue; \
	  for f in tools/*.sh; do \
	    "$$sh" -n "$$f" || { echo "lint: $$f does not parse under $$sh"; exit 1; }; \
	  done; \
	  echo "  parses under $$sh"; \
	done

# The two wire-level drivers. They are not arms and are not in the selftest's count: the
# fixtures and the proxy's I/O half are only reachable by running the real programs over real
# pipes, which is also why `mutate_mcp.py` routes their mutations to these files as the suites
# that must catch them. Each self-reports what it ran, for the reason the selftest's arm count
# does — a total that DROPS means checks were lost, which neither a pass nor a failure reports.
verify: ## Drive the MCP fixtures and the C3 proxy over real pipes (seconds; run `make dev` first)
	@test -x $(VENV)/bin/python || { \
	  echo "verify: $(VENV) is missing — run 'make dev' first."; \
	  exit 1; }
	$(VENV)/bin/python tools/verify_mcp_fixtures.py
	$(VENV)/bin/python tools/verify_mcp_proxy.py

# The one artifact neither the selftest nor the mutation suite can run is a shell script, so its
# failure paths get their own driver: each construction step failing in turn, and every catchable
# terminating signal delivered to the process group under every shell on the machine. Slower than
# `verify` (about a minute) because most of those checks are a process launch, which is why it is
# its own target rather than folded into that one.
restricted: ## Drive tools/restricted_env.sh's FAILURE paths (~1 min; run `make dev` first)
	@test -x $(VENV)/bin/python || { \
	  echo "restricted: $(VENV) is missing — run 'make dev' first."; \
	  exit 1; }
	$(VENV)/bin/python tools/verify_restricted_env.py

# Unlike `selftest`, this one NEEDS $(VENV): mutate_mcp copies the tree per mutation and runs
# `$(VENV)/bin/python -m agentskill_evals selftest` inside each copy. Checked up front because
# the alternative is discovering it as a FileNotFoundError once per mutation, which reads as
# 80-odd broken mutations rather than one missing venv. It now drives THREE suites — the
# selftest, `verify_mcp_fixtures.py` and `verify_mcp_proxy.py` — so `make verify` above is
# covered by this target too, and its three totals are three different claims.
mutation: ## Re-prove every suite's checks by mutation (SLOW; see TODO_Contained_HOME.md §4)
	@test -x $(VENV)/bin/python || { \
	  echo "mutation: $(VENV) is missing — run 'make dev' first (mutate_mcp runs the"; \
	  echo "          self-test from $(VENV) inside each mutated copy of the tree)."; \
	  exit 1; }
	$(PYTHON) -u tools/mutate_mcp.py

clean: ## Remove the dev venv, build outputs, and run artifacts
	rm -rf $(VENV) build dist ./*.egg-info artifacts

uninstall: ## Remove the pipx-installed CLI
	-pipx uninstall $(DIST)
