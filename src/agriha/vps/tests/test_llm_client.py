"""
llm_client.py のユニットテスト（Anthropic SDK版）

テスト対象:
  1. generate_response_sync() — 通常応答・ツール呼び出しループ
  2. generate_response() — 非同期ラッパー
  3. check_llm_health() — APIキー設定チェック
  4. _to_anthropic_tools() — ツール形式変換
  5. _execute_tool_call_anthropic() — ToolUseBlock実行
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest


# ---------------------------------------------------------------------------
# ヘルパー: Anthropic APIレスポンスモック生成
# ---------------------------------------------------------------------------


def _make_claude_response(text: str = "テスト応答", stop_reason: str = "end_turn"):
    """Anthropic Messages APIのテキストレスポンスモックを生成する。"""
    mock_response = MagicMock(spec=anthropic.types.Message)
    mock_response.stop_reason = stop_reason
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    mock_response.content = [text_block]
    return mock_response


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "tu_001"):
    """ツール呼び出しレスポンスのモックを生成する。

    tool_block は "text" 属性を持たない spec で生成する。
    これにより hasattr(block, "text") が False となり、実際のToolUseBlockと同じ振る舞いになる。
    """
    tool_block = MagicMock(spec=["type", "id", "name", "input"])
    tool_block.type = "tool_use"
    tool_block.id = tool_use_id
    tool_block.name = tool_name
    tool_block.input = tool_input

    mock_response = MagicMock(spec=anthropic.types.Message)
    mock_response.stop_reason = "tool_use"
    mock_response.content = [tool_block]
    return mock_response


def _patch_env_and_prompt():
    """ANTHROPIC_API_KEY パッチと get_system_prompt パッチをまとめて返す。"""
    return (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key-xxx"}),
        patch("llm_client.get_system_prompt", return_value="テスト用システムプロンプト"),
    )


# ---------------------------------------------------------------------------
# 1. generate_response_sync() テスト
# ---------------------------------------------------------------------------


class TestGenerateResponseSync:
    """generate_response_sync() の同期版テスト。"""

    def test_simple_text_response(self):
        """通常テキスト応答が返る。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response("こんにちは！今日も元気です。")

            result = llm_client.generate_response_sync("今日の状態は？")

        assert result == "こんにちは！今日も元気です。"

    def test_anthropic_client_initialized_with_api_key(self):
        """Anthropic クライアントが ANTHROPIC_API_KEY で初期化される。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test123"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response("OK")

            llm_client.generate_response_sync("テスト")

        mock_cls.assert_called_once_with(api_key="sk-ant-test123")

    def test_messages_create_called_with_correct_params(self):
        """messages.create が正しいパラメータで呼び出される。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system-prompt"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response("応答")

            llm_client.generate_response_sync("ユーザー入力")

        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == llm_client.CLAUDE_MODEL
        assert call_kwargs["max_tokens"] == 1024
        assert call_kwargs["system"] == "system-prompt"
        assert call_kwargs["messages"] == [{"role": "user", "content": "ユーザー入力"}]

    def test_content_stripped(self):
        """応答テキストの前後の空白が除去される。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = _make_claude_response("  はい、承知しました。  ")

            result = llm_client.generate_response_sync("テスト")

        assert result == "はい、承知しました。"

    def test_tool_call_loop(self):
        """ツール呼び出し → 結果送信 → テキスト応答のループが動作する。"""
        import llm_client

        tool_resp = _make_tool_use_response("sensor_status", {}, "tu_001")
        text_resp = _make_claude_response("内温は25℃です。")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
            patch("llm_client.execute_tool_call", return_value='{"temp": 25}') as mock_exec,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = [tool_resp, text_resp]

            result = llm_client.generate_response_sync("センサーの状態は？")

        assert result == "内温は25℃です。"
        assert mock_client.messages.create.call_count == 2
        # execute_tool_call が呼ばれたことを確認
        mock_exec.assert_called_once()

    def test_tool_use_id_forwarded_correctly(self):
        """tool_use_id が tool_result に正確に転送される。"""
        import llm_client

        tool_resp = _make_tool_use_response("relay_test", {}, "tu_xyz789")
        text_resp = _make_claude_response("リレー確認しました。")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
            patch("llm_client.execute_tool_call", return_value='{"status": "ok"}'),
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = [tool_resp, text_resp]

            llm_client.generate_response_sync("リレーテスト")

        # 2回目の呼び出しのメッセージに tool_result が含まれることを確認
        second_call_msgs = mock_client.messages.create.call_args_list[1][1]["messages"]
        # 最後のメッセージが user role で tool_result を含む
        last_msg = second_call_msgs[-1]
        assert last_msg["role"] == "user"
        tool_results = last_msg["content"]
        assert len(tool_results) == 1
        assert tool_results[0]["type"] == "tool_result"
        assert tool_results[0]["tool_use_id"] == "tu_xyz789"
        assert tool_results[0]["content"] == '{"status": "ok"}'

    def test_tool_call_limit(self):
        """MAX_TOOL_ROUNDS を超えたらループを終了する。"""
        import llm_client

        # 全ラウンドでツール呼び出しを返す
        responses = [
            _make_tool_use_response("sensor_status", {})
            for _ in range(llm_client.MAX_TOOL_ROUNDS + 1)
        ]

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
            patch("llm_client.execute_tool_call", return_value="{}"),
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = responses

            result = llm_client.generate_response_sync("テスト")

        assert isinstance(result, str)
        # 呼び出し回数が MAX_TOOL_ROUNDS + 1 以下
        assert mock_client.messages.create.call_count <= llm_client.MAX_TOOL_ROUNDS + 1

    def test_missing_api_key_raises(self):
        """ANTHROPIC_API_KEY が未設定なら KeyError が発生する。"""
        import llm_client

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("llm_client.get_system_prompt", return_value="system"),
        ):
            with pytest.raises(KeyError):
                llm_client.generate_response_sync("テスト")

    def test_assistant_content_appended_before_tool_result(self):
        """ツール呼び出し後、assistant content が messages に追加される。"""
        import llm_client

        tool_resp = _make_tool_use_response("sensor_status", {}, "tu_001")
        text_resp = _make_claude_response("応答")

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
            patch("llm_client.execute_tool_call", return_value="{}"),
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = [tool_resp, text_resp]

            llm_client.generate_response_sync("テスト")

        # 2回目の呼び出しには assistant + user(tool_result) が追加されている
        second_msgs = mock_client.messages.create.call_args_list[1][1]["messages"]
        roles = [m["role"] for m in second_msgs]
        assert "assistant" in roles
        assert roles[-1] == "user"

    def test_empty_content_returns_empty_string(self):
        """content にテキストブロックがない場合は空文字列を返す。"""
        import llm_client

        mock_response = MagicMock(spec=anthropic.types.Message)
        mock_response.stop_reason = "end_turn"
        mock_response.content = []  # テキストブロックなし

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.get_system_prompt", return_value="system"),
            patch("llm_client.anthropic.Anthropic") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            result = llm_client.generate_response_sync("テスト")

        assert result == ""


# ---------------------------------------------------------------------------
# 2. generate_response() 非同期版テスト
# ---------------------------------------------------------------------------


class TestGenerateResponseAsync:
    """generate_response() の非同期ラッパーテスト。"""

    @pytest.mark.asyncio
    async def test_async_simple_response(self):
        """非同期版が同期版を呼び出して正しい結果を返す。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.generate_response_sync", return_value="農場の状態です。") as mock_sync,
        ):
            result = await llm_client.generate_response("状態は？")

        assert result == "農場の状態です。"
        mock_sync.assert_called_once_with("状態は？")

    @pytest.mark.asyncio
    async def test_async_returns_string(self):
        """非同期版が文字列を返す。"""
        import llm_client

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}),
            patch("llm_client.generate_response_sync", return_value="OK"),
        ):
            result = await llm_client.generate_response("テスト")

        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 3. check_llm_health() テスト
# ---------------------------------------------------------------------------


class TestCheckLlmHealth:
    """check_llm_health() のテスト。"""

    @pytest.mark.asyncio
    async def test_health_key_present(self):
        """ANTHROPIC_API_KEY が設定されていれば True を返す。"""
        import llm_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            result = await llm_client.check_llm_health()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_key_absent(self):
        """ANTHROPIC_API_KEY が未設定なら False を返す。"""
        import llm_client

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            result = await llm_client.check_llm_health()

        assert result is False

    @pytest.mark.asyncio
    async def test_health_empty_key_is_false(self):
        """ANTHROPIC_API_KEY が空文字列の場合も False を返す。"""
        import llm_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = await llm_client.check_llm_health()

        assert result is False


# ---------------------------------------------------------------------------
# 4. _to_anthropic_tools() テスト
# ---------------------------------------------------------------------------


class TestToAnthropicTools:
    """_to_anthropic_tools() の変換テスト。"""

    def test_converts_openai_to_anthropic_format(self):
        """OpenAI形式ツールが Anthropic 形式に変換される。"""
        import llm_client

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "sensor_status",
                    "description": "センサーデータを取得する",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        result = llm_client._to_anthropic_tools(openai_tools)

        assert len(result) == 1
        assert result[0]["name"] == "sensor_status"
        assert result[0]["description"] == "センサーデータを取得する"
        assert result[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_converts_multiple_tools(self):
        """複数ツールが全て変換される。"""
        import llm_client

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": f"ツール{i}",
                    "parameters": {},
                },
            }
            for i in range(3)
        ]

        result = llm_client._to_anthropic_tools(openai_tools)

        assert len(result) == 3
        assert [r["name"] for r in result] == ["tool_0", "tool_1", "tool_2"]

    def test_empty_tools_returns_empty_list(self):
        """空リストを渡すと空リストが返る。"""
        import llm_client

        result = llm_client._to_anthropic_tools([])

        assert result == []


# ---------------------------------------------------------------------------
# 5. _execute_tool_call_anthropic() テスト
# ---------------------------------------------------------------------------


class TestExecuteToolCallAnthropic:
    """_execute_tool_call_anthropic() のテスト。"""

    def test_delegates_to_execute_tool_call(self):
        """ToolUseBlock を execute_tool_call 形式に変換して呼び出す。"""
        import llm_client

        tool_use = MagicMock()
        tool_use.name = "sensor_status"
        tool_use.input = {"channel": 1}

        with patch("llm_client.execute_tool_call", return_value='{"temp": 26}') as mock_exec:
            result = llm_client._execute_tool_call_anthropic(tool_use)

        assert result == '{"temp": 26}'
        expected_tc = {"function": {"name": "sensor_status", "arguments": {"channel": 1}}}
        mock_exec.assert_called_once_with(expected_tc)

    def test_tool_name_and_input_passed_correctly(self):
        """tool_use.name と tool_use.input が正確に変換される。"""
        import llm_client

        tool_use = MagicMock()
        tool_use.name = "relay_test"
        tool_use.input = {"relay": 3, "state": True}

        with patch("llm_client.execute_tool_call", return_value="ok") as mock_exec:
            llm_client._execute_tool_call_anthropic(tool_use)

        called_tc = mock_exec.call_args[0][0]
        assert called_tc["function"]["name"] == "relay_test"
        assert called_tc["function"]["arguments"] == {"relay": 3, "state": True}
