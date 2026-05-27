# --- Subsetting and filtering ATL24 data ---

# --- Imports ---

from sliderule import sliderule
import matplotlib.pyplot as plt
import numpy as np

# --- Configuration ---

# configure sliderule to output verbose log messages
sliderule.init(verbose=True)

# --- Plotting Helper Variables and Functions ---

# color map for ATL24 classifications
COLORS = {
    0: ['gray', 'unclassified'],
    40: ['red', 'bathymetry'],
    41: ['blue', 'sea_surface']
}

# plot ATL24 dataframes
def plot_atl24(gdf, x_min, x_max, column=None):
    start_xatc = np.min(gdf['x_atc'])
    fig,ax = plt.subplots(1, 1, figsize=(18, 6), constrained_layout=True)
    if column == None:
        for class_val, color_name in COLORS.items():
            ii=gdf["class_ph"]==class_val
            ax.plot(gdf['x_atc'][ii]-start_xatc, gdf['ortho_h'][ii], 'o', markersize=1, color=color_name[0], label=color_name[1])
    else:
        sc = ax.scatter(gdf['x_atc']-start_xatc, gdf['ortho_h'], c=gdf[column], cmap='viridis')
    ax.set_xlim(x_min, x_max)
    plt.show()

# plot ATL06 dataframes
def plot_atl06(gdf, x_min, x_max, column=None):
    start_xatc = np.min(gdf['x_atc'])
    fig,ax = plt.subplots(1, 1, figsize=(18, 6), constrained_layout=True)
    ax.plot(gdf['x_atc']-start_xatc, gdf['h_mean'], 'o', markersize=5, color='red')
    ax.set_xlim(x_min, x_max)
    plt.show()

# plot ATL03 dataframes
def plot_atl03(gdf, x_min, x_max, column=None):
    start_xatc = np.min(gdf['x_atc'])
    fig,ax = plt.subplots(1, 1, figsize=(18, 6), constrained_layout=True)
    if column == None:
        for class_val, color_name in COLORS.items():
            ii=gdf["atl24_class"]==class_val
            ax.plot(gdf['x_atc'][ii]-start_xatc, gdf['height'][ii], 'o', markersize=1, color=color_name[0], label=color_name[1])
    else:
        sc = ax.scatter(gdf['x_atc']-start_xatc, gdf['height'], c=gdf[column], cmap='viridis')
    ax.set_xlim(x_min, x_max)
    plt.show()

# --- Define Area of Interest (north shore of Dominican Republic) ---

aoi = [ { "lat": 19.42438470712139, "lon": -69.79907695695609  },
        { "lat": 19.31125534696085,  "lon": -69.79907695695609 },
        { "lat": 19.31125534696085,  "lon": -69.33527941905237 },
        { "lat": 19.42438470712139,  "lon": -69.33527941905237 },
        { "lat": 19.42438470712139,  "lon": -69.79907695695609 } ]

# --- (1) Quick Access ---

gdf1 = sliderule.run("atl24x", {}, aoi=aoi)

gdf1

gdf1.plot(column='ortho_h', cmap='viridis')

# --- (2) Access a Single Track ---

gdf2 = sliderule.run("atl24x", {"beams": "gt3r", "rgt": 202, "cycle": 12}, aoi=aoi)

gdf2

plot_atl24(gdf2, 0, 2500)

# --- (3) Detailed Access of a Single Track ---

parms = {
    "atl24": {
        "compact": False,
        "confidence_threshold": 0.0,
        "class_ph": ["unclassified", "sea_surface", "bathymetry"]
    },
    "beams": "gt3r",
    "rgt": 202,
    "cycle": 12
}
gdf3 = sliderule.run("atl24x", parms, aoi=aoi)

gdf3

plot_atl24(gdf3, 0, 2500)

plot_atl24(gdf3, 0, 2500, "confidence")

# --- (4) Access All ATL03 Photons using ATL24 as a Classifier ---

#
# UPDATE 2.4.26 - The following code will not work due to ATL03 release 006 being retired.
#   ATL03 granules used to generate ATL24 release 001 can no longer be discovered in CMR and accessed in Earthdata Cloud.
#
parms = {
    "cmr": {"version": "006"},
    "atl24": {
        "class_ph": ["unclassified", "sea_surface", "bathymetry"]
    },
    "cnf": -1,
    "beams": "gt3r",
    "rgt": 202,
    "cycle": 12
}
gdf4 = sliderule.run("atl03x", parms, aoi=aoi)

gdf4

plot_atl03(gdf4, 0, 2500)

# --- (5) Combine ATL03 Filters with ATL24 Classification ---

#
# UPDATE 2.4.26 - The following code will not work due to ATL03 release 006 being retired.
#   ATL03 granules used to generate ATL24 release 001 can no longer be discovered in CMR and accessed in Earthdata Cloud.
#
parms = {
    "cmr": {"version": "006"},
    "atl24": {
        "class_ph": ["unclassified", "sea_surface", "bathymetry"]
    },
    "cnf": 2,
    "yapc": {
        "version": 0,
        "score": 100
    },
    "beams": "gt3r",
    "rgt": 202,
    "cycle": 12
}
gdf5 = sliderule.run("atl03x", parms, aoi=aoi)

gdf5

plot_atl03(gdf5, 0, 2500)

plot_atl03(gdf5, 0, 2500, "yapc_score")

# --- (6) Run ATL06-SR Surface Fitting Algorithm on ATL24 Classified Photons ---

#
# UPDATE 2.4.26 - The following code will not work due to ATL03 release 006 being retired.
#   ATL03 granules used to generate ATL24 release 001 can no longer be discovered in CMR and accessed in Earthdata Cloud.
#
parms = {
    "cmr": {"version": "006"},
    "atl24": {
        "class_ph": ["bathymetry"]
    },
    "fit": {
        "res": 10,
        "len": 20,
        "pass_invalid": True
    },
    "cnf": -1,
    "beams": "gt3r",
    "rgt": 202,
    "cycle": 12
}
gdf6 = sliderule.run("atl03x", parms, aoi=aoi)

gdf6

plot_atl06(gdf6, 0, 2500)

# --- (7) Filtered and Ancillary Access to ATL24 ---

parms = {
    "atl24": {
        "class_ph": ["bathymetry"],
#        "confidence_threshold": 0.6,
#        "invalid_kd": False,
#        "invalid_wind_speed": False,
        "low_confidence": False,
#        "night": True,
#        "sensor_depth_exceeded": False,
        "anc_fields": ["index_ph", "index_seg"]
    },
    "beams": "gt3r",
    "rgt": 202,
    "cycle": 12
}
gdf7 = sliderule.run("atl24x", parms, aoi=aoi)

gdf7

plot_atl24(gdf7, 0, 2500)

# --- (8) Directly Query for Available ATL24 Resources ---

poly = [
    {"lat": 21.222261686673306, "lon": -73.78074797284968},
    {"lat": 21.07228912392266,  "lon": -73.78074797284968},
    {"lat": 21.07228912392266,  "lon": -73.51303956051089},
    {"lat": 21.222261686673306, "lon": -73.51303956051089},
    {"lat": 21.222261686673306, "lon": -73.78074797284968}
]

# find all ATL24 granules that intersect the above polygon and have a maximum mean depth of 10m and are collected in the summer
response = sliderule.source("earthdata", {"asset": "icesat2-atl24", "atl24": {"meandepth1": 10, "season": 2}, "poly": poly})
response
