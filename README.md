# get-era5

Builds a **local archive of daily ERA5 fields** with Google Earth Engine (GEE). Hourly
ERA5 (`ECMWF/ERA5/HOURLY`, 0.25°) is aggregated to UTC days **on Earth Engine's servers**;
only the small daily result is downloaded and written as NetCDF. There is no CDS queue.

Status: **prototype, not yet tested against live Earth Engine.**

## How it works

`era5_gee_daily.py` handles one product (e.g. `PPT`) and one year at a time:

1. builds one Earth Engine image per UTC day, with one band per requested statistic;
2. checks that every day has exactly 24 hourly images;
3. downloads the result with `xee` on the native ERA5 grid (the bbox is widened outwards
   to whole 0.25° cells) and checks that the grid is the one requested;
4. per statistic: plausibility checks, a spot check of one cell against a direct Earth
   Engine query, then writes `*.part`, reads it back, verifies it and renames it into place.

A file is kept only when it holds a complete year, so the current year is rebuilt on each
run until it is complete. If one product-year fails, the run logs it and continues; re-run
to retry.

**Day definition.** Accumulated fields use the 24 hourly amounts stamped 01:00…00:00 (each
covers the hour before its stamp). Wind speed and direction are computed hourly from u and
v before the daily statistic.

Flow variables (`PPT`, `AET`, `PET`, `Q`): `sum` is an amount per day (mm), `max` an
intensity (mm h-1). `PPT` `t` counts the hours of the day with at least 1 mm of rain, 0..24;
the 1 mm threshold is a fixed method choice (it also keeps ERA5's widespread light drizzle
out of the count).

**Wind direction (`WDA`).** Degrees clockwise from north (0 = from the north, 90 = from the
east), the direction the wind blows *from*, in 0…360. ERA5 has no 10 m direction band over
land, so each of the 24 hours gives a unit vector from its u and v; the daily value is the
angle of their mean. Every hour counts the same whatever its speed, and calm hours are left
out. Plain averaging of angles would be wrong (350° and 10° would give 180°).

## Products and statistics

| Product | ERA5 band | Statistics and units |
|---|---|---|
| `PPT` | `total_precipitation`; `max` from `maximum_total_precipitation_rate_since_previous_post_processing` | `sum` mm; `max` mm h-1 = peak rate of the day (largest model time-step rate, not the wettest hour); `t` h = duration: hours with at least 1 mm (0 on dry days) |
| `AET` | `evaporation` (sign flipped, positive = evaporation) | `sum` mm; `max` mm h-1 (largest hourly amount) |
| `PET` | `potential_evaporation` (sign flipped) | `sum` mm; `max` mm h-1 (largest hourly amount) |
| `Q` | `runoff` (surface + sub-surface water leaving the cell; not routed, not river flow) | `sum` mm; `max` mm h-1 (largest hourly amount, for flood risk) |
| `WIND` | 10 m u and v | `speed_mean`, `speed_max` m s-1 (file `WDS`); `dir_mean` degree (file `WDA`) |
| `TAS` | `temperature_2m` | `mean`, `min`, `max` °C |
| `SWVL1`…`SWVL4` | volumetric soil water, layers 0-7, 7-28, 28-100, 100-289 cm | `mean`, `min`, `max` m3 m-3 |

## Layout

```
<output_root>/
  {VAR}/
    DAILY/   COPERNICUS_ERA5_{VAR}_{DAILY-STAT}_{EXTENT}_{YEAR}.nc
  logs/      era5_gee_daily_<UTC timestamp>.log   (full debug log of each run)
```

One folder per variable code as it appears in the file names (`PPT`, `TAS`, `WDS`, ...),
so a variable's whole series sits in one place. `MONTHLY/` and `ANNUAL/` may be added next
to `DAILY/` later.

Examples: `PPT/DAILY/COPERNICUS_ERA5_PPT_DAILY-SUM_RS_2020.nc`,
`WDS/DAILY/COPERNICUS_ERA5_WDS_DAILY-MAX_RS_2020.nc`.

## Setup

1. **Python packages** (inside your venv):
   ```bash
   pip install -r requirements.txt
   ```
2. **Earth Engine access.** You need a Google Cloud project registered for Earth Engine
   (<https://code.earthengine.google.com/register>). Check the terms for your kind of use
   (free for non-commercial use) and your project's quota before large runs.
3. **Authenticate once** (opens a browser; the credentials stay in your user folder):
   ```bash
   earthengine authenticate
   ```
4. **Settings.** Copy the template and fill in your project and paths:
   ```bash
   cp era5_gee_daily.example.json era5_gee_daily.local.json
   ```
   `*.local.json` is gitignored, so the project name and local paths are never committed.

## Usage

```bash
python era5_gee_daily.py --dry-run            # grid, last available day, plan
python era5_gee_daily.py                      # build everything in the settings
python era5_gee_daily.py --only PPT,WIND      # some products only
python era5_gee_daily.py --config other.json  # another settings file
```

### Arguments

| Argument | What it does |
|---|---|
| `--config PATH` | Settings JSON. Default: `era5_gee_daily.local.json` next to the script. |
| `--only PRODUCTS` | Comma-separated products from the settings (e.g. `PPT,WIND`). Default: all. |
| `--dry-run` | Connects to Earth Engine and logs the grid, last complete day and plan. Builds nothing. |
| `--verbose` | Also shows debug details on the console (always in the log file). |

Exit codes: `0` ok; `1` some product-years failed (re-run to retry); `2` settings error;
`130` interrupted (Ctrl+C).

### Settings (`era5_gee_daily.local.json`)

| Key | Default | Meaning |
|---|---|---|
| `ee_project` | required | Earth Engine-enabled Cloud project. |
| `years` | required | Explicit list of years. The current year is cut at the last complete UTC day. |
| `bbox` | required | `{"north", "south", "west", "east"}` in degrees, lon -180..180. |
| `extent_alias` | required | Extent name in file names, e.g. `RS`. |
| `output_root` | required | Archive folder. |
| `overwrite` | `false` | `true` rebuilds files that already exist. |
| `compression` | `"int16"` | `int16`: packed 16-bit integers, smallest (about 4× smaller than float32). `rounded`: float32 rounded to `decimals`. `none`: plain float32. All are zlib-compressed. |
| `decimals` | see template | Decimals kept per product (`SWVL` covers all layers). `int16` lowers it when the value range does not fit, and logs a warning. |
| `products` | required | `{"PPT": ["sum", "max"], ...}`, see the table above. |

## Logging

The console shows progress (`[k/N] year product`), download and write times, file sizes
and the packing used, plus warnings from the plausibility checks. The log file in
`<output_root>/logs/` also has debug details: hourly image counts, spot-check values,
min/mean/max and NaN counts per band, and tracebacks of failures. The run ends with a
summary of files written, up to date and failed.

## To verify on live Earth Engine

- `xee` returns exactly the native grid requested (the script stops if it does not).
- The sign of evaporation and potential evaporation (the script warns if the mean of a
  `sum` is negative after the flip). Potential evaporation's definition changed in
  November 2021; check long series across that date.
- Earth Engine quota and run time for multi-year runs.

## Legacy

Earlier CDS pipelines are kept, untracked, in `prompts/legacy/`: `cds_hourly/` (hourly
download with local daily aggregation) and `cds_daily_stats/` (CDS daily statistics, with
the `setup_cdsapi` scripts). They were dropped because the CDS queue is too slow.
