# The Planning Sequence

The seven-phase request-planning sequence. Walk these in order for any
non-trivial request — i.e., when the fast-path triage in `SKILL.md`
already routed you here. For unambiguous requests, fast-path Phase 1
(endpoint selection), set region/time/beams from the user's request, add
the Phase 7 output block, and fire. Open one phase only when its concern
applies.

## Phase 1: Classify Intent → Select API

Map the user's study to an endpoint. This table is the one piece of factual
content that lives in this skill rather than the schema, because it encodes
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

## Phase 2: Signal & Environment → Photon Filtering

Consult `openapi.py param cnf` / `param srt` / `param atl08_class` /
`param atl24` plus `sliderule-openapi`'s
`references/parameter_couplings.md` for the photon-filtering parameter set.

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

## Phase 3: Algorithm Configuration

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

## Phase 4: Segment Geometry

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

## Phase 5: Beam & Track Selection

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

## Phase 6: Ancillary Enrichment

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

## Phase 7: Output Configuration

Consult `openapi.py param output` for the `output` block fields.

For direct HTTP API usage, the `output` block is not optional — always
include at minimum `format`, `as_geo`, and `with_checksum`. The OpenAPI
spec documents the full field list under each endpoint's `output`
parameter.
