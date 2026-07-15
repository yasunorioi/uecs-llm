"""sensors_snapshot.py — agriha_history.db の最新センサ値を sensors.yaml に書き出す cron script.

ogms-DSL daemon の sensors_path が re-read する yaml を、5min cron 等で
継続 refresh するのが目的。DSL は house を選んで control するので、
--house 引数で対象ハウス (1/2/3) を切替可能。

出力 key は DSL interpreter が期待する logical name にマップ:
    indoor_temp, indoor_humidity, indoor_co2, outdoor_temp,
    rainfall, wind_speed, wind_direction, insolar

Usage:
    python3 sensors_snapshot.py --db /home/pi/agriha_history.db --house 2 \\
        --out /home/pi/ogms-dsl-runtime/sensors.yaml

Cron (5min):
    */5 * * * * /usr/bin/python3 /home/pi/uecs-llm/tools/sensors_snapshot.py \\
        --db /home/pi/agriha_history.db --house 2 \\
        --out /home/pi/ogms-dsl-runtime/sensors.yaml

依存: 標準ライブラリ + pyyaml。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import yaml


# ══════════════════════════════════════════════
# logical name → agriha_history.db series.key mapping
# ══════════════════════════════════════════════

def build_key_map(house: int) -> dict[str, str]:
    """DSL logical sensor name → agriha_history.db series.key."""
    return {
        "indoor_temp":     f"agriha/{house}/sensor/InAirTemp",
        "indoor_humidity": f"agriha/{house}/sensor/InAirHumid",
        "indoor_co2":      f"agriha/{house}/sensor/InAirCO2",
        # 飽差は agri-env-poe v0.11.0 が InAirHD として publish (memory: project_agri_env_poe.md)
        "indoor_hd":       f"agriha/{house}/sensor/InAirHD",
        # 外気 / 雨 / 日射は farm 共通
        "outdoor_temp":    "agriha/farm/weather/WAirTemp",
        "rainfall":        "agriha/farm/weather/WRainfallAmt",
        "insolar":         "agriha/farm/weather/InRadiation",
        # 風は現地 primary weather station から、未定なら None
        "wind_speed":      "agriha/farm/weather/WWindSpd",
        "wind_direction":  "agriha/farm/weather/WWindDir",
    }


# ══════════════════════════════════════════════
# fetch latest sample per series
# ══════════════════════════════════════════════

def fetch_latest(db_path: Path, key: str, max_age_sec: int) -> float | None:
    """series.key の最新 sample を取得、max_age_sec を超えるなら None (stale drop)。"""
    now_ts = int(time.time())
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT s.value, s.ts FROM samples s "
            "JOIN series ser ON s.sid = ser.id "
            "WHERE ser.key = ? ORDER BY s.ts DESC LIMIT 1",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    value, ts = row
    if now_ts - int(ts) > max_age_sec:
        return None  # stale
    return value


def snapshot(
    db_path: Path, house: int, max_age_sec: int = 600,
) -> tuple[dict[str, float | int], dict[str, str]]:
    """logical name → latest value dict と、欠損 key の reason dict を返す。"""
    key_map = build_key_map(house)
    values: dict[str, float | int] = {}
    missing: dict[str, str] = {}
    for logical, key in key_map.items():
        v = fetch_latest(db_path, key, max_age_sec)
        if v is None:
            missing[logical] = f"missing or stale > {max_age_sec}s ({key})"
        else:
            # wind_direction は int が自然、その他 float
            if logical == "wind_direction":
                values[logical] = int(v)
            else:
                values[logical] = float(v)
    # wind 系欠損時は 0 で埋める (DSL 側で strict=False の吸収と冗長だが安全)
    values.setdefault("wind_speed", 0.0)
    values.setdefault("wind_direction", 0)
    return values, missing


# ══════════════════════════════════════════════
# atomic write (tempfile + rename)
# ══════════════════════════════════════════════

def write_yaml_atomic(values: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(values, allow_unicode=True, sort_keys=True))
    tmp.replace(out)


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="agriha_history.db → sensors.yaml snapshot")
    p.add_argument("--db", required=True, type=Path,
                   help="agriha_history.db path")
    p.add_argument("--house", type=int, default=2,
                   help="対象ハウス (1/2/3、series.key の {house} 部分)")
    p.add_argument("--out", required=True, type=Path,
                   help="出力 sensors.yaml path")
    p.add_argument("--max-age-sec", type=int, default=600,
                   help="この秒数を超えた古い値は stale として drop (default 10min)")
    args = p.parse_args(argv)

    if not args.db.exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    values, missing = snapshot(args.db, args.house, args.max_age_sec)
    write_yaml_atomic(values, args.out)

    # cron ログ用 1 行 summary
    written = sorted(k for k in values if k not in ("wind_speed", "wind_direction")
                     or values[k] != (0 if k == "wind_direction" else 0.0)
                     or k in missing) or list(values.keys())
    n_have = len(values) - sum(1 for v in values.values() if v == 0 or v == 0.0)
    print(
        f"snapshot: house={args.house} out={args.out.name} "
        f"written={len(values)} missing={len(missing)}"
        + (f" missing_keys={list(missing.keys())}" if missing else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
