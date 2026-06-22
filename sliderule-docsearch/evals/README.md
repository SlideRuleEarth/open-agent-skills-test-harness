# Evaluations

Evaluations for the `sliderule-docsearch` skill. Each `*.yaml` file is one eval,
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
agentskill-evals run --skill sliderule-docsearch

# specific agents, parallel, show failing checks
agentskill-evals run --skill sliderule-docsearch \
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
agentskill-evals run --skill sliderule-docsearch

# compare specific models — Haiku is the canonical weak-model probe (most likely to skip
# SKILL.md and answer from training; if it works on Haiku it usually works everywhere)
agentskill-evals run --skill sliderule-docsearch --model claude=claude-opus-4-8,claude-haiku-4-5

# the full models.yaml grid (opt-in; prompts to confirm, bounded by --max-cells)
agentskill-evals run --skill sliderule-docsearch --all-models
```

See the [harness README](../../harness/README.md#cross-model-testing) for cost
guardrails and how the model list is maintained.

## The evals

| File | Catches |
| ---- | ------- |
| [01-identifier-disambiguation.yaml](01-identifier-disambiguation.yaml) | Confusing `atl06x` (X-Series) with `atl06p` (legacy) when retrieval surfaces both |
| [02-cross-skill-boundary-schema.yaml](02-cross-skill-boundary-schema.yaml) | Answering parameter-default / output-schema questions with docsearch prose instead of redirecting to `sliderule-openapi` |
| [03-cross-skill-boundary-science.yaml](03-cross-skill-boundary-science.yaml) | Answering science / physical-meaning questions with docsearch prose instead of redirecting to `nsidc-reference` |
| [04-general-concept-skip.yaml](04-general-concept-skip.yaml) | Searching docsearch for general concepts (file formats, geospatial primitives) that belong to training |
| [05-multi-section-page.yaml](05-multi-section-page.yaml) | Citing a chunk from the wrong section of a multi-section page (`user_guide/icesat2.html`) just because the URL matches |
| [06-conceptual-why-use.yaml](06-conceptual-why-use.yaml) | Failing to synthesize a narrative answer from multiple chunks for a comparative "why use X over Y" question |
| [07-cross-skill-required-pairings.yaml](07-cross-skill-required-pairings.yaml) | Recommending a parameter (e.g. `atl08_class`) without surfacing its hard required-pairings constraints — carried in `sliderule-openapi`'s `parameter_couplings.md` |
| [08-conceptual-yapc-vs-native.yaml](08-conceptual-yapc-vs-native.yaml) | Picking one classifier as "the answer" on a comparative `yapc` vs native `cnf` question, instead of synthesizing the trade-off |
