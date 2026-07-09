# Simple A/B test: a prompt with and without a skill

This walks through the simplest useful experiment in the harness: take **one prompt** and ask
*"does this skill actually help?"* by running the same prompt, on the same model, **once with the
skill available and once without** — then comparing the two graded results.

You do it with a single [scenario](../../scenarios/README.md) file and two `run` commands. The only
thing that changes between the two runs is whether the skill is provisioned.

## The idea in one line

Isolation is **on by default**, so normal skill discovery sees only the skills the run provisions
(plus the agent's own vendor skills). That gives you a clean toggle:

| Arm | Command | What the model sees |
| --- | --- | --- |
| **With skill** (treatment) | `run --config <file>` | the scenario's skill(s) + vendor skills |
| **Without skill** (baseline) | `run --config <file> --no-provision` | no repo skills through normal discovery — just vendor skills |

Same prompt, same model, same rubric and assertions → the difference in the graded result is the
skill's contribution.

## 1. Write the scenario

Save a file under [`scenarios/`](../../scenarios/) — name it `<what>_on_<runner>-<model>.yaml` so
it's self-describing, e.g. `atl06-request_on_claude-haiku.yaml`:

```yaml
name: atl06-request A/B
description: Does sliderule-pipeline-direct_request help the model write a correct ATL06 request?

target:                         # pin ONE runner:model so both arms are comparable
  runner: claude
  model: claude-haiku-4-5       # cheapest claude model; omit → models.yaml default

skills:                         # the skill under test (provisioned in the "with" arm)
  - sliderule-pipeline-direct_request

# The user-supplied prompt. Keep it NEUTRAL — describe the task only, and do NOT
# reference {skills}. Both arms must get the identical prompt; if the prompt told the
# model to "use the sliderule-pipeline-direct_request skill", the baseline arm would point at a skill that
# isn't there and the comparison wouldn't be fair.
prompt: |
  Write a Python script run.py that uses SlideRule to fetch ATL06 land-ice elevations
  over a small region near Grand Mesa, Colorado, and writes the result to out.parquet.
  Keep it minimal and runnable.

# What "better" means — graded identically in both arms.
rubric:
  - Posts to a real SlideRule ATL06 endpoint (e.g. atl06p / atl06x) with a correct request envelope.
  - Defines a small region/polygon rather than guessing coordinates.
  - Writes the output to out.parquet.

assertions:
  - {type: file_exists, path: run.py}

isolated: true                  # keep the default — guarantees no globally-installed skill leaks in
```

> **Why the neutral prompt matters.** In the prompt, `{skills}` would expand to the skill's
> reference (e.g. `/sliderule-pipeline-direct_request`) in *both* arms — even the baseline, where the skill isn't
> provisioned. To keep the A/B honest, write the prompt as the task only and let the *presence of
> the skill* be the sole variable.

## 2. Preview both arms (no API cost)

`--dry-run` prints the plan and a **"Skills visible to the model"** block, so you can confirm the
toggle works before spending anything:

```bash
# with skill — should list sliderule-pipeline-direct_request under "provisioned"
agentskill-evals run --config scenarios/atl06-request_on_claude-haiku.yaml --dry-run

# without skill — "provisioned" is (none — provisioning off)
agentskill-evals run --config scenarios/atl06-request_on_claude-haiku.yaml --no-provision --dry-run
```

## 3. Run both arms

Give each a `--run-id` so the artifact directories are self-labeling:

```bash
# A — with the skill
agentskill-evals run --config scenarios/atl06-request_on_claude-haiku.yaml \
    --run-id ab-with-skill

# B — baseline, no skill
agentskill-evals run --config scenarios/atl06-request_on_claude-haiku.yaml \
    --no-provision --run-id ab-without-skill
```

Each is a single cell (one agent run + one judge call), so this is cheap. Run from the repo root
so the skills resolve; if you're in a source checkout without installing the CLI, use
`python3 -m agentskill_evals … --skills-root ..` instead.

## 4. Compare

Each run prints a pass/fail matrix and writes artifacts to
`artifacts/<run-id>/<runner>/<model>/scenario/<name>/`:

- `summary.md` / `summary.json` — the graded result (the same rubric items + assertions in both arms).
- `assertions.json` — per-rubric-item verdicts with the judge's reasoning, so you can read *why* each
  arm passed or failed.
- `result.json` and `workspace/` — the final answer and the script the agent actually wrote.

Put `ab-with-skill` next to `ab-without-skill` and the rubric pass-rates (and the judge's notes)
show what the skill changed.

## Tips

- **Keep it one model.** A/B is about the skill, not the model — pin a single cheap `target.model`.
- **Rubric carries the signal.** Deterministic assertions (e.g. `file_exists`) often pass in both
  arms; the *rubric* is usually where you'll see the skill help. Don't add `--no-judge` for an A/B.
- **CLI overrides the file.** Precedence is `CLI flag > scenario file > built-in default`, so you can
  retarget without editing the file, e.g. `--model claude=claude-opus-4-8` to A/B on a stronger model.
- **Want a multi-skill combination instead?** List several skills under `skills:` — the "with" arm
  then provisions the whole set together. See [scenarios/README.md](../../scenarios/README.md).

See [FAQ.md](../FAQ.md) for how skill visibility / isolation works, and
[README.md](../README.md) for the full harness reference.
