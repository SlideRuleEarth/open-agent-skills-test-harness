# Test a combination of skills

A per-skill eval ([`<skill>/evals/`](../../scenarios/README.md)) tests one skill in isolation. But
skills are often used *together* — e.g. `sliderule-region-picker` helps define the analysis region
and `sliderule-pipeline-direct_request` consolidates the work into a single reproducible script. A
**[scenario](../../scenarios/README.md)** is the tool for testing that: it provisions a
**combination of skills together** against one target (`runner:model`), from a single
self-describing file.

## The idea in one line

List several skills under a scenario's `skills:` block. They're all provisioned into the run, and —
because isolation is on by default — they're the only repo skills exposed through normal skill
discovery. So the run tests that combination working in concert.

## 1. Write the scenario

Save a file under [`scenarios/`](../../scenarios/), named `<what>_on_<runner>-<model>.yaml`, e.g.
`atl06-pipeline_on_claude-haiku.yaml`:

```yaml
name: region + pipeline combination
description: Do the region-picker and pipeline skills work together to produce a clean, reproducible analysis script?

target:
  runner: claude
  model: claude-haiku-4-5        # cheapest claude model; omit → models.yaml default

skills:                          # provisioned TOGETHER; the only repo skills visible (isolated)
  - sliderule-region-picker
  - sliderule-pipeline-direct_request

prompt: |
  Using {skills}, write a Python script run.py that submits an ATL06 request over a small
  region with a couple of non-default processing parameters, and writes the result to
  out.parquet. Keep it minimal and runnable.

# Rubric items that check the HANDOFF between skills — that each one pulled its weight.
rubric:
  - Defines the analysis region up front (region-picker skill) rather than guessing coordinates.
  - Consolidates the work into a single reproducible script (pipeline skill) instead of scattered snippets.
  - Builds and posts a correct request envelope to an ATL06 endpoint.
  - Writes the output to out.parquet.

assertions:
  - {type: file_exists, path: run.py}

isolated: true                   # keep the default — only these two skills are visible
```

> `{skills}` expands to both skill references (e.g. `/sliderule-region-picker, /sliderule-pipeline-direct_request`
> on Claude), so the prompt can name them as a set. If you'd rather the model *discover* which
> skills to reach for, drop `{skills}` and write the task plainly.

## 2. Preview (no API cost)

`--dry-run` prints the plan and a **"Skills visible to the model"** block — confirm both show
up under `provisioned` and that other repo skills are masked:

```bash
agentskill-evals run --config scenarios/atl06-pipeline_on_claude-haiku.yaml --dry-run
```

## 3. Run it

```bash
agentskill-evals run --config scenarios/atl06-pipeline_on_claude-haiku.yaml
```

It's a single cell (one agent run + one judge call). Run from the repo root so the skills resolve;
from an uninstalled source checkout use `python3 -m agentskill_evals … --skills-root ..`.

## 4. Read the result

Artifacts land under `artifacts/<run_id>/<runner>/<model>/scenario/<name>/`:

- `summary.md` / `summary.json` — the graded result.
- `assertions.json` — per-rubric-item verdicts with the judge's reasoning, so you can see whether
  each skill in the combination actually got used.
- `result.json` and `workspace/` — the final answer and the `run.py` the agent wrote.

## Tips

- **Rubric carries the signal.** Write rubric items per skill (one for defining the region, one for the
  single-script structure) so a failure tells you *which* skill in the combination was
  ignored.
- **Does the combination beat a subset?** A/B it: run the full set, then run again with a trimmed
  `skills:` list (or `--no-provision` for none) and compare. See
  [Simple-A-B-Test.md](Simple-A-B-Test.md).
- **CLI overrides the file.** Precedence is `CLI flag > scenario file > built-in default`, so
  `--model claude=claude-opus-4-8` or `--no-isolated` retarget the run without editing the file.
- **Scenarios are ad-hoc.** They aren't auto-discovered like per-skill evals — you always run one by
  path with `--config`.

See [scenarios/README.md](../../scenarios/README.md) for the scenario format, [FAQ.md](../FAQ.md) for
how skill visibility / isolation works, and [README.md](../README.md) for the full harness reference.
