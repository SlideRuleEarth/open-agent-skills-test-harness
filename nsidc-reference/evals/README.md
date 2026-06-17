# Evaluations

Evaluations for the `nsidc-reference` skill. Each `*.yaml` file is one eval,
loosely following the schema in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
and extended for the [`agent-skill-evals`](../../agent-skill-evals/) runner.

## Format

Each eval has:

- `name` — eval id
- `description` — what correct behavior looks like (shown to the LLM judge)
- `skills` — skills provisioned into the agent's workspace for the run
- `prompt` — the user prompt in natural language
- `files` — files the agent starts with (empty here; all state lives behind HTTPS)
- `rubric` — behaviors a correct answer must exhibit, graded by an LLM judge
- `assertions` — optional deterministic checks (filesystem, tool-call trace, …)

See the [runner README](../../agent-skill-evals/README.md) for the full field
and assertion reference.

## Running

From the repo root:

```bash
# this skill, on every installed agent, graded by the default judge (claude)
python3 -m agentskill_evals run --skill nsidc-reference

# specific agents, parallel, show failing checks
python3 -m agentskill_evals run --skill nsidc-reference \
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
python3 -m agentskill_evals run --skill nsidc-reference --model claude=claude-haiku-4-5
```

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
