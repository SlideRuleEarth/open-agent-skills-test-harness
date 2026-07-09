# Copilot Instructions

## What this repo is

This repository is a cross-agent **test harness for evaluating Agent Skills** across models and runtimes — the `harness/` directory (CLI: `agentskill-evals`) is the durable purpose of the repo. It also ships a small set of **example skills** (each a directory under `skills_examples/` containing a `SKILL.md`, consumed by Claude Code, Codex, AntiGravity (`agy`), and other agent runtimes) used to exercise and demonstrate the harness. The example skills currently target SlideRule Earth (NASA ICESat-2/GEDI) and are slated to be replaced with neutral examples — they are illustrative, not the reason the repo exists.

## Example skills

Each skill directory contains:
- `SKILL.md` — the skill manifest (YAML front matter + prose instructions read by the agent)
- `evals/` — per-skill eval specs (YAML)
- `scripts/` — optional Python helpers invoked at runtime by the skill
- `requirements.txt` — dependencies for the scripts (if present)

Current example skills (under `skills_examples/`): `sliderule-pipeline`, `sliderule-region-picker`. These are **examples** — the repo's purpose is the harness, not this particular skill set.

Example skills live in `skills_examples/<name>/` (each with a `SKILL.md`). The `Makefile` auto-discovers them via `$(wildcard skills_examples/*/SKILL.md)`, and the harness treats `skills_examples/` as its skills root — a `--skills-root` pointed at the repo root (`.` or `..`) auto-descends into `skills_examples/`.

## Skill install / symlink management

```bash
make link-project    # create committed relative symlinks in .claude/skills, .agents/skills, .antigravity/skills
make link-global     # create absolute per-user symlinks in ~/.claude/skills, ~/.agents/skills, ~/.gemini/...
make relink-project  # rebuild after adding or removing a skill
make unlink-project  # remove project symlinks
make unlink-global   # remove per-user symlinks
```

Adding a new surface (agent runtime): add its skills directory to `PROJECT_SKILL_DIRS` and/or `GLOBAL_SKILL_DIRS` in the `Makefile`, then re-run the appropriate link target.

## Zip exports (for hosted agents like Claude.ai)

```bash
make export                   # build all skills as zips into exports/
make export-sliderule-pipeline  # build a single skill
python export.py -h           # full options
make clean                    # remove exports/
```

## Eval harness (`agentskill-evals`)

### Install (from `harness/`)

```bash
make install    # installs via pipx — recommended
make dev        # local .venv editable install for harness development
```

Requires Python ≥ 3.10.

### Self-test (no agent CLIs needed)

```bash
python3 -m agentskill_evals selftest -v
```

### Running evals

All `run` invocations **require** `--skill`, `--evals`, or `--config` — no unscoped broad runs. By default only the cheapest model per runner is used; `--all-models` runs the full grid.

```bash
# what's installed and available?
agentskill-evals list-agents-configured-models --skills-root .
agentskill-evals list-evals  --skills-root .

# run one skill's evals on one agent (cheapest model)
agentskill-evals run --agent claude --skill sliderule-pipeline

# run a single eval file, skip LLM judge
agentskill-evals run --agent claude --evals skills_examples/sliderule-pipeline/evals/01-single-script-consolidation.yaml --no-judge

# preview scope and cost without spending anything
agentskill-evals run --agent copilot --skill sliderule-region-picker --all-models --dry-run

# run a combination scenario
agentskill-evals run --config scenarios/example_full_schema.yaml
```

Run artifacts land in `artifacts/<run_id>/`.

## `models.yaml` — single source of truth for model IDs

**Never hardcode model IDs anywhere in the harness code.** All models are declared in `models.yaml` at the repo root, grouped per runner. Edit only this file to add/retire models or change the cheap default. After editing, validate with `agentskill-evals list-agents-configured-models`.

## Eval file conventions

- Evals live in `<skill>/evals/` and are auto-discovered.
- File naming: `<NN>-<slug>.yaml` (e.g. `01-request-envelope-construction.yaml`).
- `{skill}` in a prompt renders as `/skill-name` (Claude/AntiGravity) or `$skill-name` (Codex); `{skills}` expands to all provisioned skills.
- `rubric:` at the top level auto-compiles to an `llm_judge` assertion.
- Every eval run is isolated by default — the model sees only the declared skills.

## Scenario conventions

Scenarios test *combinations* of skills and live in `scenarios/`. They are **not** auto-discovered; run by explicit path. File naming: `<what>_on_<runner>-<model>.yaml`. A scenario is an eval spec with an added `target: {runner, model}` block.

## Test coverage axis: models, not surfaces

Each `run` invocation targets one runner via `--agent`. Multi-vendor runners (Copilot, AntiGravity) list all models they support in `models.yaml`; overlap with other runners is expected. Copilot uses `.agents/skills/` for project-level skill discovery (the cross-agent convention shared with Codex), so existing `make link-project` symlinks work without changes.
