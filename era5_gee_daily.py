#!/usr/bin/env python3
"""
PROTOTYPE - daily ERA5 statistics computed inside Google Earth Engine (no CDS queue).

    python era5_gee_daily.py                       # settings from era5_gee_daily.local.json
    python era5_gee_daily.py --config other.json   # another settings file
    python era5_gee_daily.py --dry-run             # grid, last available day and plan only
    python era5_gee_daily.py --only PPT,WIND       # some products only
    python era5_gee_daily.py --verbose             # debug details on the console too

Hourly ERA5 (ECMWF/ERA5/HOURLY, 0.25 deg) is aggregated to UTC days ON EARTH ENGINE'S
SERVERS (sum / max / mean of the hourly values; wind speed and direction computed hourly first). Only the
small daily result is downloaded and written as NetCDF, one file per product-statistic-year:

    <output_root>/{VAR}/DAILY/COPERNICUS_ERA5_{VAR}_{DAILY-STAT}_{EXTENT}_{YEAR}.nc

Settings live in a sidecar JSON next to this script (era5_gee_daily.local.json, not
tracked by git; template: era5_gee_daily.example.json):

    ee_project   : Earth Engine-enabled Cloud project (required)
    years        : explicit list of years; the current year is cut at the last complete
                   UTC day available on Earth Engine (required)
    bbox         : {"north", "south", "west", "east"} in degrees, lon -180..180 (required);
                   widened outwards to whole native ERA5 cells (0.25 deg)
    extent_alias : extent name used in file names, e.g. "RS" (required)
    output_root  : archive folder (required)
    overwrite    : true = rebuild files that already exist (default false)
    compression  : "int16" (default) | "rounded" | "none"
    decimals     : decimals kept per product, merged over the defaults below
    products     : {"PPT": ["sum", "max"], ...} (required), see PRODUCT_DEFS / ALLOWED

Every run also writes a log file to <output_root>/logs/.

Setup (once):
    pip install -r requirements.txt
    earthengine authenticate            # opens a browser
Check Earth Engine's terms for your kind of use (it has been free for non-commercial use
only) and your project's quota before large runs.

NOT yet tested against live Earth Engine. The script therefore verifies itself:
  * every day must contain exactly 24 hourly images,
  * the grid returned by xee must match the native ERA5 grid it asked for,
  * one value per file is compared with a direct Earth Engine query before writing.

Exit codes: 0 ok, 1 some products failed, 2 settings error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

COLLECTION = "ECMWF/ERA5/HOURLY"
HIGH_VOLUME_URL = "https://earthengine-highvolume.googleapis.com"
DEFAULT_CONFIG = Path(__file__).resolve().with_name(Path(__file__).stem + ".local.json")
EPOCH = "1950-01-01"

U10, V10 = "u_component_of_wind_10m", "v_component_of_wind_10m"

# kind: accum (hourly amounts in m -> mm), instant (any other hourly field), wind
PRODUCT_DEFS = {
    "PPT": dict(kind="accum", band="total_precipitation", factor=1000.0, units="mm",
                 long_name="total precipitation"),
    # ERA5 stores evaporation negative (downward fluxes positive): flip the sign
    "AET": dict(kind="accum", band="evaporation", factor=-1000.0, units="mm",
                long_name="total evapotranspiration"),
    "PET": dict(kind="accum", band="potential_evaporation", factor=-1000.0, units="mm",
                long_name="potential evaporation"),
    # Q: total runoff (surface + sub-surface) leaving the cell, not routed downstream
    "Q": dict(kind="accum", band="runoff", factor=1000.0, units="mm",
              long_name="total runoff"),
    "TAS": dict(kind="instant", band="temperature_2m", factor=1.0, offset=-273.15,
                units="degC", long_name="2 m temperature"),
    "PPT-PEAK": dict(kind="instant",
                     band="maximum_total_precipitation_rate_since_previous_post_processing",
                     factor=3600.0, offset=0.0, units="mm h-1",
                     long_name="peak precipitation rate within the hour"),
    "WIND": dict(kind="wind"),
}
for _n, _depth in ((1, "0-7 cm"), (2, "7-28 cm"), (3, "28-100 cm"), (4, "100-289 cm")):
    PRODUCT_DEFS[f"SWVL{_n}"] = dict(
        kind="instant", band=f"volumetric_soil_water_layer_{_n}", factor=1.0, offset=0.0,
        units="m3 m-3", long_name=f"volumetric soil water, layer {_n} ({_depth})")
ALLOWED = {
    "accum": {"sum", "max"},
    "instant": {"mean", "min", "max"},
    "wind": {"speed_mean", "speed_max", "dir_mean"},
}
WIND_NAMES = {   # stat -> (file var code, how, long name, units)
    "speed_mean": ("WDS", "mean", "daily mean of hourly 10 m wind speed", "m s-1"),
    "speed_max": ("WDS", "max", "largest hourly 10 m wind speed within the UTC day", "m s-1"),
    # unit-vector mean of the 24 hourly directions: every hour counts the same, calm hours
    # (speed 0) are left out; meteorological convention (blowing FROM, clockwise, N = 0)
    "dir_mean": ("WDA", "mean", "daily mean 10 m wind direction (from which it blows, "
                 "clockwise from north; unit-vector mean of hourly directions)", "degree"),
}
# decimals kept (precision 10**-decimals): PPT/AET/PET/Q in mm, WIND in m/s,
# SWVL in m3/m3, TAS in degC, PPT-PEAK in mm/h
DEFAULT_DECIMALS = {"PPT": 2, "AET": 2, "PET": 2, "Q": 2, "WIND": 2, "SWVL": 4,
                    "TAS": 2, "PPT-PEAK": 2}
# old product keys (files already written: scripts/rename_codes.py)
RENAMED = {"PREC": "PPT", "ET": "AET", "TMP": "TAS", "PREC-PEAK": "PPT-PEAK",
           "RUNOFF": "Q"}
COMPRESSION_MODES = ("int16", "rounded", "none")
KNOWN_KEYS = {"ee_project", "years", "bbox", "extent_alias", "output_root", "overwrite",
              "compression", "decimals", "products"}

log = logging.getLogger("era5_gee_daily")


# --------------------------------------------------------------------------- #
# Settings (sidecar JSON)
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    path: Path
    ee_project: str
    years: list
    bbox: dict
    extent_alias: str
    output_root: Path
    overwrite: bool
    compression: str
    decimals: dict
    products: dict


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ValueError(f"settings file not found: {path}\n"
                         "Copy era5_gee_daily.example.json to era5_gee_daily.local.json "
                         "and fill it in.")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    missing = [k for k in ("ee_project", "years", "bbox", "extent_alias", "output_root",
                           "products") if k not in raw]
    if missing:
        raise ValueError(f"{path.name}: missing key(s) {missing}")
    ignored = set(raw) - KNOWN_KEYS
    if ignored:
        log.warning("settings keys ignored: %s", ", ".join(sorted(ignored)))

    years = raw["years"]
    if not (isinstance(years, list) and years and all(isinstance(y, int) for y in years)):
        raise ValueError("'years' must be an explicit list of integers, e.g. [2019, 2020]")
    bbox = raw["bbox"]
    try:
        bbox = {k: float(bbox[k]) for k in ("north", "south", "west", "east")}
    except (KeyError, TypeError) as exc:
        raise ValueError(f"'bbox' needs north, south, west, east ({exc})") from exc
    if not (-90 <= bbox["south"] < bbox["north"] <= 90 and -180 <= bbox["west"] < bbox["east"] <= 180):
        raise ValueError("'bbox': need south < north within -90..90 and west < east within -180..180")
    compression = raw.get("compression", "int16")
    if compression not in COMPRESSION_MODES:
        raise ValueError(f"'compression' must be one of {COMPRESSION_MODES}")
    products = {str(k).upper(): list(v) for k, v in raw["products"].items()}
    for p, stats in products.items():
        if p in RENAMED:
            raise ValueError(f"product {p} is now called {RENAMED[p]}; rename it in {path.name}")
        if p not in PRODUCT_DEFS:
            raise ValueError(f"unknown product {p}; known: {sorted(PRODUCT_DEFS)}")
        outputs(p, stats)                                       # validates the statistics

    return Config(
        path=path, ee_project=str(raw["ee_project"]), years=sorted(set(years)), bbox=bbox,
        extent_alias=str(raw["extent_alias"]).upper(), output_root=Path(raw["output_root"]),
        overwrite=bool(raw.get("overwrite", False)), compression=compression,
        decimals={**DEFAULT_DECIMALS, **raw.get("decimals", {})}, products=products,
    )


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    for name in ("googleapiclient", "google", "urllib3", "ee"):   # chatty HTTP libraries
        logging.getLogger(name).setLevel(logging.WARNING)


def add_log_file(log_dir: Path) -> Path:
    """Full DEBUG log of this run in <output_root>/logs/."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    path = log_dir / f"era5_gee_daily_{stamp}.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logging.getLogger().addHandler(fh)
    return path


def elapsed(t0: float) -> str:
    s = time.perf_counter() - t0
    return f"{s:.1f} s" if s < 120 else f"{s / 60:.1f} min"


# --------------------------------------------------------------------------- #
# Plan: which output files exist and what band each one is
# --------------------------------------------------------------------------- #
def outputs(product: str, stats: list) -> list:
    """[(stat, file var code, DAILY-STAT code, nc variable name, long_name, units, how)]"""
    d = PRODUCT_DEFS[product]
    bad = set(stats) - ALLOWED[d["kind"]]
    if bad:
        raise ValueError(f"{product}: statistic(s) {sorted(bad)} not allowed; "
                         f"choose from {sorted(ALLOWED[d['kind']])}")
    outs = []
    for s in stats:
        if d["kind"] == "wind":
            code, how, long_name, units = WIND_NAMES[s]
        else:
            code, how, units = product, s, d["units"]
            if d["kind"] == "accum":
                long_name = (f"daily total of {d['long_name']}" if s == "sum" else
                             f"largest hourly amount of {d['long_name']} within the UTC day")
                units = d["units"] if s == "sum" else f"{d['units']} h-1"
            else:
                long_name = f"daily {s} of {d['long_name']}"
        nc = f"{code}_{how}".lower().replace("-", "_") if d["kind"] == "wind" \
            else f"{code}_{s}".lower().replace("-", "_")
        outs.append((s, code, f"DAILY-{how.upper()}", nc, long_name, units, how))
    return outs


def out_path(cfg: Config, code: str, stat_code: str, year: int) -> Path:
    return cfg.output_root / code / "DAILY" / f"COPERNICUS_ERA5_{code}_{stat_code}_{cfg.extent_alias}_{year}.nc"


# --------------------------------------------------------------------------- #
# Grid
# --------------------------------------------------------------------------- #
def snap_grid(transform, bbox: dict):
    """Widen bbox to whole native cells. Returns (crs_transform, width, height).

    transform = [dx, 0, x0, 0, -dy, y0] of the native ERA5 grid (cell EDGES at x0 + k*dx)."""
    dx, _, x0, _, e, y0 = [float(v) for v in transform]
    dy = -e
    if dx <= 0 or dy <= 0:
        raise RuntimeError(f"unexpected native transform {transform}")
    col0 = math.floor((bbox["west"] - x0) / dx + 1e-9)
    col1 = math.ceil((bbox["east"] - x0) / dx - 1e-9)
    row0 = math.floor((y0 - bbox["north"]) / dy + 1e-9)
    row1 = math.ceil((y0 - bbox["south"]) / dy - 1e-9)
    if col1 <= col0 or row1 <= row0:
        raise RuntimeError("bbox is empty or inverted")
    return (dx, 0.0, x0 + col0 * dx, 0.0, -dy, y0 - row0 * dy), col1 - col0, row1 - row0


def grid_centres(transform, width, height):
    dx, _, x0, _, e, y0 = transform
    lon = x0 + (np.arange(width) + 0.5) * dx
    lat = y0 + (np.arange(height) + 0.5) * e
    return lat, lon


# --------------------------------------------------------------------------- #
# Earth Engine side
# --------------------------------------------------------------------------- #
def init_ee(cfg: Config):
    try:
        import ee
    except ImportError:
        raise RuntimeError("earthengine-api is not installed. Run: pip install -r requirements.txt")
    try:
        ee.Initialize(project=cfg.ee_project, opt_url=HIGH_VOLUME_URL)
    except Exception as exc:                                    # noqa: BLE001
        raise RuntimeError(f"Earth Engine could not start ({exc}). Run `earthengine authenticate` "
                           f"once and check 'ee_project' in {cfg.path.name}.") from exc
    return ee


def last_complete_day(ee) -> pd.Timestamp:
    """Last UTC day for which the hour ending at 00:00 of the next day is available."""
    ms = ee.ImageCollection(COLLECTION).aggregate_max("system:time_start").getInfo()
    last = pd.Timestamp(ms, unit="ms")
    return (last - pd.Timedelta(days=1)).normalize()


def daily_collection(ee, product: str, stats: list, year: int, n_days: int):
    """ImageCollection with one image per UTC day; bands = the requested statistics."""
    d = PRODUCT_DEFS[product]
    outs = outputs(product, stats)
    coll = ee.ImageCollection(COLLECTION)
    year_start = ee.Date.fromYMD(year, 1, 1)

    if d["kind"] == "wind":
        def add_speed(im):
            im = ee.Image(im)
            u, v = im.select(U10), im.select(V10)
            speed = u.hypot(v)
            # unit vector pointing to where the wind comes FROM: (east, north) = -(u, v) / speed
            moving = speed.gt(0)                               # calm hours have no direction
            from_e = u.multiply(-1).divide(speed).updateMask(moving).rename("from_e")
            from_n = v.multiply(-1).divide(speed).updateMask(moving).rename("from_n")
            return im.addBands([speed.rename("speed"), from_e, from_n])
        src = {"speed_mean": ("mean", "speed"), "speed_max": ("max", "speed")}

    def one(i):
        start = year_start.advance(ee.Number(i), "day")
        # UTC day = hourly amounts stamped 01:00 .. 00:00 of the next day (each amount covers
        # the hour BEFORE its stamp)
        hrs = coll.filterDate(start.advance(1, "hour"), start.advance(25, "hour"))
        if d["kind"] == "wind":
            hrs = hrs.map(add_speed)
        bands = []
        for stat, _, _, nc, _, _, _ in outs:
            if stat == "dir_mean":
                # compass angle of the mean unit vector: atan2(east, north), 0..360, N = 0
                e, n = hrs.select("from_e").mean(), hrs.select("from_n").mean()
                deg = e.atan2(n).multiply(180 / math.pi).add(360).mod(360)
                bands.append(deg.rename(nc))
            elif d["kind"] == "wind":
                how, band = src[stat]
                red = getattr(hrs.select(band), how)()
                bands.append(red.rename(nc))
            elif d["kind"] == "accum":
                f = d["factor"]
                if stat == "sum":
                    img = hrs.select(d["band"]).sum()
                else:   # largest hourly amount; for a sign-flipped variable that is the min
                    img = hrs.select(d["band"]).max() if f > 0 else hrs.select(d["band"]).min()
                bands.append(img.multiply(f).rename(nc))
            else:
                img = getattr(hrs.select(d["band"]), stat)()
                bands.append(img.multiply(d["factor"]).add(d["offset"]).rename(nc))
        return (ee.Image.cat(bands)
                .set({"system:time_start": start.millis(), "n_hours": hrs.size()}))

    return ee.ImageCollection(ee.List.sequence(0, n_days - 1).map(one))


def open_with_xee(ic, crs, transform, width, height) -> xr.Dataset:
    ds = xr.open_dataset(ic, engine="ee", crs=crs, crs_transform=tuple(transform),
                         shape_2d=(width, height))
    ren = {}
    for dim in ds.dims:
        if dim in ("lon", "X", "x", "longitude"):
            ren[dim] = "longitude"
        elif dim in ("lat", "Y", "y", "latitude"):
            ren[dim] = "latitude"
    return ds.rename(ren)


def normalise(ds: xr.Dataset, transform, width, height) -> xr.Dataset:
    """(time, latitude N->S, longitude W->E) and a check against the grid we asked for."""
    ds = ds.transpose("time", "latitude", "longitude")
    if not (np.diff(ds.latitude.values) < 0).all():
        ds = ds.sortby("latitude", ascending=False)
    if not (np.diff(ds.longitude.values) > 0).all():
        ds = ds.sortby("longitude")
    lat, lon = grid_centres(transform, width, height)
    if ds.sizes["latitude"] != height or ds.sizes["longitude"] != width \
            or not np.allclose(ds.latitude.values, lat, atol=1e-4) \
            or not np.allclose(ds.longitude.values, lon, atol=1e-4):
        raise RuntimeError("grid returned by xee differs from the native ERA5 grid "
                           f"(asked {height}x{width}, got {ds.sizes['latitude']}x{ds.sizes['longitude']}; "
                           f"lat {ds.latitude.values[[0, -1]]} vs {lat[[0, -1]]}, "
                           f"lon {ds.longitude.values[[0, -1]]} vs {lon[[0, -1]]})")
    return ds


def spot_check(ee, ic, ds, nc: str, crs, transform) -> None:
    """Compare one cell of the first day with a direct Earth Engine query."""
    j, i = ds.sizes["latitude"] // 2, ds.sizes["longitude"] // 2
    lat, lon = float(ds.latitude[j]), float(ds.longitude[i])
    img = ee.Image(ic.first()).select(nc)
    got = img.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([lon, lat]),
                           crs=crs, crsTransform=list(transform)).get(nc).getInfo()
    mine = float(ds[nc].isel(time=0, latitude=j, longitude=i))
    if got is None or not np.isclose(got, mine, rtol=1e-4, atol=1e-5, equal_nan=False):
        raise RuntimeError(f"spot check failed for {nc} at ({lat}, {lon}): xee {mine} vs EE {got}")
    log.debug("    spot check %s at (%.3f, %.3f): %.6g == %.6g", nc, lat, lon, mine, got)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def decimals_for(cfg: Config, product: str) -> int:
    key = "SWVL" if product.startswith("SWVL") else product
    return int(cfg.decimals.get(key, 2))


def pick_encoding(cfg: Config, da: xr.DataArray, product: str, nt: int, ny: int, nx: int):
    """(variable encoding, description, max round-trip error) for the compression mode."""
    base = {"zlib": True, "complevel": 4, "shuffle": True, "_FillValue": np.float32(np.nan),
            "chunksizes": (nt, min(100, ny), min(100, nx)), "dtype": "float32"}
    dec = decimals_for(cfg, product)
    mode = cfg.compression
    if mode == "int16":
        vals = da.values
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
        d = dec
        while (hi - lo) * 10 ** d > 65000 and d > 0:
            d -= 1                                   # range too wide for int16: coarser steps
        if (hi - lo) * 10 ** d > 65000:
            log.warning("    %s: range %.1f..%.1f too wide for int16, writing float32 instead",
                        da.name, lo, hi)
            mode = "rounded"
        else:
            if d != dec:
                log.warning("    %s: precision reduced to %d decimals to fit int16 (range %.2f..%.2f)",
                            da.name, d, lo, hi)
            scale = 10.0 ** -d
            offset = round(((hi + lo) / 2) / scale) * scale
            enc = dict(base, dtype="int16", _FillValue=np.int16(-32768),
                       scale_factor=np.float32(scale), add_offset=np.float32(offset))
            return enc, f"int16, precision {scale:g}", scale / 2
    if mode == "rounded":
        return (dict(base, least_significant_digit=dec), f"float32 rounded to {dec} decimals",
                0.5 * 10 ** -dec)
    return base, "float32", 0.0


def write_file(cfg: Config, ds_year: xr.Dataset, nc: str, long_name: str, units: str, how: str,
               path: Path, year: int, n_days: int, complete: bool, product: str) -> str:
    """Write, read back and verify, then rename into place. Returns the packing used."""
    da = ds_year[nc].astype("float32")
    da.attrs = {"long_name": long_name, "units": units,
                "cell_methods": f"time: {how} (hourly values within the UTC day)"}
    out = xr.Dataset({nc: da})
    out["time"].attrs = {"long_name": "time (UTC day start)"}
    out["latitude"].attrs = {"standard_name": "latitude", "units": "degrees_north"}
    out["longitude"].attrs = {"standard_name": "longitude", "units": "degrees_east"}
    out.attrs = {
        "title": f"ERA5 {long_name}, daily, {cfg.extent_alias}",
        "source": f"{COLLECTION} (Copernicus Climate Change Service, ERA5) aggregated on Google Earth Engine",
        "day_definition": "UTC day from hourly values (accumulated fields: the 24 hourly amounts stamped 01:00..00:00)",
        "grid": "native ERA5 grid, 0.25 degrees, cell centres",
        "product": product, "year": year, "n_days": n_days,
        "complete_year": "yes" if complete else "no",
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Conventions": "CF-1.8",
    }
    nt, ny, nx = out[nc].shape
    venc, how_packed, tol = pick_encoding(cfg, out[nc], product, nt, ny, nx)
    out.attrs["compression"] = how_packed
    enc = {nc: venc,
           "time": {"units": f"days since {EPOCH} 00:00:00", "calendar": "proleptic_gregorian",
                    "dtype": "int32"},
           "latitude": {"_FillValue": None}, "longitude": {"_FillValue": None}}
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    out.to_netcdf(part, engine="netcdf4", encoding=enc)
    with xr.open_dataset(part) as chk:                          # read-back
        if chk[nc].shape != (nt, ny, nx):
            raise RuntimeError(f"{nc}: read-back shape {chk[nc].shape} != {(nt, ny, nx)}")
        if not pd.DatetimeIndex(chk.time.values).equals(pd.DatetimeIndex(out.time.values)):
            raise RuntimeError(f"{nc}: read-back time axis differs")
        if tol > 0:                                              # packing/rounding error bound
            err = np.nanmax(np.abs(chk[nc].values.astype("float64") - out[nc].values.astype("float64")))
            if err > tol * 1.05 + 1e-6:
                raise RuntimeError(f"{nc}: round-trip error {err} > {tol}")
        if not (np.isnan(chk[nc].values) == np.isnan(out[nc].values)).all():
            raise RuntimeError(f"{nc}: NaN pattern changed on write")
    part.replace(path)
    return how_packed


def plausibility(product: str, nc: str, da: xr.DataArray) -> xr.DataArray:
    """Sanity checks. Returns the array, with float-noise negatives of PPT/Q set to 0.

    AET/PET can legitimately be negative after the sign flip (condensation / dew), so only
    their mean is checked."""
    vals = da.values
    if not np.isfinite(vals).any():
        raise RuntimeError(f"{nc}: no finite values")
    log.debug("    %s: min %.4g, mean %.4g, max %.4g, NaN cells %d", nc, float(np.nanmin(vals)),
              float(np.nanmean(vals)), float(np.nanmax(vals)), int(np.isnan(vals).sum()))
    if product.startswith("SWVL"):
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
        if lo < -1e-6 or hi > 1.0:
            log.warning("    %s: values outside 0..1 m3/m3 (min %.3f, max %.3f)", nc, lo, hi)
    if product in ("PPT", "Q"):
        lo = float(np.nanmin(vals))
        if lo < -0.01:                      # more than rounding noise (mm)
            log.warning("    %s: negative values (min %.4f mm); check the data", nc, lo)
        da = da.clip(min=0)
    if product in ("AET", "PET") and nc.endswith("_sum") and np.nanmean(vals) < 0:
        log.warning("    %s: mean is negative after the sign flip; check the sign convention", nc)
    return da


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def n_days_for(year: int, last_day: pd.Timestamp) -> int:
    end = min(pd.Timestamp(f"{year}-12-31"), last_day)
    return max(0, (end - pd.Timestamp(f"{year}-01-01")).days + 1)


def already_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with xr.open_dataset(path) as ds:
            return ds.attrs.get("complete_year") == "yes"
    except Exception:                                           # noqa: BLE001
        log.warning("%s exists but cannot be read; it will be rebuilt", path.name)
        return False


def build_product(cfg: Config, ee, product: str, todo: list, year: int, n_days: int,
                  complete: bool, grid) -> list:
    """Compute one product-year on Earth Engine and write its files. Returns written paths."""
    crs, transform, width, height = grid
    t0 = time.perf_counter()
    ic = daily_collection(ee, product, [o[0] for o in todo], year, n_days)
    hours = ic.aggregate_array("n_hours").getInfo()
    if len(hours) != n_days or any(h != 24 for h in hours):
        bad = [i for i, h in enumerate(hours) if h != 24][:5]
        raise RuntimeError(f"days without 24 hourly images, e.g. day indices {bad}")
    log.debug("  hourly images: 24 for each of the %d days (%s)", n_days, elapsed(t0))

    t1 = time.perf_counter()
    ds = normalise(open_with_xee(ic, crs, transform, width, height), transform, width, height)
    expected = pd.date_range(f"{year}-01-01", periods=n_days, freq="D")
    if not pd.DatetimeIndex(ds.time.values).equals(expected):
        raise RuntimeError("time axis differs from the expected days")
    ds = ds.load()
    log.info("  downloaded %d band(s) x %d days x %d x %d cells in %s",
             len(todo), n_days, height, width, elapsed(t1))

    written = []
    for stat, code, stat_code, nc, long_name, units, how in todo:
        t2 = time.perf_counter()
        ds[nc] = plausibility(product, nc, ds[nc])
        spot_check(ee, ic, ds, nc, crs, transform)
        path = out_path(cfg, code, stat_code, year)
        packed = write_file(cfg, ds, nc, long_name, units, how, path, year, n_days, complete, product)
        log.info("  wrote %s (%.2f MB, %s, %s)", path.name, path.stat().st_size / 1e6, packed,
                 elapsed(t2))
        written.append(path)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily ERA5 statistics computed on Google Earth Engine.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help=f"settings JSON (default: {DEFAULT_CONFIG.name} next to the script)")
    ap.add_argument("--only", help="comma-separated products (default: all in the settings)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, compute nothing")
    ap.add_argument("--verbose", action="store_true", help="debug details on the console")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        log.error("settings: %s", exc)
        return 2
    products = cfg.products
    if args.only:
        only = [k.strip().upper() for k in args.only.split(",")]
        unknown = [k for k in only if k not in products]
        if unknown:
            log.error("--only: product(s) %s not in %s", unknown, cfg.path.name)
            return 2
        products = {k: v for k, v in products.items() if k in only}

    log_path = add_log_file(cfg.output_root / "logs")
    t_run = time.perf_counter()
    log.info("settings %s | project %s | years %s | extent %s %s | compression %s",
             cfg.path.name, cfg.ee_project, ",".join(map(str, cfg.years)), cfg.extent_alias,
             cfg.bbox, cfg.compression)
    log.info("products: %s", "; ".join(f"{p} {','.join(s)}" for p, s in products.items()))
    log.info("output %s | log %s", cfg.output_root, log_path)

    written, up_to_date, failed = [], 0, []
    try:
        ee = init_ee(cfg)
        proj = ee.Image(ee.ImageCollection(COLLECTION).first()).select(0).projection().getInfo()
        crs, native = proj["crs"], proj["transform"]
        if crs != "EPSG:4326":
            raise RuntimeError(f"native CRS is {crs}, expected EPSG:4326; script needs adapting")
        transform, width, height = snap_grid(native, cfg.bbox)
        lat, lon = grid_centres(transform, width, height)
        last_day = last_complete_day(ee)
        log.info("native grid transform %s", native)
        log.info("region grid: %d x %d cells, lat %.3f..%.3f, lon %.3f..%.3f",
                 height, width, lat[0], lat[-1], lon[0], lon[-1])
        log.info("last complete UTC day on Earth Engine: %s", last_day.date())
        grid = (crs, transform, width, height)

        jobs = []
        for year in cfg.years:
            n_days = n_days_for(year, last_day)
            if n_days == 0:
                log.warning("%d: nothing available yet, skipped", year)
                continue
            complete = n_days == (366 if pd.Timestamp(f"{year}-12-31").dayofyear == 366 else 365)
            for product, stats in products.items():
                outs = outputs(product, stats)
                # existing files are kept only when they hold the full year
                todo = [o for o in outs if cfg.overwrite
                        or not already_complete(out_path(cfg, o[1], o[2], year))]
                if todo:
                    jobs.append((year, product, todo, n_days, complete))
                else:
                    up_to_date += len(outs)
                    log.info("%d %s: up to date", year, product)
        log.info("plan: %d product-year(s), %d file(s) to build, %d up to date",
                 len(jobs), sum(len(j[2]) for j in jobs), up_to_date)

        for k, (year, product, todo, n_days, complete) in enumerate(jobs, 1):
            log.info("[%d/%d] %d %s: %d file(s), %d days%s", k, len(jobs), year, product,
                     len(todo), n_days, "" if complete else " (year not finished)")
            if args.dry_run:
                continue
            t0 = time.perf_counter()
            try:
                written += build_product(cfg, ee, product, todo, year, n_days, complete, grid)
                log.info("[%d/%d] %d %s done in %s", k, len(jobs), year, product, elapsed(t0))
            except KeyboardInterrupt:
                raise
            except Exception as exc:                            # noqa: BLE001
                log.error("[%d/%d] %d %s FAILED after %s: %s", k, len(jobs), year, product,
                          elapsed(t0), exc)
                log.debug("traceback", exc_info=True)
                failed.append(f"{year} {product}")
    except KeyboardInterrupt:
        log.error("interrupted after %s; files already written are complete, a *.part file "
                  "may be left and is rebuilt on the next run", elapsed(t_run))
        return 130
    except RuntimeError as exc:
        log.error("%s", exc)
        log.debug("traceback", exc_info=True)
        return 1

    log.info("summary: %d written, %d up to date, %d product-year(s) failed, %s",
             len(written), up_to_date, len(failed), elapsed(t_run))
    if failed:
        log.error("failed: %s (re-run to retry; see %s)", ", ".join(failed), log_path)
        return 1
    log.info("done%s", " (dry run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
