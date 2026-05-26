# Field-selector references — maintenance notes

This directory holds enumerations of valid field names for SlideRule's
HDF5 ancillary-field array parameters (`atl08_fields`, `atl03_geo_fields`,
etc.). The OpenAPI spec describes the *shape* of these parameters
(array of string) and names the source HDF5 group, but doesn't
enumerate the field catalog itself. These files do.

**This file is not loaded by the skill at runtime.** It's developer
documentation for whoever has to regenerate the references — typically
after a SlideRule server release that changes the available fields.

## Origin

The generation scripts in `sliderule-openapi/scripts/` were salvaged
from the now-archived `LuaSchemaEndpoint` branch of the SlideRule repo
(commits prior to its archival). They are the canonical generator the
legacy schema-server distribution at `schema.testsliderule.org` was
built from.

Files copied verbatim, no local modifications:
- `download_h5_granules.py`
- `enumerate_h5_fields.py`

## Regeneration workflow

Requires Earthdata Login credentials configured in `~/.netrc` (or
`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD` env vars). Run from the
skill root:

```bash
# One-shot: download representative v007 granules to ~/sliderule_granules,
# then walk them and emit JSON to references/field_selectors/.
python3 scripts/enumerate_h5_fields.py --earthdata \
    --output-dir references/field_selectors/
```

The granule download directory defaults to **`~/sliderule_granules/`** —
outside the skill directory so the (large) HDF5 files can never
accidentally get checked in with skill content, and visible in your
home directory so it's obvious when you've left granules around to
clean up. ATL09 alone is ~3 GB; the full set is ~3.5 GB. Override
with `--granule-dir <path>` if you keep them elsewhere.

Granules are reused across runs — `earthaccess.download` skips files
that already exist. So the first enumerate run downloads ~3.5 GB; later
runs are fast.

Or split into two steps if you want to drive downloads and enumeration
separately:

```bash
# 1. Download one representative v007 granule per product
python3 scripts/download_h5_granules.py --output-dir ~/sliderule_granules

# 2. Walk the HDF5 structure of each granule and emit one JSON file
#    per selector into references/field_selectors/.
python3 scripts/enumerate_h5_fields.py \
    --atl03 ~/sliderule_granules/ATL03_*.h5 \
    --atl06 ~/sliderule_granules/ATL06_*.h5 \
    --atl08 ~/sliderule_granules/ATL08_*.h5 \
    --atl09 ~/sliderule_granules/ATL09_*.h5 \
    --atl13 ~/sliderule_granules/ATL13_*.h5 \
    --output-dir references/field_selectors/
```

**When you're done regenerating, `rm -rf ~/sliderule_granules/` to
reclaim the disk.**

Output per selector: `{selector, hdf5_subgroup, description, field_count,
fields[], source_granule}` — each `fields[]` entry has `name`,
`hdf5_path`, `type`, optional `unit`, `description`, `fill_value`,
`source`, `shape`.

## Coverage

`enumerate_h5_fields.py` covers eight selectors across five ICESat-2 products:

| Selector | OpenAPI parameter | Product | HDF5 subgroup |
|---|---|---|---|
| `atl03_ph` | `atl03_ph_fields` | ATL03 | `/gtxx/heights` |
| `atl03_geo` | `atl03_geo_fields` | ATL03 | `/gtxx/geolocation` |
| `atl03_corr` | `atl03_corr_fields` | ATL03 | `/gtxx/geophys_corr` |
| `atl03_bckgrd` | `atl03_bckgrd_fields` | ATL03 | `/gtxx/bckgrd_atlas` |
| `atl06` | `atl06_fields` | ATL06 | `/gtxx/land_ice_segments` (recursive) |
| `atl08` | `atl08_fields` | ATL08 | `/gtxx/land_segments` (recursive) |
| `atl09` | `atl09_fields` | ATL09 | `/profile_1/high_rate` |
| `atl13` | `atl13_fields` | ATL13 | `/gtxx` |

## Known gaps

The pipeline does **not** cover:

- **ATL24 ancillary fields** (`atl24.fields.anc_fields` in the legacy
  schema server, ~19 fields, beam-group structure). Extending
  `enumerate_h5_fields.py`'s `SELECTOR_MAP` would be straightforward —
  the HDF5 layout is similar to ATL03/ATL08.
- **GEDI L2A + L4A ancillary fields** (`anc_fields` in the legacy
  schema server, ~731 fields combined from L2A elevation/height
  metrics and L4A aboveground biomass density). Substantially more
  work — different mission, different file structure, would need new
  dispatch logic in `enumerate_h5_fields.py`.

Until the pipeline is extended, queries about valid values for the
ATL24 or GEDI ancillary-field parameters have no enumeration available
from this skill.

## When to regenerate

- After any SlideRule server release that adds new ancillary-field
  selectors or changes the underlying HDF5 group structure being
  enumerated.
- After NASA releases a new ICESat-2 standard data product version
  (currently v007). The version string is hardcoded in both scripts
  (`VERSION = "007"`); update there first.
- If the pipeline is extended to cover ATL24 or GEDI.
