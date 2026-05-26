# SlideRule algorithms

The OpenAPI spec exposes SlideRule's processing algorithms as object-typed
properties on the relevant `*Parameters` schemas. They share a description
template ("Configuration structure for the '<Name>' algorithm; when
provided the servers will..."), but the spec doesn't enumerate them as a
group anywhere. This file does.

## The five algorithms

| Spec param | Human name | Output effect on `/atl03x` | Also accepted by |
|---|---|---|---|
| `phoreal` | PhoREAL (vegetation metrics) | Returns `PhoRealDataFrame` (replaces base output) | `/atl03s`, `/atl03sp`, `/atl03v`, `/atl03vp`, `/atl08`, `/atl08p`, `/atl06`, `/atl06p`, `/bathyv`, `/bathyx`. |
| `fit` | Surface Fitter (ATL06-SR) | Returns `SurfaceFitterDataFrame` (replaces base output) | Same as `phoreal`. The `/atl06` and `/atl06p` endpoints exist specifically as the primary path for surface fitting; `fit` on those is the standard way to run ATL06-SR with custom parameters. |
| `als` | Surface Blanket (ALS / airborne laser scanning model) | Returns `SurfaceBlanketDataFrame` (replaces base output) | Same endpoints as `phoreal` and `fit`. |
| `yapc` | Yet Another Photon Classifier | Adds `yapc_score` column to the base `Atl03DataFrame`. Does not replace the output schema. | Same broad set of endpoints. |
| `atl24` | ATL24 bathymetry classification | Adds `atl24_class` and `atl24_confidence` columns to the base `Atl03DataFrame`. Does not replace the output schema. | `/atl24x` (which uses `Atl24Parameters` — dedicated bathy endpoint, accepts only `atl24`). |

## Output-replacing vs output-augmenting

The five algorithms split into two behavior classes on `/atl03x`:

- **Output-replacing** (`phoreal`, `fit`, `als`): supplying the param
  changes the response schema entirely — `/atl03x`'s `oneOf` selects
  `PhoRealDataFrame` / `SurfaceFitterDataFrame` / `SurfaceBlanketDataFrame`
  respectively instead of the base `Atl03DataFrame`. This is what the
  prose discriminator on `/atl03x`'s 200 response describes.
- **Output-augmenting** (`yapc`, `atl24`): supplying the param adds
  columns to the base `Atl03DataFrame` (`yapc_score` for yapc;
  `atl24_class` and `atl24_confidence` for atl24). The response stays
  `Atl03DataFrame`.

`/atl03x`'s `oneOf` discriminator only mentions the three
output-replacing algorithms; `yapc` and `atl24` don't change the
response schema selection.

**Look up the replacing frame's columns; don't infer them.** For the
actual column set of any output-replacing frame, call
`openapi.py schema PhoRealDataFrame` (or `SurfaceFitterDataFrame` /
`SurfaceBlanketDataFrame`) rather than guessing from the algorithm
name. This file deliberately does *not* list those columns: they live
in the schema, the single source of truth, and inlining them here would
drift out of date. One semantic trap worth stating up front, because
the column name invites it: PhoREAL's `h_canopy` is canopy *relief*
(height above terrain), not an elevation — see
`references/elevation_datums.md`.

## Algorithm enablement

For all five algorithms, supplying an empty object `{}` as the parameter
value is sufficient to enable the algorithm with default config. E.g.:

```json
{ "phoreal": {} }              # enables PhoREAL with defaults
{ "fit": {} }                  # enables surface fitting with defaults
{ "atl24": {} }                # enables ATL24 (still needs cnf=-1, srt=-1)
```

See `references/parameter_couplings.md` for required pairings with `cnf`
and `srt` when using `atl08_class` or `atl24`.

## Naming inconsistency: `als` vs `surface`

The OpenAPI spec uses `als` as the parameter name (Airborne Laser
Scanning, a synonym for the underlying Surface Blanket algorithm).
The legacy sliderule-schema distribution listed this same algorithm as
`surface` in its `atl03x.algorithms` array. Both names refer to the
same algorithm and the same `SurfaceBlanketDataFrame` output. The
canonical request-time parameter name is **`als`** — that's what the
server accepts.

## Algorithm acceptance is broader than `/atl03x`

The `*Parameters` schemas that include the algorithm fields are
`Atl03Parameters` (used by `/atl03x` and several non-x endpoints in
the atl03 and atl08 families), `Atl06DispatchParameters` (used by
`/atl06` and `/atl06p`), `Atl24Parameters` (used by `/atl24x` — only
accepts `atl24`), and `BathyParameters` (used by `/bathyx` and
`/bathyv`).

**`Atl06Parameters` (`/atl06x`, `/atl06s`, `/atl06sp`) and `Atl08Parameters`
(`/atl08x`) do not include these algorithm fields.** For surface fitting
on ATL06 data, use `/atl06` or `/atl06p` with the `fit` parameter, not
`/atl06x`.
