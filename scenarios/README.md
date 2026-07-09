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

By default each run is **isolated**: normal skill discovery sees only the skills the scenario lists
(plus the agent's own vendor skills), while other repo skills in tracked global/project locations
are masked. Add `--no-isolated` to test against your real, globally-installed setup instead.

## File format

```yaml
name: api+params combination       # optional; defaults to the filename
description: ...                   # optional
target:                            # REQUIRED
  runner: copilot                  #   required — one of: claude, codex, antigravity, copilot
  model: claude-haiku-4.5          #   optional — omit to use models.yaml's cheapest default
  # A list makes one matrix column per entry, and each entry may pin its own reasoning
  # effort (as an @suffix or a mapping) — so one scenario can compare a small model
  # thinking hard against a big model thinking little:
  # model:
  #   - claude-haiku-4.5@high
  #   - {model: claude-opus-4.6, reasoning_effort: low}
skills:                            # REQUIRED, non-empty — provisioned together
  - sliderule-api
  - sliderule-params
prompt: |                          # REQUIRED. {skills} expands to the agent's skill refs.
  Using {skills}, write ...
rubric: [ ... ]                    # optional — graded by the LLM judge
assertions:                        # optional — deterministic checks
  - {type: file_exists, path: run.py}
reasoning_effort: high             # optional — low|medium|high thinking budget for target
                                   #   models WITHOUT their own @effort pin (per cell:
                                   #   CLI --reasoning-effort > per-model pin > this field).
                                   #   Mapped natively on claude/codex/copilot; antigravity
                                   #   has no equivalent control and warns + ignores it
                                   #   (its effort tier is part of the model id).
# optional run knobs (CLI flags override these):
judge: true                        # false to skip rubric grading (see Judge section below)
isolated: true                     # false to expose globally-installed repo skills
max_cells: 25
jobs: 1
```

`vars` / `env` / `output_schema` / `timeout_sec` / `tags` / `reasoning_effort` work exactly
as in a normal eval. The only scenario-specific key is `target:`.

## Judge

The judge is the LLM that grades rubric items. By default, the judge agent and model come
from `models.yaml`:

```yaml
# models.yaml
judge:
  agent: copilot
  model: claude-haiku-4.5
```

### Per-scenario judge override

A scenario can override the judge agent, model, or both:

```yaml
# Boolean — use or skip the global judge
judge: true          # use the global judge from models.yaml (default)
judge: false         # skip rubric grading entirely

# Mapping — override agent and/or model for this scenario
judge:
  agent: codex
  model: gpt-5.4-mini
```

When `judge:` is a mapping, only the fields you include are overridden; the rest fall
through to `models.yaml`. So `judge: {model: claude-sonnet-4.6}` keeps the global judge
agent but swaps the model.

### Priority chain

For the judge agent and model, the resolution order is:

1. CLI flag (`--judge-agent` / `--judge-model`)
2. Scenario `judge:` mapping
3. `models.yaml` `judge:` block
4. Fallback (claude if installed)

## Assertions

Assertions are deterministic checks that run after the agent completes. Every
assertion has a `type` and type-specific fields. All types accept an optional
`description` to override the default message.

### Filesystem

| Type | Required | Optional | Checks |
|---|---|---|---|
| `file_exists` | `path` | `contains`, `matches`, `min_size` | File exists in workspace |
| `file_absent` | `path` | | File does NOT exist |
| `dir_exists` | `path` | | Directory exists |

### Tool / command trace

| Type | Required | Optional | Checks |
|---|---|---|---|
| `ran_command` | | `contains`, `matches`, `equals`, `ignore_case` | A shell command matched |
| `used_tool` | `name` | | Tool name appeared in the trace |
| `tool_count` | | `min`, `max` | Total tool calls within range |

### Skill interaction

These check whether the agent accessed a provisioned skill's files during the run.
`path` is relative to the skill's `references/` or `scripts/` subdirectory.

| Type | Required | Optional | Checks |
|---|---|---|---|
| `skill_triggered` | `skill` | | Any access to files under the skill dir |
| `skill_not_triggered` | `skill` | | No access to the skill dir |
| `skill_reference_read` | `skill` | `path` | Read from `<skill>/references/` |
| `skill_reference_not_read` | `skill` | `path` | No reads from `<skill>/references/` |
| `skill_script_executed` | `skill` | `path` | Command referencing `<skill>/scripts/` |
| `skill_script_not_executed` | `skill` | `path` | No commands referencing `<skill>/scripts/` |

Examples:

```yaml
assertions:
  # Verify the agent used sliderule-params
  - {type: skill_triggered, skill: sliderule-params}

  # Verify it did NOT try to use sliderule-api (boundary test)
  - {type: skill_not_triggered, skill: sliderule-api}

  # Verify it read a specific reference file
  - {type: skill_reference_read, skill: sliderule-openapi, path: parameter_couplings.md}

  # Verify it did NOT read the wrong reference
  - {type: skill_reference_not_read, skill: sliderule-openapi, path: elevation_datums.md}

  # Verify it ran the OpenAPI script
  - {type: skill_script_executed, skill: sliderule-openapi, path: openapi.py}

  # Verify it did NOT run the script (narrative question, not a spec lookup)
  - {type: skill_script_not_executed, skill: sliderule-openapi}
```

### Process / output

| Type | Required | Optional | Checks |
|---|---|---|---|
| `exit_code` | | `equals` (default 0) | Process exit code |
| `no_error` | | | Clean run: exit 0, no timeout, no harness error |
| `final_contains` | | `contains`, `matches`, `equals`, `ignore_case` | Final answer text matches |

### Schema + judge

| Type | Required | Optional | Checks |
|---|---|---|---|
| `output_matches_schema` | | `schema` | Structured output validates against JSON Schema |
| `llm_judge` | | `rubric`, `threshold` | LLM judge grades rubric items |

## Naming convention

Name files so a newcomer knows what each is for: **`<what>_on_<runner>-<model>.yaml`**, e.g.
`api+params_on_claude-haiku.yaml`, `full-pipeline_on_codex-gpt5.5.yaml`. The target also lives
inside the file; the filename just makes the directory self-describing at a glance.

## Override precedence

For a single run: **CLI flag > scenario file > models.yaml > built-in default**. So
`--agent codex` or `--model claude-opus-4-8` or `--no-isolated` on the command line
override what the file declares, without editing it. The judge follows the same
chain (see the Judge section above).

See [../harness/README.md](../harness/README.md) for the full harness
reference and [../harness/FAQ.md](../harness/FAQ.md) for how skill
visibility works.
