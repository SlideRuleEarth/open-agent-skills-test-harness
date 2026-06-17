# Makefile for SlideRule skills — build zip archives for import into claude.ai

PYTHON     ?= python3
OUTPUT_DIR ?= exports

# Skill directories (any top-level dir containing a SKILL.md)
SKILLS         := $(patsubst %/SKILL.md,%,$(wildcard */SKILL.md))
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
	$(EXPORT_TARGETS)

help: ## Show this help
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "Per-skill export: make export-<skill>   (e.g. make export-sliderule-api)"

export: ## Export all skills as zips into $(OUTPUT_DIR)/
	$(PYTHON) export.py -o $(OUTPUT_DIR)

# Per-skill export: make export-<skill> (e.g. make export-sliderule-api)
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
			ln -sfn "../../$$s" "$$d/$$s" && echo "  $$d/$$s -> ../../$$s"; \
		done; \
	done

link-global: ## Symlink skills into your per-user agent dirs (~/.claude, ~/.agents, ~/.gemini/...)
	@for d in $(GLOBAL_SKILL_DIRS); do \
		mkdir -p "$$d"; \
		for s in $(SKILLS); do \
			ln -sfn "$(CURDIR)/$$s" "$$d/$$s" && echo "  $$d/$$s -> $(CURDIR)/$$s"; \
		done; \
	done

unlink-project: ## Remove the project-level skill symlinks
	@for d in $(PROJECT_SKILL_DIRS); do \
		for s in $(SKILLS); do rm -f "$$d/$$s"; done; \
		echo "  cleared $$d/"; \
	done

unlink-global: ## Remove the per-user skill symlinks created by link-global
	@for d in $(GLOBAL_SKILL_DIRS); do \
		for s in $(SKILLS); do rm -f "$$d/$$s"; done; \
		echo "  cleared $$d/"; \
	done

relink-project: unlink-project link-project ## Rebuild project symlinks (e.g. after adding a skill)
