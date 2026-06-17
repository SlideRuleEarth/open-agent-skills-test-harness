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
| AntiGravity | `agy -p "/goal …"` | `--output-format json` (version-dependent, undocumented schema → parsed defensively) |

Every adapter maps its CLI's events onto one [`NormalizedEvent`](agentskill_evals/schema.py)
stream and a `RunResult`. Assertions, the judge, and reports only ever see that
common shape — so adding an agent is one small adapter, not a rewrite.

## Install

```bash
cd agent-skill-evals
pip install -e .                 # pulls pyyaml; jsonschema is an optional extra
# or run in place with no install:
PYTHONPATH=. python3 -m agentskill_evals --help
```

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

```bash
# from the skills repo root (each <skill>/evals/ is discovered automatically)

# what's installed?
python3 -m agentskill_evals list-agents

# what evals exist?
python3 -m agentskill_evals list-evals --skills-root .

# run everything on every installed agent (judge defaults to claude)
python3 -m agentskill_evals run --skills-root .

# one skill, specific agents, parallel, verbose failures
python3 -m agentskill_evals run --skill sliderule-docsearch \
    --agents claude codex antigravity --jobs 4 -v

# a single eval file, no judge, just deterministic checks
python3 -m agentskill_evals run --evals path/to/eval.yaml --no-judge

# grade with a different judge / model
python3 -m agentskill_evals run --skill foo --judge-agent codex
python3 -m agentskill_evals run --skill foo --model claude=claude-haiku-4-5
```

Output: a pass/fail matrix on stdout, plus per-run artifacts under
`artifacts/<run_id>/`:

```
artifacts/<run_id>/
  summary.json              # machine-readable matrix
  summary.md               # rendered table
  <agent>/<skill>/<eval>/
    stdout.jsonl           # raw agent output
    stderr.txt
    events.json            # normalized event stream
    result.json            # RunResult (final text, commands, cost, …)
    assertions.json        # per-check verdicts (incl. judge per-rubric reasons)
    workspace/             # the hermetic working dir after the run
```

`run` exits non-zero if any cell fails — drop it straight into CI.

## Migrating existing evals

The loader accepts the legacy `{skills, query, files, expected_behavior}` shape,
so old evals run as-is. To upgrade them to the canonical format:

```bash
python3 -m agentskill_evals migrate --skills-root .            # dry run (prints)
python3 -m agentskill_evals migrate --skills-root . --write --replace   # YAML, removes .json
python3 -m agentskill_evals migrate --skill foo --to json --write       # canonical JSON in place
```

## Adding a new agent

1. Subclass [`Adapter`](agentskill_evals/adapters/base.py): set `name`/`binary`/
   `skills_subdir`, implement `build_argv()` and `parse()`, optionally override
   `format_skill()` and `provision_skills()`.
2. Register it in [`adapters/__init__.py`](agentskill_evals/adapters/__init__.py)
   (or call `register()` at runtime for out-of-tree agents).
3. Add a captured sample to [`selftest.py`](agentskill_evals/selftest.py) and run
   `python3 -m agentskill_evals selftest`.

## Self-test (no CLIs required)

```bash
python3 -m agentskill_evals selftest -v
```

Runs every adapter's `parse()` against a captured sample of its CLI's real
output — a fast wiring check and a regression guard for when an agent changes
its schema.

## Notes & caveats

- **AntiGravity** (`agy`) is young: `--output-format json` is version-dependent
  and its event schema is undocumented, so the adapter parses defensively
  (JSONL → single JSON → raw text) and tool-trace extraction is best-effort.
  Prefer filesystem / `llm_judge` assertions for it. Tighten
  [`adapters/antigravity.py`](agentskill_evals/adapters/antigravity.py) once
  your build's real schema is known.
- Skills are provisioned by **symlink** when possible (small, read-only),
  falling back to a copy on platforms without symlinks.
- `--no-auto-approve` disables the per-agent "run without prompts" flag
  (`--dangerously-skip-permissions` / `--full-auto` / `--yolo` / `/goal`).
```
