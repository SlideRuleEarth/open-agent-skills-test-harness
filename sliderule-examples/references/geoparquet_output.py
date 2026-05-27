# --- Returning Data from SlideRule in the GeoParquet Format ---
#
# This tutorial walks you through the steps necessary to return data from
# SlideRule in the GeoParquet format.  The code in this notebook focuses on
# the older p-series endpoints.
#
# GeoParquet is a cloud-optimized format for storing geospatial datasets.  It
# is built on top of Apache's Parquet format and is fully compatible with all
# Parquet-based tools.  The official specification for GeoParquet can be found
# here: https://github.com/opengeospatial/geoparquet.
#
# By default, SlideRule uses its own native streaming format for
# de/serializing data across a network.  As processing results are produced by
# SlideRule, they are immediately transmitted over the network to the
# requester.  When the SlideRule Python client is being used, these results
# are used to construct a GeoDataFrame on the fly.  While this approach as
# some advantages - namely low latency responses and low memory usage on the
# server - it also has some drawbacks.  Chief among them is the processing
# required to construct the DataFrame.  Data returned by the SlideRule service
# can be thought of as rows of information.  As each row is received it is
# appended to the final DataFrame.  But because DataFrames are columnar-based,
# each time data is appended, costly memory allocations and data copies
# result.  Effort has been made to optimize this processing in the client, but
# ultimately only so much can be done and the problem remains that it is
# encumbent on the client to parse, rearrange, and construct the DataFrame.
#
# For small responses (less than 1M points), things are okay.  But as
# responses get larger, the client is unable to keep up with the SlideRule
# servers, and can bottleneck the process or even crash if it runs out of
# memory.  To address these shortcomings, SlideRule supports sending responses
# back as GeoParquet files.  When a GeoParquet file is requested, the results
# of the request are built entirely on the servers as a GeoParquet file, and
# then the final file is streamed back to the client where it is directly
# written to disk.  This allows large requests to consume server-side
# resources in parsing, rearranging, and building a DataFrame-like structure.
# Clients can then choose to open the resulting GeoParquet file immediately,
# or open it at some later time with different software.

# --- Requesting the GeoParquet Format ---
#
# The __"output"__ parameter is used to request the GeoParquet format.

# --- Import and initialize the SlideRule Python package for ICESat-2. ---

from sliderule import sliderule, icesat2
sliderule.init("slideruleearth.io")

# --- Create parameters for a typical `atl06p` processing request (it could also be `atl03sp`). ---

grand_mesa = sliderule.toregion('grandmesa.geojson')
parms = {
    "poly": grand_mesa["poly"],
    "t0": "2021-06-01",
    "t1": "2022-01-01",
    "srt": icesat2.SRT_LAND,
    "cnf": icesat2.CNF_SURFACE_HIGH,
    "len": 40.0,
    "res": 20.0
}

# --- Specify the GeoParquet format. ---
#
# The _"path"_ parameter is the name of the local file the client will write
# the parquet output to.
#
# The _"format"_ parameter is what specifies that the GeoParquet format is
# requested.
#
# The *"open_on_complete"* means that the client will return a GeoDataFrame of
# the opened GeoParquet file when making a call to the `atl06p` or `atl03sp`
# functions.  If this option is false, then the client returns the name of the
# file.

parms["output"] = { "path": "/tmp/grandmesa.parquet", "format": "parquet", "open_on_complete": False }

# --- Issue the processing request to SlideRule. ---
#
# When this completes (~15 seconds), the _gdf_ variable should now contain all
# of the results of the elevations calculated by SlideRule; and there should
# be a grandmesa.parquet file in the directory where Python was run from.

parquet_filename = icesat2.atl06p(parms)

# --- Display a summary of the results. ---
#
# For a full description of all of the fields returned from the `atl06p`
# function, see the [elevations](../../user_guide/icesat2.html#elevations)
# documentation.

import pandas as pd
df = pd.read_parquet(parquet_filename)
df
