# --- Example Running Surface Blanket (ALS) Algorithm in ATL03x ---

# --- Imports ---

from sliderule import sliderule, icesat2
import matplotlib.pyplot as plt

# --- Create SlideRule Session ---

session = sliderule.create_session(node_capacity=1, user_service=True, verbose=True)

# --- Define Area of Interest ---

aoi = sliderule.toregion("grandmesa.geojson")

# --- Make Processing Request ---

gdf = sliderule.run("atl03x", {
    "poly": aoi["poly"],
    "als": {},
    "t0": "2019-01-01",
    "t1": "2019-03-01",
    "cnt": 10,
    "cnf": icesat2.CNF_SURFACE_LOW
}, session=session)

gdf

# --- Filter and Plot Results ---

gdf1 = gdf[gdf["pflags"] == 0]
gdf2 = gdf1[gdf1["gt"] == 50]
ax = gdf2.plot(x='x_atc', y='top_of_surface', kind='scatter', color='green', label='Top of Surface')
gdf2.plot(x='x_atc', y='median_ground', kind='scatter', color='brown', label='Median Ground', ax=ax)
plt.legend()
plt.xlabel('along track distance')
plt.ylabel('height')
plt.title('Ground and Surface Top')
plt.show()
