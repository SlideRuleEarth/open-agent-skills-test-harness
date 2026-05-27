# Imports
import logging
import matplotlib.pyplot as plt
from sliderule import sliderule, gedi, earthdata

# Configuration
verbose = True
loglevel = logging.INFO

# --- How to access GEDI02_A data for an area of interest ---
#
# The code below takes about 30 seconds to execute and processes the 138 GEDI
# L2A granules that intersect the area of interest defined by the
# grandmesa.geojson file.  It is also filtering all measurements that don't
# have the L2 quality flag set or have the degrade flag set.

# call sliderule
gedi.init(verbose=verbose, loglevel=loglevel)
parms = {
    "poly": sliderule.toregion("grandmesa.geojson")["poly"],
    "degrade_filter": True,
    "l2_quality_filter": True,
}
gedi02a = gedi.gedi02ap(parms)
gedi02a

# plot elevations
f, ax = plt.subplots(1, 1, figsize=[12,8])
ax.set_title("Elevations Lowest Mode")
ax.set_aspect('equal')
vmin_lm, vmax_lm = gedi02a['elevation_lm'].quantile((0.05, 0.95))
gedi02a.plot(ax=ax, column='elevation_lm', cmap='inferno', s=0.1, vmin=vmin_lm, vmax=vmax_lm)

# --- How to list GEDI02_A granules that intersect an area of interest ---
#
# If you are just interested in knowing what granules intersect an area of
# interest, you can use the `earthdata` module in the SlideRule client.

region = sliderule.toregion("grandmesa.geojson")
granules = earthdata.cmr(short_name="GEDI02_A", polygon=region["poly"])
granules

# --- How to sample 3DEP at each GEDI02_A point for a granule in the area of interest ---
#
# The code below reads a GEDI L2A granule and for each elevation it samples
# the 3DEP 1m DEM raster whose measurements are closest in time to the GEDI
# measurement. The resulting data frame includes the data from both GEDI and
# 3DEP.

# call sliderule
gedi.init(verbose=verbose, loglevel=loglevel)
parms = {
    "poly": sliderule.toregion("grandmesa.geojson")["poly"],
    "degrade_filter": True,
    "quality_filter": True,
    "beam": 11,
    "samples": {"3dep": {"asset": "usgs3dep-1meter-dem", "use_poi_time": True}}
}
gedi02a = gedi.gedi02ap(parms, resources=['GEDI02_A_2019109210809_O01988_03_T02056_02_003_01_V002.h5'])
gedi02a

# plot elevations
gdf = gedi02a[gedi02a["3dep.value"].notna()]
fig,ax = plt.subplots(num=None, figsize=(10, 8))
fig.set_facecolor('white')
fig.canvas.header_visible = False
ax.set_title("Elevations between GEDI and 3DEP")
ax.set_xlabel('UTC')
ax.set_ylabel('height (m)')
ax.yaxis.grid(True)
sc1 = ax.scatter(gdf.index.values, gdf["elevation_lm"].values, c='blue', s=2.5)
sc2 = ax.scatter(gdf.index.values, gdf["3dep.value"].values, c='red', s=2.5)
plt.show()
