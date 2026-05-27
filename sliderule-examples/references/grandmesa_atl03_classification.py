# --- Generating ATL03 photon classifications using ATL08 and YAPC ---
#
# Plot ATL03 data with different classifications for a region over the Grand
# Mesa, CO region
#
# - [ATL08 Land and Vegetation Height product](https://nsidc.org/data/atl08)
# photon classification
# - Experimental YAPC (Yet Another Photon Classification) photon-density-based
# classification
#
# ### What is demonstrated
#
# * The `icesat2.atl03x` API is used to perform a SlideRule parallel
# subsetting request of the Grand Mesa region
# * The `earthdata.cmr` API's is used to find specific ATL03 granules
# corresponding to the Grand Mesa region
# * The `matplotlib` package is used to plot the ATL03 data subset by
# SlideRule

import warnings
warnings.filterwarnings("ignore") # suppress warnings

import numpy as np
import matplotlib.pyplot as plt
from sliderule import sliderule, icesat2, earthdata

sliderule.init(verbose=True)

# --- Intro ---
# This notebook demonstrates how to use the SlideRule Icesat-2 API to retrieve
# ATL03 data with two different classifications, one based on the external
# ATL08-product classifications, designed to distinguish between vegetation
# and ground returns, and the other based on the experimental YAPC (Yet
# Another Photon Class) algorithm.

# --- Retrieve ATL03 elevations with ATL08 classifications ---
#
# define a polygon to encompass Grand Mesa, and pick an ATL03 granule that has
# good coverage over the top of the mesa.  Note that this granule was captured
# at night, under clear-sky conditions.  Other granules are unlikely to have
# results as clear s these.

%%time

# build sliderule parameters for ATL03 subsetting request
parms = {
    # processing parameters
    "srt": icesat2.SRT_LAND,
    "len": 20,
    "res": 20,
    # classification and checks
    # still return photon segments that fail checks
    "pass_invalid": True,
    # all photons
    "cnf": -2,
    # all land classification flags
    "atl08_class": ["atl08_noise", "atl08_ground", "atl08_canopy", "atl08_top_of_canopy", "atl08_unclassified"],
    # all photons
    "yapc": dict(knn=0, win_h=6, win_x=11, min_ph=4, score=0),
}

# ICESat-2 data release
release = '006'

# region of interest
poly = [
  {'lat': 39.34603060272382, 'lon': -108.40601489205419},
  {'lat': 39.32770853617356, 'lon': -107.68485163209928},
  {'lat': 38.770676045922684, 'lon': -107.71081820956682},
  {'lat': 38.788639821001155, 'lon': -108.42635020791396},
  {'lat': 39.34603060272382, 'lon': -108.40601489205419}
]

# time bounds for CMR query
time_start = '2019-11-14'
time_end = '2019-11-15'

# find granule for each region of interest
granules_list = earthdata.cmr(short_name='ATL03', polygon=poly, time_start=time_start, time_end=time_end, version=release)

# create geodataframe
gdf = sliderule.run("atl03x", parms, aoi=poly, resources=granules_list)

gdf

# --- Reduce GeoDataFrame to plot a single beam ---
# - Convert coordinate reference system to compound projection

gdf.keys()

def reduce_dataframe(gdf, RGT=None, GT=None, spot=None, cycle=None, crs=4326):
    D3 = gdf.to_crs(crs) # convert coordinate reference system
    if RGT is not None:
        D3 = D3[D3["rgt"] == RGT]
    if GT is not None:
        D3 = D3[D3["gt"] == GT]
    if spot is not None:
        D3 = D3[D3["spot"] == spot]
    if cycle is not None:
        D3 = D3[D3["cycle"] == cycle]
    return D3

project_srs = "EPSG:26912+EPSG:5703"
D3 = reduce_dataframe(gdf, RGT=737, spot=1, crs=project_srs)

# --- Inspect Coordinate Reference System ---

D3.crs

# --- Plot the ATL08 classifications ---

plt.figure(figsize=[8,6])

colors={0:['gray', 'noise'],
        4:['gray','unclassified'],
        2:['green','canopy'],
        3:['lime', 'canopy_top'],
        1:['brown', 'ground']}
d0=np.min(D3['x_atc'])
for class_val, color_name in colors.items():
    ii=D3['atl08_class']==class_val
    plt.plot(D3['x_atc'][ii]-d0, D3['height'][ii],'o',
         markersize=1, color=color_name[0], label=color_name[1])
hl=plt.legend(loc=3, frameon=False, markerscale=5)
plt.gca().set_xlim([25000, 30000])
plt.gca().set_ylim([3050, 3300])

plt.ylabel('height, m')
plt.xlabel('$x_{ATC}$, m');

# --- Plot the YAPC classifications ---

plt.figure(figsize=[10,6])

d0=np.min(D3['x_atc'])
ii=np.argsort(D3['yapc_score'])
plt.scatter(D3['x_atc'][ii]-d0,
    D3['height'][ii],2, c=D3['yapc_score'][ii],
    vmin=100, vmax=255, cmap='plasma_r')
plt.colorbar(label='YAPC score')
plt.gca().set_xlim([25000, 30000])
plt.gca().set_ylim([3050, 3300])

plt.ylabel('height, m')
plt.xlabel('$x_{ATC}$, m');
