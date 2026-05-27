# --- Sampling the ArcticDEM Mosaic ---
#
# ### Purpose
# Demonstrate how to sample the ArcticDEM at generated ATL06-SR points

# --- Import Packages ---

import matplotlib.pyplot as plt
import matplotlib
import sliderule
from sliderule import icesat2

# --- Initialize SlideRule Python Client ---

sliderule.init(verbose=True)

# --- Make Processing Request to SlideRule ---
# ATL06-SR request includes the `samples` parameter to specify that ArcticDEM
# Mosiac dataset should be sampled at each generated ATL06 elevation.

resource = "ATL03_20190314093716_11600203_007_01.h5"
region = sliderule.toregion("dicksonfjord.geojson")
parms = { "poly": region['poly'],
          "cnf": "atl03_high",
          "ats": 5.0,
          "cnt": 5,
          "len": 20.0,
          "res": 10.0,
          "samples": {"mosaic": {"asset": "arcticdem-mosaic", "radius": 10.0, "zonal_stats": True}} }
gdf = icesat2.atl06p(parms, resources=[resource])

# --- Display GeoDataFrame ---
# Notice the columns that start with "mosaic"

gdf

# --- Print Out File Directory ---
# When a GeoDataFrame includes samples from rasters, each sample value has a
# file id that is used to look up the file name of the source raster for that
# value.

gdf.attrs['file_directory']

# --- Demonstrate How To Access Source Raster Filename for Entry in GeoDataFrame ---

filedir = gdf.attrs['file_directory']
filedir[gdf['mosaic.file_id'].iloc[0]]

# --- Difference the Sampled Value from ArcticDEM with SlideRule ATL06-SR ---

gdf["value_delta"] = gdf["h_mean"] - gdf["mosaic.value"]
gdf["value_delta"].describe()

# --- Difference the Zonal Statistic Mean from ArcticDEM with SlideRule ATL06-SR ---

gdf["mean_delta"] = gdf["h_mean"] - gdf["mosaic.mean"]
gdf["mean_delta"].describe()

# --- Difference the Zonal Statistic Mdeian from ArcticDEM with SlideRule ATL06-SR ---

gdf["median_delta"] = gdf["h_mean"] - gdf["mosaic.median"]
gdf["median_delta"].describe()

# --- Plot the Different ArcticDEM Values against the SlideRule ATL06-SR Values ---

# Setup Plot
fig,ax = plt.subplots(num=None, figsize=(10, 8))
fig.set_facecolor('white')
fig.canvas.header_visible = False
ax.set_title("SlideRule vs. ArcticDEM Elevations")
ax.set_xlabel('UTC')
ax.set_ylabel('height (m)')
legend_elements = []

# Plot SlideRule ATL06 Elevations
df = gdf[(gdf['rgt'] == 1160) & (gdf['gt'] == 10) & (gdf['cycle'] == 2)]
sc1 = ax.scatter(df.index.values, df["h_mean"].values, c='red', s=2.5)
legend_elements.append(matplotlib.lines.Line2D([0], [0], color='red', lw=6, label='ATL06-SR'))

# Plot ArcticDEM Elevations
sc2 = ax.scatter(df.index.values, df["mosaic.value"].values, c='blue', s=2.5)
legend_elements.append(matplotlib.lines.Line2D([0], [0], color='blue', lw=6, label='ArcticDEM'))

# Display Legend
lgd = ax.legend(handles=legend_elements, loc=3, frameon=True)
lgd.get_frame().set_alpha(1.0)
lgd.get_frame().set_edgecolor('white')

# Show Plot
plt.show()

# --- Plot the Sampled Value and Zonal Statistic Mean Deltas to SlideRule ATL06-SR Values ---

# Setup Plot
fig,ax = plt.subplots(num=None, figsize=(10, 8))
fig.set_facecolor('white')
fig.canvas.header_visible = False
ax.set_title("Delta Elevations between SlideRule and ArcticDEM")
ax.set_xlabel('UTC')
ax.set_ylabel('height (m)')
ax.yaxis.grid(True)

# Plot Deltas
df1 = gdf[(gdf['rgt'] == 1160) & (gdf['gt'] == 10) & (gdf['cycle'] == 2)]
sc1 = ax.scatter(df1.index.values, df1["value_delta"].values, c='blue', s=2.5)

# Plot Deltas
df2 = gdf[(gdf['rgt'] == 1160) & (gdf['gt'] == 10) & (gdf['cycle'] == 2)]
sc2 = ax.scatter(df2.index.values, df2["mean_delta"].values, c='green', s=2.5)

# Show Plot
plt.show()
