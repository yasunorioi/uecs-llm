"""test_sensors_snapshot.py — sensors_snapshot.py の単体テスト.

SQLite in-memory or tempfile で agriha_history.db 互換 schema を作り、
fetch_latest / snapshot / write_yaml_atomic を検証。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from sensors_snapshot import (  # noqa: E402
    build_key_map,
    fetch_latest,
    snapshot,
    write_yaml_atomic,
)


# ══════════════════════════════════════════════
# helpers: build agriha_history-compatible db
# ══════════════════════════════════════════════

def _make_db(tmp_path: Path, rows: list[tuple[str, float, int]]) -> Path:
    """rows: [(key, value, ts), ...] で mini agriha_history.db を作る。"""
    p = tmp_path / "history.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, key TEXT, unit TEXT)")
    conn.execute("CREATE TABLE samples (sid INTEGER, ts INTEGER, value REAL)")
    # 各 key を series に登録、複数 sample も許容
    keys_seen: dict[str, int] = {}
    for key, value, ts in rows:
        if key not in keys_seen:
            cur = conn.execute("INSERT INTO series (key, unit) VALUES (?, '')", (key,))
            keys_seen[key] = cur.lastrowid
        conn.execute("INSERT INTO samples (sid, ts, value) VALUES (?, ?, ?)",
                     (keys_seen[key], ts, value))
    conn.commit()
    conn.close()
    return p


# ══════════════════════════════════════════════
# build_key_map
# ══════════════════════════════════════════════

def test_key_map_house_2() -> None:
    m = build_key_map(2)
    assert m["indoor_temp"] == "agriha/2/sensor/InAirTemp"
    assert m["indoor_humidity"] == "agriha/2/sensor/InAirHumid"
    assert m["indoor_hd"] == "agriha/2/sensor/InAirHD"
    assert m["outdoor_temp"] == "agriha/farm/weather/WAirTemp"
    assert m["wind_direction"] == "agriha/farm/weather/WWindDir"


def test_key_map_house_prefix_swappable() -> None:
    m1 = build_key_map(1)
    m3 = build_key_map(3)
    assert m1["indoor_temp"].startswith("agriha/1/")
    assert m3["indoor_temp"].startswith("agriha/3/")
    # farm keys are shared
    assert m1["outdoor_temp"] == m3["outdoor_temp"]


# ══════════════════════════════════════════════
# fetch_latest
# ══════════════════════════════════════════════

def test_fetch_latest_returns_newest_value(tmp_path: Path) -> None:
    now = int(time.time())
    db = _make_db(tmp_path, [
        ("agriha/2/sensor/InAirTemp", 20.0, now - 300),
        ("agriha/2/sensor/InAirTemp", 22.5, now - 60),  # newest
    ])
    assert fetch_latest(db, "agriha/2/sensor/InAirTemp", max_age_sec=600) == 22.5


def test_fetch_latest_returns_none_when_missing(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [
        ("agriha/2/sensor/InAirTemp", 22.0, int(time.time())),
    ])
    assert fetch_latest(db, "agriha/2/sensor/InAirHumid", max_age_sec=600) is None


def test_fetch_latest_returns_none_when_stale(tmp_path: Path) -> None:
    """max_age_sec を超えた古い値は drop されて None。"""
    now = int(time.time())
    db = _make_db(tmp_path, [
        ("agriha/2/sensor/InAirTemp", 22.0, now - 1000),  # 16 min ago
    ])
    assert fetch_latest(db, "agriha/2/sensor/InAirTemp", max_age_sec=600) is None


def test_fetch_latest_missing_table_returns_none(tmp_path: Path) -> None:
    """壊れた db (schema なし) でも crash せず None。"""
    p = tmp_path / "empty.db"
    conn = sqlite3.connect(p)
    conn.close()
    assert fetch_latest(p, "any/key", max_age_sec=600) is None


# ══════════════════════════════════════════════
# snapshot
# ══════════════════════════════════════════════

def test_snapshot_full_house_2(tmp_path: Path) -> None:
    now = int(time.time())
    db = _make_db(tmp_path, [
        ("agriha/2/sensor/InAirTemp", 22.5, now - 60),
        ("agriha/2/sensor/InAirHumid", 88.0, now - 60),
        ("agriha/2/sensor/InAirCO2", 450.0, now - 60),
        ("agriha/farm/weather/WAirTemp", 18.0, now - 60),
        ("agriha/farm/weather/WRainfallAmt", 0.0, now - 60),
    ])
    values, missing = snapshot(db, house=2)
    assert values["indoor_temp"] == 22.5
    assert values["indoor_humidity"] == 88.0
    assert values["indoor_co2"] == 450.0
    assert values["outdoor_temp"] == 18.0
    assert values["rainfall"] == 0.0
    # wind は series 無し → デフォルト 0
    assert values["wind_speed"] == 0.0
    assert values["wind_direction"] == 0
    # insolar / indoor_hd は missing に記録
    assert "insolar" in missing
    assert "indoor_hd" in missing


def test_snapshot_missing_all_returns_defaults_and_reasons(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [])   # 空 db
    values, missing = snapshot(db, house=2)
    # wind defaults still there
    assert values["wind_speed"] == 0.0
    assert values["wind_direction"] == 0
    # 全 sensor key が missing に記録
    assert "indoor_temp" in missing
    assert "insolar" in missing
    assert len(missing) >= 7   # key_map size 通り


def test_snapshot_wind_direction_int_cast(tmp_path: Path) -> None:
    now = int(time.time())
    db = _make_db(tmp_path, [
        ("agriha/farm/weather/WWindDir", 45.7, now - 30),  # float 値
    ])
    values, _ = snapshot(db, house=2)
    # int cast される
    assert values["wind_direction"] == 45
    assert isinstance(values["wind_direction"], int)


# ══════════════════════════════════════════════
# write_yaml_atomic
# ══════════════════════════════════════════════

def test_write_yaml_atomic_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "sensors.yaml"
    values = {"indoor_temp": 22.5, "indoor_humidity": 88.0, "wind_direction": 0}
    write_yaml_atomic(values, out)
    loaded = yaml.safe_load(out.read_text())
    assert loaded == values


def test_write_yaml_atomic_creates_parent_dir(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "sensors.yaml"
    write_yaml_atomic({"x": 1}, out)
    assert out.exists()


def test_write_yaml_atomic_no_tempfile_leftover(tmp_path: Path) -> None:
    out = tmp_path / "sensors.yaml"
    write_yaml_atomic({"a": 1}, out)
    assert not out.with_suffix(".yaml.tmp").exists()
