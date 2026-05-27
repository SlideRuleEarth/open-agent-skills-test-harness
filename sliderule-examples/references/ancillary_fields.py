# --- Including Ancillary Fields ---
#
# This example targets older `p-series` endpoints and walks you through the
# steps necessary to include ancillary fields in the data returned for
# `atl03sp` and `atl06p` requests.  Ancillary fields are fields present in the
# ATL03 ICESat-2 Standard Product, but are not included in the base results
# returned by SlideRule.

# --- Background ---
#
# The ATL03 granules include data associated with the photons in different
# subgroups inside the HDF5 file.  SlideRule currently supports including
# ancillary fields from three subgroups inside those granules:
# * gtxx/geolocation
# * gtxx/geophys_corr
# * gtxx/heights
#
# When an `atl03sp` or `at06p` processing request specifies ancillary fields,
# SlideRule reads those fields from the ATL03 granules, subsets them to the
# region of interest, and correlates them to the dynamically generated photon
# segment (called and "extent" in the code) they belong to.  The result is a
# GeoDataFrame with a column for each ancillary field populated by the value
# associated with each photon for `atl03sp` requests, and elevation for
# `atl06p` requests.
#
# Note, including ancillary fields in a processing request will increase the
# amount of time it takes for the request to be processed, and also the amount
# of data returned, so it should only be used when the fields are needed by
# the end user.

# --- Including an Ancillary Field in an `atl06p` request ---
#
# The __"atl03_geo_fields"__ and __"atl03_corr_fields"__  parameters are used
# to request ancillary fields be included in `atl06p` responses.  These fields
# must come from either the __"gtxx/geolocation"__ or __"gtxx/geophys_corr"__
# subgroups respectively.

# --- Initialize ---

from sliderule import sliderule, icesat2
sliderule.init(verbose=True)

# --- Create parameters for a typical `atl06p` processing request. ---

grand_mesa = sliderule.toregion('grandmesa.geojson')
parms = {
    "poly": grand_mesa["poly"],
    "t0": "2022-01-01",
    "t1": "2022-04-01",
    "srt": icesat2.SRT_LAND,
    "cnf": icesat2.CNF_SURFACE_HIGH,
    "len": 40.0,
    "res": 20.0
}

# --- Add ancillary fields to the request. ---

parms["atl03_geo_fields"] = ["ref_azimuth", "ref_elev"]

# --- Issue the processing request to SlideRule. ---
#
# When this completes (~15 seconds), the _gdf_ variable should now contain all
# of the results of the elevations calculated by SlideRule with corresponding
# columns for the "ref_azimuth" and "ref_elev" fields.

gdf = icesat2.atl06p(parms)

# --- Display a summary of the results. ---

gdf

# --- Including an Ancillary Field in an `atl03sp` request ---
#
# The __"atl03_ph_fields"__ parameter can be used to request ancillary fields
# be included in `atl03sp` responses.  These fields must come from the
# __"gtxx/heights"__ subgroup. The __"atl03_geo_fields"__ parameter can also
# be used - but note that when it is used, the resulting data will expand so
# that each photon row in the GeoDataFrame will have the value of the
# ancillary field corresponding to the segment that the photon is in.

# --- Create parameters for a typical `atl03sp` processing request. ---

grand_mesa = sliderule.toregion('grandmesa.geojson')
parms = {
    "poly": grand_mesa["poly"],
    "srt": icesat2.SRT_LAND,
    "cnf": icesat2.CNF_SURFACE_HIGH,
    "len": 40.0,
    "res": 20.0
}

# --- Add ancillary fields to the request. ---

parms["atl03_geo_fields"] = ["ref_azimuth", "ref_elev"]
parms["atl03_ph_fields"] = ["ph_id_channel"]

# --- Issue the processing request to SlideRule. ---
#
# When this completes (~45 seconds), the _gdf_ variable should now contain all
# of the results of the photons read by SlideRule with corresponding columns
# for the "ph_id_channel", "ref_azimuth". and "ref_elev" fields.

gdf = icesat2.atl03sp(parms, resources=['ATL03_20181017222812_02950102_007_01.h5'])

# --- Display a summary of the results. ---

gdf
