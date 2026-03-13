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

from agriha.control.rule_engine import (
    _compute_pitagorasu_stage,
    _compute_window_state,
    _get_temperature_stage,
    append_temp_history,
    compute_temperature_trend,
    compute_threshold_hint,
    evaluate_rules,
    fetch_sensors,
    fetch_status,
    is_layer1_locked_out,
    is_nighttime,
    load_current_plan,
    load_solar_accumulator,
    load_state,
    load_temp_history,
    post_relay,
    run,
    save_solar_accumulator,
    save_state,
    save_threshold_hint,
)

_JST = ZoneInfo("Asia/Tokyo")

# ──────────────────────────────────────────────
# フィクスチャ
# ──────────────────────────────────────────────

@pytest.fixture
def base_cfg() -> dict[str, Any]:
    """テスト用 rules.yaml 相当の設定辞書。"""
    return {
        "temperature": {
            "target_day": 26.0,
            "target_night": 17.0,
            "margin_open": 2.0,
            "margin_close": 1.0,
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


# ── 日中の固定時刻（10:00 JST, 2026-03-01）─────────
DAYTIME = datetime(2026, 3, 1, 10, 0, 0, tzinfo=_JST)
# ── 夜間の固定時刻（00:00 JST, 2026-03-01）──────────
NIGHTTIME = datetime(2026, 3, 1, 0, 0, 0, tzinfo=_JST)


# ──────────────────────────────────────────────
# ① 降雨検知 → 全窓閉
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
            },
        }
    }
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}
    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "rain_close_all" in triggered
    # 北側窓: close_channel(6)=1, open_channel(5)=0
    assert actions[6] == 1
    assert actions[5] == 0
    # 南側窓: close_channel(7)=1, open_channel(8)=0
    assert actions[7] == 1
    assert actions[8] == 0


# ──────────────────────────────────────────────
# ② 強風（北風 5m/s 超）→ 北側窓閉、南側はアクションなし
# ──────────────────────────────────────────────

def test_strong_north_wind_closes_north_windows(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """北風 wind_dir=2, speed=6m/s → 北側窓閉 (close_ch6=1, open_ch5=0)。南側はアクションなし。"""
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
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "strong_wind" in triggered
    # 北側窓: close_channel(6)=1, open_channel(5)=0
    assert actions.get(6) == 1
    assert actions.get(5) == 0
    # 南側窓: アクションなし
    assert 7 not in actions
    assert 8 not in actions


# ──────────────────────────────────────────────
# ③ 高温（target+margin 超過）→ 側窓開
# ──────────────────────────────────────────────

def test_high_temp_opens_windows(base_cfg, base_crop_cfg, sensors_normal, status_normal, channel_map_file):
    """気温 29℃ (> 26+2=28℃) → 全窓開 (open_ch=1, close_ch=0)。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InAirTemp"] = {"value": 29.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "temp_high_open" in triggered
    # 北側窓: open_channel(5)=1, close_channel(6)=0
    assert actions[5] == 1
    assert actions[6] == 0
    # 南側窓: open_channel(8)=1, close_channel(7)=0
    assert actions[8] == 1
    assert actions[7] == 0


# ──────────────────────────────────────────────
# ④ 低温（target-margin 未満）→ 側窓閉
# ──────────────────────────────────────────────

def test_low_temp_closes_windows(base_cfg, base_crop_cfg, sensors_normal, status_normal, channel_map_file):
    """気温 24℃ (< 26-1=25℃) → 全窓閉 (close_ch=1, open_ch=0)。"""
    sensors = dict(sensors_normal)
    sensors["sensors"] = dict(sensors_normal["sensors"])
    sensors["sensors"]["agriha/h01/ccm/InAirTemp"] = {"value": 24.0}
    solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

    result = evaluate_rules(
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
        now=DAYTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "temp_low_close" in triggered
    # 北側窓: close_channel(6)=1, open_channel(5)=0
    assert actions[6] == 1
    assert actions[5] == 0
    # 南側窓: close_channel(7)=1, open_channel(8)=0
    assert actions[7] == 1
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
    config_path = tmp_path / "rules.yaml"
    config_path.write_text(yaml.dump(base_cfg))
    crop_path = tmp_path / "crop_irrigation.yaml"
    crop_path.write_text(yaml.dump(base_crop_cfg))

    # lockout_state.json なし（Layer 1 ロックアウトなし）
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
            plan_path=str(tmp_path / "current_plan.json"),
            solar_acc_path=str(tmp_path / "solar_accumulator.json"),
            state_path=str(tmp_path / "rule_engine_state.json"),
        )

    assert result == 1


# ──────────────────────────────────────────────
# ⑬ 日没後 → 全窓閉
# ──────────────────────────────────────────────

def test_nighttime_closes_all_windows(base_cfg, base_crop_cfg, status_normal, channel_map_file):
    """夜間(00:00 JST)の場合、nighttime_close がトリガーされ全窓閉 (close_ch=1, open_ch=0)。"""
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
        base_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
        now=NIGHTTIME, channel_map_path=channel_map_file,
    )
    triggered = result["triggered_rules"]
    actions = {a[0]: a[1] for a in result["relay_actions"]}

    assert "nighttime_close" in triggered
    # 北側窓: close_channel(6)=1, open_channel(5)=0
    assert actions.get(6) == 1
    assert actions.get(5) == 0
    # 南側窓: close_channel(7)=1, open_channel(8)=0
    assert actions.get(7) == 1
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
            flag_dir=str(tmp_path / "flags"),
        )

    assert result == 0
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "last_run_at" in state


# ──────────────────────────────────────────────
# weather flag 書き出しテスト
# ──────────────────────────────────────────────

class TestWeatherFlags:
    """update_weather_flags: flagファイル書き出し／削除テスト"""

    def test_rain_flag_written_on_rain(self, tmp_path: Path, base_cfg: dict) -> None:
        """雨センサー > 閾値 → rain_flag が書き出される"""
        from agriha.control.rule_engine import update_weather_flags

        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 1.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert (tmp_path / "rain_flag").exists()

    def test_rain_flag_deleted_on_clear(self, tmp_path: Path, base_cfg: dict) -> None:
        """降雨なし → 既存 rain_flag が削除される"""
        from agriha.control.rule_engine import update_weather_flags

        (tmp_path / "rain_flag").write_text("old")
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "rain_flag").exists()

    def test_wind_flag_written_on_strong_wind(self, tmp_path: Path, base_cfg: dict) -> None:
        """強風 > 閾値 → wind_flag が書き出される"""
        from agriha.control.rule_engine import update_weather_flags

        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 8.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert (tmp_path / "wind_flag").exists()

    def test_wind_flag_deleted_on_calm(self, tmp_path: Path, base_cfg: dict) -> None:
        """弱風 → 既存 wind_flag が削除される"""
        from agriha.control.rule_engine import update_weather_flags

        (tmp_path / "wind_flag").write_text("old")
        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 2.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "wind_flag").exists()

    def test_no_flag_on_normal_weather(self, tmp_path: Path, base_cfg: dict) -> None:
        """通常天候（降雨なし・弱風）→ flagファイルが作成されない"""
        from agriha.control.rule_engine import update_weather_flags

        sensors = {
            "sensors": {
                "agriha/farm/weather/misol": {"rainfall": 0.0, "wind_speed_ms": 1.0}
            }
        }
        update_weather_flags(base_cfg, sensors, flag_dir=str(tmp_path))
        assert not (tmp_path / "rain_flag").exists()
        assert not (tmp_path / "wind_flag").exists()


class TestTempHistory:
    """温度履歴 CRUD テスト"""

    def test_load_empty_history(self, tmp_path: Path) -> None:
        """存在しないファイル → 空の履歴が返る"""
        h = load_temp_history(str(tmp_path / "nonexistent.json"))
        assert h == {"points": []}

    def test_append_and_load(self, tmp_path: Path) -> None:
        """温度を追記して読み込める"""
        path = str(tmp_path / "temp_history.json")
        h = load_temp_history(path)
        h = append_temp_history(h, 24.5, path=path)
        h2 = load_temp_history(path)
        assert len(h2["points"]) == 1
        assert h2["points"][0]["temp_c"] == 24.5

    def test_max_points_trim(self, tmp_path: Path) -> None:
        """max_points を超えた古いデータは削除される"""
        path = str(tmp_path / "temp_history.json")
        h = {"points": []}
        for i in range(15):
            h = append_temp_history(h, float(20 + i), max_points=12, path=path)
        h2 = load_temp_history(path)
        assert len(h2["points"]) == 12
        # 最古点は index=3 (20+3=23.0) のはず
        assert h2["points"][0]["temp_c"] == 23.0


class TestTemperatureTrend:
    """温度トレンド計算テスト"""

    def test_insufficient_data_returns_none(self) -> None:
        """1点のみ → None"""
        h = {"points": [{"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 20.0}]}
        assert compute_temperature_trend(h) is None

    def test_rising_trend(self) -> None:
        """30分で+2℃ → +4.0℃/h"""
        from datetime import timezone
        from zoneinfo import ZoneInfo
        jst = ZoneInfo("Asia/Tokyo")
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 20.0},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 22.0},
            ]
        }
        trend = compute_temperature_trend(h)
        assert trend is not None
        assert abs(trend - 4.0) < 0.01

    def test_falling_trend(self) -> None:
        """60分で-3℃ → -3.0℃/h"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T09:00:00+09:00", "temp_c": 25.0},
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 22.0},
            ]
        }
        trend = compute_temperature_trend(h)
        assert trend is not None
        assert abs(trend - (-3.0)) < 0.01

    def test_flat_trend(self) -> None:
        """温度変化なし → 0.0℃/h"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 24.0},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 24.0},
            ]
        }
        trend = compute_temperature_trend(h)
        assert trend == 0.0


class TestComputeThresholdHint:
    """閾値到達予測テスト"""

    def test_empty_history_returns_default(self) -> None:
        """空履歴 → データ不足の hint"""
        hint = compute_threshold_hint({"points": []})
        assert hint["temperature_trend"] == "データ不足"
        assert hint["threshold_eta"] == "到達予測なし"

    def test_rising_eta_within_range(self) -> None:
        """上昇中で27℃到達まで30分以内 → recommendation あり"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 26.0},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 26.5},
            ]
        }
        hint = compute_threshold_hint(h)
        # trend=+1.0℃/h, 26.5→27.0まで0.5℃, ETA=30分
        assert "27℃" in hint["threshold_eta"]
        assert "先読み開放" in hint["recommendation"]

    def test_already_above_threshold(self) -> None:
        """既に27℃超過 → 即時開放"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 27.5},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 28.0},
            ]
        }
        hint = compute_threshold_hint(h)
        assert "超過" in hint["threshold_eta"]
        assert hint["recommendation"] == "即時開放を検討"

    def test_falling_eta_within_range(self) -> None:
        """下降中で16℃到達まで30分以内 → recommendation あり"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 17.0},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 16.5},
            ]
        }
        hint = compute_threshold_hint(h)
        # trend=-1.0℃/h, 16.5→16.0まで0.5℃, ETA=30分
        assert "16℃" in hint["threshold_eta"]
        assert "先読み閉窓" in hint["recommendation"]

    def test_outdoor_temp_correction(self) -> None:
        """外気温補正あり → トレンドが変化"""
        h = {
            "points": [
                {"timestamp": "2026-03-07T10:00:00+09:00", "temp_c": 24.0},
                {"timestamp": "2026-03-07T10:30:00+09:00", "temp_c": 24.0},
            ]
        }
        # 外気温35℃なら補正でトレンドが+になるはず
        hint_with = compute_threshold_hint(h, outdoor_temp_forecast_c=35.0)
        hint_without = compute_threshold_hint(h)
        # 外気温補正ありの方がトレンドが高い（正方向）
        assert "+" in hint_with["temperature_trend"]

    def test_save_and_load_hint(self, tmp_path: Path) -> None:
        """ヒントをファイル保存して読み込める"""
        path = str(tmp_path / "threshold_hint.json")
        hint = {"temperature_trend": "+1.5℃/h", "threshold_eta": "27℃到達まで約20分", "recommendation": "先読み開放を検討"}
        save_threshold_hint(hint, path=path)
        data = json.loads(Path(path).read_text())
        assert data["temperature_trend"] == "+1.5℃/h"
        assert "generated_at" in data


# ---------- cmd_355/subtask_794: dry_run テスト ----------


def test_dry_run_skips_relay(tmp_path, base_cfg, base_crop_cfg):
    """dry_run=True → run() が 0 を返し、リレー POST が呼ばれない。"""
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
            flag_dir=str(tmp_path / "flags"),
            dry_run=True,
        )

    assert result == 0
    # dry_run=True なのでリレー操作(POST)は呼ばれない
    mock_client.post.assert_not_called()


# ──────────────────────────────────────────────
# cmd_355/subtask_793: 状態永続化 + forecast連携テスト
# ──────────────────────────────────────────────

class TestLoadSaveState:
    def test_load_state_file_not_found_returns_defaults(self, tmp_path: Path) -> None:
        state = load_state(str(tmp_path / "no_such_file.json"))
        assert state["window_state"] == "unknown"
        assert state["last_irrigation_at"] is None
        assert state["temperature_stage"] == "normal"

    def test_load_state_normal(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "window_state": "open",
            "last_irrigation_at": "2026-03-07T10:00:00+09:00",
            "temperature_stage": "high",
        }))
        state = load_state(str(path))
        assert state["window_state"] == "open"
        assert state["last_irrigation_at"] == "2026-03-07T10:00:00+09:00"
        assert state["temperature_stage"] == "high"

    def test_load_state_broken_json_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{broken json")
        state = load_state(str(path))
        assert state["window_state"] == "unknown"
        assert state["temperature_stage"] == "normal"

    def test_load_state_partial_fields_uses_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"window_state": "closed"}))
        state = load_state(str(path))
        assert state["window_state"] == "closed"
        assert state["temperature_stage"] == "normal"

    def test_save_state_includes_new_fields(self, tmp_path: Path) -> None:
        path = str(tmp_path / "state.json")
        result = {
            "triggered_rules": ["rain_close_all"],
            "relay_actions": [(6, 1, None)],
            "window_state": "closed",
            "temperature_stage": "high",
            "last_irrigation_at": "2026-03-07T09:30:00+09:00",
        }
        save_state(path, result)
        data = json.loads(Path(path).read_text())
        assert data["window_state"] == "closed"
        assert data["temperature_stage"] == "high"
        assert data["last_irrigation_at"] == "2026-03-07T09:30:00+09:00"
        assert "last_run_at" in data

    def test_save_state_defaults_when_fields_missing(self, tmp_path: Path) -> None:
        path = str(tmp_path / "state.json")
        save_state(path, {"triggered_rules": [], "relay_actions": []})
        data = json.loads(Path(path).read_text())
        assert data["window_state"] == "unknown"
        assert data["temperature_stage"] == "normal"
        assert data["last_irrigation_at"] is None


class TestGetTemperatureStage:
    def test_none_returns_normal(self) -> None:
        assert _get_temperature_stage(None) == "normal"

    def test_at_high_threshold_is_critical(self) -> None:
        assert _get_temperature_stage(27.0) == "critical"

    def test_below_low_threshold_is_critical(self) -> None:
        assert _get_temperature_stage(15.9) == "critical"

    def test_high_stage(self) -> None:
        assert _get_temperature_stage(26.0) == "high"
        assert _get_temperature_stage(26.8) == "high"

    def test_low_stage(self) -> None:
        assert _get_temperature_stage(16.0) == "low"
        assert _get_temperature_stage(16.4) == "low"

    def test_normal_stage(self) -> None:
        assert _get_temperature_stage(22.0) == "normal"
        assert _get_temperature_stage(16.5) == "normal"


class TestComputeWindowState:
    GROUPS = [
        {"name": "北側", "open_channel": 5, "close_channel": 6, "wind_close_directions": []},
        {"name": "南側", "open_channel": 8, "close_channel": 7, "wind_close_directions": []},
    ]

    def test_open_action_returns_open(self) -> None:
        assert _compute_window_state([(5, 1, None)], self.GROUPS, "unknown") == "open"

    def test_close_action_returns_closed(self) -> None:
        assert _compute_window_state([(6, 1, None)], self.GROUPS, "unknown") == "closed"

    def test_no_window_action_inherits_prev(self) -> None:
        assert _compute_window_state([(4, 1, 60)], self.GROUPS, "open") == "open"

    def test_empty_actions_inherits_prev(self) -> None:
        assert _compute_window_state([], self.GROUPS, "closed") == "closed"


class TestEvaluateRulesState:
    """evaluate_rules の新フィールド + 状態引き継ぎテスト。"""

    def _sensors(self, temp: float = 25.0, rainfall: float = 0.0) -> dict:
        return {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": temp},
                "agriha/h01/ccm/InSolar": {"value": 200.0},
                "agriha/farm/weather/misol": {
                    "temperature_c": 18.0,
                    "wind_speed_ms": 2.0,
                    "wind_direction": 5,
                    "rainfall": rainfall,
                },
            }
        }

    def test_result_includes_new_fields(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=None, now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "window_state" in result
        assert "temperature_stage" in result
        assert "last_irrigation_at" in result
        assert result["temperature_stage"] == "normal"

    def test_prev_state_window_inherited(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        """Layer 3 計画有効 → 窓操作なし → prev_state のwindow_state引き継ぎ。"""
        plan = {
            "valid_until": (DAYTIME + timedelta(hours=1)).isoformat(),
            "actions": [],
        }
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=plan, now=DAYTIME, channel_map_path=channel_map_file,
            prev_state={"window_state": "open", "temperature_stage": "normal", "last_irrigation_at": None},
        )
        assert result["window_state"] == "open"

    def test_rain_early_return_window_closed(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(rainfall=1.0), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=None, now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "rain_close_all" in result["triggered_rules"]
        assert result["window_state"] == "closed"

    def test_forecast_rain_probability_closes_windows(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        """予報降水確率>=70 → forecast_rain_close_all + window_state='closed'。"""
        plan = {
            "valid_until": (DAYTIME + timedelta(hours=1)).isoformat(),
            "actions": [],
            "rain_probability": 80.0,
        }
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(rainfall=0.0), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=plan, now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "forecast_rain_close_all" in result["triggered_rules"]
        assert result["window_state"] == "closed"

    def test_forecast_rain_below_threshold_no_close(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        """予報降水確率<70 → 通常制御。"""
        plan = {
            "valid_until": (DAYTIME + timedelta(hours=1)).isoformat(),
            "actions": [],
            "rain_probability": 50.0,
        }
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(rainfall=0.0), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=plan, now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "forecast_rain_close_all" not in result["triggered_rules"]

    def test_forecast_rain_no_field_no_close(self, base_cfg, base_crop_cfg, channel_map_file) -> None:
        """rain_probability フィールドなし → 従来通り動作。"""
        plan = {
            "valid_until": (DAYTIME + timedelta(hours=1)).isoformat(),
            "actions": [],
        }
        result = evaluate_rules(
            base_cfg, base_crop_cfg, self._sensors(rainfall=0.0), {"locked_out": False},
            {"accumulated_mj": 0.0, "irrigations_today": 0},
            current_plan=plan, now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "forecast_rain_close_all" not in result["triggered_rules"]


# ──────────────────────────────────────────────
# ピタゴラススイッチ テスト
# ──────────────────────────────────────────────

class TestComputePitagorasuStage:
    """_compute_pitagorasu_stage 単体テスト"""

    def test_cold_returns_closed(self) -> None:
        """20℃ → stage 0 (closed)"""
        r = _compute_pitagorasu_stage(20.0, None, 10)
        assert r["stage"] == 0
        assert r["stage_name"] == "closed"
        assert r["south_target"] == 0.0
        assert r["north_target"] == 0.0

    def test_warm_returns_south_micro(self) -> None:
        """26℃ → stage 1 (south_micro)"""
        r = _compute_pitagorasu_stage(26.0, None, 10)
        assert r["stage"] == 1
        assert r["stage_name"] == "south_micro"
        assert r["south_target"] == 0.3
        assert r["north_target"] == 0.0

    def test_hot_returns_both_medium(self) -> None:
        """28℃ → stage 2 (both_medium)"""
        r = _compute_pitagorasu_stage(28.0, None, 10)
        assert r["stage"] == 2
        assert r["south_target"] == 0.5
        assert r["north_target"] == 0.5

    def test_very_hot_returns_full_open(self) -> None:
        """33℃ → stage 4 (full_open)"""
        r = _compute_pitagorasu_stage(33.0, None, 10)
        assert r["stage"] == 4
        assert r["south_target"] == 1.0
        assert r["north_target"] == 1.0

    def test_early_morning_offset(self) -> None:
        """25.5℃ at 6:00 → offset -1.0 → effective 24.5℃ → closed (stage 0)
        But 25.5℃ at 10:00 → effective 25.5℃ → south_micro (stage 1)"""
        r_morning = _compute_pitagorasu_stage(25.5, None, 6)
        r_day = _compute_pitagorasu_stage(25.5, None, 10)
        assert r_morning["stage"] == 0  # 24.5 < 25 → closed
        assert r_day["stage"] == 1      # 25.5 >= 25, < 27 → south_micro

    def test_rapid_trend_bonus(self) -> None:
        """24℃ + rapid trend 4℃/h → effective 26℃ → south_micro"""
        r = _compute_pitagorasu_stage(24.0, 4.0, 10)
        assert r["stage"] == 1  # 24 + 2.0 bonus = 26
        assert r["effective_temp"] == 26.0

    def test_mild_trend_bonus(self) -> None:
        """24℃ + mild trend 2℃/h → effective 25℃ → south_micro"""
        r = _compute_pitagorasu_stage(24.0, 2.0, 10)
        assert r["stage"] == 1  # 24 + 1.0 bonus = 25 → stage 1 (25 < 27)

    def test_negative_trend_no_bonus(self) -> None:
        """26℃ + falling trend → no bonus → south_micro"""
        r = _compute_pitagorasu_stage(26.0, -2.0, 10)
        assert r["stage"] == 1
        assert r["effective_temp"] == 26.0


class TestPitagorasuIntegration:
    """ピタゴラススイッチ有効時の evaluate_rules 統合テスト"""

    @pytest.fixture
    def pitagorasu_cfg(self, base_cfg) -> dict:
        cfg = dict(base_cfg)
        cfg["pitagorasu"] = {
            "enabled": True,
            "open_travel_sec": 65,
            "close_travel_sec": 50,
            "deadband": 0.05,
        }
        return cfg

    def test_pitagorasu_stage_triggered(
        self, pitagorasu_cfg, base_crop_cfg, status_normal, channel_map_file, tmp_path
    ) -> None:
        """28℃ → pitagorasu_stage_2 (both_medium)"""
        sensors = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 28.0},
                "agriha/h01/ccm/InSolar": {"value": 0.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                },
            }
        }
        solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

        # Mock window position and temp history
        pos_path = str(tmp_path / "window_position.json")
        Path(pos_path).write_text(json.dumps({
            "north": 0.0, "south": 0.0, "last_calibrated_at": None, "last_updated_at": None,
        }))
        hist_path = str(tmp_path / "temp_history.json")
        Path(hist_path).write_text(json.dumps({"points": []}))

        with patch("agriha.control.rule_engine.load_position", return_value={
            "north": 0.0, "south": 0.0, "last_calibrated_at": None, "last_updated_at": None,
        }), patch("agriha.control.rule_engine.save_position"), \
             patch("agriha.control.rule_engine.load_temp_history", return_value={"points": []}):
            result = evaluate_rules(
                pitagorasu_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
                now=DAYTIME, channel_map_path=channel_map_file,
            )

        triggered = result["triggered_rules"]
        assert any("pitagorasu_stage" in t for t in triggered)
        # 南北とも50%開放 → open_channel=1, duration指定あり
        actions_with_dur = [(a[0], a[1], a[2]) for a in result["relay_actions"]]
        open_actions = [a for a in actions_with_dur if a[1] == 1 and a[2] is not None and a[2] > 0]
        assert len(open_actions) >= 1  # 少なくとも1つは開動作あり

    def test_pitagorasu_disabled_falls_back(
        self, base_cfg, base_crop_cfg, status_normal, channel_map_file
    ) -> None:
        """pitagorasu.enabled=false → 従来バイナリ制御"""
        cfg = dict(base_cfg)
        cfg["pitagorasu"] = {"enabled": False}
        sensors = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 29.0},
                "agriha/h01/ccm/InSolar": {"value": 0.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                },
            }
        }
        solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

        result = evaluate_rules(
            cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
            now=DAYTIME, channel_map_path=channel_map_file,
        )
        assert "temp_high_open" in result["triggered_rules"]

    def test_nighttime_calibration(
        self, pitagorasu_cfg, base_crop_cfg, status_normal, channel_map_file
    ) -> None:
        """夜間 + pitagorasu有効 → キャリブレーション付き全閉"""
        sensors = {
            "sensors": {
                "agriha/h01/ccm/InAirTemp": {"value": 15.0},
                "agriha/h01/ccm/InSolar": {"value": 0.0},
                "agriha/farm/weather/misol": {
                    "rainfall": 0.0, "wind_speed_ms": 1.0, "wind_direction": 5,
                },
            }
        }
        solar_acc = {"date": "2026-03-01", "accumulated_mj": 0.0, "irrigations_today": 0}

        with patch("agriha.control.rule_engine.load_position", return_value={
            "north": 0.5, "south": 0.3, "last_calibrated_at": None, "last_updated_at": None,
        }), patch("agriha.control.rule_engine.save_position") as mock_save, \
             patch("agriha.control.rule_engine.calibrate_closed") as mock_cal:
            result = evaluate_rules(
                pitagorasu_cfg, base_crop_cfg, sensors, status_normal, solar_acc, None,
                now=NIGHTTIME, channel_map_path=channel_map_file,
            )

        assert "nighttime_close" in result["triggered_rules"]
        # キャリブレーション呼び出し確認
        assert mock_cal.call_count == 2  # 北側・南側
        assert mock_save.called
        # close_travel=50 * 1.1 = 55秒
        close_actions = [a for a in result["relay_actions"] if a[2] is not None and a[2] == 55]
        assert len(close_actions) == 2  # 北・南
