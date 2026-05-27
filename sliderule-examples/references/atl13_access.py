# --- Accessing ATL13 data using lake names, reference ids, and contained coordinates ---
#
# SlideRule provides an `Asset Metadata Service` to lookup ATL13 granules
# using different variables:
# * reference id
# * lake name
# * coordinate within the lake
#
# SlideRule can also be used to directly subset ATL13 using the above
# variables.

# Imports
from sliderule import sliderule

# Setup
sliderule.init(verbose=True)

# --- Retrieve ATL13 from ___SlideRule___ using `Reference ID` ---
#
# ##### Metadata from a `shell` script

# ---
# ```bash
# curl -X GET -d "{\"asset\": \"icesat2-atl13\", \"atl13\": {\"refid\":
# 5952002394}}" https://sliderule.slideruleearth.io/source/earthdata
# ```

# --- Metadata from a `python` script ---

response = sliderule.source("earthdata", {"asset": "icesat2-atl13", "atl13": {"refid": 5952002394}})
response

# --- Data from a `python` script ---

parms = { "atl13": { "refid": 5952002394 } }
gdf = sliderule.run("atl13x", parms)

# Generate a plot of the results
gdf.plot(column='ht_ortho', cmap='viridis', legend=True)

# Display the returned GeoDataFrame
gdf

# Display mapping of srcid to granule names
gdf.attrs["meta"]["srctbl"]

# --- Retrieve ATL13 from ___SlideRule___ using `Lake Name` ---
#
# ##### Metadata from a `shell` script

# ---
# ```bash
# curl -X GET -d "{\"asset\": \"icesat2-atl13\", \"atl13\": {\"name\":
# \"Caspian Sea\"}, \"max_resources\": 1500}"
# https://sliderule.slideruleearth.io/source/earthdata
# ```

# --- Metadata from a `python` script ---

response = sliderule.source("earthdata", {"asset": "icesat2-atl13", "atl13": {"name": "Caspian Sea"}, "max_resources": 1500})
response

# --- Data from a `python` script ---

# define area of interest as a geojson object because the Lake being request is very large and we only want a subset of it
area_of_interest = {
  "type": "FeatureCollection",
  "features": [ { "type": "Feature", "properties": {}, "geometry": { "coordinates": [ [ [49.63305859097062, 43.517023064094445], [49.63305859097062, 43.26673730943335], [50.39096571145933, 43.26673730943335], [50.39096571145933, 43.517023064094445], [49.63305859097062, 43.517023064094445] ] ], "type": "Polygon" } } ]
}
# process geojson into a format sliderule can understand
region = sliderule.toregion(area_of_interest)
# make atl13x request
parms = { "atl13": { "name": "Caspian Sea" }, "poly": region["poly"], "max_resources": 500, "t0": '2022-01-01', "t1": '2023-01-01' }
gdf = sliderule.run("atl13x", parms)
gdf

# --- Retrieve ATL13 from ___SlideRule___ using `Coordinate` ---
#
# ##### Metadata from a `shell` script

# ---
# ```bash
# curl -X GET -d "{\"asset\": \"icesat2-atl13\", \"atl13\":
# {\"coord\":{\"lon\":-77.40162711974297,\"lat\":38.48769543754824}}}"
# https://sliderule.slideruleearth.io/source/earthdata
# ```

# --- Metadata from a `python` script ---

coordinates = [-77.40162711974297, 38.48769543754824]
response = sliderule.source("earthdata", {"asset": "icesat2-atl13", "atl13": {"coord": {"lon": coordinates[0], "lat": coordinates[1]}}})
response

# --- Data from a `python` script ---

coordinates = [-77.40162711974297, 38.48769543754824]
parms = { "atl13": {"coord": {"lon": coordinates[0], "lat": coordinates[1]}} }
gdf = sliderule.run("atl13x", parms)
gdf
