# Evaluations

Evaluations for the `sliderule-api` skill. Each `*.yaml` file is one eval,
loosely following the schema in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
and extended for the [`agent-skill-evals`](../../agent-skill-evals/) runner.

## Format

Each eval has:

- `name` — eval id
- `description` — what correct behavior looks like (shown to the LLM judge)
- `skills` — skills provisioned into the agent's workspace for the run
- `prompt` — the user prompt in natural language
- `files` — files the agent starts with (none for these evals)
- `rubric` — behaviors a correct answer must exhibit, graded by an LLM judge
- `assertions` — optional deterministic checks (filesystem, tool-call trace, …)

See the [runner README](../../agent-skill-evals/README.md) for the full field
and assertion reference.

## Running

From the repo root:

```bash
# this skill, on every installed agent, graded by the default judge (claude)
python3 -m agentskill_evals run --skill sliderule-api

# specific agents, parallel, show failing checks
python3 -m agentskill_evals run --skill sliderule-api \
    --agents claude codex antigravity --jobs 4 -v
```

The runner provisions the skill into a hermetic per-agent workspace, captures the
tool trace + final answer, and grades each `rubric` item with the judge. Results
land in `agent-skill-evals/artifacts/<run_id>/`.

To measure the skill's *value*, re-run with `--no-provision` (skill absent) and
compare — per the best-practices doc, the with/without delta is the real signal.

## Test with weaker models

The best-practices doc recommends testing with Haiku in particular — it's the
most likely to skip SKILL.md guidance and answer from training data. A skill that
works on Haiku usually works everywhere:

```bash
python3 -m agentskill_evals run --skill sliderule-api --model claude=claude-haiku-4-5
```

## The evals

| File | Catches |
| ---- | ------- |
| [01-request-envelope-construction.yaml](01-request-envelope-construction.yaml) | A malformed request — wrong endpoint, missing the required `output` block, an open/clockwise polygon, or hitting the streaming path instead of `.arrow` |
| [02-operational-failure-fallback.yaml](02-operational-failure-fallback.yaml) | Mishandling HTTP 429/503/timeouts (no backoff / shrink), treating a valid zero-row response as an error, or not falling back to `atl03x` |
| [03-cross-skill-boundary-param-planning.yaml](03-cross-skill-boundary-param-planning.yaml) | Answering "which parameters / what default" itself instead of redirecting planning to `sliderule-params` and signatures/defaults to `sliderule-openapi` |
