"""
LINE Bot 判断履歴統合テスト (subtask_589 / cmd_265)

テスト対象:
  1. rpi_client.get_history() — httpx mock で正常系・エラー系
  2. system_prompt のコンテキスト注入フォーマット
  3. tools.py の control_history ディスパッチ
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# 1. rpi_client.get_history() テスト
# ---------------------------------------------------------------------------

class TestGetHistory:
    """rpi_client.get_history() の正常系・エラー系テスト。"""

    def test_success(self):
        """正常レスポンスでリストが返る。"""
        import rpi_client

        mock_history = [
            {
                "timestamp": "2026-02-23T14:32:15+09:00",
                "summary": "外気温低・風速高のため側窓全閉維持",
                "actions_taken": "set_relay(ch1,0)",
                "sensor_snapshot": "temp_in=22.5",
            },
            {
                "timestamp": "2026-02-23T14:27:10+09:00",
                "summary": "日射低下、灌水スキップ",
                "actions_taken": "no_action",
                "sensor_snapshot": "temp_in=22.1",
            },
        ]

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_history
        mock_resp.raise_for_status.return_value = None

        with patch.object(rpi_client.httpx, "Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = rpi_client.get_history(limit=5)

        assert len(result) == 2
        assert result[0]["summary"] == "外気温低・風速高のため側窓全閉維持"
        mock_client.get.assert_called_once()
        call_kwargs = mock_client.get.call_args
        assert "api/history" in call_kwargs[0][0]
        assert call_kwargs[1]["params"]["limit"] == 5

    def test_connect_error_returns_empty_list(self):
        """接続エラー時は空リストを返す。"""
        import rpi_client

        with patch.object(rpi_client.httpx, "Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("接続失敗")
            mock_client_cls.return_value = mock_client

            result = rpi_client.get_history()

        assert result == []

    def test_timeout_returns_empty_list(self):
        """タイムアウト時は空リストを返す。"""
        import rpi_client

        with patch.object(rpi_client.httpx, "Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.TimeoutException("timeout")
            mock_client_cls.return_value = mock_client

            result = rpi_client.get_history()

        assert result == []

    def test_http_error_returns_empty_list(self):
        """HTTP エラー時は空リストを返す。"""
        import rpi_client

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(rpi_client.httpx, "Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_resp
            )
            mock_client_cls.return_value = mock_client

            result = rpi_client.get_history()

        assert result == []

    def test_default_limit_is_10(self):
        """デフォルト limit は 10。"""
        import rpi_client

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None

        with patch.object(rpi_client.httpx, "Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            rpi_client.get_history()

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["limit"] == 10


# ---------------------------------------------------------------------------
# 2. system_prompt コンテキスト注入フォーマットテスト
# ---------------------------------------------------------------------------

class TestSystemPromptInjection:
    """get_system_prompt() のフォーマット・注入確認テスト。"""

    def _make_mock_sensors(self):
        return {
            "sensors": {
                "temp_in": 26.3,
                "humid_in": 56.0,
                "co2": 224.0,
                "temp_out": 4.6,
                "wind_speed": 4.9,
            },
            "relay": {"ch1": False, "ch2": False},
            "age_sec": 30.0,
        }

    def _make_mock_history(self):
        return [
            {
                "timestamp": "2026-02-23T14:32:15+09:00",
                "summary": "外気温低・風速高のため側窓全閉維持",
                "actions_taken": "set_relay(ch1,0)",
            },
            {
                "timestamp": "2026-02-23T14:27:10+09:00",
                "summary": "日射低下、灌水スキップ",
                "actions_taken": "no_action",
            },
        ]

    def test_datetime_injected(self):
        """日時情報がプロンプトに含まれる。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value=self._make_mock_sensors()),
            patch("rpi_client.get_history", return_value=self._make_mock_history()),
        ):
            prompt = system_prompt.get_system_prompt()

        assert "現在日時:" in prompt
        assert "JST" in prompt
        assert "日の出:" in prompt
        assert "日没:" in prompt
        assert "時間帯:" in prompt

    def test_sensor_data_injected(self):
        """センサーデータがプロンプトに含まれる。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value=self._make_mock_sensors()),
            patch("rpi_client.get_history", return_value=[]),
        ):
            prompt = system_prompt.get_system_prompt()

        assert "[現在のセンサーデータ]" in prompt
        assert "26.3℃" in prompt
        assert "56%" in prompt
        assert "224ppm" in prompt

    def test_history_injected(self):
        """制御判断履歴がプロンプトに含まれる。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value={"sensors": {}, "relay": {}}),
            patch("rpi_client.get_history", return_value=self._make_mock_history()),
        ):
            prompt = system_prompt.get_system_prompt()

        assert "[直近の制御判断]" in prompt
        assert "側窓全閉維持" in prompt
        assert "灌水スキップ" in prompt

    def test_sensor_error_graceful(self):
        """センサー取得エラー時もプロンプト生成が成功する。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value={"error": "connection_failed", "message": "接続不可"}),
            patch("rpi_client.get_history", return_value=[]),
        ):
            prompt = system_prompt.get_system_prompt()

        assert "[現在のセンサーデータ]" in prompt
        assert "取得失敗" in prompt
        # 本文は含まれる
        assert "[A] 役割定義" in prompt

    def test_history_empty_graceful(self):
        """履歴が空の場合もプロンプト生成が成功する。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value={"sensors": {}, "relay": {}}),
            patch("rpi_client.get_history", return_value=[]),
        ):
            prompt = system_prompt.get_system_prompt()

        assert "[直近の制御判断]" in prompt
        assert "履歴なし" in prompt

    def test_include_sensors_false(self):
        """include_sensors=False でセンサーデータが注入されない。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors") as mock_sensors,
            patch("rpi_client.get_history", return_value=[]),
        ):
            prompt = system_prompt.get_system_prompt(include_sensors=False)
            mock_sensors.assert_not_called()

        assert "[現在のセンサーデータ]" not in prompt

    def test_include_history_false(self):
        """include_history=False で履歴が注入されない。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value={"sensors": {}, "relay": {}}),
            patch("rpi_client.get_history") as mock_hist,
        ):
            prompt = system_prompt.get_system_prompt(include_history=False)
            mock_hist.assert_not_called()

        assert "[直近の制御判断]" not in prompt

    def test_prompt_body_contains_sections(self):
        """プロンプト本文に [A]-[G] セクションが含まれる。"""
        import system_prompt

        with (
            patch("rpi_client.get_sensors", return_value={"sensors": {}, "relay": {}}),
            patch("rpi_client.get_history", return_value=[]),
        ):
            prompt = system_prompt.get_system_prompt()

        for section in ["[A]", "[B]", "[C]", "[D]", "[E]", "[F]", "[G]"]:
            assert section in prompt, f"{section} がプロンプトに見つからない"


# ---------------------------------------------------------------------------
# 3. tools.py control_history ディスパッチテスト
# ---------------------------------------------------------------------------

class TestControlHistoryTool:
    """tools.execute_tool_call() の control_history ディスパッチテスト。"""

    def _make_tool_call(self, args: dict) -> dict:
        return {"function": {"name": "control_history", "arguments": args}}

    def test_dispatch_success(self):
        """control_history ツールが正しくディスパッチされ、結果を返す。"""
        import tools

        mock_history = [
            {
                "timestamp": "2026-02-23T14:32:15+09:00",
                "summary": "側窓全閉維持",
                "actions_taken": "set_relay(ch1,0)",
            }
        ]

        with patch("tools.get_history", return_value=mock_history):
            result_str = tools.execute_tool_call(self._make_tool_call({"hours": 6}))

        result = json.loads(result_str)
        assert "history" in result
        assert result["count"] == 1
        assert result["history"][0]["summary"] == "側窓全閉維持"

    def test_dispatch_default_hours(self):
        """hours 省略時はデフォルト(24h) で呼ばれる。"""
        import tools

        with patch("tools.get_history", return_value=[]) as mock_hist:
            tools.execute_tool_call(self._make_tool_call({}))
            # limit = 24 * 12 = 288 → min(288, 200) = 200
            mock_hist.assert_called_once_with(limit=200)

    def test_dispatch_hours_1(self):
        """hours=1 のとき limit = min(12, 200) = 12。"""
        import tools

        with patch("tools.get_history", return_value=[]) as mock_hist:
            tools.execute_tool_call(self._make_tool_call({"hours": 1}))
            mock_hist.assert_called_once_with(limit=12)

    def test_dispatch_empty_history(self):
        """履歴が空でも正常に JSON を返す。"""
        import tools

        with patch("tools.get_history", return_value=[]):
            result_str = tools.execute_tool_call(self._make_tool_call({}))

        result = json.loads(result_str)
        assert result["history"] == []
        assert result["count"] == 0
