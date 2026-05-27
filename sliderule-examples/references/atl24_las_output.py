# --- Subsetting ATL24 data to LAS or LAZ output ---
#
# Notes:
# - LAS or LAZ output contains only XYZ coordinates.
# - No ATL24 attributes are included.
# - The file represents a lightweight photon point cloud only.

from sliderule import sliderule, las
import matplotlib.pyplot as plt
import numpy as np

# configure sliderule to output verbose log messages
sliderule.init(verbose=True)

# --- Define Area of Interest (north short of Dominican Republic) ---

aoi = [ { "lat": 19.42438470712139, "lon": -69.79907695695609  },
        { "lat": 19.31125534696085,  "lon": -69.79907695695609 },
        { "lat": 19.31125534696085,  "lon": -69.33527941905237 },
        { "lat": 19.42438470712139,  "lon": -69.33527941905237 },
        { "lat": 19.42438470712139,  "lon": -69.79907695695609 } ]

# --- Define output parameters as LAZ file and run ATL24 endpoint ---

output_path = "atl24_results.laz"

parms = {"output": {"path": output_path, "format": "laz", "open_on_complete": False}}

laz_file = sliderule.run("atl24x", parms, aoi=aoi)
laz_file

# --- Reload LAZ for plotting ---
# The LAZ file stores only XYZ; reload it to visualize the point cloud.

laz_gdf = las.load(output_path)
print(f"Loaded {len(laz_gdf)} photons from {output_path}")
laz_gdf.head()

# --- Quick look plot ---

x = laz_gdf.geometry.x.to_numpy()
y = laz_gdf.geometry.y.to_numpy()
z = np.array([geom.z if getattr(geom, 'has_z', False) else np.nan for geom in laz_gdf.geometry])
fig = plt.figure(figsize=(10, 8))
ax = fig.add_subplot(111, projection='3d')
ax.scatter(x, y, z, s=4, alpha=0.4)
ax.set_xlabel('X')
ax.set_ylabel('Y')
ax.set_zlabel('Z')
ax.set_title('ATL24 photon cloud (LAZ)')
plt.show()
