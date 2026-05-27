# --- Example `h5x` Construction of Custom ATL09 DataFrame ---

from sliderule import sliderule, h5

session = sliderule.create_session(verbose=True)

# --- Building a GeoDataFrame ---

asset = "icesat2-atl09"
resource = "ATL09_20250302214125_11732601_007_01.h5"
variables = [
    "met_t10m",
    "met_ts",
    "delta_time",
    "latitude",
    "longitude"
]
groups = [
    "/profile_1/low_rate",
    "/profile_2/low_rate",
    "/profile_3/low_rate"
]
gdf = h5.h5x(variables, resource, asset, groups, index_column="delta_time", x_column="longitude", y_column="latitude", session=session)
print(gdf)
print(gdf.attrs)

# --- Building a DataFrame ---

asset = "icesat2-atl09"
resource = "ATL09_20250302214125_11732601_007_01.h5"
variables = [
    "surface_sig",
]
groups = [
    "/profile_1/high_rate",
    "/profile_2/high_rate",
    "/profile_3/high_rate"
]
gdf = h5.h5x(variables, resource, asset, groups, session=session)
print(gdf)
print(gdf.attrs)
