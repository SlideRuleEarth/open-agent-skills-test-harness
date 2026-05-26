# Conditional output columns

Columns in SlideRule's parquet response whose presence or shape depends
on the request. Two cases:

1. **Cross-cutting transforms** keyed on `output.format` — these reshape
   the column set on *every* endpoint. The latitude/longitude → geometry
   collapse below is the only one currently documented.
2. **Per-endpoint conditional columns** — individual columns that
   appear or disappear based on per-endpoint request flags (e.g.,
   `phoreal` sub-options on `/atl08x`, `atl24.compact` on `/atl24x`).

The OpenAPI spec captures one case fully — `/atl03x`'s `oneOf` response
with prose discriminator — but does not capture the cross-cutting
transforms or the per-endpoint individual-column gates for other
endpoints. This file documents what isn't (yet) in the spec.

Sources of truth: SlideRule server source at the time of skill
authoring (specific file/line citations under each entry), and the
legacy sliderule-schema distribution's output schemas at
`/source/schema/<domain>/output/<api>.json`. Re-verify after server
updates.

## Cross-cutting: GeoParquet output transform (latitude/longitude → geometry)

**Trigger:** `output.format == "geoparquet"`, which is the default on
every endpoint that exposes an `output` parameter. To suppress this
transform, the request must explicitly set
`output: { "format": "parquet", "as_geo": false }` — setting
`as_geo: false` alone is *not* sufficient because the default `format`
value is `geoparquet`.

**Effect:** any column flagged in the C++ record/column definition as
`X_COORD`/`X_COLUMN` (longitude) or `Y_COORD`/`Y_COLUMN` (latitude) is
dropped from the output schema, and a single `geometry` column is
added at the end. The geometry column is binary, WKB-encoded Point
geometry.

**CRS in the geometry column.** There is no single default CRS —
it's DataFrame-specific, and on two of the emission paths it depends
on the request. Broad strokes:

| DataFrame / path | CRS | Datum-driven override? |
|---|---|---|
| `Atl03DataFrame` | `defaultITRF(version)`: EPSG:7912 (ATL release ≤6) or EPSG:9989 (release ≥7) | Yes — `Atl03DataFrame.cpp:172` switches to the EGM2008-paired variant (EPSG:7912_EGM08 / EPSG:9989_EGM08) when `datum=EGM08`. |
| `Atl06DataFrame`, `Atl08DataFrame` | `defaultITRF(version)` (same release-dependent ITRF) | Not visible in source. |
| `Atl13DataFrame`, `Atl24DataFrame`, `BathyDataFrame` | `defaultEGM(version)`: EPSG:7912_EGM08 (≤6) or EPSG:9989_EGM08 (≥7) — already the EGM2008-paired variant by default | Not visible in source. |
| `Gedi01bDataFrame`, `Gedi02aDataFrame`, `Gedi04aDataFrame` | EPSG:7912, hardcoded (release-independent) | No. |
| `Casals1bDataFrame` | EPSG:9989, hardcoded | No. |
| `PhoRealDataFrame`, `SurfaceFitterDataFrame`, `SurfaceBlanketDataFrame` (atl03x algorithm-mode outputs) | Inherit from the `Atl03DataFrame` they're built off — PhoReal's columns are even labeled "(EPSG:9989)" in their descriptions (`PhoReal.cpp:234-235`), so they assume release ≥7. | Same as the underlying Atl03DataFrame. |
| Record-stream output (`ArrowBuilderImpl.cpp:395+`, used by legacy p/s/v-series endpoints) | OGC:CRS84 (WGS 84 lon/lat), hardcoded — does not consult `datum` at all | No. |

Practical guidance: read the CRS off the GeoParquet metadata
(`gdf.crs` in geopandas) rather than assuming a default. The defaults
are heterogeneous enough that hardcoding the expected CRS in client
code is brittle. Source files for each entry above are where to look
if behavior changes.

**Affected DataFrames** (every `*DataFrame` schema in the spec that
declares both `latitude` and `longitude` properties):

- `Atl03DataFrame`
- `Atl06DataFrame`
- `Atl08DataFrame`
- `Atl13DataFrame`
- `Atl24DataFrame`
- `BathyDataFrame`
- `Casals1bDataFrame`
- `Gedi01bDataFrame`
- `Gedi02aDataFrame`
- `Gedi04aDataFrame`
- `PhoRealDataFrame`
- `SurfaceBlanketDataFrame`
- `SurfaceFitterDataFrame`

**What the OpenAPI spec shows:** the affected schemas list `latitude`
and `longitude` as properties with no annotation that they will be
absent from the actual GeoParquet output. A user reading the spec
sees columns that won't exist in the response under default settings.

**Note on `as_geo`:** the `output.as_geo` field is marked deprecated
in the spec ("use format='geoparquet' instead"), but it isn't dead —
`OutputFields::fromLua` (`packages/core/package/OutputFields.cpp:81-89`)
implements two interactions:

- `format: "parquet"` + `as_geo: true` → server upgrades the format to
  geoparquet internally. The geometry transform happens.
- `format: "geoparquet"` (the default) → server forces internal `asGeo
  = true` regardless of what the user supplied for `as_geo`. The
  geometry transform happens.

In other words, `as_geo` can *enable* the transform when format is
explicitly `parquet` but cannot *disable* it when format is
`geoparquet` (or unset, which defaults to geoparquet). The `format`
field is the authoritative control.

**C++ source for the transform:**

- `packages/arrow/package/ArrowBuilderImpl.cpp:79, 313-321` —
  record-based output path (used by older record-stream endpoints).
  Skips fields with `X_COORD | Y_COORD` flags; appends `geometry`.
- `packages/arrow/package/ArrowDataFrame.cpp:402-413, 502-507` —
  DataFrame-based output path (used by x-series endpoints). Skips
  fields with `X_COLUMN | Y_COLUMN` encoding; appends `geometry`.

## `/atl08x`

`Atl08DataFrame` (the spec's declared response schema for `/atl08x`)
documents 26 columns as if all are always present. Two additional
columns appear only when the corresponding quality-filter parameter is
supplied in the request. These parameters live at the **top level** of
`Atl08Parameters` (not nested under any `phoreal` block — `/atl08x`
doesn't have one; see `parameter_couplings.md` > `use_abs_h` for the
dual-definition explanation).

| Column | Type | Appears when | Description |
|---|---|---|---|
| `te_quality_score` | array of int8 | request supplies top-level `te_quality_filter` (any value, including 0) | Terrain ATL08 quality score |
| `can_quality_score` | array of int8 | request supplies top-level `can_quality_filter` (any value, including 0) | Canopy ATL08 quality score |

The gate is whether the field was *supplied* in the request body, not
its value — `Atl08Parameters::fromLua` (`Atl08Parameters.cpp:60-69`)
sets `_provided = !lua_isnil(...)`, so even `te_quality_filter: 0`
(the default value) triggers the column to appear. To suppress the
column, omit the field entirely.

C++ source: `Atl08DataFrame.cpp:135-141` (column registration) and
`:194-202` (HDF5 dataset open) gate on `parms->te_quality_filter_provided`
and `parms->can_quality_filter_provided`.

The spec's `Atl08DataFrame` lists neither column, so they wouldn't
appear in a schema-driven enumeration.

## `/atl24x`

`Atl24DataFrame` (the spec's declared response schema for `/atl24x`)
documents 22 columns as if all are always present. The legacy schema
server flagged 8 of those as gated on the **negation** of
`atl24.compact` — i.e., they appear only when `atl24.compact=false`
(non-compact mode). **`compact` defaults to `true`**
(`Atl24Parameters.h:66`), so under default settings the response is
in compact mode and these 8 columns are *absent*:

| Column | Appears when |
|---|---|
| `ellipse_h` | `atl24.compact=false` |
| `invalid_kd` | `atl24.compact=false` |
| `invalid_wind_speed` | `atl24.compact=false` |
| `low_confidence_flag` | `atl24.compact=false` |
| `night_flag` | `atl24.compact=false` |
| `sensor_depth_exceeded` | `atl24.compact=false` |
| `sigma_thu` | `atl24.compact=false` |
| `sigma_tvu` | `atl24.compact=false` |

A user running `/atl24x` under default settings (`atl24.compact=true`)
will see a 14-column response. To get all 22 columns, the request
must explicitly set `atl24: { "compact": false }`. The spec doesn't
currently distinguish the two modes.

## `/atl03x` — handled by the spec's `oneOf`

`/atl03x`'s individual-column conditions on the legacy schema server
(`stages.phoreal`, `stages.atl24`, `stages.yapc`, `stages.atl08`) are
**not** missing from the OpenAPI spec — they're absorbed into the
endpoint's `oneOf` response, which selects between `Atl03DataFrame`,
`PhoRealDataFrame`, `SurfaceFitterDataFrame`, and `SurfaceBlanketDataFrame`
based on which algorithm mode was supplied. The prose discriminator
on the `oneOf` describes the gating; see SKILL.md behavioral rule #4
for how to parse it. Columns conditioned on `stages.yapc` and
`stages.atl08` are part of the base `Atl03DataFrame` (output-augmenting,
not output-replacing — see `algorithms.md`).

Note that even when `oneOf` selects, say, `PhoRealDataFrame`, the
GeoParquet transform documented at the top of this file still applies
— `PhoRealDataFrame.latitude` and `PhoRealDataFrame.longitude` collapse
into `geometry` like any other affected DataFrame.
