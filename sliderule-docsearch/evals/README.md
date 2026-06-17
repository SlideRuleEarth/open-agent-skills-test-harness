# Evaluations

Evaluations for the `sliderule-docsearch` skill. Each `*.yaml` file is one eval,
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
python3 -m agentskill_evals run --skill sliderule-docsearch

# specific agents, parallel, show failing checks
python3 -m agentskill_evals run --skill sliderule-docsearch \
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
python3 -m agentskill_evals run --skill sliderule-docsearch --model claude=claude-haiku-4-5
```

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
