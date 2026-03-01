"""Layer 3 forecast_engine.py テスト

設計書: docs/v2_three_layer_design.md §7.3
テスト方針:
  - anthropic.Anthropic() をモック（API 呼び出しなし）
  - httpx.Client をモック（REST API 呼び出しなし）
  - SQLite は :memory: で動作
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from v2_control.forecast_engine import (
    TOOLS,
    call_tool,
    extract_plan_json,
    init_db,
    is_commandgate_locked,
    is_layer1_locked,
    load_recent_history,
    run_forecast,
    save_decision,
    validate_actions,
)

_JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SENSOR_JSON = json.dumps({
    "ccm": {"InAirTemp": 25.3, "InAirHumi": 65.0, "CO2": 420},
    "ds18b20": {"temperature_c": 24.8},
    "misol": {
        "temperature_c": 18.5, "humidity_pct": 70,
        "wind_speed_ms": 2.1, "wind_direction": 5,
        "rainfall": 0.0, "uv_index": 4, "solar_radiation_wm2": 450,
    },
    "relays": {"ch1": 0, "ch2": 0, "ch3": 0, "ch4": 0, "ch5": 1, "ch6": 0, "ch7": 0, "ch8": 0},
})

STATUS_JSON = json.dumps({
    "locked_out": False,
    "uptime_sec": 86400,
    "relays": {"ch1": 0, "ch2": 0, "ch3": 0, "ch4": 0, "ch5": 1, "ch6": 0, "ch7": 0, "ch8": 0},
})

PLAN_TEXT = """以下が向こう1時間のアクション計画です。

```json
{
  "summary": "気温安定、現状維持",
  "actions": [
    {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 5, "value": 1, "duration_sec": 30, "reason": "北側窓開放維持"},
    {"execute_at": "2026-03-01T15:30:00+09:00", "relay_ch": 4, "value": 1, "duration_sec": 300, "reason": "灌水"}
  ],
  "co2_advisory": "換気中、自然値",
  "dewpoint_risk": "low",
  "next_check_note": "16時に日射低下の可能性"
}
```
"""


def _make_text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _make_tool_use_block(
    tool_id: str, name: str, input_data: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=tool_id, name=name, input=input_data or {}
    )


def _make_response(
    content: list, stop_reason: str = "end_turn"
) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """テスト用最小設定を返す。"""
    prompt_path = tmp_path / "system_prompt.txt"
    prompt_path.write_text("あなたは温室制御AIです。", encoding="utf-8")

    return {
        "claude": {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "max_tool_rounds": 5,
            "api_timeout_sec": 30.0,
        },
        "system_prompt_path": str(prompt_path),
        "db": {
            "path": str(tmp_path / "control_log.db"),
            "history_count": 3,
        },
        "state": {
            "plan_path": str(tmp_path / "current_plan.json"),
            "last_decision_path": str(tmp_path / "last_decision.json"),
            "lockout_path": str(tmp_path / "lockout_state.json"),
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


def _mock_anthropic_normal() -> MagicMock:
    """正常フロー: get_sensors → get_status → テキスト応答 の3ラウンド。"""
    mock = MagicMock()

    responses = [
        # Round 0: tool_use (get_sensors)
        _make_response(
            [_make_tool_use_block("call_1", "get_sensors")],
            stop_reason="tool_use",
        ),
        # Round 1: tool_use (get_status)
        _make_response(
            [_make_tool_use_block("call_2", "get_status")],
            stop_reason="tool_use",
        ),
        # Round 2: text response (計画 JSON)
        _make_response(
            [_make_text_block(PLAN_TEXT)],
            stop_reason="end_turn",
        ),
    ]

    mock.messages.create.side_effect = responses
    return mock


def _mock_http_client() -> MagicMock:
    """unipi-daemon REST API モック。"""
    mock = MagicMock(spec=["get", "close"])

    def _get(url: str, **kwargs):
        resp = MagicMock()
        if "/api/sensors" in url:
            resp.text = SENSOR_JSON
            resp.json.return_value = json.loads(SENSOR_JSON)
        elif "/api/status" in url:
            resp.text = STATUS_JSON
            resp.json.return_value = json.loads(STATUS_JSON)
        resp.raise_for_status = MagicMock()
        return resp

    mock.get.side_effect = _get
    return mock


# ---------------------------------------------------------------------------
# Test 1: lockout中 → 計画生成スキップ（殿裁定 MAJOR-2）
# ---------------------------------------------------------------------------

def test_layer1_lockout_skips_forecast(tmp_path):
    """Layer 1 lockout中は計画生成をスキップする。"""
    cfg = _base_config(tmp_path)

    lockout_path = Path(cfg["state"]["lockout_path"])
    lockout_until = (datetime.now(_JST) + timedelta(minutes=5)).isoformat()
    lockout_path.write_text(json.dumps({
        "layer1_lockout_until": lockout_until,
        "last_action": "emergency_open",
    }))

    mock_client = MagicMock()
    result = run_forecast(
        cfg,
        anthropic_client=mock_client,
        http_client=_mock_http_client(),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "layer1_lockout"
    mock_client.messages.create.assert_not_called()
    assert not Path(cfg["state"]["plan_path"]).exists()


# ---------------------------------------------------------------------------
# Test 2: CommandGate lockout中 → 計画生成スキップ
# ---------------------------------------------------------------------------

def test_commandgate_lockout_skips_forecast(tmp_path):
    """CommandGate lockout中は計画生成をスキップする。"""
    cfg = _base_config(tmp_path)

    http_mock = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {"locked_out": True}
    http_mock.get.return_value = status_resp
    http_mock.close = MagicMock()

    mock_client = MagicMock()
    result = run_forecast(
        cfg, anthropic_client=mock_client, http_client=http_mock,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "commandgate_lockout"
    mock_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: 正常実行 → current_plan.json 生成
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_normal_flow_generates_plan(mock_sun, tmp_path):
    """正常フローで current_plan.json が生成される。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    result = run_forecast(
        cfg,
        anthropic_client=_mock_anthropic_normal(),
        http_client=_mock_http_client(),
    )

    assert result["status"] == "ok"
    plan_path = Path(cfg["state"]["plan_path"])
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text())
    assert "actions" in plan
    assert len(plan["actions"]) == 2
    assert plan["actions"][0]["relay_ch"] == 5
    assert plan["actions"][1]["relay_ch"] == 4


# ---------------------------------------------------------------------------
# Test 4: API タイムアウト → フェイルセーフ（plan未生成）
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_api_timeout_no_plan(mock_sun, tmp_path):
    """API タイムアウト時は error を返し plan 未生成。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = TimeoutError("API timeout")

    result = run_forecast(
        cfg,
        anthropic_client=mock_client,
        http_client=_mock_http_client(),
    )

    assert result["status"] == "error"
    assert "timeout" in result["reason"].lower() or "api_error" in result["reason"].lower()
    assert not Path(cfg["state"]["plan_path"]).exists()


# ---------------------------------------------------------------------------
# Test 5: API エラー → フェイルセーフ
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_api_error_failsafe(mock_sun, tmp_path):
    """API エラー時は error を返す。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("Authentication failed")

    result = run_forecast(
        cfg,
        anthropic_client=mock_client,
        http_client=_mock_http_client(),
    )

    assert result["status"] == "error"
    assert "api_error" in result["reason"]


# ---------------------------------------------------------------------------
# Test 6: tool calling ループ正常 (get_sensors → get_status → 判断)
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_tool_calling_loop(mock_sun, tmp_path):
    """tool calling が3ラウンドで正常完了する。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    mock_anthropic = _mock_anthropic_normal()

    run_forecast(
        cfg,
        anthropic_client=mock_anthropic,
        http_client=_mock_http_client(),
    )

    assert mock_anthropic.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# Test 7: LLM出力バリデーション → relay_ch 範囲外スキップ
# ---------------------------------------------------------------------------

def test_validate_actions_relay_ch_out_of_range():
    """relay_ch が [1,8] 範囲外のアクションはスキップされる。"""
    actions = [
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 0, "value": 1, "duration_sec": 30},
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 9, "value": 1, "duration_sec": 30},
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 5, "value": 1, "duration_sec": 30},
    ]
    result = validate_actions(actions)
    assert len(result) == 1
    assert result[0]["relay_ch"] == 5


# ---------------------------------------------------------------------------
# Test 8: LLM出力バリデーション → duration_sec 超過切り詰め
# ---------------------------------------------------------------------------

def test_validate_actions_duration_clamped():
    """duration_sec > 3600 は 3600 に切り詰められる。"""
    actions = [
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 4, "value": 1, "duration_sec": 7200},
    ]
    result = validate_actions(actions)
    assert len(result) == 1
    assert result[0]["duration_sec"] == 3600


# ---------------------------------------------------------------------------
# Test 9: 判断ログ DB 書き込み確認
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_decision_log_saved(mock_sun, tmp_path):
    """正常フローで control_log.db に判断ログが保存される。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    run_forecast(
        cfg,
        anthropic_client=_mock_anthropic_normal(),
        http_client=_mock_http_client(),
    )

    import sqlite3
    db = sqlite3.connect(cfg["db"]["path"])
    rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()
    assert rows[0] >= 1
    row = db.execute(
        "SELECT summary, actions_taken FROM decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is not None
    assert row[1] is not None
    db.close()


# ---------------------------------------------------------------------------
# Test 10: system_prompt.txt + 履歴注入確認
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_system_prompt_and_history_injected(mock_sun, tmp_path):
    """system_prompt.txt と判断履歴がAPIリクエストに含まれる。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    prompt_path = Path(cfg["system_prompt_path"])
    prompt_path.write_text("テスト用プロンプト [A] [B] [C]", encoding="utf-8")

    # 事前に判断履歴を入れておく
    db = init_db(cfg["db"]["path"])
    save_decision(db, "前回:高温対応", "ch5=ON", "", "")
    db.close()

    mock_anthropic = _mock_anthropic_normal()
    run_forecast(
        cfg,
        anthropic_client=mock_anthropic,
        http_client=_mock_http_client(),
    )

    # 最初の messages.create 呼び出しの引数を検証
    call_kwargs = mock_anthropic.messages.create.call_args_list[0]
    assert "テスト用プロンプト" in call_kwargs.kwargs["system"]
    user_msg = call_kwargs.kwargs["messages"][0]["content"]
    assert "前回:高温対応" in user_msg
    assert "判断履歴" in user_msg


# ---------------------------------------------------------------------------
# Test 11: lockout_state.json が存在しない → lockout なし
# ---------------------------------------------------------------------------

def test_no_lockout_file_means_no_lockout(tmp_path):
    """lockout_state.json がない場合はロックアウトなし。"""
    assert not is_layer1_locked(tmp_path / "nonexistent.json")


# ---------------------------------------------------------------------------
# Test 12: validate_actions — value 不正
# ---------------------------------------------------------------------------

def test_validate_actions_invalid_value():
    """value が 0,1 以外のアクションはスキップされる。"""
    actions = [
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 5, "value": 2, "duration_sec": 30},
        {"execute_at": "2026-03-01T15:00:00+09:00", "relay_ch": 5, "value": -1, "duration_sec": 30},
    ]
    result = validate_actions(actions)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 13: validate_actions — execute_at 不正
# ---------------------------------------------------------------------------

def test_validate_actions_invalid_execute_at():
    """execute_at が ISO8601 でない場合はスキップされる。"""
    actions = [
        {"execute_at": "not-a-date", "relay_ch": 5, "value": 1, "duration_sec": 30},
        {"execute_at": "", "relay_ch": 5, "value": 1, "duration_sec": 30},
    ]
    result = validate_actions(actions)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 14: extract_plan_json — コードブロック内JSON
# ---------------------------------------------------------------------------

def test_extract_plan_json_from_code_block():
    """```json ... ``` ブロックからJSONを正しく抽出する。"""
    text = 'テキスト\n```json\n{"actions": [{"relay_ch": 5}]}\n```\n後続テキスト'
    result = extract_plan_json(text)
    assert result is not None
    assert "actions" in result


# ---------------------------------------------------------------------------
# Test 15: extract_plan_json — 生JSON
# ---------------------------------------------------------------------------

def test_extract_plan_json_raw():
    """生のJSONテキストからも抽出できる。"""
    text = '{"actions": [], "summary": "test"}'
    result = extract_plan_json(text)
    assert result is not None
    assert result["summary"] == "test"


# ---------------------------------------------------------------------------
# Test 16: TOOLS にset_relay が含まれていないこと
# ---------------------------------------------------------------------------

def test_tools_no_set_relay():
    """TOOLS定義にset_relayが含まれていない。"""
    tool_names = [t["name"] for t in TOOLS]
    assert "set_relay" not in tool_names
    assert "get_sensors" in tool_names
    assert "get_status" in tool_names


# ---------------------------------------------------------------------------
# Test 17: call_tool — unknown tool
# ---------------------------------------------------------------------------

def test_call_tool_unknown():
    """未知のツール名はエラーJSONを返す。"""
    mock = MagicMock()
    result = call_tool(mock, "http://localhost:8080", "", "set_relay", {"ch": 1})
    data = json.loads(result)
    assert "error" in data
    assert "unknown tool" in data["error"]


# ---------------------------------------------------------------------------
# Test 18: last_decision.json が更新される
# ---------------------------------------------------------------------------

@patch("v2_control.forecast_engine.get_sun_times")
def test_last_decision_updated(mock_sun, tmp_path):
    """正常フローで last_decision.json が更新される。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    run_forecast(
        cfg,
        anthropic_client=_mock_anthropic_normal(),
        http_client=_mock_http_client(),
    )

    last_path = Path(cfg["state"]["last_decision_path"])
    assert last_path.exists()
    data = json.loads(last_path.read_text())
    assert "timestamp" in data
    assert "summary" in data
    assert "actions_count" in data
