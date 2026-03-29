"""
tests/control/test_rule_engine.py — Layer 2 rule_engine pytest テスト (v5対応)

v5: priority chain評価、変温管理、CO2/湿度ルール、外気温ベース開度テーブル。
v4から削除: load_current_plan, PID制御, Layer 3委譲。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from agriha.control.rule_engine import (
    evaluate_rules,
    fetch_sensors,
    fetch_status,
    get_rise_rate_per_hour,
    get_schedule_target,
    get_target_opening,
    is_layer1_locked_out,
    is_nighttime,
    load_solar_accumulator,
    opening_to_relay_duration,
    post_relay,
    run,
    save_solar_accumulator,
    update_weather_flags,
)

_JST = ZoneInfo("Asia/Tokyo")

# ──────────────────────────────────────────────
# フィクスチャ
# ──────────────────────────────────────────────

@pytest.fixture
def base_cfg() -> dict[str, Any]:
    """テスト用 rules.yaml 相当の設定辞書（v5形式）。"""
    return {
        "temperature_schedule": {
            "pre_dawn": {"target": 18, "offset_min": -60},
            "morning": {"target": 25},
            "afternoon": {"target": 28},
            "evening": {"target": 23},
            "night": {"target": 17},
        },
        "morning_ventilation": {
            "max_rise_rate_per_hour": 3.0,
            "start_offset_min": -30,
        },
        "co2": {
            "outdoor_baseline": 450,
            "ventilation_trigger": 350,
            "critical_low": 300,
            "stop_ventilation_above": 430,
        },
        "humidity": {
            "ventilation_start": 85,
            "ventilation_stop": 75,
        },
        "window_step_table": {
            10: 0,
            15: 5,
            20: 25,
            25: 85,
            30: 100,
        },
        "window_motor": {
            "full_open_duration_sec": 120,
            "min_change_pct": 5,
        },
        "wind": {
            "strong_wind_threshold_ms": 5.0,
        },
        "rain": {
            "threshold_mm_h": 0.5,
            "resume_delay_min": 30,
        },
        "irrigation": {
            "channel": 4,
            "crop_config_path": "/etc/agriha/crop_irrigation.yaml",
        },
        "unipi_api": {
            "base_url": "http://localhost:8080",
            "api_key": "",
            "timeout_sec": 10,
        },
        "location": {
            "latitude": 42.888,
            "longitude": 141.603,
            "elevation": 21,
        },
    }


@pytest.fixture
def channel_map_file(tmp_path: Path) -> Path:
    """テスト用 channel_map.yaml (groups形式) を tmp_path に生成。"""
    data = {
        "irrigation": {"channel": 4, "label": "灌水ポンプ"},
        "side_window": {
            "groups": [
                {
                    "name": "北側窓",
                    "open_channel": 5,
                    "close_channel": 6,
                    "wind_close_directions": [1, 2, 16],
                },
                {
                    "name": "南側窓",
                    "open_channel": 8,
                    "close_channel": 7,
                    "wind_close_directions": [8, 9, 10],
                },
            ],
        },
        "relay_labels": {
            1: "暖房", 2: "循環扇", 3: "CO2発生器", 4: "灌水ポンプ",
            5: "北側窓(開)", 6: "北側窓(閉)", 7: "南側窓(閉)", 8: "南側窓(開)",
        },
        "valid_channels": {"min": 1, "max": 8},
    }
    p = tmp_path / "channel_map.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True)
    return p


@pytest.fixture
def base_crop_cfg() -> dict[str, Any]:
    """テスト用 crop_irrigation.yaml 相当の設定辞書。"""
    return {
        "house": {
            "house_id": "house01",
            "crop": "nasu_naga",
            "current_stage": "harvest_peak",
        },
        "crops": {
            "nasu_naga": {
                "stages": {
                    "harvest_peak": {
                        "defaults": {
                            "solar_threshold_mj": 0.9,
                            "irrigation_ml_per_plant": 270,
                        }
                    }
                }
            }
        },
    }


@pytest.fixture
def sensors_normal() -> dict[str, Any]:
    """通常時のセンサーデータ（降雨なし、弱風、適温）。"""
    return {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/h01/ccm/InSolar": {"value": 200.0},
            "agriha/farm/weather/misol": {
                "temperature_c": 18.0,
                "wind_speed_ms": 2.0,
                "wind_direction": 5,
                "rainfall": 0.0,
            },
        }
    }


@pytest.fixture
def status_normal() -> dict[str, Any]:
    """通常時のステータス（ロックアウトなし）。"""
    return {"locked_out": False, "relay_state": {}}


# ── 日中の固定時刻（10:00 JST, 2026-07-01 — 夏の昼間）─────────
DAYTIME = datetime(2026, 7, 1, 10, 0, 0, tzinfo=_JST)
# ── 夜間の固定時刻（00:00 JST, 2026-07-01）──────────
NIGHTTIME = datetime(2026, 7, 1, 0, 0, 0, tzinfo=_JST)
# ── 午後の固定時刻（13:00 JST, 2026-07-01）──────────
AFTERNOON = datetime(2026, 7, 1, 13, 0, 0, tzinfo=_JST)


# ──────────────────────────────────────────────
# v5新規: get_schedule_target テスト
# ──────────────────────────────────────────────

def test_schedule_target_morning(base_cfg):
    """10:00 JST → morning period, target=25"""
    target, period = get_schedule_target(base_cfg, DAYTIME)
    assert period == "morning"
    assert target == 25.0


def test_schedule_target_night(base_cfg):
    """00:00 JST → night period, target=17"""
    target, period = get_schedule_target(base_cfg, NIGHTTIME)
    assert period == "night"
    assert target == 17.0


def test_schedule_target_afternoon(base_cfg):
    """13:00 JST → afternoon period, target=28"""
    target, period = get_schedule_target(base_cfg, AFTERNOON)
    assert period == "afternoon"
    assert target == 28.0


# ──────────────────────────────────────────────
# v5新規: get_target_opening テスト
# ──────────────────────────────────────────────

def test_target_opening_interpolation():
    """外気温22℃ → 10℃=0%, 15℃=5%, 20℃=25%, 25℃=85% の間で線形補間"""
    table = {10: 0, 15: 5, 20: 25, 25: 85, 30: 100}
    # 22℃ → 20-25の間、ratio=0.4、25+0.4*(85-25)=49
    opening = get_target_opening(22.0, table)
    assert 40 <= opening <= 55  # 線形補間の範囲


def test_target_opening_exact_boundary():
    """外気温25℃ → テーブル値そのまま85%"""
    table = {10: 0, 15: 5, 20: 25, 25: 85, 30: 100}
    assert get_target_opening(25.0, table) == 85


def test_target_opening_below_min():
    """外気温5℃（テーブル最小以下）→ 最小値0%"""
    table = {10: 0, 15: 5, 20: 25, 25: 85, 30: 100}
    assert get_target_opening(5.0, table) == 0


def test_target_opening_above_max():
    """外気温35℃（テーブル最大以上）→ 最大値100%"""
    table = {10: 0, 15: 5, 20: 25, 25: 85, 30: 100}
    assert get_target_opening(35.0, table) == 100


# ──────────────────────────────────────────────
# v5新規: opening_to_relay_duration テスト
# ──────────────────────────────────────────────

def test_opening_to_relay_no_change():
    """開度変化が min_change_pct 未満 → none"""
    cfg = {"window_motor": {"full_open_duration_sec": 120, "min_change_pct": 5}}
    direction, duration = opening_to_relay_duration(50, 53, cfg)
    assert direction == "none"
    assert duration == 0


def test_opening_to_relay_open():
    """0% → 50% → open, 60秒"""
    cfg = {"window_motor": {"full_open_duration_sec": 120, "min_change_pct": 5}}
    direction, duration = opening_to_relay_duration(0, 50, cfg)
    assert direction == "open"
    assert duration == 60


def test_opening_to_relay_close():
    """100% → 0% → close, 120秒"""
    cfg = {"window_motor": {"full_open_duration_sec": 120, "min_change_pct": 5}}
    direction, duration = opening_to_relay_duration(100, 0, cfg)
    assert direction == "close"
    assert duration == 120


# ──────────────────────────────────────────────
# v5新規: get_rise_rate_per_hour テスト
# ──────────────────────────────────────────────

def test_rise_rate_calculation():
    """30分で+3℃ → 6℃/h"""
    t0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=_JST)
    t1 = t0 + timedelta(minutes=30)
    history = [
        {"t": t0.isoformat(), "temp": 20.0},
        {"t": t1.isoformat(), "temp": 23.0},
    ]
    rate = get_rise_rate_per_hour(history)
    assert rate is not None
    assert abs(rate - 6.0) < 0.1


def test_rise_rate_too_short():
    """データ間隔が5分未満 → None"""
    t0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=_JST)
    t1 = t0 + timedelta(minutes=3)
    history = [
        {"t": t0.isoformat(), "temp": 20.0},
        {"t": t1.isoformat(), "temp": 21.0},
    ]
    assert get_rise_rate_per_hour(history) is None


# ──────────────────────────────────────────────
# ① 降雨検知 → 全窓閉 (P1)
# ──────────────────────────────────────────────

def test_rain_closes_all_windows(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """降雨 rainfall=1.5mm/h → 全窓閉 (close_ch=1, open_ch=0)。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 1.5,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 20.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "rain_close_all" in triggered
    assert actions[6] == 1
    assert actions[5] == 0
    assert actions[7] == 1
    assert actions[8] == 0


# ──────────────────────────────────────────────
# ② 強風（北風 5m/s 超）→ 北側窓閉 (P2)
# ──────────────────────────────────────────────

def test_strong_north_wind_closes_north_windows(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """北風 wind_dir=2, speed=6m/s → 北側窓閉。南側はアクションなし。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 6.0,
                "wind_direction": 2,
                "temperature_c": 20.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "strong_wind" in triggered
    assert actions.get(6) == 1
    assert actions.get(5) == 0


# ──────────────────────────────────────────────
# ③ CO2低下 → 強制換気 (P5)
# ──────────────────────────────────────────────

def test_co2_critical_triggers_vent(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """CO2=250ppm (<300) → co2_critical_vent, 開度>=60%"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 26.0},
            "agriha/h01/ccm/InAirCO2": {"value": 250.0},
            "agriha/h01/ccm/InSolar": {"value": 200.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 22.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    assert "co2_critical_vent" in result["triggered_rules"]
    assert result["target_opening_pct"] >= 60


# ──────────────────────────────────────────────
# ④ 高湿度 → 換気 (P4)
# ──────────────────────────────────────────────

def test_humidity_triggers_vent(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """湿度=90% (>=85) → humidity_vent, 開度>=30%"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 26.0},
            "agriha/h01/ccm/InAirHumid": {"value": 90.0},
            "agriha/h01/ccm/InSolar": {"value": 200.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 22.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    assert "humidity_vent" in result["triggered_rules"]
    assert result["target_opening_pct"] >= 30


# ──────────────────────────────────────────────
# ⑤ 日射比例灌水 → 積算閾値到達で灌水実行 (P7)
# ──────────────────────────────────────────────

def test_solar_irrigation_threshold_reached(base_cfg, base_crop_cfg, sensors_normal, status_normal, channel_map_file):
    """InSolar=400W/m² × 600秒 = 0.24MJ。累積0.70+0.24=0.94 > 0.9 → 灌水実行。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InSolar"] = {"value": 400.0}
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.70, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: (a[1], a[2]) for a in result["relay_actions"]}

    assert "solar_irrigation" in triggered
    assert 4 in actions
    assert actions[4][0] == 1
    assert actions[4][1] is not None and actions[4][1] > 0
    assert result["solar_acc"]["accumulated_mj"] < 0.01
    assert result["solar_acc"]["irrigations_today"] == 1


# ──────────────────────────────────────────────
# ⑥ 日射比例灌水 → 閾値未到達で何もしない
# ──────────────────────────────────────────────

def test_solar_irrigation_threshold_not_reached(base_cfg, base_crop_cfg, sensors_normal, status_normal, channel_map_file):
    """InSolar=100W/m² × 600秒 = 0.06MJ。累積0.5+0.06=0.56 < 0.9 → 灌水なし。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InSolar"] = {"value": 100.0}
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.5, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "solar_irrigation" not in triggered
    assert result["solar_acc"]["accumulated_mj"] > 0.5


# ──────────────────────────────────────────────
# ⑦ 日付変更 → 積算値リセット
# ──────────────────────────────────────────────

def test_solar_accumulator_date_reset(tmp_path):
    """前日の solar_accumulator.json を読み込むと今日付でリセットされる。"""
    acc_path = tmp_path / "solar_accumulator.json"
    yesterday = "2026-02-28"
    acc_path.write_text(json.dumps({
        "date": yesterday,
        "accumulated_mj": 2.5,
        "irrigations_today": 5,
    }))

    acc = load_solar_accumulator(str(acc_path))
    import datetime as dt_mod
    today = dt_mod.date.today().isoformat()
    assert acc["date"] == today
    assert acc["accumulated_mj"] == 0.0
    assert acc["irrigations_today"] == 0


# ──────────────────────────────────────────────
# ⑧ Layer 1 ロックアウト中 → 全スキップ
# ──────────────────────────────────────────────

def test_layer1_lockout_skips_run(tmp_path):
    """lockout_state.json で Layer 1 ロックアウト中 → is_layer1_locked_out が True。"""
    lockout_path = tmp_path / "lockout_state.json"
    future = datetime.now(tz=_JST) + timedelta(minutes=3)
    lockout_path.write_text(json.dumps({
        "layer1_lockout_until": future.isoformat(),
        "last_action": "emergency_open",
    }))
    assert is_layer1_locked_out(str(lockout_path)) is True


def test_layer1_lockout_expired_not_locked(tmp_path):
    """lockout_state.json の期限が過去なら ロックアウトなし。"""
    lockout_path = tmp_path / "lockout_state.json"
    past = datetime.now(tz=_JST) - timedelta(minutes=10)
    lockout_path.write_text(json.dumps({
        "layer1_lockout_until": past.isoformat(),
    }))
    assert is_layer1_locked_out(str(lockout_path)) is False


# ──────────────────────────────────────────────
# ⑨ CommandGate ロックアウト中 → 全スキップ
# ──────────────────────────────────────────────

def test_commandgate_lockout_skips(tmp_path, base_cfg, base_crop_cfg):
    """GET /api/status → locked_out=True → run() が 1 を返す。"""
    config_path = tmp_path / "rules.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))

    with patch("agriha.control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        sensors_resp = MagicMock()
        sensors_resp.json.return_value = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 25.0},
                "agriha/h01/ccm/InSolar": {"value": 0.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                    "temperature_c": 20.0,
                },
            }
        }
        status_resp = MagicMock()
        status_resp.json.return_value = {"locked_out": True}
        mock_client.get.side_effect = [sensors_resp, status_resp]

        result = run(
            config_path=str(config_path),
            crop_config_path=str(crop_path),
            lockout_path=str(lockout_path),
            solar_acc_path=str(tmp_path / "solar_accumulator.json"),
            state_path=str(tmp_path / "rule_engine_state.json"),
        )

    assert result == 1


# ──────────────────────────────────────────────
# ⑩ REST API 接続失敗 → ログ出力して終了
# ──────────────────────────────────────────────

def test_api_failure_returns_error(tmp_path, base_cfg, base_crop_cfg):
    """httpx.ConnectError → run() が 1 を返す（安全側）。"""
    import httpx as httpx_mod

    config_path = tmp_path / "rules.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))

    with patch("agriha.control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = httpx_mod.ConnectError("Connection refused")

        result = run(
            config_path=str(config_path),
            crop_config_path=str(crop_path),
            lockout_path=str(lockout_path),
            solar_acc_path=str(tmp_path / "solar_accumulator.json"),
            state_path=str(tmp_path / "rule_engine_state.json"),
        )

    assert result == 1


# ──────────────────────────────────────────────
# ⑪ 日没後 → nighttime_close (P6)
# ──────────────────────────────────────────────

def test_nighttime_closes_all_windows(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """夜間(00:00 JST) → nighttime_close がトリガーされる。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 20.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 15.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=NIGHTTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]

    assert "nighttime_close" in triggered


# ──────────────────────────────────────────────
# ⑫ is_nighttime テスト
# ──────────────────────────────────────────────

def test_before_sunrise_is_nighttime(base_cfg):
    """日の出前(04:00 JST)は is_nighttime が True を返す。"""
    before_sunrise = datetime(2026, 7, 1, 3, 0, 0, tzinfo=_JST)
    assert is_nighttime(base_cfg, dt=before_sunrise) is True


def test_midday_is_not_nighttime(base_cfg):
    """正午(12:00 JST)は is_nighttime が False を返す。"""
    midday = datetime(2026, 7, 1, 12, 0, 0, tzinfo=_JST)
    assert is_nighttime(base_cfg, dt=midday) is False


# ──────────────────────────────────────────────
# ⑬ 急昇温 → 強制換気 (P3)
# ──────────────────────────────────────────────

def test_rapid_rise_triggers_vent(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """temp_history に 30分で+5℃ → rapid_rise_vent, 開度>=60%"""
    t0 = DAYTIME - timedelta(minutes=30)
    temp_history = [
        {"t": t0.isoformat(), "temp": 20.0},
        {"t": DAYTIME.isoformat(), "temp": 25.0},
    ]
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 26.0},
            "agriha/h01/ccm/InSolar": {"value": 200.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 22.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
        temp_history=temp_history,
    )
    assert "rapid_rise_vent" in result["triggered_rules"]
    assert result["target_opening_pct"] >= 60


# ──────────────────────────────────────────────
# ⑭ 正常フロー全実行テスト
# ──────────────────────────────────────────────

def test_run_normal_flow(tmp_path, base_cfg, base_crop_cfg, channel_map_file):
    """正常なAPI応答 → run() が 0 を返し state ファイルが生成される。"""
    config_path = tmp_path / "rules.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))
    state_path = tmp_path / "rule_engine_state.json"
    solar_acc_path = tmp_path / "solar_accumulator.json"

    with patch("agriha.control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        sensors_resp = MagicMock()
        sensors_resp.json.return_value = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 25.0},
                "agriha/h01/ccm/InSolar": {"value": 100.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                    "temperature_c": 20.0,
                },
            }
        }
        status_resp = MagicMock()
        status_resp.json.return_value = {"locked_out": False}
        mock_client.get.side_effect = [sensors_resp, status_resp]

        result = run(
            config_path=str(config_path),
            crop_config_path=str(crop_path),
            lockout_path=str(lockout_path),
            solar_acc_path=str(solar_acc_path),
            state_path=str(state_path),
            flag_dir=str(tmp_path / "flags"),
            channel_map_path=str(channel_map_file),
            temp_history_path=str(tmp_path / "temp_history.json"),
            window_pos_path=str(tmp_path / "window_position.json"),
        )

    assert result == 0
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "last_run_at" in state


# ──────────────────────────────────────────────
# weather flag 書き出しテスト
# ──────────────────────────────────────────────

class TestWeatherFlags:

    def test_rain_flag_written_on_rain(self, tmp_path: Path, base_cfg: dict) -> None:
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 1.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert (tmp_path / "rain_flag").exists()

    def test_rain_flag_deleted_on_clear(self, tmp_path: Path, base_cfg: dict) -> None:
        (tmp_path / "rain_flag").write_text("old")
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "rain_flag").exists()

    def test_wind_flag_written_on_strong_wind(self, tmp_path: Path, base_cfg: dict) -> None:
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 8.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert (tmp_path / "wind_flag").exists()

    def test_wind_flag_deleted_on_calm(self, tmp_path: Path, base_cfg: dict) -> None:
        (tmp_path / "wind_flag").write_text("old")
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 2.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "wind_flag").exists()

    def test_no_flag_on_normal_weather(self, tmp_path: Path, base_cfg: dict) -> None:
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "rain_flag").exists()
        assert not (tmp_path / "wind_flag").exists()


# ──────────────────────────────────────────────
# v5: 温度制御テスト — 外気温ベースステップテーブル (P6)
# ──────────────────────────────────────────────

def test_temp_above_target_opens_window(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """室温>目標 + 外気温22℃ → ステップテーブルで開度決定"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 30.0},
            "agriha/h01/ccm/InSolar": {"value": 200.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
                "temperature_c": 22.0,
            },
        }
    }
    solar_acc = {"date": "2026-07-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    assert "temp_schedule" in result["triggered_rules"]
    # 外気温22℃ → テーブル補間で ~49%
    assert result["target_opening_pct"] > 0
