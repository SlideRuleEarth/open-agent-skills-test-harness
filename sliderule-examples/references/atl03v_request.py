# --- ATL03 Viewer Request Example ---
#
# The `atl03v` endpoint subsets the ATL03 photon cloud at the segment level
# and returns a segment level dataset for quickly viewing data coverage.
#
# #### What is demonstrated
#
# * The `icesat2.atl03vp` API is used to perform a SlideRule proxied
# processing request of the Boulder Watershed region
# * The `matplotlib` and `geopandas` packages are used to plot the data
# returned by SlideRule

import matplotlib.pyplot as plt
from sliderule import icesat2

# --- Define an area of interest ---

# There are multiple ways to define an area of interest;
# below, we have a simple polygon defined as a list of
# dictionaries containing "lat" and "lon" keys; the first
# and last entries must match to close the polygon
region = [ {"lon":-105.82971551223244, "lat": 39.81983728534918},
           {"lon":-105.30742121965137, "lat": 39.81983728534918},
           {"lon":-105.30742121965137, "lat": 40.164048017973755},
           {"lon":-105.82971551223244, "lat": 40.164048017973755},
           {"lon":-105.82971551223244, "lat": 39.81983728534918} ]

# --- Make `atl03v` request ---

# Build ATL03 Viewer Request
parms = {
    "poly": region,
    "track": 1
}

# Request ATL03 Viewer Data
gdf = icesat2.atl03vp(parms)

# --- Display result statistics ---

print("Reference Ground Tracks: {}".format(gdf["rgt"].unique()))
print("Cycles: {}".format(gdf["cycle"].unique()))
print("Received {} segments".format(len(gdf)))

# --- Display GeoDataFrame ---

gdf

# --- Create plot of results ---

# Calculate Extent
lons = [p["lon"] for p in region]
lats = [p["lat"] for p in region]
lon_margin = (max(lons) - min(lons)) * 0.1
lat_margin = (max(lats) - min(lats)) * 0.1

# Create Plot
fig,(ax1) = plt.subplots(num=None, ncols=1, figsize=(12, 6))
box_lon = [e["lon"] for e in region]
box_lat = [e["lat"] for e in region]

# Plot SlideRule Ground Tracks
ax1.set_title("SlideRule Zoomed Ground Tracks")
vmin = gdf['segment_ph_cnt'].quantile(0.15)
vmax = gdf['segment_ph_cnt'].quantile(0.85)
gdf.plot(ax=ax1, column=gdf["segment_ph_cnt"], cmap='viridis', s=3.0, zorder=3, vmin=vmin, vmax=vmax, legend=True)
ax1.plot(box_lon, box_lat, linewidth=1.5, color='r', zorder=2)
ax1.set_xlim(min(lons) - lon_margin, max(lons) + lon_margin)
ax1.set_ylim(min(lats) - lat_margin, max(lats) + lat_margin)
ax1.set_aspect('equal', adjustable='box')

# Show Plot
plt.tight_layout()
