# MEMORY

- 2026-10-20 — Draft scripts moved from `prompts/incoming/` to repo root (`era5_common.py`, `era5_download.py`, `era5_daily.py`, `test_pipeline.py`).
- 2026-10-20 — `era5_common.py` kept as shared module; standalone scripts is now a recommendation, no micro modules.
- 2026-10-20 — Fixes: manifest `complete_year` now matches the file attribute; wind speed plausibility window 0..100 m/s.
- 2026-10-20 — Added `config_example.json` (tracked template); `archive_*/` gitignored; README usage synced to actual config names.
- 2026-10-20 — Added `config_rsne.local.json`: small-region real test (NE Rio Grande do Sul, bbox -28/-52.5/-31/-49.5), 2020-2024, all variables, raw kept.
- 2026-10-20 — README: added CLI arguments table and exit codes.
- 2026-10-20 — Added README Setup section (packages, CDS token, licences, setup_cdsapi.ps1/.sh); fixed next-step hint in both setup scripts.
- 2026-10-20 — Added requirements.txt; README setup uses it.
- 2026-10-20 — CDS requests now send year/month as lists (scalars left the web form blank, request shown as incomplete); test updated.
- 2026-10-20 — Major refactor: switched to `derived-era5-single-levels-daily-statistics`; hourly download and local daily stage removed. Single standalone `era5_download.py` (1 variable x 1 stat x 1 year x <=6 months per request, format detected on arrival). `era5_common.py`/`era5_daily.py` removed; old pipeline kept in `prompts/legacy/`. Test, configs, requirements (cdsapi only) and README rewritten.
- 2026-10-20 — Major refactor: moved to Google Earth Engine (`era5_gee_daily.py`, daily stats computed on GEE from ECMWF/ERA5/HOURLY). Settings moved to sidecar `era5_gee_daily.local.json` (gitignored; template `era5_gee_daily.example.json`). Logging via `logging`, per-run log file in `<output_root>/logs/`, failed product-years logged and skipped. CDS pipelines and `setup_cdsapi` scripts archived in `prompts/legacy/`. README and requirements rewritten.
