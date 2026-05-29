# Evaluations

Data-driven evaluations for the `nsidc-reference` skill, following
the schema described in Anthropic's
[Agent Skills best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices).

Each `*.json` file is a single eval with four fields:

- `skills` — which skills are loaded for the run
- `query` — the user prompt in natural language
- `files` — files the agent starts with in context (empty for this skill;
  all state lives behind HTTPS)
- `expected_behavior` — a list of behaviors a correct answer must exhibit

Anthropic's docs are explicit that **there is no built-in runner** — see
the "Evaluation Structure" section of the best-practices page:

> There is not currently a built-in way to run these evaluations. Users
> can create their own evaluation system.

So these files are author-facing rubrics, not a CI test suite. To grade:

1. Start a Claude session with `skills` loaded and only those.
2. Paste `query` as the first user message.
3. Observe tool calls + final response.
4. Check every `expected_behavior` entry against what Claude did.
5. Optionally re-run the same query **without** the skill loaded and compare
   — per the doc, the with/without delta is the real measure of skill value.

## Test with weaker models

The best-practices doc recommends testing with Haiku in particular — it's
the most likely to skip SKILL.md guidance and answer from training data.
A skill that works on Haiku usually works everywhere.

## The evals

| File | Catches |
| ---- | ------- |
| [01-algorithm-prefer-atbd.json](01-algorithm-prefer-atbd.json) | Citing a user-guide chunk for an algorithm-theory question instead of preferring the matching ATBD |
| [02-variable-prefer-userguide.json](02-variable-prefer-userguide.json) | Citing an ATBD chunk for a variable-definition question instead of preferring the user guide where data formats live |
| [03-cross-skill-boundary-docsearch.json](03-cross-skill-boundary-docsearch.json) | Answering SlideRule API / how-to questions with NSIDC science prose instead of redirecting to `sliderule-docsearch` |
| [04-cross-skill-boundary-schema.json](04-cross-skill-boundary-schema.json) | Answering structured SlideRule output-schema questions from NSIDC docs instead of redirecting to `sliderule-openapi` |
| [05-general-concept-skip.json](05-general-concept-skip.json) | Searching nsidc-reference for general concepts (file formats, geospatial primitives) that belong to training |
| [06-page-citation.json](06-page-citation.json) | Citing only the PDF URL without a page number — users want to find the source paragraph in a 200-page document |
| [07-source-product-routing.json](07-source-product-routing.json) | Citing chunks from the wrong product when the query names a specific product (e.g., ATL08 query lands on ATL03 chunks) |
