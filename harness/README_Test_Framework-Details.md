# Test Framework: `harness`

A self-contained Python harness (this folder, [`harness/`](.)) that runs each
**Agent Skill** through real coding-agent CLIs and grades the result.

> Note: the root [Makefile](../Makefile) is for exporting / symlink-installing the skills — not
> testing. The harness has its own [Makefile](Makefile).

## What it tests — a 3-axis matrix

`evals × (runner, model)`.

- A **runner** is the CLI used to reach a model, via a pluggable *adapter*:
  [claude.py](agentskill_evals/adapters/claude.py),
  [codex.py](agentskill_evals/adapters/codex.py),
  [antigravity.py](agentskill_evals/adapters/antigravity.py),
  [copilot.py](agentskill_evals/adapters/copilot.py).
- **Models** come from [models.yaml](../models.yaml) (the single source of truth; the cheapest
  is each runner's default). Each `run` targets one runner via `--agent`; multi-vendor runners
  list all models they support (overlap across runners is expected).

## Where evals live

Per skill, in `<skill>/evals/*.yaml` (auto-discovered). Each eval
([spec.py](agentskill_evals/spec.py)) has:

- `name`, `description`
- `skills` — provisioned into the run workspace
- `prompt` — the user prompt
- `files` — files the agent starts with
- `rubric` — behaviors graded by an LLM judge
- `assertions` — deterministic checks
- optional `tags` / `vars` / `env` / `output_schema` / `timeout_sec`

## Scenarios — the first-class higher-level eval

Per-skill evals test one skill at a time. A **scenario** is a higher-level, ad-hoc eval: one
self-describing file ([scenarios/](../scenarios/)) that provisions a **combination of skills
together** and pins a **target** (`runner:model`), run with `agentskill-evals run --config
<file>`. Because runs are isolated by default, a scenario tests exactly its declared skill set
(plus the agent's vendor skills) — nothing else from this repo leaks in. CLI flags override the
file (`CLI > scenario > default`). See [scenarios/README.md](../scenarios/README.md).

## How one cell runs

[runner.py](agentskill_evals/runner.py) →
[exec.py](agentskill_evals/exec.py):

1. Build a hermetic workspace, provision the eval's skill(s) (symlink, fallback copy), and —
   by default — isolate skill visibility via two layers: an **isolated HOME** that masks
   globally-installed repo skills, and a **`git init`** in the workspace that stops agents from
   walking up to the repo root to discover project-level skills. Together these ensure the model
   sees only what's provisioned plus the agent's vendor skills (`--no-isolated` opts out).
2. Run the agent CLI through its adapter with the eval prompt.
3. Adapter `parse()` normalizes that CLI's output into a common
   [`NormalizedEvent`](agentskill_evals/schema.py) stream + `RunResult`
   (tool calls, commands, files touched, `final_text`, `structured_output`, cost). Tool-trace
   extraction degrades gracefully when a CLI emits plain text (e.g. AntiGravity).
4. Grade.

## Grading — two layers

[assertions.py](agentskill_evals/assertions.py),
[judge.py](agentskill_evals/judge.py):

- **Deterministic assertions:** filesystem (`file_exists` / `file_absent` / `dir_exists`),
  tool trace (`ran_command` / `used_tool` / `tool_count`), `exit_code`, `no_error`,
  `final_contains`, `output_matches_schema` — with `contains` / `matches` / `equals` match
  modes.
- **LLM judge:** each `rubric` item graded by a fixed (cheap) judge model; `--no-judge` skips
  it.

## CLI

Entry point `agentskill-evals` ([cli.py](agentskill_evals/cli.py)):
`run`, `list-agents-configured-models`, `list-agents-available-models`, `list-evals`, `list-skills`, `selftest`. `run --config <file>`
runs a scenario (below); `list-skills` audits skill visibility (superset vs per-runner
masked/kept, with drift warnings).

## Cost guardrails

Every cell is one agent run **plus** one judge call, and the axes multiply, so:
cheapest-model default, a hard `--max-cells` ceiling, a multi-cell confirmation prompt
(fail-closed without a TTY), `--dry-run`, a cheapest-by-default judge, and an unparseable
`models.yaml` is fatal for `run` (warning-only for `list-agents-configured-models`).

## Output

A pass/fail wide grid (eval rows × `runner:model` columns) plus pass-rate-per-target
(i.e. per `runner:model`) on stdout, and artifacts under
`harness/artifacts/<run_id>/<runner>/<model>/<skill>/<eval>/`
(`summary.json` with top-level `targets` + per-cell results).

## Self-test

[selftest.py](agentskill_evals/selftest.py) runs every adapter's `parse()`
against captured CLI fixtures — a wiring / regression check that needs no agent CLIs or
third-party dependencies.

---

See [harness/README.md](README.md) for install, the full eval
field/assertion reference, isolation, scenarios, cross-model testing, and the `models.yaml`
maintenance workflow; [harness/FAQ.md](FAQ.md) for "which skills can
the model see during a test?"; and [scenarios/README.md](../scenarios/README.md) for combination scenarios.
