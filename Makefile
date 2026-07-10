# Makefile for the example skills — build zip archives for import into claude.ai

PYTHON     ?= python3
OUTPUT_DIR ?= exports

# Skill directories (each example skill lives under skills_examples/<name>/SKILL.md)
SKILLS_DIR     := skills_examples
SKILLS         := $(patsubst $(SKILLS_DIR)/%/SKILL.md,%,$(wildcard $(SKILLS_DIR)/*/SKILL.md))
EXPORT_TARGETS := $(addprefix export-,$(SKILLS))

# --- Skill symlink layout (one source of truth: this repo) -----------------
# Each agent runtime scans a different skills directory. We point all of them
# at the real skill folders here via symlinks. To support a new agent, just
# add its directory to the right list below — the link/unlink targets pick it
# up automatically.
#
# PROJECT_SKILL_DIRS: per-repo, committed to git. Symlinks are RELATIVE so they
# resolve in any clone. Each entry MUST be two levels deep (<platform>/skills).
PROJECT_SKILL_DIRS := \
	.claude/skills \
	.agents/skills \
	.antigravity/skills

# GLOBAL_SKILL_DIRS: per-user install locations. Symlinks are ABSOLUTE (point
# at this checkout) and are NOT committed.
GLOBAL_SKILL_DIRS := \
	$(HOME)/.claude/skills \
	$(HOME)/.agents/skills \
	$(HOME)/.gemini/config/skills \
	$(HOME)/.gemini/antigravity-ide/skills

.PHONY: help export list clean \
	link-project link-global unlink-project unlink-global relink-project \
	agents agents-probe run-scenario dry-run-scenario \
	release release-dry-run \
	$(EXPORT_TARGETS)

# --- Harness shortcuts ------------------------------------------------------
# Thin wrappers around the agentskill-evals CLI (install it first: see
# harness/README.md — `cd harness && make install`). Scenario targets take
# SCENARIO=<file> and pass ARGS="..." through to the CLI.
AGENTSKILL_EVALS ?= agentskill-evals
ARGS ?=

define require_cli
	@command -v $(AGENTSKILL_EVALS) >/dev/null 2>&1 || { \
		echo "error: '$(AGENTSKILL_EVALS)' not found on your PATH."; \
		echo "  install it first:  cd harness && make install   (or make dev)"; \
		exit 1; \
	}
endef

define require_scenario
	@if [ -z "$(SCENARIO)" ]; then \
		echo "error: SCENARIO is not set."; \
		echo "  usage:  make $@ SCENARIO=scenarios/<file>.yaml [ARGS=\"-v --jobs 4\"]"; \
		echo "  available scenarios:"; \
		ls scenarios/*.yaml 2>/dev/null | sed 's/^/    /' || echo "    (none found)"; \
		exit 1; \
	fi
endef

help: ## That's me!
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "# Makefile Help       "
	@printf "\033[37m%-30s\033[0m %s\n" "#-----------------------------------------------------------------------------------------"
	@printf "\033[37m%-30s\033[0m %s\n" "#----target--------------------description------------------------------------------------"
	@grep -E '^[a-zA-Z_-].+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Per-skill export: make export-<skill>   (e.g. make export-sliderule-pipeline-direct-request)"
	@echo
	@echo SKILLS: $(SKILLS)
	@echo OUTPUT_DIR: $(OUTPUT_DIR)
	@echo PROJECT_SKILL_DIRS: $(PROJECT_SKILL_DIRS)
	@echo GLOBAL_SKILL_DIRS: $(GLOBAL_SKILL_DIRS)

export: ## Export all skills as zips into $(OUTPUT_DIR)/
	$(PYTHON) export.py -o $(OUTPUT_DIR)

# Per-skill export: make export-<skill> (e.g. make export-sliderule-pipeline-direct-request)
$(EXPORT_TARGETS): export-%:
	$(PYTHON) export.py -o $(OUTPUT_DIR) $*

list: ## List the discovered skills
	@for s in $(SKILLS); do echo "  $$s"; done

clean: ## Remove the exports/ directory
	rm -rf $(OUTPUT_DIR)

link-project: ## Create committed, relative skill symlinks (.claude/.agents/.antigravity)
	@for d in $(PROJECT_SKILL_DIRS); do \
		mkdir -p "$$d"; \
		for s in $(SKILLS); do \
			ln -sfn "../../$(SKILLS_DIR)/$$s" "$$d/$$s" && echo "  $$d/$$s -> ../../$(SKILLS_DIR)/$$s"; \
		done; \
	done

link-global: ## Symlink skills into your per-user agent dirs (~/.claude, ~/.agents, ~/.gemini/...)
	@for d in $(GLOBAL_SKILL_DIRS); do \
		mkdir -p "$$d"; \
		for s in $(SKILLS); do \
			ln -sfn "$(CURDIR)/$(SKILLS_DIR)/$$s" "$$d/$$s" && echo "  $$d/$$s -> $(CURDIR)/$(SKILLS_DIR)/$$s"; \
		done; \
	done

unlink-project: ## Remove the project-level skill symlinks
	@for d in $(PROJECT_SKILL_DIRS); do \
		for s in $(SKILLS); do rm -f "$$d/$$s"; done; \
		echo "  cleared $$d/"; \
	done

# Removes by symlink TARGET, not by the current $(SKILLS) list, so it also cleans
# stale links left by renamed/removed skills — plus broken links (their checkout
# moved or was deleted). Symlinks resolving anywhere else are untouched.
unlink-global: ## Remove per-user skill symlinks pointing into this checkout (incl. stale/broken)
	@$(PYTHON) -c 'import os, sys; \
repo = os.path.realpath(os.getcwd()); \
dirs = [d for d in sys.argv[1:] if os.path.isdir(d)]; \
links = [os.path.join(d, n) for d in dirs for n in sorted(os.listdir(d)) \
         if os.path.islink(os.path.join(d, n))]; \
stale = [p for p in links if not os.path.exists(os.path.realpath(p)) \
         or os.path.realpath(p) == repo \
         or os.path.realpath(p).startswith(repo + os.sep)]; \
[print(f"  removed {p} -> {os.readlink(p)}") or os.remove(p) for p in stale]; \
print(f"  removed {len(stale)} link(s) into this checkout (or broken) " \
      f"across {len(dirs)} dir(s)")' $(GLOBAL_SKILL_DIRS)

relink-project: unlink-project link-project ## Rebuild project symlinks (e.g. after adding a skill)

agents: ## Show the runners and their configured models (from models.yaml — free)
	$(require_cli)
	$(AGENTSKILL_EVALS) list-agents-configured-models $(ARGS)

agents-probe: ## Probe the installed agent CLIs for available models (has a small cost)
	$(require_cli)
	$(AGENTSKILL_EVALS) list-agents-available-models $(ARGS)

run-scenario: ## Run a scenario: make run-scenario SCENARIO=scenarios/<file>.yaml [ARGS="-v"]
	$(require_cli)
	$(require_scenario)
	$(AGENTSKILL_EVALS) run --config "$(SCENARIO)" $(ARGS)

dry-run-scenario: ## Preview a scenario's scope/cost + skill visibility (no API spend)
	$(require_cli)
	$(require_scenario)
	$(AGENTSKILL_EVALS) run --config "$(SCENARIO)" --dry-run $(ARGS)

# --- Releases ----------------------------------------------------------------
# Tags version the HARNESS (see issue #75): v$(VERSION) must equal the version
# in harness/pyproject.toml. main is protected, so the version bump itself lands
# via a normal PR first; these targets only publish what is already on main.
PYPROJECT_VERSION = $(shell sed -n 's/^version *= *"\(.*\)"/\1/p' harness/pyproject.toml)

define release_checks
	@if [ -z "$(VERSION)" ]; then \
		echo "error: VERSION is not set.  usage:  make $@ VERSION=X.Y.Z"; \
		echo "  harness/pyproject.toml currently says: $(PYPROJECT_VERSION)"; \
		exit 1; \
	fi
	@[ "$$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { \
		echo "error: releases are cut from main (currently on '$$(git rev-parse --abbrev-ref HEAD)')."; exit 1; }
	@[ -z "$$(git status --porcelain)" ] || { \
		echo "error: working tree is not clean:"; git status --short; exit 1; }
	@git fetch -q origin main
	@[ "$$(git rev-parse HEAD)" = "$$(git rev-parse origin/main)" ] || { \
		echo "error: local main is not in sync with origin/main — pull/push first."; exit 1; }
	@[ "$(PYPROJECT_VERSION)" = "$(VERSION)" ] || { \
		echo "error: harness/pyproject.toml says '$(PYPROJECT_VERSION)', not '$(VERSION)'."; \
		echo "  bump it via a normal PR first — tags version the harness (issue #75)."; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null || { \
		echo "error: tag v$(VERSION) already exists locally."; exit 1; }
	@[ -z "$$(git ls-remote --tags origin "v$(VERSION)")" ] || { \
		echo "error: tag v$(VERSION) already exists on origin."; exit 1; }
	@echo "release gate: harness selftest..."
	@cd harness && $(PYTHON) -m agentskill_evals selftest >/dev/null || { \
		echo "error: harness selftest FAILED — not releasing."; exit 1; }
	@echo "release gate: selftest passed."
	@$(MAKE) --no-print-directory export
	@echo "zips to attach:"; ls -1 $(OUTPUT_DIR)/*.zip | sed 's/^/  /'
endef

release: ## Tag v$(VERSION) on main and publish a GitHub Release with the skill zips
	$(release_checks)
	git tag -a "v$(VERSION)" -m "harness v$(VERSION)"
	git push origin "v$(VERSION)"
	gh release create "v$(VERSION)" $(OUTPUT_DIR)/*.zip --generate-notes \
		--title "harness v$(VERSION)"
	@echo "released:  https://github.com/SlideRuleEarth/open-agent-skills-test-harness/releases/tag/v$(VERSION)"

release-dry-run: ## Run every release check and show what would be tagged/uploaded (no tag, no publish)
	$(release_checks)
	@echo "dry run — would: git tag v$(VERSION); push it; gh release create with the zips above."
