# Makefile for SlideRule skills — build zip archives for import into claude.ai

PYTHON     ?= python3
OUTPUT_DIR ?= exports

# Skill directories (any dir containing a SKILL.md)
SKILLS := $(patsubst %/SKILL.md,%,$(wildcard */SKILL.md))

.PHONY: help export export-% list clean

help: ## Show this help
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "Per-skill export: make export-<skill>   (e.g. make export-sliderule-api)"

export: ## Export all skills as zips into $(OUTPUT_DIR)/
	$(PYTHON) export.py -o $(OUTPUT_DIR)

export-%: ## Export a single skill by name (e.g. export-sliderule-api)
	$(PYTHON) export.py -o $(OUTPUT_DIR) $*

list: ## List the discovered skills
	@for s in $(SKILLS); do echo "  $$s"; done

clean: ## Remove the exports/ directory
	rm -rf $(OUTPUT_DIR)
