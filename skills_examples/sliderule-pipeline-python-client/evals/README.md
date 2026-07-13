# Evaluations

Evaluations for the `sliderule-pipeline-python-client` skill. Each `*.yaml` file is one eval,
loosely following the schema in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
and extended for the [`harness`](../../../harness/) runner.

## Format

Each eval has:

- `name` — eval id
- `description` — what correct behavior looks like (shown to the LLM judge)
- `skills` — skills provisioned into the agent's workspace for the run
- `prompt` — the user prompt in natural language
- `files` — files the agent starts with (none for these evals)
- `rubric` — behaviors a correct answer must exhibit, graded by an LLM judge
- `assertions` — optional deterministic checks (filesystem, tool-call trace, …)

See the [runner README](../../../harness/README.md) for the full field
and assertion reference.

## Running

From the repo root (install the `agentskill-evals` CLI first — see the [runner README](../../../harness/README.md#install)):

```bash
# this skill on one agent, graded by the default judge
agentskill-evals run --skill sliderule-pipeline-python-client --agent copilot

# specific agent + model, parallel, show failing checks
agentskill-evals run --skill sliderule-pipeline-python-client \
    --agent claude --jobs 4 -v
```

The runner provisions the skill into a hermetic per-agent workspace, captures the
tool trace + final answer, and grades each `rubric` item with the judge. Results
land in `harness/artifacts/<run_id>/`.

To measure the skill's *value*, re-run with `--no-provision` (skill absent) and
compare — per the best-practices doc, the with/without delta is the real signal.

## Compare across models

How well a skill works is determined mostly by the model, so the harness tests across
**models** (not across every surface — see the
[harness README](../../../harness/README.md#what-we-test-models-not-surfaces)).
Models live in the repo-root [`models.yaml`](../../../models.yaml); a plain run uses the
cheapest model per runner, `--all-models` runs the full set.

```bash
# cheapest model per runner (safe default)
agentskill-evals run --skill sliderule-pipeline-python-client --agent copilot

# compare specific models — Haiku is the canonical weak-model probe (most likely to skip
# SKILL.md and answer from training; if it works on Haiku it usually works everywhere)
agentskill-evals run --skill sliderule-pipeline-python-client --agent claude --model claude=claude-opus-4-8,claude-haiku-4-5

# the full models.yaml grid (opt-in; prompts to confirm, bounded by --max-cells)
agentskill-evals run --skill sliderule-pipeline-python-client --agent copilot --all-models
```

See the [harness README](../../../harness/README.md#cross-model-testing) for cost
guardrails and how the model list is maintained.

## The evals

| File | Catches |
| ---- | ------- |
| [01-single-script-consolidation.yaml](01-single-script-consolidation.yaml) | Splitting a fetch → filter → aggregate workflow across separate ad-hoc steps instead of one consolidated client-based pipeline script |
| [02-surface-reproducible-script.yaml](02-surface-reproducible-script.yaml) | Running an ephemeral inline/heredoc script instead of saving + surfacing a named `pipeline.py` under `agents_outputs/<run>/` |
| [03-task-metrics-reporting.yaml](03-task-metrics-reporting.yaml) | Omitting the "SlideRule Task Summary" task-metrics block — including on an empty-GeoDataFrame or failed request |
| [04-cross-skill-boundary-direct-request.yaml](04-cross-skill-boundary-direct-request.yaml) | Forcing the Python client when the user explicitly asked for raw HTTP requests (`sliderule-pipeline-direct-request` territory) |
| [05-checkpoint-fetch.yaml](05-checkpoint-fetch.yaml) | Re-paying a minutes-long fetch on rerun — no `raw.parquet` checkpoint guard on the client call, or fabricated/reused request timing on a cached rerun |
