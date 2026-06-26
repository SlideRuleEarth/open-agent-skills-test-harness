# Evaluations

Evaluations for the `sliderule-api` skill. Each `*.yaml` file is one eval,
loosely following the schema in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
and extended for the [`harness`](../../harness/) runner.

## Format

Each eval has:

- `name` — eval id
- `description` — what correct behavior looks like (shown to the LLM judge)
- `skills` — skills provisioned into the agent's workspace for the run
- `prompt` — the user prompt in natural language
- `files` — files the agent starts with (none for these evals)
- `rubric` — behaviors a correct answer must exhibit, graded by an LLM judge
- `assertions` — optional deterministic checks (filesystem, tool-call trace, …)

See the [runner README](../../harness/README.md) for the full field
and assertion reference.

## Running

From the repo root (install the `agentskill-evals` CLI first — see the [runner README](../../harness/README.md#install)):

```bash
# this skill on one agent, graded by the default judge
agentskill-evals run --skill sliderule-api --agent copilot

# specific agent + model, parallel, show failing checks
agentskill-evals run --skill sliderule-api \
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
[harness README](../../harness/README.md#what-we-test-models-not-surfaces)).
Models live in the repo-root [`models.yaml`](../../models.yaml); a plain run uses the
cheapest model per runner, `--all-models` runs the full set.

```bash
# cheapest model per runner (safe default)
agentskill-evals run --skill sliderule-api --agent copilot

# compare specific models — Haiku is the canonical weak-model probe (most likely to skip
# SKILL.md and answer from training; if it works on Haiku it usually works everywhere)
agentskill-evals run --skill sliderule-api --agent claude --model claude=claude-opus-4-8,claude-haiku-4-5

# the full models.yaml grid (opt-in; prompts to confirm, bounded by --max-cells)
agentskill-evals run --skill sliderule-api --agent copilot --all-models
```

See the [harness README](../../harness/README.md#cross-model-testing) for cost
guardrails and how the model list is maintained.

## The evals

| File | Catches |
| ---- | ------- |
| [01-request-envelope-construction.yaml](01-request-envelope-construction.yaml) | A malformed request — wrong endpoint, missing the required `output` block, an open/clockwise polygon, or hitting the streaming path instead of `.arrow` |
| [02-operational-failure-fallback.yaml](02-operational-failure-fallback.yaml) | Mishandling HTTP 429/503/timeouts (no backoff / shrink), treating a valid zero-row response as an error, or not falling back to `atl03x` |
| [03-cross-skill-boundary-param-planning.yaml](03-cross-skill-boundary-param-planning.yaml) | Answering "which parameters / what default" itself instead of redirecting planning to `sliderule-params` and signatures/defaults to `sliderule-openapi` |
