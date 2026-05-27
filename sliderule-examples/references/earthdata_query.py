# --- EarthData Query Example ---
#
# Demonstrate methods for querying CMR, CMR-STAC, and TNM

# --- Imports ---

import json
from sliderule import sliderule, earthdata

# --- Configuration ---

sliderule.init("slideruleearth.io", verbose=True)

aoi = sliderule.toregion("grandmesa.geojson")

# --- 1. General CMR Query using Request Parameters ---
# Finds all ATL03 granules that intersect an area of interest

parms = {
    "asset": "icesat2",
    "poly": aoi["poly"]
}
resources = earthdata.search(parms)
print(f'Query returned {len(resources)} resources')

# --- 2. More Specific CRM Query using Request Parameters ---
# Finds all ATL06 granules that interst an area of interest between two
# collection dates

parms = {
    "asset": "icesat2-atl06",
    "poly": aoi["poly"],
    "t0": "2022-01-01T00:00:00",
    "t1": "2023-01-01T00:00:00",
}
resources = earthdata.search(parms)
print(f'Query returned {len(resources)} resources')

# --- 3. Directly Query CMR ---
# Finds all version `006` ATL06 granules that interset an area of interest

resources = earthdata.cmr(short_name="ATL06", version="006", polygon=aoi["poly"])
print(f'Query returned {len(resources)} resources')

# --- 4. Directly Query STAC ---
# Finds all HLS tiles thst intersect the area of interest and were collected
# between the two provided dates

response = earthdata.stac(short_name="HLS", polygon=aoi["poly"], time_start="2021-01-01T00:00:00Z", time_end="2022-01-01T23:59:59Z")
geojson = json.loads(response)
print(f'Returned {geojson["context"]["returned"]} features')

# --- 5. Make CMR Request using SlideRule Server ---
# Make same request as above (when earthdata.search was used), but this time
# use the SlideRule server to perform the CMR query

parms = {
    "asset": "icesat2",
    "poly": aoi["poly"]
}
response = sliderule.source("earthdata", parms)
print(f'Query returned {len(response)} resources')
