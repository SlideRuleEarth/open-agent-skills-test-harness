# agent-skill-evals

A cross-agent harness for testing **Agent Skills** against multiple coding-agent
CLIs. Write one eval; run it against **Claude Code**, **Codex**, and
**AntiGravity** (and any other CLI you add an adapter for). Grade with
deterministic checks (filesystem, tool-call trace, output schema) and/or an
**LLM judge**.

This is the runner that Anthropic's
[Agent Skills best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
doc says doesn't exist yet ("There is not currently a built-in way to run these
evaluations").

```
                          ┌──────────────┐
   per-skill evals  ──►   │   runner     │   ──►  artifacts/ + pass/fail matrix
   <skill>/evals/*.yaml   │  (matrix)    │
                          └──────┬───────┘
                                 │  normalized events + RunResult
        ┌────────────┬──────────┼──────────────┐
     claude        codex      antigravity     (your adapter)
   adapter        adapter      adapter
        │  build argv + parse that CLI's output into one common shape
        ▼
   the agent CLI runs in a hermetic workspace with the skill provisioned
```

## Why a normalized layer

Each agent CLI speaks a different dialect of "structured output":

| Agent | Invocation | Output |
|-------|-----------|--------|
| Claude Code | `claude -p … --output-format stream-json --verbose` | JSONL: `system`/`assistant`/`user`/`result`; tool calls in `tool_use` blocks; `--json-schema` → `structured_output` on the result event |
| Codex | `codex exec --json --full-auto` | JSONL: `item.started`/`item.completed` with `item.type` = `command_execution`/`file_change`/`agent_message` |
| AntiGravity | `agy -p "<prompt>" --dangerously-skip-permissions` | plain text by default (`agy` 1.0.9 has no `--output-format`) → parsed defensively: JSONL → single JSON → raw text |

Every adapter maps its CLI's events onto one [`NormalizedEvent`](agentskill_evals/schema.py)
stream and a `RunResult`. Assertions, the judge, and reports only ever see that
common shape — so adding an agent is one small adapter, not a rewrite.

## Install

The harness is a normal Python package (`pyproject.toml`, entry point
`agentskill-evals`). **Install it in an isolated environment** — don't `pip install`
into system Python: modern macOS/Linux block that under PEP 668, and it risks
dependency clashes with other tools. Pick the path that matches what you're doing:

**Users — run the evals** (isolated CLI, on your PATH):

```bash
pipx install ./agent-skill-evals          # from a clone
# or straight from git — note the subdirectory (the package isn't at the repo root):
pipx install "git+https://github.com/SlideRuleEarth/sliderule-skills.git#subdirectory=agent-skill-evals"

uv tool install ./agent-skill-evals       # uv users (also manages the Python version)
```

This puts `agentskill-evals` on your PATH with `pyyaml` already pulled in. From the
repo, `make install` (run in `agent-skill-evals/`) does the pipx install for you.

**Contributors — edit the harness** (isolated venv + editable install):

```bash
cd agent-skill-evals
make dev                                  # creates .venv and installs -e ".[schema]"
# or by hand:
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[schema]"
```

The `[schema]` extra adds `jsonschema` for stricter `output_schema` checks (a
built-in fallback works without it).

`selftest` and `--help` need no dependencies; any command that reads YAML evals or
`models.yaml` (`run`, `list-evals`, and full `list-agents` output) needs `pyyaml`,
which every install above provides. To poke at it without installing, run from
inside `agent-skill-evals/`: `python3 -m agentskill_evals selftest`.

Install whichever agent CLIs you want to test (`claude`, `codex`, `agy`).
Missing ones are simply marked `ERR` / skipped — `list-agents` shows what's
available.

## Eval format

Evals are **per-skill**: each skill directory owns an `evals/` folder.

```
sliderule-docsearch/
  SKILL.md
  evals/
    identifier-disambiguation.yaml
    cross-skill-boundary-schema.yaml
```

A spec (YAML or JSON):

```yaml
name: scaffold-readme
description: The agent should create a README with a title, summary, and Usage section.
skills: [scaffold-readme]        # provisioned into each agent's workspace
prompt: |
  Scaffold a README for a project called "Acme Widgets" using the {skill} skill.
tags: [scaffolding]
timeout_sec: 300

# Deterministic checks (no model). All must pass.
assertions:
  - {type: file_exists, path: README.md, matches: "(?s)^#\\s.+##\\s*Usage"}
  - {type: file_absent, path: package.json}

# Behaviors graded by an LLM judge — one verdict per item.
rubric:
  - The README has a top-level title naming the project "Acme Widgets".
  - The README has a Usage section containing a fenced code block.

# Optional: force/validate the agent's final structured answer.
output_schema: null
```

`{skill}` in a prompt is rendered per-adapter (`/scaffold-readme` for Claude/
AntiGravity, `$scaffold-readme` for Codex). `{skills}` expands to all of
them. `skills:` are copied/symlinked into each agent's project-local skills dir
(`.claude/skills` for Claude, `.agents/skills` for Codex, `.antigravity/skills`
for AntiGravity) so the run is hermetic.

### Fields

| field | meaning |
|-------|---------|
| `name` | eval id (defaults to filename) |
| `description` | what correct behavior looks like (given to the judge) |
| `prompt` | the user message (legacy `query` also accepted) |
| `skills` | skills provisioned into the workspace (legacy `skill` accepted) |
| `files` | files seeded into the workspace (paths relative to the eval file) |
| `fixture` | a directory copied in as the starting workspace |
| `agents` | restrict to specific agents (default: all selected on the CLI) |
| `timeout_sec` | per-cell timeout (default 600) |
| `tags` | filter with `run --tag` |
| `vars` | `{placeholder}` substitutions into the prompt |
| `env` | extra env vars for the agent process |
| `assertions` | deterministic checks (below) |
| `rubric` | behaviors graded by the LLM judge (legacy `expected_behavior` accepted) |
| `output_schema` | JSON Schema for the final structured answer |

### Assertion types

| type | checks |
|------|--------|
| `file_exists` | `path` exists; optional `contains` / `matches` (regex) / `min_size` |
| `file_absent` / `dir_exists` | workspace structure |
| `ran_command` | a shell command matched `contains` / `matches` / `equals` (from the tool trace) |
| `used_tool` | a tool named `name` was invoked |
| `tool_count` | number of tool calls within `min`/`max` |
| `exit_code` | process exit code `equals` (default 0) |
| `no_error` | clean run (no crash/timeout) |
| `final_contains` | final answer matched `contains` / `matches` |
| `output_matches_schema` | structured output validates against `output_schema` (or inline `schema`) |
| `llm_judge` | `rubric` items graded by the judge; `threshold` = fraction that must pass (default 1.0) |

`rubric:` at the top level is auto-compiled into one `llm_judge` assertion, and
`output_schema:` into one `output_matches_schema` assertion.

## Usage

Examples use the installed `agentskill-evals` CLI (see [Install](#install)). From a
source checkout without installing, run them as `python3 -m agentskill_evals …` from
inside `agent-skill-evals/`, adding `--skills-root ..` so the harness finds the sibling
skill evals and the repo-root `models.yaml` (the default skills-root is the current
directory).

```bash
# from the skills repo root (each <skill>/evals/ is discovered automatically)

# what's installed?
agentskill-evals list-agents

# what evals exist?
agentskill-evals list-evals --skills-root .

# run everything on every installed agent (judge defaults to claude)
agentskill-evals run --skills-root .

# one skill, specific agents, parallel, verbose failures
agentskill-evals run --skill sliderule-docsearch \
    --agents claude codex antigravity --jobs 4 -v

# a single eval file, no judge, just deterministic checks
agentskill-evals run --evals path/to/eval.yaml --no-judge

# grade with a different judge / model
agentskill-evals run --skill foo --judge-agent codex
agentskill-evals run --skill foo --model claude=claude-haiku-4-5
```

Output: a pass/fail matrix on stdout, plus per-run artifacts under
`artifacts/<run_id>/`:

```
artifacts/<run_id>/
  summary.json              # machine-readable matrix (per-cell `model`; top-level `targets`)
  summary.md               # rendered table
  <runner>/<model>/<skill>/<eval>/    # <model> is `_default` when no model is set
    stdout.jsonl           # raw agent output
    stderr.txt
    events.json            # normalized event stream
    result.json            # RunResult (final text, commands, cost, …)
    assertions.json        # per-check verdicts (incl. judge per-rubric reasons)
    workspace/             # the hermetic working dir after the run
```

`run` exits non-zero if any cell fails — drop it straight into CI.

## Cross-model testing

The same skill can behave very differently on different **models**, so model is a
first-class axis: a run is a matrix of `evals × (runner, model)`. A "runner" is the
harness used to reach a model (Claude Code, Codex, AntiGravity); the models live in
**`models.yaml`** at the repo root — the single source of truth (no model ids are
hardcoded in the harness).

> ⚠️ **Cost.** Every cell is a full agent run **plus** a judge call, and the axes
> multiply (`evals × runners × models`). The whole suite across every model is
> currently ~231 cells (33 evals × 7 runner/model targets, ≈462 paid LLM calls),
> and it grows as evals and models are added. To keep that from happening by accident:
> a plain `run` uses only the **cheapest** model per runner; the full grid needs
> `--all-models`; there's a hard `--max-cells` ceiling (default 25) and a
> confirmation prompt for any multi-cell run. Stay cheap with `--skill`/`--evals`,
> `--agents`, `--no-judge`, and preview with `--dry-run`.

`models.yaml` (grouped per runner so each model change is a one-block edit):

```yaml
agents:
  claude:
    default: claude-haiku-4-5        # cheapest — used by a plain `run`
    models: [claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5]   # full set, --all-models
  codex:
    default: gpt-5.4-mini
    models: [gpt-5.5, gpt-5.4-mini]
judge:
  agent: claude
  model: claude-haiku-4-5            # the (cheap) model that grades rubrics
```

Per-runner model selection, in priority order:

1. `--model claude=opus,haiku` (comma list; repeatable; `runner=` or bare for all) — explicit.
2. `--all-models` — the runner's full `models:` list.
3. otherwise — the runner's `default:` (cheapest); if unset, the runner's own built-in default.

```bash
# cheapest model per runner (the safe default)
agentskill-evals run --skill sliderule-region-picker

# compare specific models on one runner
agentskill-evals run --skill sliderule-region-picker \
    --model claude=claude-opus-4-8,claude-haiku-4-5

# the full grid (opt-in; will ask to confirm, and is bounded by --max-cells)
agentskill-evals run --skill sliderule-region-picker --all-models

# preview scope/cost without spending anything
agentskill-evals run --skill sliderule-region-picker --all-models --dry-run
```

The terminal shows a single wide grid (eval rows × `runner:model` columns) plus a
pass-rate-by-target footer; `summary.json` records each cell's `model` and the
top-level `targets`.

### What we test: models, not surfaces

Behavioral coverage is keyed to the **model** — for these domain-knowledge skills, once
the skill text reaches the model, the model determines whether the guidance is followed.
A *surface* (Claude Code, Codex, AntiGravity, CoPilot, any aggregator) matters for
**installation** (per surface — see the root README) and, second-order, for scaffolding;
we test one representative runner per model and accept that minor effect.

So **aggregators / passthrough surfaces (CoPilot, etc.) are install targets, not test
runners** — running an already-covered model through another surface adds ~no coverage and
multiplies cost. AntiGravity is itself an aggregator (`agy` can run Claude/Gemini/GPT-OSS),
so to avoid duplication **each model is listed under exactly one runner** in `models.yaml`.
A surface only becomes a test runner if it reaches a model no existing runner covers.

### Maintaining the models tested

This is the framework's main upkeep over time. `models.yaml` is the only place to edit.

- **Add a model:** add a line under that runner's `models:`.
- **Roll a model off:** delete its line (and repoint `default:` if it was the default).
- **Change the cheap default:** set `default:` to any id in that runner's `models:`.
- **Add a new runner:** add a `<runner>:` block — the runner must also have an adapter
  (see "Adding a new runner").

Find current ids: **claude** → Anthropic model docs (or the `claude-api` skill); **codex**
→ `codex --help` / OpenAI Codex docs; **antigravity** → `agy models` (map the display name
to its `--model` token).

Validate an edit (no guessing):

```bash
agentskill-evals list-agents          # resolved models + default + any warnings
agentskill-evals run … --dry-run      # confirm targets/cell count, spend nothing
# optional cheap smoke for a new id (catches typos / rolled-off ids):
agentskill-evals run --evals <one>.yaml --model <runner>=<new-id> --no-judge
```

`list-agents` and the top of every `run` surface load-time validation warnings (a `default:`
not in `models:`, duplicates, an unknown runner) without hard-blocking. The one exception is
a `models.yaml` that exists but can't be parsed: `run` treats that as fatal (otherwise it
would silently fall back to each CLI's own, possibly pricier, default and break the
"cheapest model by default" guarantee), while `list-agents` still degrades to a warning. A
model that has been retired surfaces as a run error annotated `model 'x' rejected by
<runner> — check models.yaml`, so the fix location is obvious.

## Migrating existing evals

The loader accepts the legacy `{skills, query, files, expected_behavior}` shape,
so old evals run as-is. To upgrade them to the canonical format:

```bash
agentskill-evals migrate --skills-root .            # dry run (prints)
agentskill-evals migrate --skills-root . --write --replace   # YAML, removes .json
agentskill-evals migrate --skill foo --to json --write       # canonical JSON in place
```

## Adding a new runner

A runner is the harness used to reach a model. Add one **only if it reaches a model no
existing runner covers** (see "What we test: models, not surfaces"); a surface that just
runs already-covered models needs install support, not an adapter — see
[Adding a new surface](../README.md#adding-a-new-surface) in the root README.

1. Subclass [`Adapter`](agentskill_evals/adapters/base.py): set `name`/`binary`/
   `skills_subdir`, implement `build_argv()` and `parse()`, optionally override
   `format_skill()` and `provision_skills()`.
2. Register it in [`adapters/__init__.py`](agentskill_evals/adapters/__init__.py)
   (or call `register()` at runtime for out-of-tree agents).
3. Add a captured sample to [`selftest.py`](agentskill_evals/selftest.py) and run
   `python3 -m agentskill_evals selftest`.
4. Add a `<runner>:` block to `models.yaml` for the models it covers.

## Self-test (no CLIs required)

```bash
python3 -m agentskill_evals selftest -v
```

Runs every adapter's `parse()` against a captured sample of its CLI's real
output — a fast wiring check and a regression guard for when an agent changes
its schema.

## Notes & caveats

- **AntiGravity** (`agy`) is young: 1.0.9 emits **plain text** (no `--output-format`),
  so the adapter parses defensively (JSONL → single JSON → raw text) and tool-trace
  extraction is best-effort. Prefer filesystem / `llm_judge` assertions for it. Tighten
  [`adapters/antigravity.py`](agentskill_evals/adapters/antigravity.py) once
  your build exposes a structured schema.
- Skills are provisioned by **symlink** when possible (small, read-only),
  falling back to a copy on platforms without symlinks.
- `--no-auto-approve` disables the per-agent "run without prompts" flag
  (`--dangerously-skip-permissions` for Claude/AntiGravity, `--full-auto` for Codex).
