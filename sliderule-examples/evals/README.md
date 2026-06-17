# Evaluations

Evaluations for the `sliderule-examples` skill. Each `*.yaml` file is one eval,
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
python3 -m agentskill_evals run --skill sliderule-examples

# specific agents, parallel, show failing checks
python3 -m agentskill_evals run --skill sliderule-examples \
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
python3 -m agentskill_evals run --skill sliderule-examples --model claude=claude-haiku-4-5
```

## The evals

| File | Catches |
| ---- | ------- |
| [01-cite-real-notebook.yaml](01-cite-real-notebook.yaml) | Answering "show me an example of `atl06p`" from training instead of reading `references/index.md` → the matching file and citing the real notebook |
| [02-no-example-no-fabrication.yaml](02-no-example-no-fabrication.yaml) | Inventing a notebook when none matches (there is no `atl08p` example) instead of reporting that none exists |
| [03-cross-skill-boundary-parameter-docs.yaml](03-cross-skill-boundary-parameter-docs.yaml) | Treating an example's `cnf` value as authoritative for a parameter-docs question instead of routing to `sliderule-openapi` |
