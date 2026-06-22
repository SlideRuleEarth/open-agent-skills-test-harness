# Evaluations

Evaluations for the `nsidc-reference` skill. Each `*.yaml` file is one eval,
loosely following the schema in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
and extended for the [`harness`](../../harness/) runner.

## Format

Each eval has:

- `name` — eval id
- `description` — what correct behavior looks like (shown to the LLM judge)
- `skills` — skills provisioned into the agent's workspace for the run
- `prompt` — the user prompt in natural language
- `files` — files the agent starts with (empty here; all state lives behind HTTPS)
- `rubric` — behaviors a correct answer must exhibit, graded by an LLM judge
- `assertions` — optional deterministic checks (filesystem, tool-call trace, …)

See the [runner README](../../harness/README.md) for the full field
and assertion reference.

## Running

From the repo root (install the `agentskill-evals` CLI first — see the [runner README](../../harness/README.md#install)):

```bash
# this skill, on every installed agent, graded by the default judge (claude)
agentskill-evals run --skill nsidc-reference

# specific agents, parallel, show failing checks
agentskill-evals run --skill nsidc-reference \
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
agentskill-evals run --skill nsidc-reference

# compare specific models — Haiku is the canonical weak-model probe (most likely to skip
# SKILL.md and answer from training; if it works on Haiku it usually works everywhere)
agentskill-evals run --skill nsidc-reference --model claude=claude-opus-4-8,claude-haiku-4-5

# the full models.yaml grid (opt-in; prompts to confirm, bounded by --max-cells)
agentskill-evals run --skill nsidc-reference --all-models
```

See the [harness README](../../harness/README.md#cross-model-testing) for cost
guardrails and how the model list is maintained.

## The evals

| File | Catches |
| ---- | ------- |
| [01-algorithm-prefer-atbd.yaml](01-algorithm-prefer-atbd.yaml) | Citing a user-guide chunk for an algorithm-theory question instead of preferring the matching ATBD |
| [02-variable-prefer-userguide.yaml](02-variable-prefer-userguide.yaml) | Citing an ATBD chunk for a variable-definition question instead of preferring the user guide where data formats live |
| [03-cross-skill-boundary-docsearch.yaml](03-cross-skill-boundary-docsearch.yaml) | Answering SlideRule API / how-to questions with NSIDC science prose instead of redirecting to `sliderule-docsearch` |
| [04-cross-skill-boundary-schema.yaml](04-cross-skill-boundary-schema.yaml) | Answering structured SlideRule output-schema questions from NSIDC docs instead of redirecting to `sliderule-openapi` |
| [05-general-concept-skip.yaml](05-general-concept-skip.yaml) | Searching nsidc-reference for general concepts (file formats, geospatial primitives) that belong to training |
| [06-page-citation.yaml](06-page-citation.yaml) | Citing only the PDF URL without a page number — users want to find the source paragraph in a 200-page document |
| [07-source-product-routing.yaml](07-source-product-routing.yaml) | Citing chunks from the wrong product when the query names a specific product (e.g., ATL08 query lands on ATL03 chunks) |
