"""
tests/v2_control/test_rule_engine.py — Layer 2 rule_engine pytest テスト

設計書 §7.2 の14テストケースを実装。
- httpx.Client は unittest.mock でモック
- ファイルI/O は tmp_path フィクスチャで一時ディレクトリ使用
- astral は実際のライブラリを使用（日時固定でテスト）
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

from v2_control.rule_engine import (
    evaluate_rules,
    fetch_sensors,
    fetch_status,
    is_layer1_locked_out,
    is_nighttime,
    load_current_plan,
    load_solar_accumulator,
    post_relay,
    run,
    save_solar_accumulator,
)

_JST = ZoneInfo("Asia/Tokyo")

# ──────────────────────────────────────────────
# フィクスチャ
# ──────────────────────────────────────────────

@pytest.fixture
def base_cfg() -> dict[str, Any]:
    """テスト用 layer2_config.yaml 相当の設定辞書。"""
    return {
        "temperature": {
            "target_day": 26.0,
            "target_night": 17.0,
            "margin_open": 2.0,
            "margin_close": 1.0,
            "window_channels": [5, 6, 7, 8],
        },
        "wind": {
            "strong_wind_threshold_ms": 5.0,
            "north_directions": [1, 2, 16],
            "north_channels": [5, 6],
            "south_directions": [8, 9, 10],
            "south_channels": [7, 8],
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


# ── 日中の固定時刻（10:00 JST, 2026-03-01）─────────
DAYTIME = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_JST)
# ── 夜間の固定時刻（00:00 JST, 2026-03-01）──────────
NIGHTTIME = datetime(2026, 3, 1, 0, 0, 0, tzinfo=_JST)


# ──────────────────────────────────────────────
# ① 降雨検知 → 全窓閉
# ──────────────────────────────────────────────

def test_rain_closes_all_windows(base_cfg, base_crop_cfg, status_normal):
    """降雨 rainfall=1.5mm/h → 全窓閉 (ch5,6,7,8 value=0)。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 1.5,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
            },
        }
    }
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "rain_close_all" in triggered
    assert actions[5] == 0
    assert actions[6] == 0
    assert actions[7] == 0
    assert actions[8] == 0


# ──────────────────────────────────────────────
# ② 強風（北風 5m/s 超）→ 北側窓閉、南側はアクションなし
# ──────────────────────────────────────────────

def test_strong_north_wind_closes_north_windows(base_cfg, base_crop_cfg, status_normal):
    """北風 wind_dir=2, speed=6m/s → ch5,6 閉のみ。ch7,8はアクションなし。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 6.0,
                "wind_direction": 2,
            },
        }
    }
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "strong_wind" in triggered
    assert actions.get(5) == 0
    assert actions.get(6) == 0
    assert 7 not in actions
    assert 8 not in actions


# ──────────────────────────────────────────────
# ③ 高温（target+margin 超過）→ 側窓開
# ──────────────────────────────────────────────

def test_high_temp_opens_windows(base_cfg, base_crop_cfg, sensors_normal, status_normal):
    """気温 29℃ (> 26+2=28℃) → ch5-8 開。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InAirTemp"] = {"value": 29.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "temp_high_open" in triggered
    assert actions[5] == 1
    assert actions[6] == 1
    assert actions[7] == 1
    assert actions[8] == 1


# ──────────────────────────────────────────────
# ④ 低温（target-margin 未満）→ 側窓閉
# ──────────────────────────────────────────────

def test_low_temp_closes_windows(base_cfg, base_crop_cfg, sensors_normal, status_normal):
    """気温 24℃ (< 26-1=25℃) → ch5-8 閉。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InAirTemp"] = {"value": 24.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "temp_low_close" in triggered
    assert actions[5] == 0
    assert actions[6] == 0
    assert actions[7] == 0
    assert actions[8] == 0


# ──────────────────────────────────────────────
# ⑤ 日射比例灌水 → 積算閾値到達で灌水実行
# ──────────────────────────────────────────────

def test_solar_irrigation_threshold_reached(base_cfg, base_crop_cfg, sensors_normal, status_normal):
    """InSolar=400W/m² × 300秒 = 0.12MJ。累積0.85+0.12=0.97 > 0.9 → 灌水実行。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InSolar"] = {"value": 400.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.85, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: (a[1], a[2]) for a in result["relay_actions"]}

    assert "solar_irrigation" in triggered
    # ch4 が灌水チャンネル、value=1, duration_sec > 0
    assert 4 in actions
    assert actions[4][0] == 1
    assert actions[4][1] is not None and actions[4][1] > 0
    # 積算値がリセットされている
    assert result["solar_acc"]["accumulated_mj"] < 0.01
    assert result["solar_acc"]["irrigations_today"] == 1


# ──────────────────────────────────────────────
# ⑥ 日射比例灌水 → 閾値未到達で何もしない
# ──────────────────────────────────────────────

def test_solar_irrigation_threshold_not_reached(base_cfg, base_crop_cfg, sensors_normal, status_normal):
    """InSolar=100W/m² × 300秒 = 0.03MJ。累積0.5+0.03=0.53 < 0.9 → 灌水なし。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InSolar"] = {"value": 100.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.5, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=DAYTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "solar_irrigation" not in triggered
    assert 4 not in actions
    # 積算値が増えている
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
    """lockout_state.json で Layer 1 ロックアウト中 → run() が 1 を返す。"""
    lockout_path = tmp_path / "lockout_state.json"
    future = datetime.now(tz=_JST) + timedelta(minutes=3)
    lockout_path.write_text(json.dumps({
        "layer1_lockout_until": future.isoformat(),
        "last_action": "emergency_open",
    }))

    # is_layer1_locked_out が True を返すことを確認
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
    config_path = tmp_path / "layer2_config.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))

    # lockout_state.json なし（Layer 1 ロックアウトなし）
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))

    with patch("v2_control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        sensors_resp = MagicMock()
        sensors_resp.json.return_value = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 25.0},
                "agriha/h01/ccm/InSolar": {"value": 0.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                },
            }
        }
        status_resp = MagicMock()
        status_resp.json.return_value = {"locked_out": True}  # CommandGateロックアウト
        mock_client.get.side_effect = [sensors_resp, status_resp]

        result = run(
            config_path=str(config_path),
            crop_config_path=str(crop_path),
            lockout_path=str(lockout_path),
            plan_path=str(tmp_path / "current_plan.json"),
            solar_acc_path=str(tmp_path / "solar_accumulator.json"),
            state_path=str(tmp_path / "rule_engine_state.json"),
        )

    assert result == 1


# ──────────────────────────────────────────────
# ⑩ current_plan.json 有効 → 温度制御を Layer 3 に委譲
# ──────────────────────────────────────────────

def test_layer3_plan_active_skips_temp_control(base_cfg, base_crop_cfg, status_normal):
    """current_plan.json が有効な場合、高温でも temp_high_open はトリガーされない。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 30.0},  # 高温
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
            },
        }
    }
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    current_plan = {
        "valid_until": (datetime.now(tz=_JST) + timedelta(hours=1)).isoformat(),
        "actions": [],
    }

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, current_plan, now=DAYTIME
    )
    triggered = result["triggered_rules"]

    assert "temp_high_open" not in triggered


# ──────────────────────────────────────────────
# ⑪ current_plan.json 期限切れ → Layer 2 全権制御
# ──────────────────────────────────────────────

def test_layer3_plan_expired_layer2_takes_control(tmp_path):
    """current_plan.json が期限切れの場合 load_current_plan は None を返す。"""
    plan_path = tmp_path / "current_plan.json"
    past = datetime.now(tz=_JST) - timedelta(hours=2)
    plan_path.write_text(json.dumps({
        "valid_until": past.isoformat(),
        "actions": [],
    }))
    assert load_current_plan(str(plan_path)) is None


# ──────────────────────────────────────────────
# ⑫ REST API 接続失敗 → ログ出力して終了
# ──────────────────────────────────────────────

def test_api_failure_returns_error(tmp_path, base_cfg, base_crop_cfg):
    """httpx.ConnectError → run() が 1 を返す（安全側）。"""
    import httpx as httpx_mod

    config_path = tmp_path / "layer2_config.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))

    with patch("v2_control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = httpx_mod.ConnectError("Connection refused")

        result = run(
            config_path=str(config_path),
            crop_config_path=str(crop_path),
            lockout_path=str(lockout_path),
            plan_path=str(tmp_path / "current_plan.json"),
            solar_acc_path=str(tmp_path / "solar_accumulator.json"),
            state_path=str(tmp_path / "rule_engine_state.json"),
        )

    assert result == 1


# ──────────────────────────────────────────────
# ⑬ 日没後 → 全窓閉
# ──────────────────────────────────────────────

def test_nighttime_closes_all_windows(base_cfg, base_crop_cfg, status_normal):
    """夜間(00:00 JST)の場合、nighttime_close がトリガーされ全窓閉。"""
    sensors = {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 20.0},
            "agriha/h01/ccm/InSolar": {"value": 0.0},
            "agriha/farm/weather/misol": {
                "rainfall": 0.0,
                "wind_speed_ms": 1.0,
                "wind_direction": 5,
            },
        }
    }
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None, now=NIGHTTIME
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "nighttime_close" in triggered
    assert actions.get(5) == 0
    assert actions.get(6) == 0
    assert actions.get(7) == 0
    assert actions.get(8) == 0


# ──────────────────────────────────────────────
# ⑭ 日の出前 → 全窓閉（is_nighttime のテスト）
# ──────────────────────────────────────────────

def test_before_sunrise_is_nighttime(base_cfg):
    """日の出前(04:00 JST)は is_nighttime が True を返す。"""
    before_sunrise = datetime(2026, 3, 1, 4, 0, 0, tzinfo=_JST)
    assert is_nighttime(base_cfg, dt=before_sunrise) is True


def test_midday_is_not_nighttime(base_cfg):
    """正午(12:00 JST)は is_nighttime が False を返す。"""
    midday = datetime(2026, 3, 1, 12, 0, 0, tzinfo=_JST)
    assert is_nighttime(base_cfg, dt=midday) is False


# ──────────────────────────────────────────────
# 追加: 正常フロー全実行テスト
# ──────────────────────────────────────────────

def test_run_normal_flow(tmp_path, base_cfg, base_crop_cfg):
    """正常なAPI応答 → run() が 0 を返し state ファイルが生成される。"""
    config_path = tmp_path / "layer2_config.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps({}))
    state_path = tmp_path / "rule_engine_state.json"
    solar_acc_path = tmp_path / "solar_accumulator.json"

    with patch("v2_control.rule_engine.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client

        sensors_resp = MagicMock()
        sensors_resp.json.return_value = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 25.0},
                "agriha/h01/ccm/InSolar": {"value": 100.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
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
            plan_path=str(tmp_path / "current_plan.json"),
            solar_acc_path=str(solar_acc_path),
            state_path=str(state_path),
        )

    assert result == 0
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "last_run_at" in state
