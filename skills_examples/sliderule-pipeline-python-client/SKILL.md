---
name: sliderule-pipeline-python-client
description: >
  Behavioral directives for orchestrating SlideRule analyses as single-script
  pipelines using the SlideRule Python client (the `sliderule` package), and
  reporting task metrics. Use when the workflow goes through the Python client —
  `sliderule.init()` plus `icesat2.atl06p()` / `atl03x` / `gedi` request
  functions returning GeoDataFrames — and a multi-step workflow
  (fetch → parse → filter → aggregate → export) should be consolidated into one
  script execution rather than separate invocations. A SQL or pandas
  filtering/aggregation pass can be folded into the same pipeline script.
  Governs HOW the work is structured (single script, defensive coding, JSON
  export), HOW the executed script is surfaced (saved as a file and surfaced as
  a reproducible script), and HOW results are reported (task metrics). For
  pipelines that call the SlideRule HTTP API directly (requests + Parquet
  parsing, no `sliderule` package), use `sliderule-pipeline-direct-request`
  instead.
---

# SlideRule Pipeline Orchestration — Python Client

## Scope

This skill covers pipelines built on the **SlideRule Python client** — the
`sliderule` package (`sliderule.init()`, `icesat2.atl06p(parms)`, and friends),
which returns GeoDataFrames and handles request framing, retries, and response
decoding for you. If the user wants raw HTTP requests to the SlideRule service
(a `requests.post` envelope with Parquet parsing — e.g. to avoid the client
dependency), use the `sliderule-pipeline-direct-request` skill instead. When the
user doesn't specify an access method and the `sliderule` package is available
(or installable), prefer this client path — it is the supported, higher-level
interface.

## Requirements

- Python 3.8+
- Packages: `sliderule`, plus `pandas`/`geopandas` (pulled in by the client)
  and `duckdb` if the pipeline does in-script SQL
- Network access to `https://sliderule.slideruleearth.io` (the client's default)
- Platform-neutral — works with any agent that can execute Python scripts

See `CHANGELOG.md` for version history.

## Pipeline Approach — Single-Script Execution

Combine fetch → filter → aggregate → export into a **single Python script**
executed in one step. Every separate script invocation adds overhead — the agent
must process the output, decide on the next step, and issue another execution.
A single pipeline script should:

- **Call the client request function** (`sliderule.init()` once, then e.g.
  `gdf = icesat2.atl06p(parms)`) and extract result metadata
- **Filter/clean the data** (e.g., IQR outlier removal) — use explicit `float()`
  casts on numpy/pandas values to avoid JSON serialization errors with `float32`
- **Aggregate** (e.g., per-cycle, per-RGT/beam) and compute summary statistics
- **Export dashboard-ready JSON** with compact keys, subsampled photon arrays,
  and pre-computed metrics — all in one file
- **Print a concise summary** to stdout (row counts, height stats, timing) so the
  next step (chart or visualization creation) has what it needs

The client absorbs the request-framing and response-decoding failures that made
fragmented workflows expensive over raw HTTP. The residual risks with the client
are narrower — `float32` JSON serialization, algorithm-dependent columns, and
height-datum filter mistakes — and each is cheapest to catch inside the one
script (the explicit casts, inline schema print, and percentile check below)
rather than across separate invocations that each re-process prior output.

### Checkpoint the Fetch

The client call is the only expensive step — requests can take minutes — while
everything downstream is cheap pandas. Structure `pipeline.py` so a downstream
bug never re-pays the fetch: save `raw.parquet` to the run directory immediately
after `reset_index()`, and guard the client call with an existence check so a
rerun loads the checkpoint instead of re-fetching.

```python
raw_path = RUN_DIR / "raw.parquet"
if raw_path.exists():
    gdf = gpd.read_parquet(raw_path)    # rerun after a downstream fix: seconds
else:
    gdf = icesat2.atl06p(parms).reset_index()  # first run: minutes
    gdf.to_parquet(raw_path)
```

This keeps the pipeline a single reproducible file while making the
fix-and-rerun loop cost seconds instead of minutes. A fresh run directory (new
timestamp) always re-fetches, so cached data cannot silently leak across
studies.

## Surface the Pipeline Script as a Reproducible File

Save the consolidated pipeline as a named script **file** and surface that same
file to the user. Do not run code that lives only inside an ephemeral execution
call (an inline string or heredoc) — the user needs to see and rerun exactly what
produced their results.

The mechanism is the same in any agent that can write and execute files:

1. Create a run directory under `agents_outputs/` named from the agent name, a stable
   study slug, and a UTC timestamp, e.g.
   `agents_outputs/codex_lake_tahoe_atl13_20260526T154211Z/`.
   Use lowercase ASCII slugs with words separated by underscores. Start with the
   agent identifier (`codex`, `claude`, etc.) so runs can be compared across
   agents. Include enough study detail in the study slug to compare related runs
   later (`dickson_fjord_atl06`, `tahoe_atl13_summer_2024`, etc.).
2. Write the pipeline to that run directory as `pipeline.py`. All other artifacts
   from the run must go in the same run directory, with plots under `plots/` when
   there is more than one image.
3. **Execute that same file** (e.g.
   `python agents_outputs/codex_lake_tahoe_atl13_20260526T154211Z/pipeline.py`),
   not a separate heredoc or inline string. Running the saved file is what
   guarantees the surfaced script matches what actually ran, with no drift.
4. Surface that file to the user alongside the task summary and any visualization,
   using whatever file-presentation mechanism the host environment provides.

Run directories should be self-contained and comparison-friendly. Use these
artifact names unless the study needs a clearer domain-specific name:

```text
agents_outputs/<agent>_<study_slug>_<YYYYMMDDTHHMMSSZ>/
  prompt.md
  pipeline.py
  manifest.json
  results.json
  raw.parquet
  summary.csv
  plots/
```

`prompt.md` must contain the user's request verbatim, exactly as received (no
paraphrase, no agent commentary). Write it once at the start of the run so a
reader can compare the raw ask against the parameters the pipeline ended up using.
For autonomous runs with no user prompt, write a one-line note identifying the
trigger instead (cron, schedule, etc.).

`manifest.json` must list the agent identifier, study slug, timestamp, request
parameters, the client request function(s) called, generated files, task metrics,
software/library versions when available (`sliderule.version` and the package
version), and any cache/retry behavior. If a run does not produce a given
artifact type, omit it from the directory and manifest rather than creating
placeholders.

Prefer run-local paths over shared filenames. Do not overwrite artifacts from a
previous run. If rerunning the same study in the same second would collide, append
`_01`, `_02`, etc. to the run directory name.

This matters because SlideRule requests are parameter-heavy (polygon, time window,
algorithm block, filters) and can take minutes. A user who can open the script can
change one coordinate or date and rerun, instead of reconstructing the whole
request from the chat transcript — reproducibility is part of the deliverable, not
just the chart.

Keep the surfaced `pipeline.py` self-contained: imports, `sliderule.init()`, the
`parms` dict, the client call, filtering, aggregation, export, and the stdout
summary all in the one file. For multi-request workflows (e.g. comparing two time
windows), keep all requests in that single script so the one file reproduces the
entire comparison.

## Defensive Coding Tips

- Check the height distribution (percentiles) before applying physical filters —
  ICESat-2 heights are ellipsoidal (WGS84), not orthometric, so lake surfaces may
  be tens of meters different from commonly cited elevations.
- Always cast numpy scalars: `round(float(val), 4)` not `round(val, 4)`.
- Call `sliderule.init()` once at the top of the script; don't re-init per request.
- Wrap the full pipeline in try/except with informative error messages so a failure
  mid-script still reports what succeeded.
- **Only real returned rows reach the chart.** Subsample or aggregate
  to shrink the export; never pad, mock, or interpolate values to fill
  a sparse or empty result. If the request returned zero usable rows,
  export the metadata and the zero-row finding, not a placeholder
  series.
- **Critical: always `reset_index()` before saving parquet.** The client returns
  GeoDataFrames indexed by time. If you save with `to_parquet(path, index=False)`,
  the time column is silently dropped and you must re-fetch (which can take
  minutes). Call `gdf = gdf.reset_index()` immediately after the client call.
- **Print the schema inline.** Right after `reset_index()`, add
  `print("Columns:", gdf.columns.tolist())` so the pipeline's stdout includes the
  actual column names. This avoids a separate schema-inspection (`DESCRIBE`) step and catches
  API/algorithm-dependent column differences (e.g., PhoREAL vs. surface) early.
- An empty result comes back as an **empty GeoDataFrame**, not an error — check
  `len(gdf)` before filtering/aggregating and report the zero-row finding.

## Task Metrics

After every SlideRule client request, report a compact summary of task metrics.
Capture timing and data stats during execution and present them alongside (or
just before) the visualization.

### What to Capture

```python
import time
from sliderule import icesat2, sliderule

sliderule.init()

start = time.time()
gdf = icesat2.atl06p(parms)
elapsed = time.time() - start

gdf = gdf.reset_index()

metrics = {
    "request_function": "icesat2.atl06p",
    "request_duration_sec": round(elapsed, 1),
    "rows": len(gdf),
    "columns": len(gdf.columns),
    "memory_mb": round(float(gdf.memory_usage(deep=True).sum()) / 1e6, 2),
    "time_window": f"{parms.get('t0', '?')} to {parms.get('t1', '?')}",
}
```

### How to Report

> **SlideRule Task Summary**
> Request: `icesat2.atl06p` · Region: Adirondacks (0.15° × 0.15°)
> Time window: 2022-06-01 → 2022-09-01
> Request duration: 12.3 s · In-memory: 2.41 MB
> Rows: 48,217 · Columns: 22

Adapt to what's relevant — lake name for water studies, algorithm for canopy,
per-request timing for multi-request workflows. Always report duration and reason
even if a request fails or returns zero rows.

On a checkpointed rerun that loaded `raw.parquet` instead of calling the client,
report `loaded from raw.parquet (cached)` in place of the request duration —
never reuse a duration from a previous run or fabricate one.

For multiple client calls, also report total wall-clock time, per-request row
counts, and any throttle/retry behavior the client surfaced.
