# Evaluations

Data-driven evaluations for the `sliderule-docsearch` skill, following
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
| [01-identifier-disambiguation.json](01-identifier-disambiguation.json) | Confusing `atl06x` (X-Series) with `atl06p` (legacy) when retrieval surfaces both |
| [02-cross-skill-boundary-schema.json](02-cross-skill-boundary-schema.json) | Answering parameter-default / output-schema questions with docsearch prose instead of redirecting to `sliderule-openapi` |
| [03-cross-skill-boundary-science.json](03-cross-skill-boundary-science.json) | Answering science / physical-meaning questions with docsearch prose instead of redirecting to `nsidc-reference` |
| [04-general-concept-skip.json](04-general-concept-skip.json) | Searching docsearch for general concepts (file formats, geospatial primitives) that belong to training |
| [05-multi-section-page.json](05-multi-section-page.json) | Citing a chunk from the wrong section of a multi-section page (`user_guide/icesat2.html`) just because the URL matches |
| [06-conceptual-why-use.json](06-conceptual-why-use.json) | Failing to synthesize a narrative answer from multiple chunks for a comparative "why use X over Y" question |
| [07-cross-skill-required-pairings.json](07-cross-skill-required-pairings.json) | Recommending a parameter (e.g. `atl08_class`) without surfacing its hard required-pairings constraints — carried in `sliderule-openapi`'s `parameter_couplings.md`, not in SKILL.md routing examples |
| [08-conceptual-yapc-vs-native.json](08-conceptual-yapc-vs-native.json) | Picking one classifier as "the answer" on a comparative `yapc` vs native `cnf` question, instead of synthesizing the trade-off across multiple chunks |
