"""export_sensor_csv.py — ArSprout CSVs → 1h resample → TiDE input CSV.

データソース:
  1. ArSprout 個別CSVファイル（/home/yasu/Project/unipi-agri-ha/data/）
  2. Open-Meteo Archive API（外気象共変量）

出力:
  datetime_utc, InAirTemp, InAirHumid, InAirCO2,
  WTemp, WAirHumid, WWindSpeed, WRainfall,
  om_temp2m, om_humidity2m, om_radiation, om_precip, om_wind10m,
  hour_sin, hour_cos, doy_sin, doy_cos

Usage:
  python3 tools/export_sensor_csv.py --output data/sensor_hourly.csv
  python3 tools/export_sensor_csv.py --data-dir /path/to/data --output out.csv --no-openmeteo
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR_DEFAULT = Path("/home/yasu/Project/unipi-agri-ha/data")
OUTPUT_DEFAULT = Path("data/sensor_hourly.csv")

# ArSprout CSV definitions: (filename_suffix, column_alias, datetime_col)
ARSPROUT_FILES = [
    ("arsprout_InAirTemp_20250501_20250930.csv",  "InAirTemp",   "datetime"),
    ("arsprout_InAirHumid_20250501_20250930.csv", "InAirHumid",  "datetime"),
    ("arsprout_InAirCO2_20250501_20250930.csv",   "InAirCO2",    "datetime"),
    ("arsprout_WTemp_20250501_20250930.csv",       "WTemp",       "datetime"),
    ("arsprout_WAirHumid_20250501_20250930.csv",   "WAirHumid",   "datetime"),
    ("arsprout_WWindSpeed_20250501_20250930.csv",  "WWindSpeed",  "datetime"),
    ("arsprout_WRainfall_20250501_20250930.csv",   "WRainfall",   "datetime"),
]

# Open-Meteo coordinates (恵庭市近郊)
LAT = 42.888
LON = 141.603


def load_arsprout_csv(path: Path, alias: str, dt_col: str) -> pd.Series:
    """Load an ArSprout single-value CSV and return a named Series with UTC index."""
    df = pd.read_csv(path)
    df["_ts"] = pd.to_datetime(df[dt_col], utc=True)
    df = df.set_index("_ts")["value"].rename(alias)
    return df


def fetch_openmeteo(start: str, end: str) -> pd.DataFrame:
    """Fetch hourly weather data from Open-Meteo Archive API.

    Returns DataFrame with UTC hourly index and columns:
      om_temp2m, om_humidity2m, om_radiation, om_precip, om_wind10m
    """
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={LAT}&longitude={LON}"
        f"&start_date={start}&end_date={end}"
        f"&hourly=temperature_2m,relative_humidity_2m,"
        f"shortwave_radiation,precipitation,wind_speed_10m"
        f"&timezone=UTC"
    )
    logger.info("Fetching Open-Meteo: %s ~ %s", start, end)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    hourly = data["hourly"]
    df = pd.DataFrame({
        "om_temp2m":    hourly["temperature_2m"],
        "om_humidity2m": hourly["relative_humidity_2m"],
        "om_radiation":  hourly["shortwave_radiation"],
        "om_precip":     hourly["precipitation"],
        "om_wind10m":    hourly["wind_speed_10m"],
    }, index=pd.to_datetime(hourly["time"], utc=True))
    return df


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add cyclic time features (hour, day-of-year)."""
    idx = df.index
    hour = idx.hour + idx.minute / 60.0
    doy = idx.day_of_year
    df["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    df["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.0)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.0)
    return df


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Export ArSprout CSVs to 1h TiDE input CSV")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR_DEFAULT,
                        help="Directory containing ArSprout CSV files")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT,
                        help="Output CSV path")
    parser.add_argument("--no-openmeteo", action="store_true",
                        help="Skip Open-Meteo fetch")
    args = parser.parse_args()

    # ── 1. Load ArSprout CSVs ──────────────────────────────────────────
    series_list = []
    for fname, alias, dt_col in ARSPROUT_FILES:
        fpath = args.data_dir / fname
        if not fpath.exists():
            logger.warning("Missing file: %s (skip)", fpath)
            continue
        s = load_arsprout_csv(fpath, alias, dt_col)
        logger.info("Loaded %s: %d rows, %s ~ %s", alias, len(s), s.index.min(), s.index.max())
        series_list.append(s)

    if not series_list:
        raise RuntimeError("No ArSprout CSV files found")

    df_raw = pd.concat(series_list, axis=1)
    logger.info("Merged raw: %d rows, columns=%s", len(df_raw), list(df_raw.columns))

    # ── 2. Resample to 1h (mean) ─────────────────────────────────────
    df_1h = df_raw.resample("1h").mean()
    logger.info("After 1h resample: %d rows", len(df_1h))

    # ── 3. Trim to overlapping range (all target columns have data) ──
    target_cols = ["InAirTemp", "InAirHumid", "InAirCO2"]
    available_targets = [c for c in target_cols if c in df_1h.columns]
    df_1h = df_1h.dropna(subset=available_targets, how="all")

    # Find range where at least 50% of target data is present
    start_date = df_1h[available_targets].dropna(how="all").index.min()
    end_date = df_1h[available_targets].dropna(how="all").index.max()
    df_1h = df_1h.loc[start_date:end_date]
    logger.info("Trimmed to %s ~ %s: %d rows", start_date, end_date, len(df_1h))

    # ── 4. Open-Meteo covariates ──────────────────────────────────────
    if not args.no_openmeteo:
        try:
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            df_om = fetch_openmeteo(start_str, end_str)
            df_1h = df_1h.join(df_om, how="left")
            logger.info("Open-Meteo joined: %d rows", len(df_1h))
        except Exception as e:
            logger.warning("Open-Meteo fetch failed: %s (skip)", e)

    # ── 5. Interpolate missing values ────────────────────────────────
    # Ensure all columns are numeric before interpolation
    df_1h = df_1h.apply(pd.to_numeric, errors="coerce")
    df_1h = df_1h.interpolate(method="linear", limit_direction="both")
    df_1h = df_1h.ffill().bfill()

    # ── 6. Time features ─────────────────────────────────────────────
    df_1h = add_time_features(df_1h)

    # ── 7. Save ──────────────────────────────────────────────────────
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df_1h.index.name = "datetime_utc"
    df_1h.to_csv(args.output)
    logger.info("Saved: %s (%d rows, %d columns)", args.output, len(df_1h), len(df_1h.columns))
    logger.info("Columns: %s", list(df_1h.columns))

    # Summary stats for targets
    for col in available_targets:
        if col in df_1h.columns:
            s = df_1h[col]
            logger.info("  %s: mean=%.2f, std=%.2f, null=%d", col, s.mean(), s.std(), s.isna().sum())


if __name__ == "__main__":
    main()
