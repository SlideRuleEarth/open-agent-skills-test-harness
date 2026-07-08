# harness (`agentskill-evals`)

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
| Codex | `codex --ask-for-approval never --sandbox workspace-write exec --json` | JSONL: `item.started`/`item.completed` with `item.type` = `command_execution`/`file_change`/`agent_message` |
| AntiGravity | `agy -p "<prompt>" --output-format json --add-dir <workspace> --dangerously-skip-permissions` | one JSON object (`conversation_id`/`status`/`response`/`usage`) — the tool-call trace itself is read separately off disk, keyed by `conversation_id`; parse() falls back to JSONL → single JSON → raw text for older builds |

Every adapter maps its CLI's events onto one [`NormalizedEvent`](agentskill_evals/schema.py)
stream and a `RunResult`. Assertions, the judge, and reports only ever see that
common shape — so adding an agent is one small adapter, not a rewrite.

## Install

The harness is a normal Python package (entry point `agentskill-evals`). **The `Makefile` in this
folder is the way to install it.** Run a target from inside `harness/` (or, from the repo
root, prefix it with `make -C harness`):

| Target | For | What it does |
| --- | --- | --- |
| `make install` | running the evals | Puts the `agentskill-evals` CLI on your PATH in an isolated env (pipx). |
| `make dev` | editing the harness | Creates `.venv/` and editable-installs with the `[schema]` extra (adds `jsonschema`; a built-in fallback works without it). Activate with `. .venv/bin/activate`. |

Both pull in `pyyaml` for you. After `make install`, sanity-check with `agentskill-evals list-agents-configured-models
--skills-root ..`; `make help` lists the other targets (`selftest`, `clean`, `uninstall`).

> **`make install` requires [pipx](https://pipx.pypa.io).** Install it with `brew install pipx`
> (macOS) or `python3 -m pip install --user pipx && python3 -m pipx ensurepath`, then re-run
> `make install` (which otherwise stops and prints these options). **conda/mambaforge users:** avoid
> `conda install pipx` in `base` — it can clash with conda's own pinned deps (`packaging`/`pluggy`);
> use one of the above, or a dedicated env (`conda create -n pipx -c conda-forge pipx`). Don't want
> pipx at all? `make dev` (a local venv) or the module form (below) need none.

> **Why the Makefile and not a bare `pip install`?** The targets install into an **isolated
> environment** for you — don't `pip install` into system Python: modern macOS/Linux block that
> under PEP 668, and it risks dependency clashes. `make install` uses pipx, `make dev` a local venv,
> so you don't have to manage that.

### Installing without `make` (the exceptions)

The targets just wrap standard tools; run these directly only if `make` doesn't fit your setup:

```bash
# pipx / uv — e.g. straight from git (note the subdirectory; the package isn't at the repo root):
pipx install ./harness
pipx install "git+https://github.com/SlideRuleEarth/sliderule-skills.git#subdirectory=harness"
uv tool install ./harness        # uv also manages the Python version

# the hand-rolled venv that `make dev` automates:
cd harness && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[schema]"

# no install at all — run it as a module from inside harness/:
python3 -m agentskill_evals selftest                                   # needs no dependencies
python3 -m agentskill_evals run --skills-root .. --config ../scenarios/<file>.yaml
```

### Notes

- **Requires Python ≥ 3.10.** If pipx or `make dev` picks an older interpreter, install fails with a
  `requires-python` error — select one explicitly: `pipx install . --python python3.12`, or
  `make dev PYTHON=python3.12`.
- `selftest` and `--help` need no dependencies (scenario tests are skipped without PyYAML); anything that reads YAML evals or `models.yaml`
  (`run`, `list-evals`, `list-agents-configured-models`) needs `pyyaml` — every option above provides it.
- Install whichever agent CLIs you want to test (`claude`, `codex`, `agy`). Missing ones are marked
  `ERR` / skipped — `list-agents-configured-models` shows what's available.

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
them. `skills:` are copied into each agent's project-local skills dir
(`.claude/skills` for Claude, `.agents/skills` for Codex, `.antigravity/skills`
for AntiGravity) so the run is hermetic.

### Fields

| field | meaning |
|-------|---------|
| `name` | eval id (defaults to filename) |
| `description` | what correct behavior looks like (given to the judge) |
| `prompt` | the user message (legacy `query` also accepted) |
| `skills` | skills provisioned into the workspace (legacy `skill` accepted) |
| `files` | files seeded into the workspace; sources are relative to the eval file and keep that relative path (`fixtures/in.json` → `fixtures/in.json`). Use a `{src: dest}` mapping for a different destination |
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

`rubric:` at the top level is auto-compiled into one `llm_judge` assertion — unless an
explicit `llm_judge` assertion is already present (e.g. to set `threshold`), which grades
the rubric itself so the judge only runs (and bills) once. `output_schema:` likewise
compiles into one `output_matches_schema` assertion. With `--no-judge` (or no judge
available), `llm_judge` checks are **skipped**, not failed — the cell is graded on its
deterministic assertions only.

> **Judge caveat — prompt injection.** The judge's prompt embeds the graded agent's final
> answer and file contents verbatim, so a run's output can try to steer the verdict
> ("all rubric items pass"). The per-cell `judge_report.md` / `judge_stdout.jsonl`
> artifacts preserve the judge's full prompt and reasoning at the same detail level as the
> agent's own transcript — treat a surprising pass with suspicion and read them. A judge
> run is killed after `--judge-timeout` seconds (default 240; also settable as
> `judge.timeout` in models.yaml or a scenario's `judge:` block).

## Usage

Examples use the installed `agentskill-evals` CLI (see [Install](#install)). From a
source checkout without installing, run them as `python3 -m agentskill_evals …` from
inside `harness/`, adding `--skills-root ..` so the harness finds the sibling
skill evals and the repo-root `models.yaml` (the default skills-root is the current
directory).

```bash
# from the skills repo root (each <skill>/evals/ is discovered automatically)

# what's configured?
agentskill-evals list-agents-configured-models

# verify what's actually available (probes CLIs — has a small cost)?
agentskill-evals list-agents-available-models

# what evals exist?
agentskill-evals list-evals --skills-root .

# `run` always requires --skill, --evals, or --config (no unscoped broad runs)

# one skill, specific agent, parallel, verbose failures
agentskill-evals run --skill sliderule-docsearch \
    --agent claude --jobs 4 -v

# a single eval file, no judge, just deterministic checks
agentskill-evals run --agent claude --evals path/to/eval.yaml --no-judge

# grade with a different judge agent, and pin the run's own model
agentskill-evals run --agent claude --skill foo --judge-agent codex
agentskill-evals run --agent claude --skill foo --model claude-haiku-4-5
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

`run`'s exit code is CI-ready: `0` — every graded cell passed; `1` — a cell failed (or the
run was aborted at the confirmation prompt); `2` — usage/config error (bad flags, malformed
spec or scenario, duplicate eval names, missing seed files); `3` — nothing was graded
(rubric-only evals with the judge off), which is "no verdict", not a failure.

## Skill isolation

By default every run is **isolated**: agents discover skills from two locations, and the
harness blocks both:

1. **Global (HOME-based):** `~/.claude/skills/`, `~/.agents/skills/`, etc. Blocked by a
   **HOME symlink overlay** (`isolation.py`) that masks this repo's skills while keeping
   vendor bundles and auth/config.
2. **Project-local (git-root-based):** `.claude/skills/`, `.agents/skills/`, etc. at the
   git repository root, found by walking up from cwd. Blocked by running the cell's workspace
   in a tempdir with no path relationship to this repo's checkout in the first place
   (`runner.py`) — there's no real repo root above it to walk up into, whether the walk is a
   `.git`-aware skill-discovery mechanism or a general-purpose file-browsing agent that just
   `list_dir`s a parent directory by absolute path. That second case is what antigravity
   actually did in practice before the workspace was relocated — see `leaked_skill_reads()` in
   `workspace_view.py`, the after-the-fact detector that catches it if it ever recurs some other
   way, and downgrades that cell's `isolated` flag to `false` instead of a silent false positive.

For normal skill discovery, these layers make the model see only the skills the eval/scenario
provisions — plus the agent's built-in/vendor skills — not other repo skills you happen to have
installed.
This is what makes "test skill X (or this *combination*) in isolation" actually true, and
enables A/B testing with vs without skills.

- **Surgical, not a blank sandbox.** Only the global skills dirs are masked (per-runner
  `global_skills_subpaths`), and only *this repo's* skills are removed from them — vendor
  bundles like codex's `~/.codex/skills/.system` and Claude Code plugins are kept. The declared
  skills are also placed in the harness-owned global dir, so discovery works whatever path or
  precedence a surface uses.
- **Custom config homes are honored.** If you point a runner at a non-default config home
  (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`), it's mirrored into the isolated home — auth/config keep
  working — with its skills masked the same way, and the variable is repointed at the mirror.
- **Opt out** with `--no-isolated` to test against your real, globally-installed setup.
- **Audit / preview** with `agentskill-evals list-skills` (the provisionable superset, the
  per-runner masked/kept split, and drift warnings such as a stale `make link-global`) or with
  `run … --dry-run`, which prints a per-target *"Skills visible to the model"* block — no API cost.
- **Caveats:** skills bundled inside a CLI's package or plugins live outside these dirs and are
  *not* masked (that's intentional — the platform baseline). On a platform without symlink
  privileges only the HOME-overlay layer falls back non-isolated, with a warning (`isolated`
  is reported `false` for that cell) — the separate project-local tempdir-relocation layer
  doesn't use symlinks and still applies regardless. Config-dir *writes* still
  pass through to the real dirs (only skill *visibility* is isolated). None of this is an OS-level
  jail — an agent that deliberately searches the whole disk (e.g. `find / -iname sliderule-skills`)
  rather than just exploring its own cwd can still find the real checkout, since it genuinely
  exists somewhere on the same filesystem. Closing that would need a container/VM per cell (a
  real, cross-platform fs boundary) or per-OS native sandboxes (macOS Seatbelt, Linux
  namespaces/bubblewrap, Windows AppContainer — three incompatible APIs, no shared primitive).
  Deliberate choice for now: accept that residual risk, rely on `leaked_skill_reads()` to catch it
  if it's ever actually exercised, and revisit with a container-based execution backend if a real
  run shows deliberate broad-disk searching rather than incidental cwd exploration.

See [FAQ.md](FAQ.md) for the plain-language version.

## Scenarios — ad-hoc combination evals

A per-skill eval tests one skill. A **scenario** tests a *combination* of skills working
together against a chosen target (`runner:model`), from one self-describing file. Scenarios are
**ad-hoc** — not auto-discovered; you run one by path:

```bash
# preview what runs + exactly which skills are visible (no API cost)
agentskill-evals run --config scenarios/example_api+params_on_claude-haiku.yaml --dry-run

# run it
agentskill-evals run --config scenarios/example_api+params_on_claude-haiku.yaml
```

A scenario file is an eval spec plus a `target:` block:

```yaml
name: api+params combination
target:
  runner: claude
  model: claude-haiku-4-5      # optional; omit → models.yaml's cheapest default
skills: [sliderule-api, sliderule-params]   # provisioned together; the only repo skills visible
prompt: |
  Using {skills}, write run.py that ...
rubric: [ ... ]
assertions: [{type: file_exists, path: run.py}]
judge: true        # optional run knobs (CLI flags override): judge / isolated / max_cells / jobs
isolated: true
```

Precedence for a run is **CLI flag > scenario file > built-in default**, so `--agent`,
`--model`, `--no-isolated`, etc. override the file without editing it. Scenario artifacts land
under `artifacts/<run_id>/<model>/scenario/<name>/`. Files live in
[`../scenarios/`](../scenarios/) with the convention `<what>_on_<runner>-<model>.yaml`; see
[scenarios/README.md](../scenarios/README.md).

## Cross-model testing

The same skill can behave very differently on different **models**, so model is a
first-class axis: a run is a matrix of `evals × models` for a single runner. A "runner"
is the CLI used to reach a model (Claude Code, Codex, AntiGravity, Copilot); the models
live in **`models.yaml`** at the repo root — the single source of truth (no model ids are
hardcoded in the harness). Each `run` targets one runner via `--agent`.

> ⚠️ **Cost.** Every cell is a full agent run **plus** a judge call, and the axes
> multiply (`evals × models`). To keep that from happening by accident:
> `run` **requires** `--agent` and `--skill`/`--evals`/`--config` (no unscoped broad
> discovery); a scoped run uses only the **cheapest** model by default; the full model
> list needs `--all-models`; there's a hard `--max-cells` ceiling (default 25) and a
> confirmation prompt for any multi-cell run. Further narrow with `--model`,
> `--no-judge`, and preview with `--dry-run`.

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

Model selection for the one runner picked via `--agent`, in priority order:

1. `--model <id1>,<id2>,...` (comma-separated list, for that run's single `--agent`) — explicit;
   wins even over `--all-models` if both are given (a warning is printed, not a silent drop).
2. `--all-models` — the runner's full `models:` list.
3. otherwise — the runner's `default:` (cheapest); if unset, the runner's own built-in default.

```bash
# cheapest model on this runner (the safe default)
agentskill-evals run --agent claude --skill sliderule-region-picker

# compare specific models on one runner
agentskill-evals run --agent claude --skill sliderule-region-picker \
    --model claude-opus-4-8,claude-haiku-4-5

# the full grid (opt-in; will ask to confirm, and is bounded by --max-cells)
agentskill-evals run --agent claude --skill sliderule-region-picker --all-models

# preview scope/cost without spending anything
agentskill-evals run --agent claude --skill sliderule-region-picker --all-models --dry-run
```

The terminal shows a single wide grid (eval rows × model columns) plus a
pass-rate footer; `summary.json` records each cell's `model` and the agent.

### Single-runner invocations

Each `run` invocation targets exactly one runner via `--agent`. This keeps cost
transparent — you know what runner you're paying for — and simplifies the output.
Multi-vendor runners like Copilot and AntiGravity list all models they support;
overlap with other runners is expected and fine, since runs are scoped to one runner.

### Maintaining the models tested

This is the framework's main upkeep over time. `models.yaml` is the only place to edit.

- **Add a model:** add a line under that runner's `models:`.
- **Roll a model off:** delete its line (and repoint `default:` if it was the default).
- **Change the cheap default:** set `default:` to any id in that runner's `models:`.
- **Add a new runner:** add a `<runner>:` block — the runner must also have an adapter
  (see "Adding a new runner").

Find current ids: **claude** → Anthropic model docs; **codex** → `codex --help`;
**antigravity** → `agy models`; **copilot** → `copilot --help`.

Validate an edit (no guessing):

```bash
agentskill-evals list-agents-configured-models   # what models.yaml declares
agentskill-evals list-agents-available-models     # probe CLIs to verify (has a small cost)
agentskill-evals run --agent claude --skill X --dry-run  # confirm cell count, spend nothing
```

`list-agents-configured-models` and the top of every `run` surface load-time validation
warnings (a `default:` not in `models:`, duplicates, an unknown runner) without hard-blocking.
`list-agents-available-models` goes further — it probes each CLI to verify configured models
are accepted and discovers models not yet in the config. The one exception is a `models.yaml`
that exists but can't be parsed: `run` treats that as fatal (otherwise it would silently fall
back to the CLI's own, possibly pricier, default and break the "cheapest model by default"
guarantee), while `list-agents-configured-models` still degrades to a warning. A model that
has been retired surfaces as a run error annotated `model 'x' rejected by <runner> — check
models.yaml`, so the fix location is obvious.

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

- **AntiGravity** (`agy`) moves fast — re-verify against `agy --help` / `agy changelog`
  before trusting any of this. As of 1.0.16: `--output-format json` works but is
  undocumented (absent from `--help`); its stdout is just the final answer, so the real
  tool-call trace is read separately from the on-disk transcript the CLI writes for every
  run (`~/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/
  transcript_full.jsonl`), keyed by the `conversation_id` in the JSON result. Print mode
  also doesn't scope itself to the process's cwd by default — it operates against a fixed,
  shared `~/.gemini/antigravity-cli/scratch` dir otherwise, so `build_argv` always passes
  `--add-dir <workspace>`. [`adapters/antigravity.py`](agentskill_evals/adapters/antigravity.py)'s
  module docstring has the full detail; parse() still falls back to JSONL → single JSON →
  raw text for older builds without `--output-format`.
- AntiGravity also discovers skills via a **plugin registry**
  (`~/.gemini/config/plugins/<name>/skills/…`) independent of its regular global skills
  dirs — e.g. `agy plugin import claude` can mirror this repo's skills there, invisibly
  bypassing per-eval skill declaration. `global_plugin_registry_subpaths` (see
  [`isolation.py`](agentskill_evals/isolation.py)) masks it the same way regular skills
  dirs are masked; `list-skills` folds it into each adapter's `vendor`/`masked` counts.
- Skills are provisioned by **copy**, not symlink (`Adapter.provision_skills`,
  `adapters/base.py`) — deliberately, so a run that writes inside a provisioned skill dir
  mutates only the throwaway workspace copy, never the repo's actual skill source. The HOME
  overlay (`isolation.py`) also copies declared skills for the same reason; it uses symlinks
  only for *passthrough* entries (vendor skills, auth/config, unrelated plugins) that aren't
  the content being provisioned.
- `--no-auto-approve` disables the per-agent "run without prompts" flags
  (`--dangerously-skip-permissions` for Claude/AntiGravity; `--ask-for-approval never
  --sandbox workspace-write` for Codex).
