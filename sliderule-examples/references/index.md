# SlideRule Example Index

## Examples

| File | Topic | Title | APIs | Notebook |
|---|---|---|---|---|
| ancillary_fields.py | ancillary | Including Ancillary Fields | atl03sp, atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/ancillary_fields.ipynb) |
| arcticdem_mosaic.py | raster | Sampling the ArcticDEM Mosaic | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/arcticdem_mosaic.ipynb) |
| arcticdem_request.py | raster | Including ArcticDEM Samples | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/arcticdem_request.ipynb) |
| arcticdem_strip_boundaries.py | raster |  | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/arcticdem_strip_boundaries.ipynb) |
| atl03v_request.py | photon-data |  | atl03vp | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl03v_request.ipynb) |
| atl06_glims_subset.py | land-ice |  | atl06sp, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl06_glims_subset.ipynb) |
| atl06p_request.py | land-ice |  | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl06p_request.ipynb) |
| atl09_atmo_sampler.py | atmosphere | Example `atmo` ATL09 Sampler | — | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl09_atmo_sampler.ipynb) |
| atl09_h5x.py | atmosphere | Example `h5x` Construction of Custom ATL09 DataFrame | — | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl09_h5x.ipynb) |
| atl13_access.py | inland-water | Accessing ATL13 data using lake names, reference ids, and contained coordinates | init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl13_access.ipynb) |
| atl24_access.py | bathymetry | Subsetting and filtering ATL24 data | init | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl24_access.ipynb) |
| atl24_las_output.py | bathymetry | Subsetting ATL24 data to LAS or LAZ output | init | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/atl24_las_output.ipynb) |
| blanket_surface.py | advanced | Example Running Surface Blanket (ALS) Algorithm in ATL03x | toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/blanket_surface.ipynb) |
| boulder_watershed.py | canopy | Using `atl03x` to get ICESat-2 data over the Boulder Watershed | — | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/boulder_watershed.ipynb) |
| earthdata_query.py | data-discovery | EarthData Query Example | init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/earthdata_query.ipynb) |
| first_request.py | getting-started | Making Your First Request | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/first_request.ipynb) |
| gedi_l2a_access.py | gedi |  | init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/gedi_l2a_access.ipynb) |
| geoparquet_output.py | output-format | Returning Data from SlideRule in the GeoParquet Format | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/geoparquet_output.ipynb) |
| grandmesa.py | land-ice | Generating a Custom ATL06 over Grand Mesa, CO | atl06p, init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/grandmesa.ipynb) |
| grandmesa_atl03_classification.py | photon-data | Generating ATL03 photon classifications using ATL08 and YAPC | init | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/grandmesa_atl03_classification.ipynb) |
| job_runner.py | advanced | Running SlideRule Jobs (Batch Processing) | — | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/job_runner.ipynb) |
| phoreal.py | canopy | Running the PhoREAL algorithm over Grand Mesa, CO | init, toregion | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/phoreal.ipynb) |
| user_url_raster.py | raster | User Raster Sampling | init | [notebook](https://github.com/SlideRuleEarth/sliderule/blob/main/clients/python/examples/user_url_raster.ipynb) |

## By Topic

- **advanced**: blanket_surface.py, job_runner.py
- **ancillary**: ancillary_fields.py
- **atmosphere**: atl09_atmo_sampler.py, atl09_h5x.py
- **bathymetry**: atl24_access.py, atl24_las_output.py
- **canopy**: boulder_watershed.py, phoreal.py
- **data-discovery**: earthdata_query.py
- **gedi**: gedi_l2a_access.py
- **getting-started**: first_request.py
- **inland-water**: atl13_access.py
- **land-ice**: atl06_glims_subset.py, atl06p_request.py, grandmesa.py
- **output-format**: geoparquet_output.py
- **photon-data**: atl03v_request.py, grandmesa_atl03_classification.py
- **raster**: arcticdem_mosaic.py, arcticdem_request.py, arcticdem_strip_boundaries.py, user_url_raster.py

## By API

- **atl03sp**: ancillary_fields.py
- **atl03vp**: atl03v_request.py
- **atl06p**: ancillary_fields.py, arcticdem_mosaic.py, arcticdem_request.py, arcticdem_strip_boundaries.py, atl06p_request.py, first_request.py, geoparquet_output.py, grandmesa.py
- **atl06sp**: atl06_glims_subset.py
- **init**: ancillary_fields.py, arcticdem_mosaic.py, arcticdem_request.py, arcticdem_strip_boundaries.py, atl06_glims_subset.py, atl06p_request.py, atl13_access.py, atl24_access.py, atl24_las_output.py, earthdata_query.py, first_request.py, gedi_l2a_access.py, geoparquet_output.py, grandmesa.py, grandmesa_atl03_classification.py, phoreal.py, user_url_raster.py
- **toregion**: ancillary_fields.py, arcticdem_mosaic.py, arcticdem_request.py, arcticdem_strip_boundaries.py, atl06_glims_subset.py, atl06p_request.py, atl13_access.py, blanket_surface.py, earthdata_query.py, first_request.py, gedi_l2a_access.py, geoparquet_output.py, grandmesa.py, phoreal.py
