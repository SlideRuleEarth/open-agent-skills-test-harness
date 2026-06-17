# Evaluations

Evaluations for the `sliderule-openapi` skill. Each `*.yaml` file is one eval,
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
python3 -m agentskill_evals run --skill sliderule-openapi

# specific agents, parallel, show failing checks
python3 -m agentskill_evals run --skill sliderule-openapi \
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
python3 -m agentskill_evals run --skill sliderule-openapi --model claude=claude-haiku-4-5
```

## The evals

| File | Catches |
| ---- | ------- |
| [01-required-pairing-coupling.yaml](01-required-pairing-coupling.yaml) | Presenting `atl08_class` in isolation instead of surfacing its hard required pairings from `references/parameter_couplings.md` |
| [02-cross-skill-boundary-narrative.yaml](02-cross-skill-boundary-narrative.yaml) | Slicing the spec to answer how/why narrative instead of routing it to `sliderule-docsearch` (and physical meaning to `nsidc-reference`) |
| [03-datum-orthometric-route.yaml](03-datum-orthometric-route.yaml) | Mislabeling `use_abs_h` output as orthometric (it's ellipsoidal) and missing that orthometric output needs a `datum`/geoid conversion |
