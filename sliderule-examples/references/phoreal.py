# --- Running the PhoREAL algorithm over Grand Mesa, CO ---
#
# Demonstrate running the PhoREAL algorithm in SlideRule to produce canopy
# metrics over the Grand Mesa, Colorado region.

# --- Imports ---

import logging
import sliderule
from sliderule import icesat2

# --- Initialize Client ---

sliderule.init(verbose=True, loglevel=logging.INFO)

# --- Processing parameters ---

parms = {
    "poly": sliderule.toregion('grandmesa.geojson')['poly'], # subset to Grand Mesa area of interest
    "t0": '2019-11-14T00:00:00Z', # time range is one day - November 14, 2019
    "t1": '2019-11-15T00:00:00Z',
    "srt": icesat2.SRT_LAND, # use the land surface type for ATL03 photon confidence levels
    "len": 100, # generate statistics over a 100m segment
    "res": 100, # generate statistics every 100m
    "pass_invalid": True, # do not perform any segment-level filtering
    "atl08_class": ["atl08_ground", "atl08_canopy", "atl08_top_of_canopy"], # exclude noise and unclassified photons
    "atl08_fields": ["h_dif_ref"], # include these fields as extra columns in the dataframe
    "phoreal": {"binsize": 1.0, "geoloc": "center"} # run the PhoREAL algorithm
}

# --- Make Proessing Request ---

atl08 = sliderule.run("atl03x", parms)

# --- Print Resulting GeoDataFrame ---

atl08

# --- Plot Canopy Height ---

canopy_gt1l = atl08[atl08['gt'] == icesat2.GT1L]
canopy_gt1l.plot.scatter(x='x_atc', y='h_canopy')

# --- Plot Landcover ---

atl08.plot('landcover', legend=True)

# --- Create and Plot 75th percentile Across All Ground Tracks ---

atl08['75'] = atl08.apply(lambda row : row["canopy_h_metrics"][icesat2.P['75']], axis = 1)
atl08.plot.scatter(x='x_atc', y='75')
