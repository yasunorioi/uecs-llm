"""agriha_control.py のユニットテスト。

llama-server (OpenAI互換API) と unipi-daemon REST API をモックし、
tool calling ループのロジックをテストする。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agriha_control import (
    TOOLS,
    call_tool,
    get_sun_times,
    get_time_period,
    init_db,
    llm_chat,
    load_recent_history,
    run_control_loop,
    save_decision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """テスト用 DB を tmp_path に作成。"""
    return init_db(tmp_path / "test_control_log.db")


@pytest.fixture
def system_prompt_file(tmp_path: Path) -> Path:
    """テスト用システムプロンプトファイル。"""
    p = tmp_path / "system_prompt.txt"
    p.write_text("あなたは温室制御AIです。テスト用。", encoding="utf-8")
    return p


@pytest.fixture
def mock_http_client() -> MagicMock:
    """モック HTTP クライアント（unipi-daemon REST API 用）。"""
    client = MagicMock()

    def _make_get_response(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "/api/sensors" in url:
            resp.text = json.dumps({
                "sensors": {
                    "agriha/h01/ccm/sensor/InAirTemp": {"value": 25.5},
                    "agriha/h01/ccm/sensor/InAirHumid": {"value": 80.0},
                    "agriha/farm/weather/misol": {
                        "temperature": 18.0,
                        "wind_speed": 2.5,
                        "rainfall": 0.0,
                    },
                },
                "updated_at": 1740000000.0,
                "age_sec": 5.0,
            })
        elif "/api/status" in url:
            resp.text = json.dumps({
                "house_id": "h01",
                "uptime_sec": 3600,
                "locked_out": False,
                "lockout_remaining_sec": 0.0,
                "relay_state": {
                    "ch1": False, "ch2": False, "ch3": False, "ch4": False,
                    "ch5": False, "ch6": False, "ch7": False, "ch8": False,
                },
                "ts": 1740000000.0,
            })
        return resp

    def _make_post_response(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = json.dumps({"ch": 4, "value": 1, "queued": True})
        return resp

    client.get = MagicMock(side_effect=_make_get_response)
    client.post = MagicMock(side_effect=_make_post_response)
    client.close = MagicMock()
    return client


def _make_llm_response(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """llama-server OpenAI互換レスポンスを構築するヘルパー。"""
    message: dict[str, Any] = {}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [
            {
                "message": message,
                "finish_reason": "stop" if tool_calls is None else "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _make_llm_client(responses: list[dict[str, Any]]) -> MagicMock:
    """llama-server 用モック HTTP クライアントを作成。"""
    client = MagicMock()
    call_count = 0

    def _post(url: str, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if call_count < len(responses):
            resp.json = MagicMock(return_value=responses[call_count])
        else:
            resp.json = MagicMock(return_value=_make_llm_response(content="（応答なし）"))
        call_count += 1
        return resp

    client.post = MagicMock(side_effect=_post)
    client.close = MagicMock()
    return client


# ---------------------------------------------------------------------------
# DB テスト
# ---------------------------------------------------------------------------

class TestDB:
    def test_init_db_creates_table(self, tmp_path: Path) -> None:
        db = init_db(tmp_path / "test.db")
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert ("decisions",) in tables
        db.close()

    def test_init_db_creates_parent_dirs(self, tmp_path: Path) -> None:
        db = init_db(tmp_path / "sub" / "dir" / "test.db")
        assert (tmp_path / "sub" / "dir" / "test.db").exists()
        db.close()

    def test_save_and_load_history(self, tmp_db: sqlite3.Connection) -> None:
        save_decision(tmp_db, "温度OK", "現状維持", "{}", "temp=25")
        save_decision(tmp_db, "灌水実行", "relay ch4=ON", "{}", "temp=30")

        history = load_recent_history(tmp_db, n=3)
        assert "温度OK" in history
        assert "灌水実行" in history

    def test_load_empty_history(self, tmp_db: sqlite3.Connection) -> None:
        history = load_recent_history(tmp_db, n=3)
        assert "初回起動" in history

    def test_history_order(self, tmp_db: sqlite3.Connection) -> None:
        """履歴は古い順に表示される。"""
        save_decision(tmp_db, "first", "a", "{}", "")
        save_decision(tmp_db, "second", "b", "{}", "")
        save_decision(tmp_db, "third", "c", "{}", "")

        history = load_recent_history(tmp_db, n=3)
        lines = history.strip().split("\n")
        assert "first" in lines[0]
        assert "third" in lines[2]

    def test_history_limit(self, tmp_db: sqlite3.Connection) -> None:
        """n=2 なら直近2件のみ。"""
        for i in range(5):
            save_decision(tmp_db, f"decision_{i}", f"action_{i}", "{}", "")

        history = load_recent_history(tmp_db, n=2)
        assert "decision_3" in history
        assert "decision_4" in history
        assert "decision_0" not in history


# ---------------------------------------------------------------------------
# call_tool テスト
# ---------------------------------------------------------------------------

class TestCallTool:
    def test_get_sensors(self, mock_http_client: MagicMock) -> None:
        result = call_tool(
            mock_http_client, "http://test:8080", "", "get_sensors", {}
        )
        data = json.loads(result)
        assert "sensors" in data

    def test_get_status(self, mock_http_client: MagicMock) -> None:
        result = call_tool(
            mock_http_client, "http://test:8080", "", "get_status", {}
        )
        data = json.loads(result)
        assert data["locked_out"] is False

    def test_set_relay(self, mock_http_client: MagicMock) -> None:
        result = call_tool(
            mock_http_client,
            "http://test:8080",
            "",
            "set_relay",
            {"ch": 4, "value": 1, "duration_sec": 300, "reason": "灌水"},
        )
        data = json.loads(result)
        assert data["queued"] is True
        # POST が正しい URL で呼ばれたか
        mock_http_client.post.assert_called_once()
        call_args = mock_http_client.post.call_args
        assert "/api/relay/4" in call_args[0][0]

    def test_set_relay_default_reason(self, mock_http_client: MagicMock) -> None:
        call_tool(
            mock_http_client,
            "http://test:8080",
            "",
            "set_relay",
            {"ch": 5, "value": 1},
        )
        call_args = mock_http_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["reason"] == "LLM auto"

    def test_api_key_header(self, mock_http_client: MagicMock) -> None:
        call_tool(
            mock_http_client, "http://test:8080", "secret123", "get_sensors", {}
        )
        call_args = mock_http_client.get.call_args
        assert call_args[1]["headers"]["X-API-Key"] == "secret123"

    def test_unknown_tool(self, mock_http_client: MagicMock) -> None:
        result = call_tool(
            mock_http_client, "http://test:8080", "", "unknown_tool", {}
        )
        data = json.loads(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# llm_chat テスト
# ---------------------------------------------------------------------------

class TestLlmChat:
    def test_text_response(self) -> None:
        """テキストのみの応答。"""
        llm_client = _make_llm_client([
            _make_llm_response(content="現状維持です。"),
        ])
        msg = llm_chat(llm_client, "http://test:8081", [], [])
        assert msg["content"] == "現状維持です。"
        assert msg["tool_calls"] is None

    def test_tool_call_response(self) -> None:
        """tool_calls を含む応答。"""
        llm_client = _make_llm_client([
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "get_sensors",
                        "arguments": "{}",
                    },
                }
            ]),
        ])
        msg = llm_chat(llm_client, "http://test:8081", [], TOOLS)
        assert msg["tool_calls"] is not None
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "get_sensors"
        assert msg["tool_calls"][0]["function"]["arguments"] == {}

    def test_tool_call_args_as_dict(self) -> None:
        """arguments が dict の場合もパースできる。"""
        llm_client = _make_llm_client([
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "set_relay",
                        "arguments": {"ch": 4, "value": 1},
                    },
                }
            ]),
        ])
        msg = llm_chat(llm_client, "http://test:8081", [], TOOLS)
        assert msg["tool_calls"][0]["function"]["arguments"] == {"ch": 4, "value": 1}

    def test_tool_call_args_as_json_string(self) -> None:
        """arguments が JSON 文字列の場合もパースできる。"""
        llm_client = _make_llm_client([
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "set_relay",
                        "arguments": '{"ch": 5, "value": 0}',
                    },
                }
            ]),
        ])
        msg = llm_chat(llm_client, "http://test:8081", [], TOOLS)
        assert msg["tool_calls"][0]["function"]["arguments"] == {"ch": 5, "value": 0}


# ---------------------------------------------------------------------------
# ツール定義テスト
# ---------------------------------------------------------------------------

class TestToolDefs:
    def test_tool_count(self) -> None:
        assert len(TOOLS) == 3

    def test_tool_names(self) -> None:
        names = {t["function"]["name"] for t in TOOLS}
        assert names == {"get_sensors", "get_status", "set_relay"}

    def test_set_relay_required_params(self) -> None:
        set_relay = next(
            t for t in TOOLS if t["function"]["name"] == "set_relay"
        )
        required = set_relay["function"]["parameters"]["required"]
        assert "ch" in required
        assert "value" in required


# ---------------------------------------------------------------------------
# run_control_loop テスト（統合）
# ---------------------------------------------------------------------------

class TestControlLoop:
    def test_no_action_loop(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """LLM が tool_calls なしで応答 → 現状維持。"""
        llm_client = _make_llm_client([
            _make_llm_response(content="現状維持。温度は適正範囲内です。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        assert result["actions"] == "現状維持"
        assert "現状維持" in result["summary"]

    def test_sensor_read_loop(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """LLM が get_sensors を呼んでから最終応答。"""
        llm_client = _make_llm_client([
            # Round 1: get_sensors を呼ぶ
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {
                        "name": "get_sensors",
                        "arguments": "{}",
                    },
                }
            ]),
            # Round 2: 最終応答
            _make_llm_response(content="内温25.5℃、湿度80%。現状維持。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        assert "現状維持" in result["actions"]
        assert "get_sensors" in result["sensor_snapshot"]

    def test_relay_control_loop(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """LLM が get_sensors → set_relay → 最終応答。"""
        llm_client = _make_llm_client([
            # Round 1: get_sensors
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "get_sensors", "arguments": "{}"},
                }
            ]),
            # Round 2: set_relay
            _make_llm_response(tool_calls=[
                {
                    "id": "call_2",
                    "function": {
                        "name": "set_relay",
                        "arguments": json.dumps({
                            "ch": 4, "value": 1,
                            "duration_sec": 300, "reason": "灌水5分",
                        }),
                    },
                }
            ]),
            # Round 3: 最終応答
            _make_llm_response(content="灌水を5分間実行しました。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        assert "relay ch4=ON" in result["actions"]
        mock_http_client.post.assert_called_once()

    def test_max_rounds_limit(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """max_tool_rounds で打ち切り。"""
        # 無限に get_sensors を呼び続ける LLM
        infinite_calls = [
            _make_llm_response(tool_calls=[
                {
                    "id": f"call_{i}",
                    "function": {"name": "get_sensors", "arguments": "{}"},
                }
            ])
            for i in range(10)
        ]

        llm_client = _make_llm_client(infinite_calls)

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
            "max_tool_rounds": 3,
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        # LLM は最大 3 回呼ばれる
        assert llm_client.post.call_count == 3

    def test_tool_call_error_handling(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
    ) -> None:
        """REST API エラー時もループが継続する。"""
        # HTTP client が例外を投げる
        error_client = MagicMock()
        error_client.get = MagicMock(side_effect=Exception("Connection refused"))
        error_client.close = MagicMock()

        llm_client = _make_llm_client([
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "get_sensors", "arguments": "{}"},
                }
            ]),
            _make_llm_response(content="センサー取得に失敗しました。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=error_client,
        )

        # エラーでも最終応答まで到達
        assert "センサー取得に失敗" in result["summary"]

    def test_missing_system_prompt(
        self,
        tmp_path: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """システムプロンプトが存在しない場合はデフォルトを使用。"""
        llm_client = _make_llm_client([
            _make_llm_response(content="現状維持。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(tmp_path / "nonexistent.txt"),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        # デフォルトプロンプトで動作
        assert result["actions"] == "現状維持"

    def test_db_persistence(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """制御ループ後に DB にレコードが保存される。"""
        llm_client = _make_llm_client([
            _make_llm_response(content="現状維持。"),
        ])

        db_path = tmp_path / "test.db"
        config = {
            "db_path": str(db_path),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        # DB を直接確認
        db = sqlite3.connect(str(db_path))
        rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()
        assert rows[0] == 1
        db.close()

    def test_multiple_tool_calls_in_one_round(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """1ラウンドで複数ツール呼び出し。"""
        llm_client = _make_llm_client([
            _make_llm_response(tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "get_sensors", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ]),
            _make_llm_response(content="全データ確認済み。現状維持。"),
        ])

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }

        result = run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
        )

        assert "get_sensors" in result["sensor_snapshot"]
        assert "get_status" in result["sensor_snapshot"]


# ---------------------------------------------------------------------------
# 日の出/日没ヘルパーテスト
# ---------------------------------------------------------------------------

class TestSunTimes:
    """get_sun_times / get_time_period の単体テスト。"""

    def _jst(self, year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    def test_get_sun_times_returns_datetimes(self) -> None:
        """日の出/日没が JST aware datetime として返る。"""
        now = self._jst(2026, 2, 23, 12)
        sunrise, sunset = get_sun_times(42.888, 141.603, 21, dt=now)
        assert isinstance(sunrise, datetime)
        assert isinstance(sunset, datetime)
        assert sunrise.tzinfo is not None
        assert sunset.tzinfo is not None

    def test_get_sun_times_order(self) -> None:
        """日の出 < 日没 である。"""
        now = self._jst(2026, 2, 23, 12)
        sunrise, sunset = get_sun_times(42.888, 141.603, 21, dt=now)
        assert sunrise < sunset

    def test_get_sun_times_reasonable_range(self) -> None:
        """北海道 2月の日の出/日没が常識的な範囲に収まる。"""
        now = self._jst(2026, 2, 23, 12)
        sunrise, sunset = get_sun_times(42.888, 141.603, 21, dt=now)
        # 2月北海道: 日の出 06:00-07:30、日没 17:00-18:30 が妥当な範囲
        assert 5 <= sunrise.hour <= 8
        assert 16 <= sunset.hour <= 19

    def test_get_time_period_before_sunrise(self) -> None:
        """日の出前 → "日の出前"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        now     = self._jst(2026, 2, 23, 4, 0)
        assert get_time_period(now, sunrise, sunset) == "日の出前"

    def test_get_time_period_daytime(self) -> None:
        """日中 → "日中（日の出後〜日没前1時間）"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        now     = self._jst(2026, 2, 23, 11, 35)
        assert get_time_period(now, sunrise, sunset) == "日中（日の出後〜日没前1時間）"

    def test_get_time_period_near_sunset(self) -> None:
        """日没前1時間 → "日没前1時間"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        now     = self._jst(2026, 2, 23, 16, 45)
        assert get_time_period(now, sunrise, sunset) == "日没前1時間"

    def test_get_time_period_after_sunset(self) -> None:
        """日没後 → "日没後"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        now     = self._jst(2026, 2, 23, 20, 0)
        assert get_time_period(now, sunrise, sunset) == "日没後"

    def test_get_time_period_exactly_at_sunrise(self) -> None:
        """日の出ちょうど → "日中"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        assert get_time_period(sunrise, sunrise, sunset) == "日中（日の出後〜日没前1時間）"

    def test_get_time_period_exactly_at_sunset(self) -> None:
        """日没ちょうど → "日没後"。"""
        sunrise = self._jst(2026, 2, 23, 6, 15)
        sunset  = self._jst(2026, 2, 23, 17, 23)
        assert get_time_period(sunset, sunrise, sunset) == "日没後"


# ---------------------------------------------------------------------------
# 日時注入統合テスト
# ---------------------------------------------------------------------------

class TestDatetimeInjection:
    """run_control_loop の user message への日時・日の出/日没注入を確認。"""

    def _jst(self, year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
        from zoneinfo import ZoneInfo
        return datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    def _run_and_capture_user_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
        now: datetime,
    ) -> str:
        """run_control_loop を実行し、最初の user message content を返す。"""
        captured: list[dict[str, Any]] = []

        def _post(url: str, **kwargs: Any) -> MagicMock:
            if not captured and "json" in kwargs:
                captured.extend(kwargs["json"].get("messages", []))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value=_make_llm_response(content="現状維持。"))
            return resp

        llm_client = MagicMock()
        llm_client.post = MagicMock(side_effect=_post)
        llm_client.close = MagicMock()

        config = {
            "db_path": str(tmp_path / "test.db"),
            "system_prompt_path": str(system_prompt_file),
            "unipi_api": "http://test:8080",
            "llama_server_url": "http://test:8081",
        }
        run_control_loop(
            config,
            llm_client=llm_client,
            http_client=mock_http_client,
            now=now,
        )
        user_msg = next(m for m in captured if m["role"] == "user")
        return user_msg["content"]

    def test_iso8601_datetime_in_user_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """ISO 8601 形式の現在日時が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 11, 35)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "2026-02-23T11:35:00" in content

    def test_sunrise_in_user_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """「日の出: HH:MM」が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 11, 35)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "日の出:" in content

    def test_sunset_in_user_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """「日没: HH:MM」が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 11, 35)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "日没:" in content

    def test_time_period_before_sunrise_in_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """深夜 → "日の出前" が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 3, 0)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "日の出前" in content

    def test_time_period_daytime_in_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """昼 → "日中" が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 11, 35)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "日中" in content

    def test_time_period_after_sunset_in_message(
        self,
        tmp_path: Path,
        system_prompt_file: Path,
        mock_http_client: MagicMock,
    ) -> None:
        """夜 → "日没後" が user message に含まれる。"""
        now = self._jst(2026, 2, 23, 20, 0)
        content = self._run_and_capture_user_message(
            tmp_path, system_prompt_file, mock_http_client, now
        )
        assert "日没後" in content
