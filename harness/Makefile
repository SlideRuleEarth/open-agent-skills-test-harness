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

.PHONY: help install dev selftest lint mutation clean uninstall

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
lint: ## Lint with the pinned Ruff (run `make dev` first)
	@test -x $(VENV)/bin/ruff || { \
	  echo "lint: $(VENV)/bin/ruff is missing — run 'make dev' first (it installs the"; \
	  echo "      exact Ruff version pyproject.toml's required-version pins; a different"; \
	  echo "      build reports different findings for the same code)."; \
	  exit 1; }
	$(VENV)/bin/ruff check .

# Unlike `selftest`, this one NEEDS $(VENV): mutate_mcp copies the tree per mutation and runs
# `$(VENV)/bin/python -m agentskill_evals selftest` inside each copy. Checked up front because
# the alternative is discovering it as a FileNotFoundError once per mutation, which reads as
# 80-odd broken mutations rather than one missing venv.
mutation: ## Re-prove the self-test's arms by mutation (SLOW, ~10 min; run `make dev` first)
	@test -x $(VENV)/bin/python || { \
	  echo "mutation: $(VENV) is missing — run 'make dev' first (mutate_mcp runs the"; \
	  echo "          self-test from $(VENV) inside each mutated copy of the tree)."; \
	  exit 1; }
	$(PYTHON) -u tools/mutate_mcp.py

clean: ## Remove the dev venv, build outputs, and run artifacts
	rm -rf $(VENV) build dist ./*.egg-info artifacts

uninstall: ## Remove the pipx-installed CLI
	-pipx uninstall $(DIST)
