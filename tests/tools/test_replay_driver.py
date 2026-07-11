"""tools/replay_driver.py の pytest.

sensor_log.db 読取, primitive stub, entry shape の 3 レイヤをテスト。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
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


def _write_sensor_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sensor_log (
            timestamp TEXT NOT NULL,
            source    TEXT NOT NULL,
            metric    TEXT NOT NULL,
            value     REAL NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO sensor_log(timestamp, source, metric, value) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


# ── read_latest_snapshot ────────────────────────────

def test_read_latest_snapshot_picks_max_ts_per_metric(tmp_path):
    now = datetime.now(_JST)
    ts_old = (now - timedelta(minutes=30)).replace(microsecond=0).isoformat(timespec="seconds").replace("+09:00","")
    ts_new = (now - timedelta(minutes=5)).replace(microsecond=0).isoformat(timespec="seconds").replace("+09:00","")
    db = tmp_path / "s.db"
    _write_sensor_db(db, [
        (ts_old, "ccm", "temp_inside", 20.0),
        (ts_new, "ccm", "temp_inside", 22.5),  # newer wins
        (ts_new, "ccm", "humidity", 80.0),
        (ts_new, "ccm", "co2", 400.0),
    ])
    snapshot, latest_ts = rd.read_latest_snapshot(db)
    assert snapshot["indoor_temp"] == 22.5
    assert snapshot["indoor_humidity"] == 80.0
    assert snapshot["indoor_co2"] == 400.0
    # 欠損は 0 埋め (rainfall/wind) / None (outdoor/insolar)
    assert snapshot["rainfall"] == 0.0
    assert snapshot["wind_speed"] == 0.0
    assert snapshot["outdoor_temp"] is None
    assert snapshot["insolar"] is None
    assert latest_ts == ts_new


def test_read_latest_snapshot_drops_stale(tmp_path):
    now = datetime.now(_JST)
    ts_stale = (now - timedelta(hours=3)).replace(microsecond=0).isoformat(timespec="seconds").replace("+09:00","")
    db = tmp_path / "s.db"
    _write_sensor_db(db, [
        (ts_stale, "ccm", "temp_inside", 20.0),
    ])
    snapshot, _ = rd.read_latest_snapshot(db, max_age_min=60)
    assert "indoor_temp" not in snapshot  # 3h > 60min → skip


def test_read_latest_snapshot_ignores_unknown_metric(tmp_path):
    now = datetime.now(_JST)
    ts = (now - timedelta(minutes=1)).replace(microsecond=0).isoformat(timespec="seconds").replace("+09:00","")
    db = tmp_path / "s.db"
    _write_sensor_db(db, [
        (ts, "ccm", "temp_inside", 21.0),
        (ts, "ccm", "unknown_metric", 99.0),
    ])
    snapshot, _ = rd.read_latest_snapshot(db)
    assert snapshot["indoor_temp"] == 21.0
    assert "unknown_metric" not in snapshot


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

    # 手作り sensor_log.db: 湿度スパイクで P4 発火する状況
    now = datetime.now(_JST)
    ts = (now - timedelta(minutes=2)).replace(microsecond=0).isoformat(timespec="seconds").replace("+09:00","")
    db = tmp_path / "s.db"
    _write_sensor_db(db, [
        (ts, "ccm", "temp_inside", 26.0),
        (ts, "ccm", "humidity", 92.0),   # ≥85 → humidity_vent 発火
        (ts, "ccm", "co2", 420.0),       # >350 → co2 系は発火せず
    ])

    log_out = tmp_path / "log.jsonl"
    rc = rd.main([
        "--sensor-db", str(db),
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
    assert entry["interp"]["target_opening_pct"] >= 30  # humidity_vent の 30 が floor
    assert "humidity_vent" in entry["interp"]["triggered"]
