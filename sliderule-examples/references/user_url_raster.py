# --- User Raster Sampling ---
#
# This notebook demonstrates how to sample a user-provided raster and compare
# results with SlideRule's supported ESA Copernicus dataset path.
#
# ESA Copernicus supports the AWS S3 protocol but is hosted at the San Diego
# Supercomputer Center (SDSC) on S3-compatible object storage. That means the
# same data can be accessed through either S3 protocol paths or HTTPS URLs.
#
# In SlideRule, the supported dataset asset (`esa-copernicus-30meter`) is
# implemented through GDAL `/vsis3/`. The same VRT can also be accessed over
# HTTPS through GDAL `/vsicurl/`, making it a good case for comparing
# supported-dataset sampling with user-specified URL raster sampling.

import sliderule
from sliderule import raster

sliderule.init(url="slideruleearth.io", verbose=False)
# sliderule.init(url="localhost", organization=None, verbose=False)

def sample_one(asset, parms=None):
    parms = parms or {}
    gdf = raster.sample(asset, [[-108.1, 39.1]], parms=parms)
    if len(gdf) == 0:
        raise RuntimeError(f"No samples returned for asset={asset}, parms={parms}")
    return float(gdf["value"].iat[0]), gdf["file"].iat[0]

# ---
# The COP30 VRT used here advertises a 2D CRS that does not fully describe the
# required vertical datum handling. In SlideRule's built-in `esa-
# copernicus-30meter` path, CRS handling is overridden to `EPSG:9055+3855`,
# and pixel elevations are vertically adjusted using the appropriate PROJ
# grid.
# Sampling in this path is performed through the GDAL `/vsis3/` driver.

dataset_val, dataset_file = sample_one("esa-copernicus-30meter")
print(f"  value={dataset_val:.2f}")
print(f"  file={dataset_file}")

# ---
# Now access the same COP30 VRT as a user-provided URL (`user-url-raster`)
# using only `url`.
# In this mode, SlideRule uses the raster's native CRS metadata. Because this
# VRT CRS is incomplete for vertical adjustment, the returned elevation is
# typically biased (about +15 m at this test point).
#
# For `user-url-raster`, you must provide either `elevation_bands` or `bands`:
# - Use `elevation_bands` for bands that represent elevation and require
# vertical datum handling.
# - Use `bands` for scalar/discrete values (band names or numeric strings),
# which are sampled as-is without vertical adjustment.

url = "https://opentopography.s3.sdsc.edu/raster/COP30/COP30_hh.vrt"

user_raw_val, user_raw_file = sample_one("user-url-raster", {"url": url, "elevation_bands": ["1"]})
print(f"  value={user_raw_val:.2f}")
print(f"  file={user_raw_file}")
print(f"  delta_vs_dataset={user_raw_val - dataset_val:+.2f} m")

# --- Scalar Band Sampling (`bands`) ---
#
# The next case uses `bands` instead of `elevation_bands`. This tells
# SlideRule to treat the band as scalar/discrete data.
# No vertical datum adjustment is applied in this mode, even if `target_crs`
# or `proj_pipeline` is provided.

user_scalar_raw_val, user_scalar_raw_file = sample_one(
    "user-url-raster",
    {"url": url, "bands": ["1"]}
)
print("Scalar mode (bands) with url only")
print(f"  value={user_scalar_raw_val:.2f}")
print(f"  file={user_scalar_raw_file}")
print(f"  delta_vs_dataset={user_scalar_raw_val - dataset_val:+.2f} m")

# ---
# Continue using the user-specified raster, but provide `target_crs` so
# SlideRule can apply the correct vertical reference handling for elevation
# bands.

user_target_val, user_target_file = sample_one("user-url-raster", {"url": url, "target_crs": "EPSG:9055+3855", "elevation_bands": ["1"]})
print(f"  value={user_target_val:.2f}")
print(f"  file={user_target_file}")
print(f"  delta_vs_dataset={user_target_val - dataset_val:+.2f} m")

# ---
# Another way to achieve the same elevation correction is to provide an
# explicit `proj_pipeline`.
# In this mode, SlideRule keeps raster CRS metadata but uses your pipeline as
# the coordinate operation instead of auto-building a transform from
# source/target CRS definitions.

pipeline = "+proj=pipeline \
            +step +proj=axisswap +order=2,1 \
            +step +proj=unitconvert +xy_in=deg +xy_out=rad \
            +step +proj=cart +ellps=GRS80 \
            +step +inv +proj=helmert +x=-0.0007 +y=-0.0012 +z=0.0261 +rx=0 +ry=0 +rz=0 \
                    +s=-0.00212 +dx=-0.0001 +dy=-0.0001 +dz=0.0019 +drx=0 +dry=0 +drz=0 \
                    +ds=-0.00011 +t_epoch=2010 +convention=position_vector \
            +step +inv +proj=cart +ellps=WGS84 \
            +step +inv +proj=vgridshift +grids=us_nga_egm08_25.tif +multiplier=1 \
            +step +proj=unitconvert +xy_in=rad +xy_out=deg \
            +step +proj=axisswap +order=2,1"

user_pipeline_val, user_pipeline_file = sample_one("user-url-raster", {"url": url, "proj_pipeline": pipeline, "elevation_bands": ["1"]})
print(f"  value={user_pipeline_val:.2f}")
print(f"  file={user_pipeline_file}")
print(f"  delta_vs_dataset={user_pipeline_val - dataset_val:+.2f} m")

# --- Public AWS URL Example ---
#
# `user-url-raster` can also sample rasters from public AWS-hosted URLs over
# HTTPS (`/vsicurl/`) when no sign-in is required.

aws_url = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt"
aws_lon = -108.0
aws_lat = 39.0

aws_gdf = raster.sample(
    "user-url-raster",
    [[aws_lon, aws_lat]],
    parms={"url": aws_url, "elevation_bands": ["1"]}
)

if len(aws_gdf) == 0:
    raise RuntimeError("No samples returned for public AWS URL example")

print("Public AWS URL sample")
print(f"  value={aws_gdf['value'].iat[0]:.2f}")
print(f"  file={aws_gdf['file'].iat[0]}")

# ---
# For ICESat-2-driven sampling workflows (ATLxx inputs), prefer setting
# `target_crs` when the VRT/raster requires a specific destination CRS because
# the source CRS is already determined by the product stream.
#
# Pipeline-only usage can be sufficient for simple value sampling, but
# remember that SlideRule enforces traditional GIS axis order (lon/lat), and
# raster CRS metadata is also used for metric operations such as resampling
# radius and slope/aspect calculations.
#
# If `proj_pipeline` and `target_crs` are inconsistent, coordinate transforms
# will execute while metric-derived outputs can become physically incorrect.
# For robust scientific use, provide `target_crs`, or provide both a fully
# specified pipeline and a matching `target_crs`.
