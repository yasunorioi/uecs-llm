"""Layer 3 forecast_engine.py テスト

設計書: docs/v2_three_layer_design.md §7.3
テスト方針:
  - openai.OpenAI() をモック（API 呼び出しなし）
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
    build_plan_from_search_results,
    build_search_query,
    call_tool,
    check_connectivity,
    convert_llm_to_pid_override,
    extract_plan_json,
    fetch_weather_forecast,
    get_weather_with_cache,
    init_db,
    is_commandgate_locked,
    is_layer1_locked,
    load_recent_history,
    log_search,
    run_forecast,
    save_decision,
    search_kousatsu,
    should_skip_llm,
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


def _make_oai_tool_call(tool_id: str, name: str, arguments: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _make_oai_response(
    content: str | None = None,
    tool_calls: list | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    """OpenAI SDK互換レスポンスを生成する。"""
    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """テスト用最小設定を返す。"""
    prompt_path = tmp_path / "system_prompt.txt"
    prompt_path.write_text("あなたは温室制御AIです。", encoding="utf-8")

    return {
        "llm": {
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


def _mock_openai_normal() -> MagicMock:
    """正常フロー: get_sensors → get_status → テキスト応答 の3ラウンド。"""
    mock = MagicMock()

    responses = [
        # Round 0: tool_calls (get_sensors)
        _make_oai_response(tool_calls=[_make_oai_tool_call("call_1", "get_sensors")]),
        # Round 1: tool_calls (get_status)
        _make_oai_response(tool_calls=[_make_oai_tool_call("call_2", "get_status")]),
        # Round 2: text response (計画 JSON)
        _make_oai_response(content=PLAN_TEXT),
    ]

    mock.chat.completions.create.side_effect = responses
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
        llm_client=mock_client,
        http_client=_mock_http_client(),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "layer1_lockout"
    mock_client.chat.completions.create.assert_not_called()
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
        cfg, llm_client=mock_client, http_client=http_mock,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "commandgate_lockout"
    mock_client.chat.completions.create.assert_not_called()


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
        llm_client=_mock_openai_normal(),
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
    mock_client.chat.completions.create.side_effect = TimeoutError("API timeout")

    result = run_forecast(
        cfg,
        llm_client=mock_client,
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
    mock_client.chat.completions.create.side_effect = RuntimeError("Authentication failed")

    result = run_forecast(
        cfg,
        llm_client=mock_client,
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
    mock_openai = _mock_openai_normal()

    run_forecast(
        cfg,
        llm_client=mock_openai,
        http_client=_mock_http_client(),
    )

    assert mock_openai.chat.completions.create.call_count == 3


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
        llm_client=_mock_openai_normal(),
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

    mock_openai = _mock_openai_normal()
    run_forecast(
        cfg,
        llm_client=mock_openai,
        http_client=_mock_http_client(),
    )

    # 最初の chat.completions.create 呼び出しの引数を検証
    call_kwargs = mock_openai.chat.completions.create.call_args_list[0]
    # messages[0] が system プロンプト
    assert call_kwargs.kwargs["messages"][0]["role"] == "system"
    assert "テスト用プロンプト" in call_kwargs.kwargs["messages"][0]["content"]
    # messages[1] が user メッセージ（判断履歴を含む）
    user_msg = call_kwargs.kwargs["messages"][1]["content"]
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
    """TOOLS定義にset_relayが含まれていない（OpenAI tools形式）。"""
    tool_names = [t["function"]["name"] for t in TOOLS]
    assert "set_relay" not in tool_names
    assert "get_sensors" in tool_names
    assert "get_status" in tool_names
    # OpenAI形式: type="function" であること
    assert all(t["type"] == "function" for t in TOOLS)


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
        llm_client=_mock_openai_normal(),
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


# ---------------------------------------------------------------------------
# Test 34-38: search_kousatsu
# ---------------------------------------------------------------------------

def test_search_kousatsu_normal() -> None:
    """正常レスポンスを辞書として返す。"""
    resp_data = {"total_hits": 3, "results": [{"snippet": "test", "rank": 1}]}
    resp_bytes = json.dumps(resp_data).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = resp_bytes
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = search_kousatsu("側窓 温度 27℃")

    assert result["total_hits"] == 3
    assert len(result["results"]) == 1


def test_search_kousatsu_timeout() -> None:
    """タイムアウト時は total_hits=0 を返す。"""
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = search_kousatsu("query")
    assert result == {"total_hits": 0, "results": []}


def test_search_kousatsu_connection_error() -> None:
    """接続エラー時は total_hits=0 を返す。"""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        result = search_kousatsu("query")
    assert result == {"total_hits": 0, "results": []}


# ---------------------------------------------------------------------------
# Test 39-42: should_skip_llm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hits,threshold,expected", [
    (0, 3, False),
    (2, 3, False),
    (3, 3, True),
    (5, 3, True),
])
def test_should_skip_llm(hits: int, threshold: int, expected: bool) -> None:
    """閾値以上のヒット数でLLMをスキップする。"""
    result = should_skip_llm({"total_hits": hits, "results": []}, threshold=threshold)
    assert result is expected


# ---------------------------------------------------------------------------
# Test 43-44: build_plan_from_search_results
# ---------------------------------------------------------------------------

def test_build_plan_from_search_results_with_json() -> None:
    """snippetにJSONが含まれている場合、アクションを抽出する。"""
    now = datetime(2026, 3, 1, 10, 0, tzinfo=_JST)
    snippet_plan = json.dumps({
        "summary": "側窓開放が必要",
        "actions": [
            {
                "execute_at": "2026-03-01T10:30:00+09:00",
                "relay_ch": 5,
                "value": 1,
                "duration_sec": 60,
                "reason": "換気",
            }
        ],
    })
    search_results = {
        "total_hits": 3,
        "results": [{"snippet": snippet_plan, "rank": 1}],
    }
    plan = build_plan_from_search_results(search_results, now=now)
    assert plan["summary"] == "側窓開放が必要"
    assert len(plan["actions"]) == 1
    assert plan["actions"][0]["relay_ch"] == 5
    assert plan["next_check_note"] == "高札検索によりLLMスキップ"


def test_build_plan_from_search_results_no_json() -> None:
    """snippetにJSONがない場合、空のアクションを返す。"""
    now = datetime(2026, 3, 1, 10, 0, tzinfo=_JST)
    search_results = {
        "total_hits": 3,
        "results": [{"snippet": "過去に側窓を開けた", "rank": 1}],
    }
    plan = build_plan_from_search_results(search_results, now=now)
    assert plan["actions"] == []
    assert "generated_at" in plan
    assert "valid_until" in plan


# ---------------------------------------------------------------------------
# Test 45-46: convert_llm_to_pid_override
# ---------------------------------------------------------------------------

def test_convert_llm_to_pid_override_high_risk(tmp_path: Path) -> None:
    """dewpoint_risk=high → humidity_max=80 の pid_override.json を生成する。"""
    pid_path = tmp_path / "pid_override.json"
    plan = {"dewpoint_risk": "high", "summary": "test"}

    with patch("agriha.control.forecast_engine.PID_OVERRIDE_PATH", str(pid_path)):
        convert_llm_to_pid_override(plan)

    assert pid_path.exists()
    data = json.loads(pid_path.read_text())
    assert data["humidity_max"] == 80
    assert data["dewpoint_risk"] == "high"


def test_convert_llm_to_pid_override_low_risk(tmp_path: Path) -> None:
    """dewpoint_risk=low → humidity_max=90 の pid_override.json を生成する。"""
    pid_path = tmp_path / "pid_override.json"
    plan = {"dewpoint_risk": "low", "summary": "test"}

    with patch("agriha.control.forecast_engine.PID_OVERRIDE_PATH", str(pid_path)):
        convert_llm_to_pid_override(plan)

    data = json.loads(pid_path.read_text())
    assert data["humidity_max"] == 90


@pytest.mark.parametrize("co2_mode,expected_setpoint", [
    ("ventilate", 400),
    ("accumulate", 700),
    ("neutral", 550),
    ("unknown_mode", 550),  # デフォルト
    (None, 550),            # co2_modeキーなし
])
def test_convert_llm_to_pid_override_co2_mode(
    tmp_path: Path, co2_mode: str | None, expected_setpoint: int
) -> None:
    """co2_mode → co2_setpoint 変換が正しいことを確認する（§7準拠）。"""
    pid_path = tmp_path / "pid_override.json"
    plan: dict = {"dewpoint_risk": "low"}
    if co2_mode is not None:
        plan["co2_mode"] = co2_mode

    with patch("agriha.control.forecast_engine.PID_OVERRIDE_PATH", str(pid_path)):
        convert_llm_to_pid_override(plan)

    data = json.loads(pid_path.read_text())
    assert data["co2_setpoint"] == expected_setpoint


# ---------------------------------------------------------------------------
# Test 47: log_search — jsonl追記
# ---------------------------------------------------------------------------

def test_log_search_appends_jsonl(tmp_path: Path) -> None:
    """log_search が search_log.jsonl に1行追記する。"""
    log_path = tmp_path / "search_log.jsonl"

    with patch("agriha.control.forecast_engine.SEARCH_LOG_PATH", str(log_path)):
        log_search("側窓 温度", hits=3, skipped_llm=True, plan_source="kousatsu")
        log_search("灌水", hits=0, skipped_llm=False, plan_source="llm")

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    entry1 = json.loads(lines[0])
    assert entry1["query"] == "側窓 温度"
    assert entry1["hits"] == 3
    assert entry1["skipped_llm"] is True
    assert entry1["source"] == "kousatsu"
    entry2 = json.loads(lines[1])
    assert entry2["skipped_llm"] is False


# ---------------------------------------------------------------------------
# Test 48: run_forecast — 高札スキップパス (skip_llm=True)
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.get_sun_times")
@patch("agriha.control.forecast_engine.search_kousatsu")
def test_run_forecast_kousatsu_skip_path(
    mock_search: MagicMock,
    mock_sun: MagicMock,
    tmp_path: Path,
) -> None:
    """高札検索で3件以上ヒット → LLMをスキップしてplanを生成する。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    snippet_plan = json.dumps({
        "summary": "高札由来の計画",
        "actions": [
            {
                "execute_at": now.replace(minute=30).isoformat(),
                "relay_ch": 5,
                "value": 1,
                "duration_sec": 60,
                "reason": "高札ヒット",
            }
        ],
    })
    mock_search.return_value = {
        "total_hits": 5,
        "results": [{"snippet": snippet_plan, "rank": 1}],
    }

    mock_anthropic = MagicMock()
    cfg = _base_config(tmp_path)
    cfg["state"]["plan_path"] = str(tmp_path / "current_plan.json")

    with patch(
        "agriha.control.forecast_engine.SEARCH_LOG_PATH",
        str(tmp_path / "search_log.jsonl"),
    ), patch(
        "agriha.control.forecast_engine.PID_OVERRIDE_PATH",
        str(tmp_path / "pid_override.json"),
    ):
        result = run_forecast(
            cfg,
            anthropic_client=mock_anthropic,
            http_client=_mock_http_client(),
        )

    assert result["status"] == "ok"
    # LLMは呼ばれていないこと
    mock_anthropic.chat.completions.create.assert_not_called()
    # plan が書き込まれていること
    plan_path = Path(cfg["state"]["plan_path"])
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text())
    assert plan["summary"] == "高札由来の計画"
    assert plan["next_check_note"] == "高札検索によりLLMスキップ"
    # search_log が書き込まれていること
    log_path = tmp_path / "search_log.jsonl"
    assert log_path.exists()
    log_entry = json.loads(log_path.read_text().strip().splitlines()[0])
    assert log_entry["skipped_llm"] is True
    assert log_entry["source"] == "kousatsu"


# ---------------------------------------------------------------------------
# Test 49: llm config — llm.base_url / llm.api_key_env フィールドが設定に含まれる
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.get_sun_times")
def test_llm_config_has_base_url_and_api_key_env(mock_sun, tmp_path):
    """llm config に base_url / api_key_env を設定した場合も正常に動作する。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    cfg = _base_config(tmp_path)
    cfg["llm"]["base_url"] = "https://custom.llm.example.com/v1"
    cfg["llm"]["api_key_env"] = "CUSTOM_API_KEY"

    with (
        patch(
            "agriha.control.forecast_engine.SEARCH_LOG_PATH",
            str(tmp_path / "search_log.jsonl"),
        ),
        patch(
            "agriha.control.forecast_engine.PID_OVERRIDE_PATH",
            str(tmp_path / "pid_override.json"),
        ),
    ):
        result = run_forecast(
            cfg,
            llm_client=_mock_openai_normal(),
            http_client=_mock_http_client(),
        )

    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 50: backward compat — "claude" キーが "llm" にマージされる
# ---------------------------------------------------------------------------

@patch("agriha.control.forecast_engine.get_sun_times")
def test_backward_compat_claude_config_key(mock_sun, tmp_path):
    """後方互換性: "claude" キーが設定されても正常に動作する。"""
    now = datetime.now(_JST)
    mock_sun.return_value = {
        "sunrise": now.replace(hour=5, minute=30),
        "sunset": now.replace(hour=17, minute=30),
        "elevation": 21,
    }

    prompt_path = tmp_path / "system_prompt.txt"
    prompt_path.write_text("後方互換テスト", encoding="utf-8")

    cfg = {
        "claude": {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "max_tool_rounds": 3,
            "api_timeout_sec": 15.0,
        },
        "system_prompt_path": str(prompt_path),
        "db": {"path": str(tmp_path / "control_log.db"), "history_count": 3},
        "state": {
            "plan_path": str(tmp_path / "current_plan.json"),
            "last_decision_path": str(tmp_path / "last_decision.json"),
            "lockout_path": str(tmp_path / "lockout_state.json"),
        },
        "unipi_api": {"base_url": "http://localhost:8080", "api_key": "", "timeout_sec": 10},
        "location": {"latitude": 42.888, "longitude": 141.603, "elevation": 21},
    }

    with (
        patch(
            "agriha.control.forecast_engine.SEARCH_LOG_PATH",
            str(tmp_path / "search_log.jsonl"),
        ),
        patch(
            "agriha.control.forecast_engine.PID_OVERRIDE_PATH",
            str(tmp_path / "pid_override.json"),
        ),
    ):
        result = run_forecast(
            cfg,
            llm_client=_mock_openai_normal(),
            http_client=_mock_http_client(),
        )

    assert result["status"] == "ok"
