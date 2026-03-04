"""Layer 3 forecast_engine.py テスト

設計書: docs/v2_three_layer_design.md §7.3
テスト方針:
  - anthropic.Anthropic() をモック（API 呼び出しなし）
  - httpx.Client をモック（REST API 呼び出しなし）
  - SQLite は :memory: で動作
"""

from __future__ import annotations

import json
import os
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agriha.control.forecast_engine import (
    TOOLS,
    VC_CACHE_TTL,
    build_search_query,
    call_tool,
    check_connectivity,
    extract_plan_json,
    fetch_weather_forecast,
    get_weather_with_cache,
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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

@patch("agriha.control.forecast_engine.get_sun_times")
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


# ---------------------------------------------------------------------------
# Test 19: fetch_weather_forecast — VC_API_KEY 未設定 → None
# ---------------------------------------------------------------------------

def test_fetch_weather_forecast_no_key():
    """VC_API_KEY未設定時はNoneを返す。"""
    with patch.dict(os.environ, {"VC_API_KEY": ""}):
        result = fetch_weather_forecast()
    assert result is None


# ---------------------------------------------------------------------------
# Test 20: fetch_weather_forecast — 正常取得
# ---------------------------------------------------------------------------

def test_fetch_weather_forecast_success():
    """正常取得時にAPIレスポンスdictを返す。"""
    fake_response = {"days": [{"hours": [{"temp": 10.0}]}]}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with (
        patch.dict(os.environ, {"VC_API_KEY": "dummy-key"}),
        patch("urllib.request.urlopen", return_value=mock_resp),
    ):
        result = fetch_weather_forecast()
    assert result is not None
    assert "days" in result


# ---------------------------------------------------------------------------
# Test 21: fetch_weather_forecast — HTTP 401 エラー → None
# ---------------------------------------------------------------------------

def test_fetch_weather_forecast_http_error():
    """HTTP エラー時はNoneを返す。"""
    http_err = urllib.error.HTTPError(
        url="", code=401, msg="Unauthorized", hdrs=None, fp=None
    )
    with (
        patch.dict(os.environ, {"VC_API_KEY": "bad-key"}),
        patch("urllib.request.urlopen", side_effect=http_err),
    ):
        result = fetch_weather_forecast()
    assert result is None


# ---------------------------------------------------------------------------
# Test 22: get_weather_with_cache — TTL内キャッシュ命中
# ---------------------------------------------------------------------------

def test_get_weather_with_cache_hit(tmp_path: Path) -> None:
    """TTL内のキャッシュがある場合、API呼び出しなしでキャッシュを返す。"""
    cached_data = {
        "days": [{"temp": 15.0}],
        "_cached_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_file = tmp_path / "vc_cache.json"
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")
    with (
        patch("agriha.control.forecast_engine.VC_CACHE_PATH", str(cache_file)),
        patch(
            "agriha.control.forecast_engine.fetch_weather_forecast"
        ) as mock_fetch,
    ):
        result = get_weather_with_cache()
    mock_fetch.assert_not_called()
    assert result is not None
    assert "days" in result


# ---------------------------------------------------------------------------
# Test 23: get_weather_with_cache — TTL切れ → API再取得
# ---------------------------------------------------------------------------

def test_get_weather_with_cache_expired(tmp_path: Path) -> None:
    """TTL切れキャッシュがある場合はAPIを再呼び出しする。"""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cached_data = {"days": [{"temp": 5.0}], "_cached_at": old_time}
    cache_file = tmp_path / "vc_cache.json"
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")
    new_data = {"days": [{"temp": 20.0}]}
    with (
        patch("agriha.control.forecast_engine.VC_CACHE_PATH", str(cache_file)),
        patch(
            "agriha.control.forecast_engine.fetch_weather_forecast",
            return_value=new_data,
        ) as mock_fetch,
    ):
        result = get_weather_with_cache()
    mock_fetch.assert_called_once()
    assert result is not None
    assert result["days"][0]["temp"] == 20.0


# ---------------------------------------------------------------------------
# Test 24: get_weather_with_cache — API失敗 + 古いキャッシュ → フォールバック
# ---------------------------------------------------------------------------

def test_get_weather_with_cache_api_fail_stale(tmp_path: Path) -> None:
    """API失敗時は古いキャッシュにフォールバックする。"""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    stale_data = {"days": [{"temp": 3.0}], "_cached_at": old_time}
    cache_file = tmp_path / "vc_cache.json"
    cache_file.write_text(json.dumps(stale_data), encoding="utf-8")
    with (
        patch("agriha.control.forecast_engine.VC_CACHE_PATH", str(cache_file)),
        patch(
            "agriha.control.forecast_engine.fetch_weather_forecast",
            return_value=None,
        ),
    ):
        result = get_weather_with_cache()
    assert result is not None
    assert result["days"][0]["temp"] == 3.0


# ---------------------------------------------------------------------------
# Test 25: get_weather_with_cache — キャッシュなし + API失敗 → None
# ---------------------------------------------------------------------------

def test_get_weather_with_cache_no_cache_no_api(tmp_path: Path) -> None:
    """キャッシュなし・API失敗の場合はNoneを返す。"""
    cache_file = tmp_path / "vc_cache.json"  # 存在しない
    with (
        patch("agriha.control.forecast_engine.VC_CACHE_PATH", str(cache_file)),
        patch(
            "agriha.control.forecast_engine.fetch_weather_forecast",
            return_value=None,
        ),
    ):
        result = get_weather_with_cache()
    assert result is None


# ---------------------------------------------------------------------------
# Test 26: build_search_query — 冬/夜/寒い/不明
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.datetime")
def test_build_search_query_winter_night_cold(mock_dt: MagicMock) -> None:
    """冬・夜・氷点下のクエリを正しく生成する。"""
    mock_dt.now.return_value = datetime(2026, 1, 15, 2, 0, tzinfo=_JST)
    result = build_search_query({"misol": {"temperature_c": -2.0}}, None)
    assert result == "winter_night_cold_unknown"


# ---------------------------------------------------------------------------
# Test 27: build_search_query — 夏/午後/暑い/晴れ
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.datetime")
def test_build_search_query_summer_afternoon_hot_clear(mock_dt: MagicMock) -> None:
    """夏・午後・猛暑・晴れのクエリを正しく生成する。"""
    mock_dt.now.return_value = datetime(2026, 7, 20, 14, 0, tzinfo=_JST)
    weather = {"currentConditions": {"temp": 32.0, "conditions": "Clear"}}
    result = build_search_query({"misol": {"temperature_c": 32.0}}, weather)
    assert result == "summer_afternoon_hot_clear"


# ---------------------------------------------------------------------------
# Test 28: build_search_query — 温度バンド全パターン
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.datetime")
def test_build_search_query_temp_bands(mock_dt: MagicMock) -> None:
    """各温度バンドが正しくマッピングされる。"""
    mock_dt.now.return_value = datetime(2026, 4, 1, 10, 0, tzinfo=_JST)
    cases = [
        (-1.0, "cold"),
        (4.9, "cold"),
        (5.0, "cool"),
        (14.9, "cool"),
        (15.0, "warm"),
        (24.9, "warm"),
        (25.0, "hot"),
    ]
    for temp, expected_band in cases:
        result = build_search_query({"misol": {"temperature_c": temp}}, None)
        assert expected_band in result, (
            f"temp={temp} → expected {expected_band!r} in {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 29: build_search_query — 天気条件マッピング
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.datetime")
def test_build_search_query_weather_conditions(mock_dt: MagicMock) -> None:
    """天気条件キーワードが正しくマッピングされる。"""
    mock_dt.now.return_value = datetime(2026, 6, 1, 9, 0, tzinfo=_JST)
    cases = [
        ("Rain, Overcast", "rain"),
        ("Snow", "snow"),
        ("Cloudy", "cloudy"),
        ("Clear", "clear"),
        ("Sunny", "clear"),
        ("Fog", "unknown"),
    ]
    for conditions, expected_weather in cases:
        weather = {"currentConditions": {"temp": 15.0, "conditions": conditions}}
        result = build_search_query({"misol": {"temperature_c": 15.0}}, weather)
        assert result.endswith(expected_weather), (
            f"conditions={conditions!r} → expected {expected_weather!r} suffix in {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 30: build_search_query — weather_data なし → unknown
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.datetime")
def test_build_search_query_no_weather_data(mock_dt: MagicMock) -> None:
    """weather_data=None の場合、天気は unknown になる。"""
    mock_dt.now.return_value = datetime(2026, 3, 1, 8, 0, tzinfo=_JST)
    result = build_search_query({"misol": {"temperature_c": 10.0}}, None)
    assert result.endswith("_unknown")


# ---------------------------------------------------------------------------
# Test 31: check_connectivity — ping 成功
# ---------------------------------------------------------------------------

def test_check_connectivity_success() -> None:
    """ping が成功する場合 True を返す。"""
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        assert check_connectivity() is True


# ---------------------------------------------------------------------------
# Test 32: check_connectivity — ping 失敗
# ---------------------------------------------------------------------------

def test_check_connectivity_failure() -> None:
    """ping が失敗する場合 False を返す。"""
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        assert check_connectivity() is False


# ---------------------------------------------------------------------------
# Test 33: check_connectivity — 例外発生 → False
# ---------------------------------------------------------------------------

def test_check_connectivity_exception() -> None:
    """subprocess.run が例外を投げた場合 False を返す。"""
    with patch("subprocess.run", side_effect=OSError("command not found")):
        assert check_connectivity() is False
