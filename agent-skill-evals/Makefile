# Makefile for agent-skill-evals — install and test the eval harness.
#
# Two audiences, two targets:
#   make install   end users  -> isolated CLI on your PATH (pipx)
#   make dev       contributors -> local .venv with an editable install
# Run these from inside agent-skill-evals/.

PYTHON ?= python3
VENV   ?= .venv

.PHONY: help install dev selftest clean uninstall

help: ## That's me!
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "# agent-skill-evals — install & test"
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "#----target--------------------description------------------------------------------------"
	@grep -E '^[a-zA-Z_-].+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo

install: ## End users: install the CLI in an isolated env, on your PATH (pipx)
	pipx install .
	@echo "installed — try:  agentskill-evals list-agents --skills-root .."

dev: ## Contributors: create $(VENV)/ and editable-install with extras
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[schema]"
	@echo "activate with:  . $(VENV)/bin/activate"

selftest: ## Run the parser self-test (no agent CLIs or third-party deps required)
	$(PYTHON) -m agentskill_evals selftest

clean: ## Remove the dev venv, build outputs, and run artifacts
	rm -rf $(VENV) build dist ./*.egg-info artifacts

uninstall: ## Remove the pipx-installed CLI
	-pipx uninstall agentskill-evals
