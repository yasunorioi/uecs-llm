"""tests/chat/test_linebot.py — LINE Bot ユニットテスト

linebot_handler.py および /callback エンドポイントのテスト。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── linebot_handler のユニットテスト ───────────────────────────────────────


def test_verify_signature_valid() -> None:
    """正しい署名が True を返す。"""
    from agriha.chat.linebot_handler import verify_signature

    secret = "test_secret"
    body = b'{"events":[]}'
    hash_val = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    signature = base64.b64encode(hash_val).decode("utf-8")
    assert verify_signature(body, signature, secret) is True


def test_verify_signature_invalid() -> None:
    """不正な署名が False を返す。"""
    from agriha.chat.linebot_handler import verify_signature

    assert verify_signature(b"body", "invalidsig", "secret") is False


def test_verify_signature_empty() -> None:
    """空署名が False を返す。"""
    from agriha.chat.linebot_handler import verify_signature

    assert verify_signature(b"body", "", "secret") is False


# ── call_tool テスト ───────────────────────────────────────────────────────


def test_call_tool_get_sensors() -> None:
    """get_sensors が /api/sensors を呼ぶ。"""
    from agriha.chat.linebot_handler import call_tool

    mock_resp = MagicMock()
    mock_resp.text = '{"temp": 25.0}'
    mock_http = MagicMock()
    mock_http.get.return_value = mock_resp

    result = call_tool(mock_http, "http://localhost:8080", "", "get_sensors", {})
    mock_http.get.assert_called_once_with("http://localhost:8080/api/sensors", headers={})
    assert result == '{"temp": 25.0}'


def test_call_tool_get_status() -> None:
    """get_status が /api/status を呼ぶ。"""
    from agriha.chat.linebot_handler import call_tool

    mock_resp = MagicMock()
    mock_resp.text = '{"relays": []}'
    mock_http = MagicMock()
    mock_http.get.return_value = mock_resp

    result = call_tool(mock_http, "http://localhost:8080", "", "get_status", {})
    mock_http.get.assert_called_once_with("http://localhost:8080/api/status", headers={})
    assert result == '{"relays": []}'


def test_call_tool_set_relay() -> None:
    """set_relay が /api/relay/{channel} にPOSTする。"""
    from agriha.chat.linebot_handler import call_tool

    mock_resp = MagicMock()
    mock_resp.text = '{"ok": true}'
    mock_http = MagicMock()
    mock_http.post.return_value = mock_resp

    result = call_tool(
        mock_http, "http://localhost:8080", "apikey",
        "set_relay", {"channel": 3, "value": 1, "duration_sec": 60},
    )
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "http://localhost:8080/api/relay/3"
    assert call_args[1]["headers"]["X-API-Key"] == "apikey"
    assert result == '{"ok": true}'


def test_call_tool_unknown() -> None:
    """不明なツール名はエラーJSONを返す。"""
    from agriha.chat.linebot_handler import call_tool

    result = call_tool(MagicMock(), "http://localhost:8080", "", "unknown_tool", {})
    assert "unknown tool" in json.loads(result).get("error", "")


# ── handle_message テスト ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_message_no_tool_call() -> None:
    """ツール呼び出しなしで直接テキスト応答を返す。"""
    from agriha.chat.linebot_handler import handle_message

    mock_msg = MagicMock()
    mock_msg.tool_calls = None
    mock_msg.content = "温室は快適です。"
    mock_msg.model_dump.return_value = {"role": "assistant", "content": "温室は快適です。"}
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_choice.finish_reason = "stop"
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = mock_response

    result = await handle_message(
        text="今の状況は？",
        llm_client=mock_llm,
        llm_cfg={"model": "claude-haiku-4-5-20251001", "max_tokens": 512},
        system_prompt="あなたは農業アシスタントです。",
        http_client=MagicMock(),
    )
    assert result == "温室は快適です。"


@pytest.mark.asyncio
async def test_handle_message_with_tool_call() -> None:
    """ツール呼び出し → 結果をフィード → 最終応答。"""
    from agriha.chat.linebot_handler import handle_message

    # 1回目: tool call
    tc = MagicMock()
    tc.id = "tc_001"
    tc.function.name = "get_sensors"
    tc.function.arguments = "{}"

    msg1 = MagicMock()
    msg1.tool_calls = [tc]
    msg1.content = None
    msg1.model_dump.return_value = {"role": "assistant", "tool_calls": []}
    choice1 = MagicMock()
    choice1.message = msg1
    choice1.finish_reason = "tool_use"

    # 2回目: 最終応答
    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "気温は25度です。"
    msg2.model_dump.return_value = {"role": "assistant", "content": "気温は25度です。"}
    choice2 = MagicMock()
    choice2.message = msg2
    choice2.finish_reason = "stop"

    mock_resp1 = MagicMock(); mock_resp1.choices = [choice1]
    mock_resp2 = MagicMock(); mock_resp2.choices = [choice2]

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.side_effect = [mock_resp1, mock_resp2]

    mock_http = MagicMock()
    sensor_resp = MagicMock(); sensor_resp.text = '{"temp": 25.0}'
    mock_http.get.return_value = sensor_resp

    result = await handle_message(
        text="今の気温は？",
        llm_client=mock_llm,
        llm_cfg={"model": "claude-haiku-4-5-20251001"},
        system_prompt="あなたは農業アシスタントです。",
        http_client=mock_http,
    )
    assert result == "気温は25度です。"
    assert mock_llm.chat.completions.create.call_count == 2


# ── send_reply / send_push テスト ────────────────────────────────────────


def test_send_reply_success() -> None:
    """send_reply が成功時 True を返す。"""
    from agriha.chat.linebot_handler import send_reply

    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        result = send_reply("reply_token_123", "テスト返信", "access_token_xyz")
    assert result is True


def test_send_reply_failure() -> None:
    """send_reply が例外時 False を返す。"""
    from agriha.chat.linebot_handler import send_reply

    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = send_reply("token", "msg", "access_token")
    assert result is False


def test_send_push_success() -> None:
    """send_push が成功時 True を返す。"""
    from agriha.chat.linebot_handler import send_push

    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.__enter__.return_value = mock_resp
        result = send_push("user_id_abc", "プッシュ通知テスト", "access_token_xyz")
    assert result is True


def test_send_push_failure() -> None:
    """send_push が例外時 False を返す。"""
    from agriha.chat.linebot_handler import send_push

    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = send_push("user_id", "msg", "access_token")
    assert result is False


# ── /callback エンドポイントテスト ───────────────────────────────────────


def _make_client(env_overrides: dict[str, str] | None = None) -> TestClient:
    """テスト用 FastAPI クライアントを生成する。"""
    import os
    from unittest.mock import patch

    defaults = {
        "LINE_CHANNEL_SECRET": "test_secret",
        "LINE_CHANNEL_ACCESS_TOKEN": "test_access_token",
        "LINE_USER_ID": "U123456",
        "UNIPI_API_URL": "http://localhost:8080",
        "CONTROL_LOG_DB": ":memory:",
        "UI_AUTH_USER": "admin",
        "UI_AUTH_PASS": "agriha",
    }
    if env_overrides:
        defaults.update(env_overrides)

    with patch.dict(os.environ, defaults, clear=False):
        import importlib
        import agriha.chat.app as app_module
        importlib.reload(app_module)
        return TestClient(app_module.app, raise_server_exceptions=False)


def _make_signature(body: bytes, secret: str) -> str:
    hash_val = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(hash_val).decode("utf-8")


def test_callback_returns_200_on_valid_text_event() -> None:
    """有効な署名 + テキストイベントで 200 OK が返る。"""
    import sys
    from unittest.mock import MagicMock

    # openai が未インストール環境でもテスト可能にする
    mock_openai_module = MagicMock()
    mock_openai_module.OpenAI = MagicMock(return_value=MagicMock())
    with patch.dict(sys.modules, {"openai": mock_openai_module}):
        client = _make_client()
        body_dict = {
            "events": [
                {
                    "type": "message",
                    "replyToken": "reply_token_abc",
                    "message": {"type": "text", "text": "こんにちは"},
                }
            ]
        }
        body = json.dumps(body_dict).encode("utf-8")
        sig = _make_signature(body, "test_secret")

        with (
            patch("agriha.chat.linebot_handler.handle_message", new_callable=AsyncMock) as mock_hm,
            patch("agriha.chat.linebot_handler.send_reply") as mock_sr,
        ):
            mock_hm.return_value = "こんにちは！"
            mock_sr.return_value = True
            response = client.post(
                "/callback",
                content=body,
                headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
            )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_callback_returns_403_on_invalid_signature() -> None:
    """不正な署名で 403 が返る。"""
    client = _make_client()
    body = b'{"events":[]}'
    response = client.post(
        "/callback",
        content=body,
        headers={"X-Line-Signature": "invalidsig", "Content-Type": "application/json"},
    )
    assert response.status_code == 403


def test_callback_returns_503_when_not_configured() -> None:
    """LINE設定未完了で 503 が返る。"""
    client = _make_client({"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_ACCESS_TOKEN": ""})
    body = b'{"events":[]}'
    response = client.post(
        "/callback",
        content=body,
        headers={"X-Line-Signature": "anysig", "Content-Type": "application/json"},
    )
    assert response.status_code == 503


def test_callback_ignores_non_text_events() -> None:
    """テキスト以外のイベントは handle_message を呼ばず 200 を返す。"""
    client = _make_client()
    body_dict = {
        "events": [
            {"type": "follow", "replyToken": "tok"},
            {"type": "message", "replyToken": "tok2", "message": {"type": "image"}},
        ]
    }
    body = json.dumps(body_dict).encode("utf-8")
    sig = _make_signature(body, "test_secret")

    with patch("agriha.chat.linebot_handler.handle_message", new_callable=AsyncMock) as mock_hm:
        response = client.post(
            "/callback",
            content=body,
            headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
        )
        mock_hm.assert_not_called()

    assert response.status_code == 200


# ── NullClaw切替テスト ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_message_nullclaw_get_sensors() -> None:
    """T7: is_nullclaw=True でも get_sensors は正常に呼ばれる。"""
    from agriha.chat.linebot_handler import handle_message

    tc = MagicMock()
    tc.id = "tc_007"
    tc.function.name = "get_sensors"
    tc.function.arguments = "{}"

    msg1 = MagicMock()
    msg1.tool_calls = [tc]
    msg1.content = None
    msg1.model_dump.return_value = {"role": "assistant", "tool_calls": []}
    choice1 = MagicMock()
    choice1.message = msg1
    choice1.finish_reason = "tool_use"

    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "気温は25度です。"
    msg2.model_dump.return_value = {"role": "assistant", "content": "気温は25度です。"}
    choice2 = MagicMock()
    choice2.message = msg2
    choice2.finish_reason = "stop"

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.side_effect = [
        MagicMock(choices=[choice1]),
        MagicMock(choices=[choice2]),
    ]

    mock_http = MagicMock()
    mock_http.get.return_value = MagicMock(text='{"temp": 25.0}')

    result = await handle_message(
        text="今の気温は？",
        llm_client=mock_llm,
        llm_cfg={"model": "nullclaw-local"},
        system_prompt="アシスタントです。",
        http_client=mock_http,
        is_nullclaw=True,
    )
    assert result == "気温は25度です。"
    mock_http.get.assert_called_once()  # get_sensors が呼ばれた


@pytest.mark.asyncio
async def test_handle_message_nullclaw_set_relay_blocked() -> None:
    """T10: is_nullclaw=True かつ set_relay 試行→unipi-daemon APIを呼ばず制御不可メッセージが返る。"""
    from agriha.chat.linebot_handler import handle_message

    # 1回目: set_relay tool call
    tc = MagicMock()
    tc.id = "tc_010"
    tc.function.name = "set_relay"
    tc.function.arguments = '{"channel": 1, "state": true}'

    msg1 = MagicMock()
    msg1.tool_calls = [tc]
    msg1.content = None
    msg1.model_dump.return_value = {"role": "assistant", "tool_calls": []}
    choice1 = MagicMock()
    choice1.message = msg1
    choice1.finish_reason = "tool_use"

    # 2回目: 最終応答
    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "制御操作はできません。"
    msg2.model_dump.return_value = {"role": "assistant", "content": "制御操作はできません。"}
    choice2 = MagicMock()
    choice2.message = msg2
    choice2.finish_reason = "stop"

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.side_effect = [
        MagicMock(choices=[choice1]),
        MagicMock(choices=[choice2]),
    ]

    mock_http = MagicMock()

    result = await handle_message(
        text="開けろ",
        llm_client=mock_llm,
        llm_cfg={"model": "nullclaw-local"},
        system_prompt="アシスタントです。",
        http_client=mock_http,
        is_nullclaw=True,
    )
    # set_relayが実際のAPIを呼ばずに制御不可メッセージが返った
    mock_http.post.assert_not_called()
    assert "APIキー" in result or "制御" in result
