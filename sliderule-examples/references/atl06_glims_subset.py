# --- Subset ATL06 to GLIMS Shapefile ---
#
# This notebook uses SlideRule to retrieve ATL06 segments that intersect a
# provided shapefile.
# 1. Generate the convex hull of the region of interest characterized by the
# shapefile so that we have a polygon to submit to CMR to get all ATL06
# granules that could possibly intersect the region of interest.
# 2. Convert the shapefile to a geojson, and in the process, buffer out the
# polygons so that no points are missed by SlideRule
# 3. Make the processing request to SlideRule to retrieve all ATL06 segments
# within the region of interest
# 4. Trim the returned values to the original shapefile to get rid of any
# segments that were only included in the bufferred region

from sliderule import sliderule, icesat2, earthdata
from shapely.geometry import Polygon, MultiPolygon, mapping
import geopandas as gpd
import geojson

# --- Read in shapefile ---

# read shapefile
gdf = gpd.read_file("glims_polygons.shp")

# --- Get granules that intersect larger area of interest ---

# create a multipolygon with simplified internal polygons (needed to get convex hull)
polygons = list(gdf.geometry)
cleaned_polygons = [polygon.convex_hull for polygon in polygons]
cleaned_multipoly = MultiPolygon(cleaned_polygons)

# build geojson of multipolygon
cleaned_glims_geojson = "cleaned_glims.geojson"
geojson_obj = geojson.Feature(geometry=mapping(cleaned_multipoly))
with open(cleaned_glims_geojson, "w") as geojson_file:
    geojson.dump(geojson_obj, geojson_file)

# get sliderule region of geojson
region = sliderule.toregion(cleaned_glims_geojson)

# query CMR for granules that intersect larger area of interest
cmr_parms = {
    "asset": "icesat2-atl06",
    "poly": region["poly"]
}
earthdata.set_max_resources(400)
granules = earthdata.search(cmr_parms)
print(f"Found {len(granules)} granules intersecting GLIMS polygons")
print("\n".join(granules[:10]))

# --- Get detailed geojson of area of interest ---

# create a multipolygon of internal polygons
multipoly = MultiPolygon(list(gdf.geometry))

# buffer out multiplygon
buffered_multipoly = multipoly.buffer(0.01)

# build geojson of multipolygon
glims_geojson = "glims.geojson"
geojson_obj = geojson.Feature(geometry=mapping(buffered_multipoly))
with open(glims_geojson, "w") as geojson_file:
    geojson.dump(geojson_obj, geojson_file)

g = gpd.read_file("glims.geojson")
g.plot(markersize=1)

# open the geojson and read in as raw bytes
with open(glims_geojson, mode='rt') as file:
    datafile = file.read()

# build polygon + raster mask for Sliderule
cellsize = 0.001
region = sliderule.toregion(cleaned_glims_geojson, cellsize=cellsize)

# --- Use sliderule to generate subsetted ATL06 over area of interest ---

# initialize the client
sliderule.init(verbose=True)

# atl06 subsetting parameters
atl06_parms = {
    "poly": region["poly"],
    "region_mask": region["raster"],
}

# make processing request
atl06 = icesat2.atl06sp(atl06_parms, resources=granules)

# display results
atl06

# plot results
atl06.plot(markersize=1)

# save results to a geoparquet file
atl06.to_parquet("glims_atl06.geoparquet")

# --- Trim the output to GLIMS polygons ---
# The subsetting on SlideRule used a buffered multipolygon so that it wouldn't
# miss any data.  The steps below further trim the data to the exact region of
# interest.

# read data from geoparquet file, set ICESat-2 crs
atl06rb = gpd.read_parquet("glims_atl06.geoparquet")
gdf = gdf.set_crs("EPSG:7912", allow_override=True)

# trim geodataframe to initial shapefile
trimmed_gdf = gpd.sjoin(atl06rb, gdf, how='inner', predicate='within')

# plot trimmed results
trimmed_gdf.plot(markersize=1)

# save trimmed results
trimmed_gdf.to_parquet("glims_subsetted_atl06.geoparquet")

# display trimmed results
trimmed_gdf
