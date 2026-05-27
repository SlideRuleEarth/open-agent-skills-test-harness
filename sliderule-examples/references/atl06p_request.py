# --- ATL06-SR Request Example ---
#
# The `atl06p` endpoint subsets the ATL03 photon cloud and runs them through a
# modified ATL06 surface fitting algorithm to produce a customized ATL06
# dataset.
#
# #### What is demonstrated
#
# * The `icesat2.atl06p` API is used to perform a SlideRule proxied processing
# request of the Grand Mesa region
# * The `icesat2.cmr` and `icesat2.h5p` API's are used to manually retrieve
# specific ATL06 datasets corresponding to the Grand Mesa region
# * The `pyproj` and `shapely` packages are used to subset ATL06 data that was
# manually retrieved
# * The `matplotlib` package is used to plot the data processed by SlideRule
# alongside the manually retrieved and subsetted data
#
# #### Notes
#
# The resulting datasets plotted at the bottom of the notebook show that
# existing ATL06 data is not available for the entire Grand Mesa region.  By
# using the SlideRule API to process ATL03 data and produce a customized ATL06
# dataset, elevation data can be returned for the entire region of interest.

import time
import geopandas as gpd
import matplotlib.pyplot as plt
import concurrent.futures
from pyproj import Transformer, CRS
from shapely.geometry import Polygon, Point
from sliderule import sliderule, icesat2, earthdata, h5

# --- Configure the SlideRule Python Client ---

# configure the client to output verbose log messages
sliderule.init(verbose=True)

# show how many nodes are running in the public cluster
num_nodes, _ = sliderule.update_available_servers()
print(f'Cluster running {num_nodes} nodes')

# --- Define an area of interest ---

region = sliderule.toregion('grandmesa.geojson')

# display raw polygon
print(f'Polygon: {region["poly"]}')

# display plot of polygon
region["gdf"].plot()

# --- Make `atl06p` processing request ---

# define processing parameters
parms = {
    "poly": region["poly"],
    "srt": icesat2.SRT_LAND,
    "cnf": icesat2.CNF_SURFACE_HIGH,
    "ats": 10.0,
    "cnt": 10,
    "len": 40.0,
    "res": 20.0
}

# make request
atl06_sr = icesat2.atl06p(parms)

# Display Statistics
print("Reference Ground Tracks: {}".format(atl06_sr["rgt"].unique()))
print("Cycles: {}".format(atl06_sr["cycle"].unique()))
print("Received {} elevations".format(atl06_sr.shape[0]))

# --- Plot `atl06p` results ---

# prepare coordinate lists for plotting the region of interest polygon
region_lon = [e["lon"] for e in region["poly"]]
region_lat = [e["lat"] for e in region["poly"]]

# generate plot
f, ax = plt.subplots()
ax.set_title("ATL06-SR Points")
ax.set_aspect('equal')
atl06_sr.plot(ax=ax, column='h_mean', cmap='inferno', s=0.1)
ax.plot(region_lon, region_lat, linewidth=1, color='g');

atl06_sr

# --- Retrieve elevations directly from the ICESat-2 ATL06 Standard Data Product using `icesat2.h5p` API ---
#
# This method of reading H5 data directly is the recommended method and runs
# faster than `icesat2.h5` as each dataset is read in parallel on the server
# and shares a common cache. The code below has a couple other optimizations
# including only sampling every 10th coordinate for point inclusion, and
# reading the lat,lon information first and then reading only the necessary
# heights.
#
# See https://nsidc.org/data/atl06 for the source dataset.

# read ATL06 resource and return heights within polygon
def subsetted_read(resource, polygon, transformer):

    heights = []
    latitudes = []
    longitudes = []
    api_time = 0

    try:

        # List of tracks to read
        tracks = ["1l", "1r", "2l", "2r", "3l", "3r"]

        # Build list of each lat,lon dataset to read
        geodatasets = []
        for track in tracks:
            prefix = "/gt"+track+"/land_ice_segments/"
            geodatasets.append({"dataset": prefix+"latitude", "startrow": 0, "numrows": -1})
            geodatasets.append({"dataset": prefix+"longitude", "startrow": 0, "numrows": -1})

        # Read lat,lon from resource
        api_start = time.perf_counter()
        geocoords = h5.h5p(geodatasets, resource, "icesat2")
        api_stop = time.perf_counter()
        api_time += (api_stop - api_start)

        # Build list of the subsetted h_li datasets to read
        hidatasets = []
        for track in tracks:
            prefix = "/gt"+track+"/land_ice_segments/"
            lat_dataset = geocoords[prefix+"latitude"]
            lon_dataset = geocoords[prefix+"longitude"]
            startrow = -1
            numrows = -1
            index = 0
            while index < len(lat_dataset):
                lat = lat_dataset[index]
                lon = lon_dataset[index]
                c = transformer.transform(lat, lon)
                point = Point(c[0], c[1])
                intersect = point.within(polygon)
                if startrow == -1 and intersect:
                    startrow = index
                elif startrow != -1 and not intersect:
                    break
                index += 10 # only sample values for speed increase
            if startrow >= 0:
                numrows = index - startrow
            if numrows > 0:
                hidatasets.append({"dataset": prefix+"h_li", "startrow": startrow, "numrows": numrows, "prefix": prefix})

        # Read h_li from resource
        if len(hidatasets) > 0:
            api_start = time.perf_counter()
            hivalues = h5.h5p(hidatasets, resource, "icesat2")
            api_stop = time.perf_counter()
            api_time += (api_stop - api_start)

        # Append results
        for entry in hidatasets:
            heights += hivalues[entry["prefix"]+"h_li"].tolist()
            latitudes += geocoords[entry["prefix"]+"latitude"][entry["startrow"]:entry["startrow"]+entry["numrows"]].tolist()
            longitudes += geocoords[entry["prefix"]+"longitude"][entry["startrow"]:entry["startrow"]+entry["numrows"]].tolist()

    except Exception as e:
        pass

    # Return results
    return {"resource":  resource,
            "h_li":      heights,
            "latitude":  latitudes,
            "longitude": longitudes,
            "time":      api_time}

# Initialize Total Time Spent Inside API
api_total_time = 0

# Query ATL06 Files from NASA CMR System
resources = earthdata.cmr(polygon=region["poly"], short_name='ATL06')
print('Retrieved %s resources that intersect region' % (len(resources)))

# Create Projection Transformer
transformer = Transformer.from_crs(4326, 3857) # GPS to Web Mercator

# Project Polygon
pregion = []
for point in region["poly"]:
    ppoint = transformer.transform(point["lat"], point["lon"])
    pregion.append(ppoint)
polygon = Polygon(pregion)

# Initialize Result Dataset
results = {"latitude": [], "longitude": [], "h_li":[]}

# Update Available Servers #
num_servers, _ = sliderule.update_available_servers()
print('Allocating %d workers across %d processing nodes' % (num_servers, num_servers))

# Make Parallel Processing Requests
with concurrent.futures.ThreadPoolExecutor(max_workers=num_servers) as executor:
    futures = [executor.submit(subsetted_read, resource, polygon, transformer) for resource in resources]
    # Wait for Results
    result_cnt = 0
    for future in concurrent.futures.as_completed(futures):
        result_cnt += 1
        result = future.result()
        print('%d results returned for %s (%d out of %d)' % (len(result["h_li"]), result["resource"], result_cnt, len(resources)))
        results["h_li"] += result["h_li"]
        results["latitude"] += result["latitude"]
        results["longitude"] += result["longitude"]
        api_total_time += result["time"]

# Build GeoDataframe of ATL06 Standard Data Product
geometry = gpd.points_from_xy(results["longitude"], results["latitude"])
df = gpd.pd.DataFrame(results)
atl06_sdp = gpd.GeoDataFrame(df, geometry=geometry)

# Filter Height Values
atl06_sdp = atl06_sdp[atl06_sdp["h_li"] < 10000]

# --- Trim ATL06 points to region polygon ---

# Create shapely polygon
pregion = Polygon(zip(region_lon, region_lat))

# Using geopandas
idx = atl06_sdp.within(pregion)
atl06_sdp = atl06_sdp[idx]

# --- Plot ATL06-SR vs. ATL06 ---

# Set color ramp limits
vmin, vmax = atl06_sdp['h_li'].quantile((0.02, 0.98))

# Create dictionary of common keyword arguments
plot_kw = {'cmap':'inferno', 's':0.1, 'vmin':vmin, 'vmax':vmax}

f, axa = plt.subplots(2,1, figsize=(10,10)) # sharex=True, sharey=True
axa[0].set_title("ATL06-SR Points")
atl06_sr.plot(ax=axa[0], column='h_mean', **plot_kw)
axa[1].set_title("ATL06 Points")
atl06_sdp.plot(ax=axa[1], column='h_li', **plot_kw)

for ax in axa:
    # Plot the region polygon
    ax.plot(region_lon, region_lat, linewidth=1, color='g')
    ax.set_aspect('equal');
    ax.set_facecolor('lightgray')
