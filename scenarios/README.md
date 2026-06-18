# scenarios/ — ad-hoc combination tests

A **scenario** is a single, self-describing test file that provisions a *combination* of
skills **together** and runs them against one **target** (a `runner:model`). It's the
first-class way to ask: *"with exactly these skills available, can this model do the task?"*

Unlike the per-skill evals under `<skill>/evals/`, scenarios are **ad-hoc**: they are **not
auto-discovered**. You run one explicitly by path.

## Run one

From the **repo root** (so the skills resolve and the run is isolated against this repo):

```bash
agentskill-evals run --config scenarios/example_api+params_on_claude-haiku.yaml
```

Preview first — see the plan **and exactly which skills the model will see**, with no API cost:

```bash
agentskill-evals run --config scenarios/example_api+params_on_claude-haiku.yaml --dry-run
```

By default each run is **isolated**: the model sees only the skills the scenario lists (plus the
agent's own vendor skills), never other skills you have installed globally. Add `--no-isolated`
to test against your real, globally-installed setup instead.

## File format

```yaml
name: api+params combination       # optional; defaults to the filename
description: ...                   # optional
target:                            # REQUIRED
  runner: claude                   #   required — one of: claude, codex, antigravity
  model: claude-haiku-4-5          #   optional — omit to use models.yaml's cheapest default
skills:                            # REQUIRED, non-empty — provisioned together
  - sliderule-api
  - sliderule-params
prompt: |                          # REQUIRED. {skills} expands to the agent's skill refs.
  Using {skills}, write ...
rubric: [ ... ]                    # optional — graded by the LLM judge
assertions:                        # optional — deterministic checks
  - {type: file_exists, path: run.py}
# optional run knobs (CLI flags override these):
judge: true                        # false to skip rubric grading
isolated: true                     # false to expose globally-installed repo skills
max_cells: 25
jobs: 1
```

`vars` / `env` / `output_schema` / `timeout_sec` / `tags` work exactly as in a normal eval.
The only scenario-specific key is `target:`.

## Naming convention

Name files so a newcomer knows what each is for: **`<what>_on_<runner>-<model>.yaml`**, e.g.
`api+params_on_claude-haiku.yaml`, `full-pipeline_on_codex-gpt5.5.yaml`. The target also lives
inside the file; the filename just makes the directory self-describing at a glance.

## Override precedence

For a single run: **CLI flag > scenario file > built-in default**. So
`--agents codex` or `--model claude=claude-opus-4-8` or `--no-isolated` on the command line
override what the file declares, without editing it.

See [../agent-skill-evals/README.md](../agent-skill-evals/README.md) for the full harness
reference and [../agent-skill-evals/FAQ.md](../agent-skill-evals/FAQ.md) for how skill
visibility works.
