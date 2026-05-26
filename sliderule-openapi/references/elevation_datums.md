# Elevation datum and height-reference routes

What a SlideRule height column *means* depends on two independent
choices. The OpenAPI spec can't express either; consult this file when
the user mentions orthometric output, MSL, geoid or tide correction,
absolute vs. relief heights, or sets a `datum`.

## Two axes, not one list

| Axis | The question | Default | Moved by |
|---|---|---|---|
| **Reference point** | height above the *terrain* (relief) or above a *surface* (absolute)? | product/algorithm-dependent | `use_abs_h` (ATL08 canopy) |
| **Reference surface** | *ellipsoid* (WGS84) or *geoid* (orthometric / MSL / datum)? | WGS84 ellipsoid | `atl03_corr_fields: ["geoid"]` or `datum` |

The classic mistake is collapsing these into one. **Orthometric output
needs both resolved: absolute heights, measured from the geoid.** No
single parameter does both — `use_abs_h` only moves Axis 1,
`geoid`/`datum` only move Axis 2.

## Axis 1 — relief vs. absolute

ATL08 canopy metrics default to *relief*: height above the local
terrain, not above any global surface. PhoREAL's `h_canopy` (and its
max/min/mean/metric siblings) are relief values. Subtracting a geoid
from relief is meaningless.

`use_abs_h: true` (top-level, `/atl08x` only) switches `Atl08DataFrame`
to read the **canopy** `_abs` HDF5 datasets — `h_canopy_abs`,
`h_max_canopy_abs`, `h_min_canopy_abs`, `h_mean_canopy_abs`, and
`canopy_h_metrics_abs` (all under `land_segments/canopy/`) — instead
of their relief siblings (`Atl08DataFrame.cpp:198,203-207`). Dataset
selection, not arithmetic. **Terrain is not affected:** `h_te_median`
and `h_te_uncertainty` are read unconditionally from
`land_segments/terrain/` (`Atl08DataFrame.cpp:195-196`), since ATL08's
terrain heights are already ellipsoidal and have no `_abs` variants.

**`_abs` is ellipsoidal, not orthometric.** The `_abs` heights are
measured from the WGS84 ellipsoid — per the spec's own wording
(*"return ellipsoidal elevations instead of relief measurements"*) and
the ATL08 ATBD. They are absolute, but still on Axis 2's *ellipsoid*
end. Reporting `use_abs_h` output as orthometric is wrong by the full
geoid undulation — commonly tens of meters (e.g. ~16 m) — and
mislabels the datum.

The nested `phoreal.use_abs_h: true` (under `Atl03Parameters` /
`Atl06DispatchParameters` / `BathyParameters`) is a different field of
the same name that tweaks the PhoREAL runner's metric calculation; its
spec description flags it as *"non-standard and for special cases
only."* See `parameter_couplings.md` for the top-level-vs-nested split.

## Axis 2 — ellipsoid vs. geoid/datum

SlideRule heights are WGS84 ellipsoidal by default. Two routes produce
non-ellipsoidal (orthometric / MSL / datum) output:

| Route | Endpoints | Mechanism |
|---|---|---|
| `atl03_corr_fields: ["geoid", ...]` | `/atl03x` + ATL03 dispatch (`/atl03s`, `/atl03sp`, `/atl03v`, `/atl03vp`) | Appends arbitrary fields from the ATL03 `geophys_corr/` HDF5 group as columns; you subtract client-side. Common values: `geoid`, `tide_earth`, `tide_ocean`, `tide_load`, `pole_tide`, `dac`, `geoid_free2mean`. The server doesn't enforce a closed enum — any `geophys_corr/*` field name is accepted (`Atl03DataFrame.cpp:242`). |
| `datum: "NAVD88"` / `"EGM08"` / `"ITRF2014"` / `"ITRF2020"` | `/atl03x`, `/atl06x`, `/atl08x`, `/atl13x`, `/atl24x` | Server-side conversion before serialization. Enum at `MathLib.h:74-79`. See "Accepts vs applies." |

**Accepts vs applies.** The spec records endpoint-level *acceptance* of
`datum`, not its *effect* on the columns a given algorithm produces.
Whether `datum` actually converts `phoreal` canopy elevations, `fit`
surface elevations, or `atl24` depths isn't expressible in the spec.
Validate on a first request: co-sample a geoid model via `samples` and
compare the server's datum output against your own client-side
subtraction.

## Putting both axes together — orthometric canopy

Because canopy needs both axes resolved, every orthometric canopy route
ends in a geoid subtraction:

- **Relief route:** `h_te + h_canopy - geoid` — terrain ellipsoidal
  height + canopy relief − geoid. The `geoid` from `atl03_corr_fields`
  corrects the terrain term (`h_te`); it is *not* subtracted from
  `h_canopy` directly.
- **Absolute route:** `h_canopy_abs - geoid` — `use_abs_h` gives the
  ellipsoidal canopy-top (Axis 1), then the geoid takes it to
  orthometric (Axis 2).

The two recurring errors map one-to-one to the axes: `h_canopy - geoid`
forgets Axis 1 (`h_canopy` is relief); reporting `h_canopy_abs` as-is
forgets Axis 2 (it's still ellipsoidal).

## Endpoint × route compatibility

| Endpoint | `atl03_corr_fields` | `datum` | top-level `use_abs_h` | nested `phoreal.use_abs_h` |
|---|---|---|---|---|
| `/atl03x` | ✓ | ✓ | — | ✓ |
| `/atl06x` | — | ✓ | — | — |
| `/atl08x` | — | ✓ | ✓ | — |
| `/atl13x` | — | ✓ | — | — |
| `/atl24x` | — | ✓ | — | — |

`datum` acceptance is per the `applies-to` check; its effect on
columns carries the "Accepts vs applies" caveat above.

Asymmetries worth noting:

- `/atl08x` is the only endpoint with top-level `use_abs_h`, but it's
  locked to ATL08's native 100 m segments (no `res`/`len`/`cnt`) and
  rejects `phoreal: {...}`.
- `/atl03x` has `res`/`len`/`cnt` and accepts `phoreal` (carrying
  nested `phoreal.use_abs_h`) but has no top-level `use_abs_h`.
- So user-controlled resolution and the absolute-canopy switch never
  coexist on one endpoint.

For older dispatch endpoints (`/atl03s`, `/atl06p`, `/atl08p`,
`/bathyv`, `/bathyx`, …), check `datum` and `use_abs_h` acceptance per
endpoint with `openapi.py applies-to <endpoint>`.
