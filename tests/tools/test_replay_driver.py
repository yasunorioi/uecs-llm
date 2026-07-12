"""tools/replay_driver.py の pytest.

agriha_history.db 読取, primitive stub, entry shape の 3 レイヤをテスト。
(旧 sensor_log.db スキーマは 2026-07-12 廃止)
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(_TOOLS))

_spec = importlib.util.spec_from_file_location("replay_driver", _TOOLS / "replay_driver.py")
assert _spec and _spec.loader
rd = importlib.util.module_from_spec(_spec)
sys.modules["replay_driver"] = rd
_spec.loader.exec_module(rd)

_JST = ZoneInfo("Asia/Tokyo")


def _write_history_db(
    path: Path,
    series: list[tuple[str, str]],  # (key, unit)
    samples: list[tuple[str, int, float]],  # (key, ts_epoch, value)
) -> None:
    """agriha_history.db 相当を作る (series + samples)。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE series(id INTEGER PRIMARY KEY, key TEXT UNIQUE, unit TEXT);
        CREATE TABLE samples(sid INTEGER, ts INTEGER, value REAL);
        CREATE INDEX ix_samples ON samples(sid, ts);
        """
    )
    sid_map: dict[str, int] = {}
    for key, unit in series:
        cur = conn.execute("INSERT INTO series(key, unit) VALUES(?,?)", (key, unit))
        sid_map[key] = cur.lastrowid
    conn.executemany(
        "INSERT INTO samples(sid, ts, value) VALUES(?,?,?)",
        [(sid_map[k], ts, v) for (k, ts, v) in samples],
    )
    conn.commit()
    conn.close()


# ── build_metric_map ────────────────────────────────

def test_metric_map_house_templated():
    m = rd.build_metric_map("2")
    assert m["agriha/2/sensor/InAirTemp"] == "indoor_temp"
    assert m["agriha/2/sensor/InAirCO2"] == "indoor_co2"
    # farm 共通は house に依存しない
    assert m["agriha/farm/weather/WWindSpeed"] == "wind_speed"


# ── read_latest_snapshot ────────────────────────────

def test_read_latest_snapshot_picks_max_ts_per_topic(tmp_path):
    now = int(time.time())
    db = tmp_path / "h.db"
    _write_history_db(
        db,
        series=[
            ("agriha/1/sensor/InAirTemp", "C"),
            ("agriha/1/sensor/InAirHumid", "%"),
            ("agriha/1/sensor/InAirCO2", "ppm"),
            ("agriha/farm/weather/WWindSpeed", "m s-1"),
        ],
        samples=[
            ("agriha/1/sensor/InAirTemp", now - 1800, 20.0),  # 30 分前 (old)
            ("agriha/1/sensor/InAirTemp", now - 300, 22.5),   # 5 分前 (new)
            ("agriha/1/sensor/InAirHumid", now - 300, 80.0),
            ("agriha/1/sensor/InAirCO2",  now - 300, 400.0),
            ("agriha/farm/weather/WWindSpeed", now - 300, 3.5),
        ],
    )
    snapshot, latest_ts = rd.read_latest_snapshot(db, house_id="1")
    assert snapshot["indoor_temp"] == 22.5     # 新しい方が勝つ
    assert snapshot["indoor_humidity"] == 80.0
    assert snapshot["indoor_co2"] == 400.0
    assert snapshot["wind_speed"] == 3.5       # farm 共通も拾える
    # 欠損 defaults
    assert snapshot["rainfall"] == 0.0
    assert snapshot["outdoor_temp"] is None
    assert latest_ts is not None
    assert "+09:00" in latest_ts


def test_read_latest_snapshot_drops_stale(tmp_path):
    now = int(time.time())
    db = tmp_path / "h.db"
    _write_history_db(
        db,
        series=[("agriha/1/sensor/InAirTemp", "C")],
        samples=[("agriha/1/sensor/InAirTemp", now - 3 * 3600, 20.0)],  # 3h old
    )
    snapshot, _ = rd.read_latest_snapshot(db, house_id="1", max_age_min=60)
    assert "indoor_temp" not in snapshot


def test_read_latest_snapshot_house_isolation(tmp_path):
    """house=1 なら h2 のセンサは拾わない。"""
    now = int(time.time())
    db = tmp_path / "h.db"
    _write_history_db(
        db,
        series=[
            ("agriha/1/sensor/InAirTemp", "C"),
            ("agriha/2/sensor/InAirTemp", "C"),
        ],
        samples=[
            ("agriha/1/sensor/InAirTemp", now - 60, 21.0),
            ("agriha/2/sensor/InAirTemp", now - 60, 25.0),
        ],
    )
    s1, _ = rd.read_latest_snapshot(db, house_id="1")
    s2, _ = rd.read_latest_snapshot(db, house_id="2")
    assert s1["indoor_temp"] == 21.0
    assert s2["indoor_temp"] == 25.0


def test_read_latest_snapshot_ignores_unmapped_series(tmp_path):
    now = int(time.time())
    db = tmp_path / "h.db"
    _write_history_db(
        db,
        series=[
            ("agriha/1/sensor/InAirTemp", "C"),
            ("agriha/2/actuator/VenSdWinopr", ""),  # DSL 対象外
        ],
        samples=[
            ("agriha/1/sensor/InAirTemp", now - 60, 21.0),
            ("agriha/2/actuator/VenSdWinopr", now - 60, 42.0),
        ],
    )
    snapshot, _ = rd.read_latest_snapshot(db, house_id="1")
    assert snapshot["indoor_temp"] == 21.0
    # actuator 系は snapshot に載らない (defaults 除く)
    assert "VenSdWinopr" not in snapshot


# ── time-of-day period ──────────────────────────────

def test_time_of_day_period_boundaries():
    def at(h):
        return datetime(2026, 7, 11, h, 0, tzinfo=_JST)
    assert rd._time_of_day_period(at(2)) == "night"
    assert rd._time_of_day_period(at(5)) == "pre_dawn"
    assert rd._time_of_day_period(at(10)) == "morning"
    assert rd._time_of_day_period(at(13)) == "afternoon"
    assert rd._time_of_day_period(at(17)) == "evening"
    assert rd._time_of_day_period(at(20)) == "night"


# ── simple primitives ───────────────────────────────

def test_build_simple_primitives_all_callable():
    cfg = {
        "temperature_schedule": {
            "morning": {"target": 25},
            "night":   {"target": 17},
        },
        "window_step_table": {20: 10, 25: 40, 30: 80},
    }
    crop = {"solar_threshold_mj": 2.0, "irrigation_duration_sec": 120}
    p = rd.build_simple_primitives(cfg, crop)
    # 全部 callable で、呼んで raise しない
    assert p["rise_rate"]() is None
    assert p["solar_accumulated_mj"]() == 0.0
    assert p["is_night"]() in (True, False)
    assert p["schedule_target_temp"]() in (17.0, 25.0)
    assert p["window_step_lookup"](25) == 40
    # None を渡しても 0 を返す (センサ欠損許容)
    assert p["window_step_lookup"](None) == 0
    assert p["solar_threshold"]() == 2.0
    assert p["irrigation_duration"]() == 120


def test_window_step_lookup_linear_interpolation():
    cfg = {"temperature_schedule": {}, "window_step_table": {20: 10, 30: 90}}
    lookup = rd.build_simple_primitives(cfg, {})["window_step_lookup"]
    # 中間補間
    assert lookup(25) == 50
    # 上下 clamp
    assert lookup(15) == 10
    assert lookup(40) == 90


# ── entry shape ─────────────────────────────────────

def test_build_entry_has_replay_mode_and_null_live(tmp_path):
    from agriha.control.automation_interpreter import Interpreter, load_automations
    autos = load_automations([
        {"id": "hum", "priority": 60,
         "trigger": "indoor_humidity >= 85",
         "action": {"kind": "set_min_opening", "value": 30}},
    ])
    result = Interpreter(autos).evaluate(
        sensors={"indoor_humidity": 90},
        state={}, config={}, crop_config={},
        primitives={}, strict=False,
    )
    entry = rd.build_entry(result, {"indoor_humidity": 90}, "2026-07-11T10:00:00")
    assert entry["mode"] == "replay"
    assert entry["match"] is None
    assert entry["live"] is None
    assert entry["delta"] is None
    assert entry["interp"]["triggered"] == ["hum"]
    assert entry["interp"]["target_opening_pct"] == 30
    assert entry["sensors"]["indoor_humidity"] == 90


# ── end-to-end CLI (repo 内 config を使う) ──────────

def test_cli_e2e_writes_replay_entry(tmp_path):
    # 実 configs (repo 内) を使う
    repo = Path(__file__).resolve().parents[2]
    autos_yaml = repo / "config" / "automations.yaml"
    rules_yaml = repo / "config" / "rules.yaml"
    crop_yaml  = repo / "config" / "crop_irrigation.yaml"

    # 手作り agriha_history.db: 湿度スパイクで P4 発火する状況 (house=1)
    now = int(time.time())
    db = tmp_path / "h.db"
    _write_history_db(
        db,
        series=[
            ("agriha/1/sensor/InAirTemp", "C"),
            ("agriha/1/sensor/InAirHumid", "%"),
            ("agriha/1/sensor/InAirCO2",  "ppm"),
        ],
        samples=[
            ("agriha/1/sensor/InAirTemp", now - 120, 26.0),
            ("agriha/1/sensor/InAirHumid", now - 120, 92.0),  # ≥85 → humidity_vent
            ("agriha/1/sensor/InAirCO2",  now - 120, 420.0),  # >350 → co2 系不発
        ],
    )

    log_out = tmp_path / "log.jsonl"
    rc = rd.main([
        "--sensor-db", str(db),
        "--house", "1",
        "--automations", str(autos_yaml),
        "--config", str(rules_yaml),
        "--crop", str(crop_yaml),
        "--out", str(log_out),
    ])
    assert rc == 0
    lines = log_out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["mode"] == "replay"
    assert entry["interp"]["target_opening_pct"] >= 30
    assert "humidity_vent" in entry["interp"]["triggered"]
