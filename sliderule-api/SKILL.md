---
name: sliderule-api
description: >
  HTTP mechanics for the SlideRule Earth processing API. Activates when
  the agent is ready to build and execute a POST request to /source/{api}.arrow
  after the request has been planned via the `sliderule-params` skill.
  Covers the request envelope, endpoint catalog, required output block,
  polygon format, operational failure modes (HTTP 429/503, timeouts), and
  when to fall back between APIs.
---

# SlideRule HTTP API

## Requirements

Requires Python 3.8+, network access to `sliderule.slideruleearth.io`
(Processing API), and the `pyarrow` and `requests` packages. Schema
lookups delegate to the `sliderule-openapi` skill.

See `CHANGELOG.md` for version history.

This skill handles request mechanics only. Request *content* (which
parameters to set, which algorithm block to include, how to configure
filters) comes from the `sliderule-params` skill. Do not build a
request without consulting `sliderule-params` first.

## Endpoints

This skill handles **only** the Processing API. Schema lookups —
parameter definitions, output column schemas, ancillary field-name
enumeration — belong to the `sliderule-openapi` skill, which loads the
bundled OpenAPI 3.1 specification. Do not load the spec directly from
this skill.

| Service | Base URL | Used for |
|---|---|---|
| **Processing API** | `https://sliderule.slideruleearth.io` | `POST /source/{api}.arrow` requests; `/source/version`, `/source/health` info endpoints |

For schema lookups (per-endpoint request parameters, output columns,
and ancillary field-name enumeration), use the `sliderule-openapi`
skill.

## Quick Reference (Processing API)

| Item | Value |
|---|---|
| Base URL | `https://sliderule.slideruleearth.io` |
| Endpoint pattern | `POST /source/{api}.arrow` |
| Request body | `{"parms": { ... }}` |
| Response format | Apache Parquet (binary) |
| Auth | None required for public cluster |

## Critical: Use the `.arrow` suffix for Processing

The Processing API exposes APIs at `/source/{api}` with two output modes
selected by suffix:

- **`/source/{api}.arrow`** — Returns a complete GeoParquet file. This
  is the path to use.
- `/source/{api}` (no suffix) — Uses a custom streaming binary protocol.
  Does NOT work from sandboxed environments or HTTP proxies that drop
  chunked transfer-encoded POST responses. Avoid.

Info endpoints use the same `/source/` prefix with GET and do work:
`/source/version`, `/source/health`.

## Schema Lookups

Schema lookups (what parameters an endpoint accepts, what columns it
returns, what fields a `*_fields` selector exposes) are **not** this
skill's responsibility — they belong to the `sliderule-openapi` skill,
which loads the bundled OpenAPI 3.1 specification. Whenever you need
schema facts while building or debugging a request, invoke
`sliderule-openapi` — don't load the spec directly from this skill.

## Available APIs

| API | Endpoint | Description |
|---|---|---|
| `atl03x` | `/source/atl03x.arrow` | Photon-level data with optional algorithms (fit, phoreal, yapc, atl24, atl13). The most flexible ICESat-2 API. |
| `atl06x` | `/source/atl06x.arrow` | Land ice surface elevation segments. |
| `atl08x` | `/source/atl08x.arrow` | Land and vegetation height segments. |
| `atl13x` | `/source/atl13x.arrow` | Inland water surface height. |
| `atl24x` | `/source/atl24x.arrow` | Coastal bathymetry classified photons. |
| `gedil4ax` | `/source/gedil4ax.arrow` | GEDI L4A footprint biomass. |

When in doubt about which API to use, `atl03x` is the most flexible —
it returns raw photons by default and accepts pluggable algorithm
blocks. The higher-level APIs are stricter about what qualifies as
valid data and may return zero rows where `atl03x` would return
millions of photons.

## Making a Request

```python
import requests, io
import pyarrow.parquet as pq

url = "https://sliderule.slideruleearth.io/source/atl03x.arrow"  # Processing API
req = {"parms": {
    # ... parameters from the `sliderule-params` planning output
    "output": {"format": "parquet", "as_geo": False, "with_checksum": False}
}}

r = requests.post(url, json=req, timeout=300)
r.raise_for_status()
table = pq.read_table(io.BytesIO(r.content))
df = table.to_pandas().reset_index()  # SlideRule returns time_ns as the index; reset_index recovers it as a column
```

### Required output block

Always include this in `parms`:

```json
"output": {
  "format": "parquet",
  "as_geo": false,
  "with_checksum": false
}
```

### Polygon format

Counter-clockwise array of `{lon, lat}` objects. First and last point
must match (closed ring).

```json
"poly": [
  {"lon": -120.15, "lat": 38.95},
  {"lon": -120.00, "lat": 38.95},
  {"lon": -120.00, "lat": 39.10},
  {"lon": -120.15, "lat": 39.10},
  {"lon": -120.15, "lat": 38.95}
]
```

Keep polygons small for initial exploration — a 0.2° × 0.2° box is a
good starting point. Larger regions with broad time windows can return
hundreds of millions of photons and take minutes to process. For
interactive region drawing, use the `sliderule-region-picker` skill.

## Operational Failure Modes

**HTTP 429 (rate limited).** The server is throttling requests. The
response body may include a wait time before the throttle releases. If
a wait time is given, report it to the user and ask whether to wait or
stop. If no timing hint is present, wait 30–60 seconds and retry with a
smaller request.

**HTTP 503 (server busy).** The server is overloaded or temporarily
unavailable. Wait 30–60 seconds and retry. If it persists, try a
smaller request.

**In both cases**, favor reducing region size rather than time filter
for studies that are temporal.

**Timeouts.** Large requests can take 30–120 seconds. Set `timeout=300`
in Python `requests.post()`, or `--max-time 300` in curl.

**Zero rows with valid schema.** Not a failure — the algorithm ran but
found no qualifying data. Fall back to `atl03x` with no algorithm block
to inspect raw photons, or relax quality filters.

## Health Check

Before complex queries, especially after errors (Processing API):

```python
import requests
requests.get("https://sliderule.slideruleearth.io/source/version").text
requests.get("https://sliderule.slideruleearth.io/source/health").text
```

OpenAPI spec sanity check — invoke the `sliderule-openapi` skill with
no arguments; it loads the spec and prints a slim index of endpoints,
tags, and schema names to stdout. A successful load confirms the spec
source (URL or local file) is reachable and parseable.

## Workflow Position

1. **Region defined** — via coordinates or `sliderule-region-picker`.
2. **Request planned** — via `sliderule-params`, which consults
   `sliderule-openapi` for parameter definitions and defaults, plus
   the curated `parameter_couplings.md` reference for known couplings.
   Produces the `parms` dict.
3. **Request executed** — this skill. POST to the Processing API's
   `/source/{api}.arrow`.
4. **Response parsed** — read the Parquet into a DataFrame, extract the
   `meta`/`sliderule`/`recordinfo` metadata blocks from the schema, and
   resolve column meanings via `sliderule-openapi`.
5. **Optional SQL/pandas pass** — filter, aggregate, or join the Parquet
   (DuckDB or pandas) before charting.
6. **Optional orchestration** — `sliderule-pipeline` combines steps
   3–5 into a single script with task-metrics reporting.

For scientific meaning of values in the response, consult
`nsidc-reference`.
