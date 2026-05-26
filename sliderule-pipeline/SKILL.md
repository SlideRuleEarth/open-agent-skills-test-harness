---
name: sliderule-pipeline
description: >
  Behavioral directives for orchestrating SlideRule analyses as single-script
  pipelines and reporting task metrics. Use when consolidating a multi-step
  SlideRule workflow (fetch → parse → filter → aggregate → export) into one
  script execution rather than separate invocations; pairs with sliderule-api.
  A SQL or pandas filtering/aggregation pass can be folded into the same
  pipeline script. Governs HOW the work is structured (single script, defensive coding,
  JSON export), HOW the executed script is surfaced (saved as a file and surfaced
  as a reproducible script), and HOW results are reported (task metrics). Also
  trigger when the user asks about pipeline patterns, execution efficiency, task
  reporting format, or wants to see, save, or reproduce the exact script that was
  run.
compatibility: >
  No additional dependencies beyond those required by sliderule-api.
  Platform-neutral — works with any agent that can execute Python scripts.
metadata:
  author: cugarteblair
  version: "1.4"
---

# SlideRule Pipeline Orchestration

## Pipeline Approach — Single-Script Execution

Combine fetch → parse → filter → aggregate → export into a **single Python script**
executed in one step. Every separate script invocation adds overhead — the agent
must process the output, decide on the next step, and issue another execution.
A single pipeline script should:

- **POST the API request**, parse the Parquet response, and extract metadata
- **Filter/clean the data** (e.g., IQR outlier removal) — use explicit `float()`
  casts on numpy/pandas values to avoid JSON serialization errors with `float32`
- **Aggregate** (e.g., per-cycle, per-RGT/beam) and compute summary statistics
- **Export dashboard-ready JSON** with compact keys, subsampled photon arrays,
  and pre-computed metrics — all in one file
- **Print a concise summary** to stdout (row counts, height stats, timing) so the
  next step (chart or visualization creation) has what it needs

This avoids the common failure pattern of: script #1 fetches data → script #2 tries
a filter with wrong assumptions → script #3 fixes a serialization bug → script #4
re-exports. Each failed iteration wastes an execution cycle and forces the agent to
re-process prior output.

## Surface the Pipeline Script as a Reproducible File

Save the consolidated pipeline as a named script **file** and surface that same
file to the user. Do not run code that lives only inside an ephemeral execution
call (an inline string or heredoc) — the user needs to see and rerun exactly what
produced their results.

The mechanism is the same in any agent that can write and execute files:

1. Write the pipeline to a study-named file in your environment's persistent
   output location, e.g. a file named `sliderule_pipeline_lake_tahoe.py`.
2. **Execute that same file** (e.g. `python sliderule_pipeline_lake_tahoe.py`),
   not a separate heredoc or inline string. Running the saved file is what
   guarantees the surfaced script matches what actually ran, with no drift.
3. Surface that file to the user alongside the task summary and any visualization,
   using whatever file-presentation mechanism the host environment provides.

This matters because SlideRule requests are parameter-heavy (polygon, time window,
algorithm block, filters) and can take minutes. A user who can open the script can
change one coordinate or date and rerun, instead of reconstructing the whole
request from the chat transcript — reproducibility is part of the deliverable, not
just the chart.

Keep the surfaced file self-contained: imports, the `parms` dict, the POST,
parsing, filtering, aggregation, export, and the stdout summary all in the one file.
For multi-request workflows (e.g. comparing two time windows), keep all requests in
that single script so the one file reproduces the entire comparison.

## Defensive Coding Tips

- Check the height distribution (percentiles) before applying physical filters —
  ICESat-2 heights are ellipsoidal (WGS84), not orthometric, so lake surfaces may
  be tens of meters different from commonly cited elevations.
- Always cast numpy scalars: `round(float(val), 4)` not `round(val, 4)`.
- Set `timeout=300` on all `requests.post()` calls.
- Wrap the full pipeline in try/except with informative error messages so a failure
  mid-script still reports what succeeded.
- **Only real returned rows reach the chart.** Subsample or aggregate
  to shrink the export; never pad, mock, or interpolate values to fill
  a sparse or empty result. If the request returned zero usable rows,
  export the metadata and the zero-row finding, not a placeholder
  series.
- **Critical: always `reset_index()` before saving parquet.** SlideRule returns
  `time_ns` as the DataFrame index. If you save with `to_parquet(path, index=False)`,
  the time column is silently dropped and you must re-fetch (which can take minutes).
  Always call `df = df.reset_index()` immediately after `table.to_pandas()`.
- **Print the schema inline.** Right after `reset_index()`, add
  `print("Columns:", df.columns.tolist())` so the pipeline's stdout includes the
  actual column names. This avoids a separate schema-inspection (`DESCRIBE`) step and catches
  API/algorithm-dependent column differences (e.g., PhoREAL vs. surface) early.

## Task Metrics

After every SlideRule API request, report a compact summary of task metrics.
Capture timing and data stats during execution and present them alongside (or
just before) the visualization.

### What to Capture

```python
import time

start = time.time()
r = requests.post(url, json=req, timeout=300)
elapsed = time.time() - start

table = pq.read_table(io.BytesIO(r.content))
df = table.to_pandas()
meta = json.loads(table.schema.metadata[b'meta'])

metrics = {
    "api": api_name,
    "request_duration_sec": round(elapsed, 1),
    "response_size_mb": round(len(r.content) / 1e6, 2),
    "rows": len(df),
    "columns": len(df.columns),
    "granules_processed": len(meta.get("srctbl", {})),
    "time_window": f"{parms.get('t0', '?')} to {parms.get('t1', '?')}",
}
```

### How to Report

> **SlideRule Task Summary**
> API: `atl03x` (PhoREAL) · Region: Adirondacks (0.15° × 0.15°)
> Time window: 2022-06-01 → 2022-09-01
> Request duration: 12.3 s · Response: 2.41 MB
> Rows: 48,217 · Columns: 22 · Granules: 14

Adapt to what's relevant — lake name for water studies, algorithm for canopy,
per-request timing for multi-request workflows. Always report duration and reason
even if a request fails or returns zero rows.

For multiple API calls, also report total wall-clock time, per-request row counts,
and any throttle encounters (HTTP 429/503).
