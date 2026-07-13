# Changelog

All notable changes to the `sliderule-pipeline-python-client` skill are recorded here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## 1.1

- Reworded the single-script justification for the client era: the
  raw-requests decode-failure chain no longer applies; named the residual risks
  (`float32` JSON export, algorithm-dependent columns, height-datum filters).
- Added "Checkpoint the Fetch": save `raw.parquet` immediately after
  `reset_index()` and guard the client call with an existence check, so a
  downstream fix reruns in seconds instead of re-paying a minutes-long fetch.
- Task Metrics: a checkpointed rerun reports `loaded from raw.parquet (cached)`
  instead of a request duration; never reuse or fabricate timing.
- Updated eval 01 rubric wording to match; added eval 05 (checkpoint-fetch).

## 1.0

- Initial version, split from `sliderule-pipeline-direct-request` (#71): the same
  single-script pipeline discipline (consolidation, reproducible `pipeline.py`,
  task metrics), taught through the SlideRule Python client (`sliderule` package)
  instead of direct HTTP requests.
