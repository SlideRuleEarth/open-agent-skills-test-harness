# sliderule-openapi scripts

Helper scripts that back the `sliderule-openapi` skill.

| Script | Purpose |
|---|---|
| `openapi.py` | Loader and slicer for the SlideRule OpenAPI 3.1 specification. Most invocations from the skill route through this. |
| `download_h5_granules.py` | Pipeline support: fetches sample HDF5 granules used to enumerate ancillary field selectors. |
| `enumerate_h5_fields.py` | Pipeline support: walks downloaded granules and emits the per-selector JSON files under `references/field_selectors/`. |

`openapi.py` is documented in `SKILL.md`. The rest of this file covers the
normalization passes `openapi.py` applies between loading and slicing —
material that's relevant to maintainers and to anyone surprised by a slice
that differs from the upstream spec, but not needed on every skill
invocation.

## Spec quirks the helper smooths over

A handful of known issues in the upstream spec are normalized at load
time before the model sees the slice. The transforms are deterministic,
version-stable, and stop being no-ops once upstream fixes the issue.

- **`format: "binary"` on string fields is stripped.** ~430 occurrences
  across the spec apply this format to textual identifiers (`asset`,
  `resource`, `resources`, all ancillary field-name arrays, etc.).
  These are ASCII strings, not binary payloads; the format annotation
  misleads code-generation tooling. The helper drops the format and
  leaves the field as plain `type: string`.
- **`format: "x-object"` is normalized case-by-case.** In the upstream
  spec this annotation is a catch-all "shape not expressible here"
  marker that gets applied to structurally distinct defects. The
  helper unwinds them into three arms:
  - **Wrong-type or under-specified parameters** — `datum`, `proj`,
    `aoi_bbox`, `coord`, and polygon vertices via `poly[items]` are
    replaced inline with their actual schemas. `datum` and `proj`
    become string enums; `aoi_bbox` becomes a 4-element array of
    numbers `[lon_min, lat_min, lon_max, lat_max]`; `coord` becomes
    an object with `lon`/`lat` properties; polygon vertices become
    `{lat, lon}` objects. The corrected schemas are sourced from
    the server's `convertFromLua` definitions in `RequestFields.cpp`
    and `GeoFields.cpp`. Notable: the upstream `datum` description
    advertises `ITRF2020`, but the server's string-input parser does
    not accept it — the corrected enum drops it.
  - **Inner-record sub-fields** — output-side fields like
    `atl06rec.elevation`, `gedi01brec.footprint`, `atl03rec.photons`,
    `swotl2geo.scan`, etc. encode the name of the target schema in
    their `description`. The helper detects this and rewrites the
    node as a `$ref`, after which the slicer follows it like any
    other ref and the actual sub-record schema appears in the output.
  - **Fallback** — anything that doesn't match the two arms above gets
    its `format` stripped and an `x-shape: "see-references"` marker
    added. This branch should be empty in practice; if anything lands
    here it's an unhandled upstream defect that should be added to
    the inline table or surfaced via `references/`.

A consumer who wants the unmodified spec can pass `--no-normalize` to
disable both transforms.
