# --- ArcticDEM Strips Example ---
#
# ### Purpose
# Demonstrate how to work with individual strips when sampling ArcticDEM at
# ATL06-SR points
#
# ### Prerequisites
# 1. Access to the PGC S3 bucket `pgc-opendata-dems`
# 2. `gdalinfo` tool installed local to jupyter lab

# --- Import Packages ---

import sliderule
from sliderule import icesat2
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pyproj
import re
import os

# --- Initialize Python Client ---

sliderule.init(verbose=True)

# --- Build Region of Interest ---

xy0=np.array([  -73000., -2683000.])
transformer = pyproj.Transformer.from_crs(3413, 4326)
xyB=[xy0[0]+np.array([-1, 1, 1, -1, -1])*1.e4, xy0[1]+np.array([-1, -1, 1, 1, -1])*1.e4]
llB=transformer.transform(*xyB)
poly=[{'lat':lat,'lon':lon} for lat, lon in zip(*llB)]
plist = []
for p in poly:
    plist += p["lat"],
    plist += p["lon"],
region_of_interest = sliderule.toregion(plist)
region_of_interest["gdf"].plot()

# --- Make Processing Request ---
# ATL06-SR request includes the `samples` parameter to specify that ArcticDEM
# Strips dataset should be sampled at each generated ATL06 elevation.

parms = { "poly": region_of_interest["poly"],
          "cnf": "atl03_high",
          "ats": 10.0,
          "cnt": 5,
          "len": 40.0,
          "res": 120.0,
          "rgt": 658,
          "time_start":'2020-01-01',
          "time_end":'2021-01-01',
          "samples": {"strips": {"asset": "arcticdem-strips", "with_flags": True}} }
gdf = icesat2.atl06p(parms)

# --- Print Out File Directory ---
# When a GeoDataFrame includes samples from rasters, each sample value has a
# file id that is used to look up the file name of the source raster for that
# value.

gdf

gdf.attrs['file_directory']

# --- Pull Out Bounding Box of Raster ---
# This step requires AWS credentials to be able to access S3 and `gdalinfo` be
# installed on the host machine to read the bounding box for each raster
# sampled by SlideRule.

# helper functions
def getXY(line):
    line = line.replace("(","$")
    line = line.replace(")","$")
    point = line.split("$")[1]
    coord = point.split(",")
    x = float(coord[0].strip())
    y = float(coord[1].strip())
    return x, y

def getLonLat(line):
    line = line.replace("(","$")
    line = line.replace(")","$")
    point = line.split("$")[3]
    coord = point.split(",")
    deg, minutes, seconds, direction = re.split('[d\'"]', coord[1].strip())
    lon = (float(deg) + float(minutes)/60 + float(seconds)/(60*60)) * (-1 if direction in ['W', 'S'] else 1)
    deg, minutes, seconds, direction = re.split('[d\'"]', coord[0].strip())
    lat = (float(deg) + float(minutes)/60 + float(seconds)/(60*60)) * (-1 if direction in ['W', 'S'] else 1)
    return [lon, lat]

def getBB(dem):
    os.system("gdalinfo {} > /tmp/r.txt".format(dem))
    with open("/tmp/r.txt", "r") as file:
        lines = file.readlines()
        for line in lines:
            if "Upper Left" in line:
                ul = getLonLat(line)
            elif "Lower Left" in line:
                ll = getLonLat(line)
            elif "Upper Right" in line:
                ur = getLonLat(line)
            elif "Lower Right" in line:
                lr = getLonLat(line)
    return ul + ll + lr + ur + ul

file_dict = {}
for file_id, file_name in gdf.attrs['file_directory'].items():
    if "bitmask" in file_name:
        continue
    if file_name not in file_dict:
        file_dict[file_name] = {"ids": []}
    file_dict[file_name]["ids"].append(file_id)

# get boundaries for each raster
for file_name in file_dict:
    print("Retrieving raster info for:", file_name)
    rlist = getBB(file_name)
    file_dict[file_name]["region"] = sliderule.toregion(rlist)

# --- Pull Out Individual DEM Values and Put in Separate Columns ---

def getValue(x, file_ids):
    for file_id in file_ids:
        l = np.where(x['strips.file_id'] == file_id)[0]
        if len(l) == 1:
            return x['strips.value'][l[0]]
    return None
sampled_data = gdf[gdf['strips.time'].notnull()]
for file_name in file_dict:
    sampled_data[file_name] = sampled_data.apply(lambda x: getValue(x, file_dict[file_name]["ids"]), axis=1)

# --- Plot Overlays of Boundaries and Returns ---

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    fig = plt.figure(num=None, figsize=(24, 24))
    region_lons = [p["lon"] for p in region_of_interest["poly"]]
    region_lats = [p["lat"] for p in region_of_interest["poly"]]
    ax = {}
    k = 0
    for file_name in file_dict:
        raster = file_dict[file_name]
        raster_lons = [p["lon"] for p in raster["region"]["poly"]]
        raster_lats = [p["lat"] for p in raster["region"]["poly"]]
        plot_data = sampled_data[sampled_data[file_name].notnull()]
        ax[k] = plt.subplot(5,4,k+1)
        gdf.plot(ax=ax[k], column='h_mean', color='y', markersize=0.5)
        plot_data.plot(ax=ax[k], column='h_mean', color='b', markersize=0.5)
        ax[k].plot(region_lons, region_lats, linewidth=1.5, color='r', zorder=2)
        ax[k].plot(raster_lons, raster_lats, linewidth=1.5, color='g', zorder=2)
        k += 1
    plt.tight_layout()

# --- Plot the Different ArcticDEM Values against the SlideRule ATL06-SR Values ---

# Select DEM File ID
file_name = list(file_dict.keys())[0]

# Setup Plot
fig,ax = plt.subplots(num=None, figsize=(10, 8))
ax.set_title("SlideRule vs. ArcticDEM Elevations")
ax.set_xlabel('distance (m)')
ax.set_ylabel('height (m)')
legend_elements = []

# Filter Data to Plot
plot_data = sampled_data[sampled_data[file_name].notnull()]

# Set X Axis
x_axis = plot_data["x_atc"]

# Plot SlideRule ATL06 Elevations
sc1 = ax.scatter(x_axis, plot_data["h_mean"].values, c='red', s=2.5)
legend_elements.append(matplotlib.lines.Line2D([0], [0], color='red', lw=6, label='ATL06-SR'))

# Plot ArcticDEM Elevations
sc2 = ax.scatter(x_axis, plot_data[file_name].values, c='blue', s=2.5)
legend_elements.append(matplotlib.lines.Line2D([0], [0], color='blue', lw=6, label='ArcticDEM'))

# Display Legend
lgd = ax.legend(handles=legend_elements, loc=3, frameon=True)
lgd.get_frame().set_alpha(1.0)
lgd.get_frame().set_edgecolor('white')

# Show Plot
plt.show()
