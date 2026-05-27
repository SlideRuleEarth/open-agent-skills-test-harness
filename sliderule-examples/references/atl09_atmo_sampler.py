# --- Example `atmo` ATL09 Sampler ---

from sliderule import sliderule, icesat2

session = sliderule.create_session(verbose=True)

region = [
    {"lon":-105.82971551223244, "lat": 39.81983728534918},
    {"lon":-105.30742121965137, "lat": 39.81983728534918},
    {"lon":-105.30742121965137, "lat": 40.164048017973755},
    {"lon":-105.82971551223244, "lat": 40.164048017973755},
    {"lon":-105.82971551223244, "lat": 39.81983728534918}
]

# --- Sampling from ATL03 ---

parms = {
    "poly": region,
    "cnf": icesat2.CNF_SURFACE_HIGH,
    "rgt": 554,
    "cycle": 8,
    "region": 2,
    "atl09_fields": [
        "bckgrd_atlas/bckgrd_counts",
        "high_rate/dem_h",
        "low_rate/met_t10m"
    ],
    "quality_ph": [
        "atl03_quality_nominal",
        "atl03_quality_tx_full_sat_ir_effect",
    ]
}
gdf = sliderule.run("atl03x", parms, session=session)
gdf

# --- Sampling from ATL06 ---

parms = {
    "poly": region,
    "rgt": 554,
    "cycle": 8,
    "region": 2,
    "atl09_fields": [
        "bckgrd_atlas/bckgrd_counts",
        "high_rate/dem_h",
        "low_rate/met_t10m"
    ]
}
gdf = sliderule.run("atl06x", parms, session=session)
gdf
