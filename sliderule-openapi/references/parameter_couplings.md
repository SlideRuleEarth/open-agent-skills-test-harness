# Parameter couplings

Relationships between SlideRule request parameters that aren't visible from
the OpenAPI spec. Sourced from the legacy sliderule-schema distribution's
icesat2 domain document at the time of skill authoring — drift risk if the
couplings change in future SlideRule releases. Re-verify against the C++
parameter code in `datasets/icesat2/package/` and
`datasets/gedi/package/` when the underlying SlideRule version updates.

When a user is constructing a request, treat these as **required context**,
not optional trivia: a request that sets one parameter without honoring its
couplings will either silently produce wrong output or be rejected.

## ICESat-2 photon filtering (`cnf`, `srt`, `atl08_class`, `atl24`)

The photon-filtering parameters are interdependent. The same photon receives
different confidence (`cnf`) scores depending on which surface type (`srt`)
is applied, and adding classification algorithms (`atl08_class`, `atl24`)
imposes additional pairing requirements.

### `cnf` (confidence threshold)
- **Depends on:** `srt` (confidence scoring is surface-type-dependent)
- **Required pairings:**
  - With `atl08_class`: set `cnf=0` or lower to let all ATL08-classified
    photons through. `cnf=0` is the docs' recommended value; `cnf=-2`
    (includes TEP candidates) is also valid.
  - With `atl24`: set `cnf=-1` (all photons) to let all ATL24-classified
    photons through.

### `srt` (surface reference type)
- **Depends on:** `cnf` (the two filters interact)
- **Required pairings:**
  - With `atl08_class`: set `srt=0` (land). ATL08 classification operates
    on land-surface data only.
  - With `atl24`: set `srt=-1` (dynamic) to allow all ATL24 photon
    classifications.
- **Interaction detail:** forcing `srt` overrides the per-photon surface
  mask globally. Use when the automatic mask misclassifies the surface
  (e.g., small lakes classified as land, ice shelf margins).

### `atl08_class`
- **Required pairings:** `cnf` ≤ 0 and `srt=0` (see above).
- **Implicit behavior:** setting `atl08_class` activates the ATL08
  classifier on its own. The `atl08_class` column appears in the output
  whether or not `atl08_fields` is also requested. The `atl08_fields`
  parameter (which adds ATL08 `land_segments` fields) is optional and
  separate — it requires either `atl08_class` or `phoreal` to already be
  active.

### `atl24` (bathymetry classification)
- **Required pairings:** `cnf=-1` (all photons — bathymetric returns are
  inherently low-confidence), `srt=-1` (dynamic).

## Segment control (`res`, `len`, `cnt`)

Applies to: atl03x, atl06x, atl08x and their dispatch counterparts.

- **`len` interacts with `res` and `cnt`:**
  - `res < len` → overlapping extents (correlated samples)
  - `res = len` → adjacent extents (independent samples)
  - `res > len` → gaps between extents
  - Default `len=40`, `res=20` gives 2:1 overlap.
- **`len` interacts with `cnt`:** must have at least `cnt` photons within
  `len` for the extent to be valid. Longer extents average more photons
  (smoother, less noise) but lose fine-scale surface features.

## Algorithm parameters

### `fit` (Surface Fitter)
- An empty object `{}` is sufficient to enable surface fitting. No fields
  are required.

### `phoreal` (PhoREAL)

`phoreal` is declared on multiple request-class hierarchies, so several
endpoints will accept it in the request body. But schema-accepts and
server-consumes are not the same thing — on some endpoints the field
parses cleanly and has no effect on output. Behavior by endpoint:

| Endpoint(s) | Effect of providing `phoreal` |
|---|---|
| `/atl03x` | Full PhoREAL algorithm. The endpoint script `atl03x.lua` invokes `icesat2.phoreal(parms)` explicitly. The per-photon DataFrame is replaced by per-extent vegetation statistics; ATL08 land surface classifications are consulted; noise photons are filtered out automatically. |
| `/atl08`, `/atl08p` | Full PhoREAL algorithm via `Atl08Dispatch::phorealAlgorithm` in `Atl08Dispatch.cpp`. Canopy metrics are computed from `parms->phoreal` fields; output records include the phoreal-derived columns. |
| `/atl03s`, `/atl03sp`, `/atl03v`, `/atl03vp` | Reader-side side effect only. The `if(phoreal)` blocks in `Atl03Reader.cpp` are gated on the PHOREAL stage flag — when phoreal is provided, the reader opens additional ATL08 HDF5 datasets (`signal_photons/ph_h`, `land_segments/segment_landcover`, `segment_snowcover`) and attaches them to the photon stream as `relief`, `landcover`, `snowcover` columns. No algorithm runs. Output includes the extra columns. |
| `/atl06`, `/atl06p` | **Effectively no-op for output.** The endpoint wires `Atl03Reader` → `Atl06Dispatch`. `Atl03Reader` still pays the reader-side cost above (extra HDF5 reads, larger intermediate photon records), but `Atl06Dispatch` emits a different output record (`elevation_t`) that doesn't carry the ATL08-attached columns. Net effect: output unchanged, request slower. Do not provide `phoreal` to these endpoints unless that reader-side behavior is wanted for some other purpose. |
| `/bathyv`, `/bathyx` | **Silently ignored.** The bathy endpoints use `BathyDataFrame` (and `BathyViewer`) — independent readers that open ATL03 directly via `H5Object` and never use `Atl03Reader`. Nothing under `datasets/bathy/` reads `parms->phoreal` or the PHOREAL stage flag. Output unchanged, no cost. |
| `/atl08x` | Schema does not accept `phoreal` — `Atl08Parameters` has no such member. Sending `phoreal: {...}` is silently ignored. The PhoREAL-adjacent fields that exist on atl08x (`use_abs_h`, `te_quality_filter`, `can_quality_filter`) are top-level instead — see below. |

Stage-flag mechanics: `Atl03Parameters::fromLua` sets
`stages[STAGE_PHOREAL] = true` whenever a `phoreal` block is provided
(and also forces `STAGE_ATL08 = true` with a reasonable default
photon-class selection). Endpoints whose reader or dispatch code is
gated on `STAGE_PHOREAL` react to the flag; ones whose code never
checks it (Atl06Dispatch, bathy processors) don't, even though the
flag is set.

### `use_abs_h` (and `te_quality_filter`, `can_quality_filter`)

`use_abs_h` appears in the OpenAPI spec at **two different nesting
levels with two different descriptions**. These are not the same field
with conflicting documentation — they are different C++ fields on
different request classes that happen to share the name.

| Endpoint(s) | Where to set it | What it controls |
|---|---|---|
| `/atl08x` | top level: `{ "use_abs_h": true }` | Selects which HDF5 datasets `Atl08DataFrame` reads (`land_segments/canopy/h_canopy_abs` vs `h_canopy`, and the parallel `_abs`/non-`_abs` paths for max/min/mean/metrics). C++ source: `Atl08Parameters` top-level field, consumed in `Atl08DataFrame.cpp` (search `parms->use_abs_h`). Spec description: *"Read absolute (instead of relative) height metrics; i.e. return ellipsoidal elevations instead of relief measurements."* This is the headline absolute-height switch — note it returns *ellipsoidal* elevations, **not** orthometric (absolute ≠ orthometric; subtract the geoid for orthometric). See `references/elevation_datums.md`. |
| `/atl03x`, `/atl08`, `/atl08p`, `/atl03s`, `/atl03sp`, `/atl03v`, `/atl03vp`, `/atl06`, `/atl06p`, `/bathyv`, `/bathyx` | nested: `{ "phoreal": { "use_abs_h": true } }` | Affects the PhoREAL runner's per-extent canopy metric calculation. C++ source: `Atl03Parameters::PhorealFields::use_abs_h` (or its sibling in `Atl06DispatchParameters`/`BathyParameters`). Spec description: *"Use absolute height values when calculating the metrics; this is non-standard and for special cases only."* |

`te_quality_filter` and `can_quality_filter` follow the same pattern:
top-level fields on `Atl08Parameters` (i.e. `/atl08x`), and (where
they exist at all) nested under `phoreal` everywhere else.

**Reading the OpenAPI slice via `param use_abs_h`:** the `param`
subcommand walks all `*Parameters` schemas and returns every occurrence
of a given key name. It will show both `use_abs_h` definitions side by
side with their differing descriptions. The output does not currently
indicate that one is top-level and the other is nested under `phoreal`.
Treat them as two separate parameters distinguished by their owning
schema:

- `use_abs_h` in `Atl08Parameters` → top-level on /atl08x
- `use_abs_h` in `Atl03Parameters` → under `phoreal` on /atl03x, /atl08, /atl08p, etc.
- `use_abs_h` in `Atl06DispatchParameters` → under `phoreal` on /atl06, /atl06p
- `use_abs_h` in `BathyParameters` → under `phoreal` on /bathyv, /bathyx

Setting one does not affect the other, and no single endpoint exposes
both, so they cannot be set together in the same request.

For the full set of absolute-height and datum-conversion routes (this
`use_abs_h` field, plus `datum` and `atl03_corr_fields`), with the
endpoint × route compatibility matrix and the "accepts vs applies"
caveat for `datum`, see `references/elevation_datums.md`.

### `atl08_fields` (ATL08 land_segments fields)
- **On atl03x:** requires either `atl08_class` or `phoreal` to already
  be active — setting `atl08_fields` alone has no effect. It does *not*
  itself activate the ATL08 classifier; it appends ATL08 `land_segments`
  fields to output whose photons are already being classified by one of
  those other mechanisms.
- **On atl08x:** this prerequisite does not apply. The endpoint reads
  the native ATL08 `land_segments` HDF5 group directly, so
  `atl08_fields` appends columns unconditionally.

## GEDI L4A quality filtering

Applies to: gedil4ax.

These are independent flag/filter pairs — each filter param toggles
whether the corresponding flag is consulted. None have required pairings
with each other.

- `l4_quality_filter` interacts with `l4_quality_flag`
- `l2_quality_filter` interacts with `l2_quality_flag`
- `degrade_filter` interacts with `degrade_flag`
- `surface_filter` interacts with `surface_flag`
