# `samples` parameter shape

The OpenAPI spec currently has `samples: {type: "object", properties: {}}`
— shape is undocumented. This file fills that gap until the spec is
updated.

Source of truth: SlideRule docs at
<https://docs.slideruleearth.io/user_guide/raster_sampling.html>

## What `samples` does

Configures raster sampling: at every measurement location in the output
(photon, footprint, segment, etc.), the server samples one or more
raster datasets and appends the sampled values as additional columns
in the returned DataFrame.

## Shape

`samples` is a **dict of dicts**. Each outer key is an arbitrary
user-chosen label that becomes a column-name prefix in the output.
Each outer value is a config dict that selects one raster and
describes how to sample it.

```json
{
  "samples": {
    "mosaic": {
      "asset": "arcticdem-mosaic",
      "radius": 10.0,
      "zonal_stats": true
    },
    "strips": {
      "asset": "arcticdem-strips",
      "algorithm": "CubicSpline"
    }
  }
}
```

The example above adds two raster samples per measurement: one labeled
`mosaic` (a 10m-radius zonal-stats sample of `arcticdem-mosaic`) and
one labeled `strips` (cubic-spline-interpolated `arcticdem-strips`).

## Config-dict fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `asset` | string | yes | Raster asset name from SlideRule's asset directory |
| `algorithm` | string | no | One of `'NearestNeighbour'` (default), `'Bilinear'`, `'Cubic'`, `'CubicSpline'`, `'Lanczos'`, `'Average'`, `'Mode'`, `'Gauss'` |
| `radius` | number (meters) | no | Kernel size when sampling, or region size when computing zonal stats |
| `zonal_stats` | boolean | no | If true, emit count/min/max/mean/median/stddev/MAD columns instead of (or in addition to) the raw sample |
| `slope_aspect` | boolean | no | Emit slope and aspect columns |
| `slope_scale_length` | number (meters) | no | Region size used for slope/aspect computation |
| `with_flags` | boolean | no | Emit a 32-bit auxiliary flag column with raster-specific bit semantics |
| `t0` / `t1` | ISO 8601 string | no | Time filter applied when selecting which rasters in the asset to sample (e.g. `"2018-10-13T00:00:00Z"`) |
| `substr` | string | no | Substring filter on raster filenames within the asset |
| `bands` | array of string | no | Which raster bands to sample. Some assets expose dataset-specific custom bands (e.g., `landsat-hls` provides `NDSI`, `NDVI`, `NDWI`) |
| `key_space` | int64 | no | Expert-only client-side parallelization tuning; in general should not be set |

## Output column naming

Each entry in `samples` produces columns prefixed with the entry's outer
key. For example, `samples.mosaic` with `zonal_stats=true` produces
columns named `mosaic.value`, `mosaic.count`, `mosaic.min`,
`mosaic.max`, `mosaic.mean`, `mosaic.median`, `mosaic.stdev`,
`mosaic.mad`, etc.
