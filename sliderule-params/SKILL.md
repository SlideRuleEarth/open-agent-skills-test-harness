---
name: sliderule-params
description: >
  Plan and configure SlideRule API request parameters through systematic
  reasoning about study type, signal characteristics, and parameter
  interactions. Use this skill BEFORE constructing any SlideRule request —
  it forces a deliberate planning phase that prevents default-driven mistakes,
  with a fast-path triage so unambiguous requests skip the deep reference reads.
  Trigger whenever a SlideRule analysis is being set up, when the user asks
  about available parameters, when tuning a request beyond defaults, when
  asking "what options do I have", "how do I filter", "what algorithms are
  available", or when choosing between APIs for a given study. This skill is
  the planning companion to sliderule-api (HTTP mechanics) and
  sliderule-pipeline (execution). Always consult this skill alongside
  sliderule-api for any processing request.
compatibility: >
  Requires the `sliderule-openapi` skill as the transport for schema
  lookups (which loads a bundled OpenAPI spec from an HTTPS URL or a
  local file).
metadata:
  author: cugarteblair
  version: "2.6"
  source: https://sliderule.slideruleearth.io/openapi/sliderule.json
---

# SlideRule Request Parameter Planning

## Sources

Two sources sit behind this skill. Keep them distinct.

- **OpenAPI specification** — accessed through the
  **`sliderule-openapi` skill**, never loaded directly from this
  skill. That's where every parameter name, type, default, and
  valid-value list lives. Parameter *couplings* (which aren't
  expressed in the OpenAPI spec) live in `sliderule-openapi`'s
  curated `references/parameter_couplings.md`. Subcommand
  references below (`openapi.py endpoint atl06x`, etc.) are
  arguments to `sliderule-openapi`'s `scripts/openapi.py`.
- **Processing API:** `https://sliderule.slideruleearth.io` — runs
  requests against `/source/{api}.arrow`. Not used by this skill directly;
  it's the target of the request that planning produces. See
  `sliderule-api` for HTTP mechanics.

## Why This Skill Exists

The model already knows remote sensing and ICESat-2 science from training.
The OpenAPI spec (fetched via `sliderule-openapi`) carries parameter
names, types, defaults, valid values, and applicability per endpoint.
Parameter couplings live in `sliderule-openapi`'s
`references/parameter_couplings.md`. What none of those carry — and what
this skill provides — is the *reasoning sequence*: how to walk from a
user's study intent to a complete, well-tuned request, plus the
judgment of how much of that sequence a given request actually needs.
Triage first (next section); for non-trivial requests, walk the phases
and don't skip the sequence.

## Fast Path vs. Full Planning

Most requests are unambiguous and need a thin slice of this skill, not
the whole reference shelf. Reading every reference file before a request
fires is wasted context. Triage first.

**Fast path** — take it when *none* of the triggers below fire. Map
intent to an endpoint (Phase 1 table), set region / time / beams from
the request, add the `output` block (Phase 7), and fire. No
reference-file reads and no `openapi` lookups beyond a default you
genuinely don't know. Issuing the request still needs `sliderule-api`
for HTTP mechanics; `sliderule-pipeline` is a write-time concern (how to
structure the execution script), not a planning prerequisite.

**Open one reference only when its trigger is present:**

| Open / look up | Only when the request involves |
|---|---|
| `parameter_couplings.md` | `atl08_class`, `atl24`, or `atl13` (silent wrong/empty-output pairings — required, not optional); combining algorithms; or tuning `res`/`len`/`cnt` off defaults |
| `elevation_datums.md` | orthometric, MSL, "above sea level", geoid/tide correction, a named `datum`, or relief-vs-absolute heights |
| `algorithms.md` | configuring an `atl03x` algorithm block and unsure of its output effect |
| `samples_shape.md` | raster co-sampling via `samples` |
| `field_selectors/<sel>.json` | ancillary `*_fields` selectors |
| `openapi.py param`/`schema`/`applies-to` | you need an exact default, valid-value list, or endpoint acceptance you don't already know |

A trigger firing takes you off the fast path for *that one concern* —
open the single file it points to, not the whole shelf.

**Full planning** — walk all seven phases, expecting reference reads —
when the study is novel or ambiguous, several features are combined, a
datum/orthometric requirement is in play, or a prior request came back
empty or wrong.

The classifier pairings (`atl08_class`, `atl24`, `atl13`) and the datum
two-axis trap are the failures worth the read. Absent those, default to
fast — most requests have no landmine.

**Maintaining the trigger table.** The fast path is only as safe as this
list is complete — it covers the silent-failure modes this suite has
actually hit. When a test run surfaces a new one, add a row keyed to
that failure rather than widening the fast path past an untested
coupling.

## Where the Facts Live

Every parameter name, default, and valid value is documented in the
OpenAPI spec. **Look up parameters via the `sliderule-openapi` skill** —
it loads the spec, slices the relevant fragment, and pretty-prints the
result. Do not rely on memory.

Subcommands worth invoking during planning (arguments to
`sliderule-openapi`'s `scripts/openapi.py`):

| Subcommand | What it returns |
|---|---|
| `openapi.py` (no args) | Endpoint catalog with request/response schema names — start here when cold. |
| `openapi.py applies-to <endpoint>` | Flat parameter list for one endpoint with one-line descriptions — the typical starting point once an endpoint is chosen. |
| `openapi.py endpoint <endpoint>` | Full request + response slice for one endpoint, including all transitively-referenced component schemas. |
| `openapi.py param <name>` | Cross-schema parameter lookup — useful when checking how a parameter applies across endpoint families. |
| `openapi.py schema <name>` | One component schema by name (e.g., `Atl03Parameters`, `Atl06DataFrame`). |

Plus the curated reference files in `sliderule-openapi/references/`:

| Reference | What it covers |
|---|---|
| `parameter_couplings.md` | Required pairings, depends-on relationships, implicit behaviors — not in the spec |
| `samples_shape.md` | The `samples` parameter's actual dict-of-dicts shape — spec has it as `properties: {}` |
| `algorithms.md` | The five algorithms (phoreal, fit, als, yapc, atl24) and their output effects |
| `conditional_columns.md` | `/atl08x` and `/atl24x` columns that appear only under specific mode flags |
| `field_selectors/fields_<selector>.json` | Valid HDF5 field names per `*_fields` parameter |

Typical invocation while planning:

```bash
python scripts/openapi.py applies-to atl06x
python scripts/openapi.py param cnf
```

**Parameter couplings.** When constructing a request, consult
`references/parameter_couplings.md` for documented `depends_on`,
`required_pairings`, `interaction_detail`, and `implicit_behavior`
relationships. These are not expressed in the OpenAPI spec — only the
parameter signatures are. Treat the curated couplings as required context,
not optional trivia.

**Applies-to.** When a parameter's description ends with "supported by
<endpoints>", honor it: passing a parameter to an endpoint that doesn't
accept it is a silent no-op at best and a rejection at worst.

## The Planning Sequence

### Phase 1: Classify Intent → Select API

Map the user's study to an endpoint. This table is the one piece of factual
content that lives in the skill rather than the schema, because it encodes
*study-type reasoning* rather than parameter definitions.

| Intent | API | Notes |
|--------|-----|-------|
| Raw photons | `atl03x` (no algorithm) | Most flexible, highest data volume |
| Surface elevation (land ice) | `atl06x` or `atl03x` + `"fit": {}` | `fit: {}` runs ATL06-style fitting inside atl03x |
| Canopy / vegetation | `atl03x` + `"phoreal": {...}` or `atl08x` | PhoREAL in atl03x is more configurable |
| Inland water level | `atl13x` or `atl03x` + `"atl13": {...}` | atl13x needs water body identification |
| Bathymetry (depth) | `atl24x` or `atl03x` + `"atl24": {...}` | |
| Biomass density | `gedil4ax` | GEDI, not ICESat-2 |

**The atl03x-vs-x-series tradeoff:** `atl03x` is the universal entry point.
It returns raw photons by default, but adding an algorithm block (`fit`,
`phoreal`, `yapc`, `atl24`, `atl13`) changes it into a processed product.
The other x-series APIs are pre-configured pipelines that enforce stricter
data quality — they may return zero rows where `atl03x` with the equivalent
algorithm returns data. Choose x-series when you want the canonical product;
choose `atl03x` + algorithm when you want control over filtering or need to
combine features (e.g., YAPC scores on fitted extents).

### Phase 2: Signal & Environment → Photon Filtering

Consult `openapi.py param cnf` / `param srt` / `param atl08_class` /
`param atl24` plus `references/parameter_couplings.md` for the
photon-filtering parameter set.

Key reasoning questions:

- **What surface type is the study over?** This drives `srt`, and because
  `cnf` is evaluated per-surface-type, it also reshapes which photons
  survive the confidence filter. The `cnf` ↔ `srt` coupling is
  documented in `parameter_couplings.md`.
- **How noisy is the signal environment?** Daytime over bright surfaces
  (snow, sand) needs tighter `cnf`; nighttime or dark surfaces tolerate
  looser settings.
- **Is a downstream classifier going to re-filter the photons?** If so,
  honor the `required_pairings` in `parameter_couplings.md` —
  `atl08_class` and `atl24` both document `cnf` and `srt` values that
  must be set to let classified photons through. Skipping this quietly
  produces empty or wrong output.

### Phase 3: Algorithm Configuration

Only applies to `atl03x`. Other APIs have their algorithms built in.
Consult `sliderule-openapi`'s `references/algorithms.md` for each
algorithm's behavior and `references/parameter_couplings.md` for
implicit-behavior details.

Reasoning questions:

- **What does the user actually want to measure?** Surface elevation →
  `fit`. Vegetation metrics → `phoreal`. Photon-level signal quality →
  `yapc`. Bathymetry → `atl24`. Water body mean elevation → `atl13`.
- **Do I need multiple algorithms?** `yapc` appends scores to photons
  (`output_effect: appends`), so it composes with other algorithms.
  `fit`, `phoreal`, `atl24`, and `atl13` each replace the photon
  DataFrame (`output_effect: replaces`), so they don't compose with
  each other within a single request.
- **Does enabling this algorithm trigger implicit filtering?**
  `parameter_couplings.md` flags this. PhoREAL auto-filters noise
  photons; `atl08_class` with an ATL08 ancillary field auto-filters
  unclassified photons. Know what gets silently removed before inspecting
  output.

### Phase 4: Segment Geometry

Consult the `segment_control` group. Applies to `atl03x` (when using
`fit` or `phoreal`), `atl06x`, and `atl08x`.

Reasoning questions:

- **What spatial resolution does the study need?** Set `res` to the
  desired posting interval. The `res`/`len`/`cnt` interaction is
  documented in `parameter_couplings.md` (overlap vs adjacent vs gaps).
- **How much averaging is appropriate?** Longer `len` smooths noise
  but loses fine-scale features. Sparse photon densities (weak beams,
  steep slopes, vegetation) need longer `len` to meet the `cnt`
  minimum; otherwise extents get discarded.

### Phase 5: Beam & Track Selection

Consult `openapi.py param beams` / `param spots` / `param track` /
`param granule` for the beam and track selection parameters.

Reasoning questions:

- **Strong beams only, or all six?** Weak beams have lower SNR and
  may not produce valid fits over dark or low-reflectance surfaces.
  The `spots` parameter is atl03x-only (its description names the
  applicable endpoint); `spots` is orientation-independent, while
  `beams` is orientation-dependent.
- **Is the study constrained to specific tracks or cycles?** Use the
  `granule` filter object rather than downloading everything and
  filtering client-side.

### Phase 6: Ancillary Enrichment

Consult `openapi.py param atl08_fields` / `param atl03_geo_fields` /
etc. for the ancillary-field parameters, and `openapi.py param samples`
plus `references/samples_shape.md` for raster sampling.

Reasoning questions:

- **What additional HDF5 fields does the study need?** Consult
  `sliderule-openapi`'s `references/field_selectors/` directory — one
  JSON file per `*_fields` selector listing every valid field name
  with HDF5 path, type, units, and description. Each added field costs
  processing time and bytes; request only what you'll use.
- **Does the user want orthometric heights (above geoid / MSL),
  tide-corrected heights, or output in a named datum (NAVD88, EGM08),
  rather than ellipsoidal?** Height meaning splits across two
  independent axes: reference *point* (relief above terrain vs.
  absolute) and reference *surface* (WGS84 ellipsoid vs. geoid/datum).
  Orthometric needs both resolved — absolute heights, measured from the
  geoid — and no single parameter does both. `datum` (server-side) and
  `atl03_corr_fields: ["geoid"]` (client-side columns) move the surface
  axis; top-level `use_abs_h` on `/atl08x` only moves the point axis
  (relief → absolute *ellipsoidal*, still not orthometric). Whenever the
  user says "MSL", "orthometric", "above sea level", "tide-corrected",
  "geoid-corrected", or names a `datum`, consult `sliderule-openapi`'s
  `references/elevation_datums.md` for the two-axis decision tree. Flag
  during planning — route choice constrains which endpoint can satisfy a
  resolution requirement, and correction-column routes must be
  requested up front (re-running is the only way to add them after
  the fact).
- **Should external rasters be co-sampled?** A DEM for elevation
  differencing, land cover for stratification, NDVI from HLS for
  vegetation context — decide during planning, not after, because
  re-sampling means re-running the whole request.
- **Does an ATL08 field need interpolated rather than nearest-
  neighbor reduction?** Append `%` to the field name in `atl08_fields`
  to request interpolated reduction (otherwise nearest-neighbor mode
  is used). Only matters for PhoREAL output.

### Phase 7: Output Configuration

Consult `openapi.py param output` for the `output` block fields.

For direct HTTP API usage, the `output` block is not optional — always
include at minimum `format`, `as_geo`, and `with_checksum`. The OpenAPI
spec documents the full field list under each endpoint's `output`
parameter.

## SlideRule-Specific Gotchas

These don't live in the OpenAPI spec because they're usage-level traps,
not parameter definitions.

- **`fit: {}` (empty dict) is the enable signal.** You don't need to
  populate any fields to run ATL06-style fitting inside `atl03x`.
  An empty object is sufficient; the top-level `segment_control`
  parameters drive it.
- **`%` suffix on `atl08_fields` switches reduction mode.** Without
  the suffix, per-photon ATL08 values are reduced to per-extent by
  nearest neighbor (mode). With `%`, they're reduced by interpolation
  (average). Only matters for PhoREAL output.
- **X-series APIs return `time_ns` as the DataFrame index.** Always
  call `reset_index()` immediately after `table.to_pandas()` to
  preserve the time column before saving to Parquet. The older
  p-series behavior (time as a regular column) is gone.
- **X-series stricter than `atl03x` + algorithm.** If `atl06x`
  returns empty and the polygon/time window look fine, try
  `atl03x` with `"fit": {}` — the same photons may survive the
  looser atl03x quality floor.
- **Quality filters compound silently.** `cnf`, `srt`, `quality_ph`,
  `atl08_class`, and algorithm-level filters all AND together.
  Empty output usually means over-filtering, not missing data.
  When debugging, relax one filter at a time.

## Reporting the Plan

After walking the phases, report the parameter plan to the user *before*
firing the request. The plan makes the reasoning auditable, gives the
user a chance to correct misunderstandings, and documents the request
for later reproducibility. Example:

> **Request Plan: Coral Reef Bathymetry, Belize**
> - API: `atl03x` + `atl24` algorithm
> - `cnf: -1`, `srt: -1` (required pairing for ATL24 per `parameter_couplings.md`)
> - `atl24.class_ph: ["bathymetry", "sea_surface"]`
> - `atl24.night: ["on"]` (less background noise)
> - Time window: 2023-01-01 to 2023-12-31
> - Raster co-sample: `cop-dem` at 10m radius
> - Output: GeoParquet with geometry

Then hand off to `sliderule-api` for HTTP execution and
`sliderule-pipeline` for scripting patterns.

## Theory Consultation

When planning needs deeper scientific grounding — what a confidence level
physically represents, how the fit algorithm's iterative rejection works,
what refraction correction does to bathymetry depths — consult the
`nsidc-reference` skill. That skill searches the ATBDs and User Guides.
This skill handles "what to set and how"; nsidc-reference handles "why
the science says so." The SlideRule docs themselves are searchable via
the `sliderule-docsearch` skill.
