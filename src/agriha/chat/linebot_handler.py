"""linebot_handler.py — LINE Bot Webhook処理モジュール

設計書: docs/linebot_design.md
- 署名検証 (HMAC-SHA256)
- 全テキスト→LLM tool calling→Reply
- ツール: get_sensors, get_status, set_relay
- forecast_engine.py と同じ OpenAI SDK互換クライアントパターン
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger("linebot_handler")

# ── ツール定義 (OpenAI tools 形式) ─────────────────────────────────────────

LINEBOT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_sensors",
            "description": (
                "全センサーデータ取得。"
                "CCM内気象(温度/湿度/CO2) + DS18B20 + Misol外気象(気温/風速/風向/降雨) "
                "+ リレー状態を返す。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": (
                "デーモン状態取得。"
                "リレー状態(ch1-8 ON/OFF) + ロックアウト状態 + 稼働時間を返す。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_relay",
            "description": "リレーチャンネルのON/OFFを制御する。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "リレーチャンネル番号(1-8)",
                    },
                    "value": {
                        "type": "integer",
                        "description": "0=OFF, 1=ON",
                    },
                    "duration_sec": {
                        "type": "integer",
                        "description": "自動OFF秒数(0=永続)",
                    },
                },
                "required": ["channel", "value"],
            },
        },
    },
]


# ── 署名検証 ──────────────────────────────────────────────────────────────

def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """LINE Webhook 署名検証（HMAC-SHA256）。

    Args:
        body: リクエストボディのバイト列。
        signature: X-Line-Signature ヘッダー値。
        secret: LINE Channel Secret。

    Returns:
        署名が正しければ True、そうでなければ False。
    """
    hash_val = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ── REST API ツール呼び出し ────────────────────────────────────────────────

def call_tool(
    http_client: Any,
    base_url: str,
    api_key: str,
    name: str,
    args: dict[str, Any],
) -> str:
    """ツール名に応じて unipi-daemon REST API を呼ぶ。set_relay も対応。"""
    import httpx  # type: ignore

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    if name == "get_sensors":
        r = http_client.get(f"{base_url}/api/sensors", headers=headers)
        r.raise_for_status()
        return r.text

    if name == "get_status":
        r = http_client.get(f"{base_url}/api/status", headers=headers)
        r.raise_for_status()
        return r.text

    if name == "set_relay":
        channel = int(args.get("channel", 0))
        value = int(args.get("value", 0))
        duration_sec = int(args.get("duration_sec", 0))
        payload = json.dumps(
            {"value": value, "duration_sec": duration_sec, "reason": "LINE Bot"},
            ensure_ascii=False,
        ).encode("utf-8")
        r = http_client.post(
            f"{base_url}/api/relay/{channel}",
            headers={**headers, "Content-Type": "application/json"},
            content=payload,
        )
        r.raise_for_status()
        return r.text

    return json.dumps({"error": f"unknown tool: {name}"})


# ── LLM tool calling メッセージ処理 ──────────────────────────────────────

async def handle_message(
    text: str,
    llm_client: Any,
    llm_cfg: dict[str, Any],
    system_prompt: str,
    http_client: Any,
    base_url: str = "http://localhost:8080",
    api_key: str = "",
) -> str:
    """テキスト→LLM tool calling→応答テキスト生成。

    forecast_engine.py の run_forecast() 内LLM呼び出しと同パターン。

    Args:
        text: ユーザーからのテキスト入力。
        llm_client: OpenAI互換クライアント。
        llm_cfg: forecast.yaml の llm セクション。
        system_prompt: /etc/agriha/system_prompt.txt の内容。
        http_client: unipi-daemon REST API 用 HTTP クライアント。
        base_url: unipi-daemon ベースURL。
        api_key: unipi-daemon API キー。

    Returns:
        LLM からの最終テキスト応答。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    max_rounds = llm_cfg.get("max_tool_rounds", 5)
    final_text = "（応答なし）"

    for round_num in range(max_rounds):
        response = llm_client.chat.completions.create(
            model=llm_cfg.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=llm_cfg.get("max_tokens", 1024),
            tools=LINEBOT_TOOLS,
            messages=messages,
        )

        choice = response.choices[0]
        msg = choice.message
        has_tool_calls = bool(msg.tool_calls)

        messages.append(msg.model_dump(exclude_unset=False))

        if not has_tool_calls:
            final_text = msg.content or "（応答なし）"
            break

        # ツール呼び出し処理
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            try:
                tool_input = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {}

            logger.info("Tool call [round %d]: %s", round_num, tool_name)
            try:
                result_text = call_tool(http_client, base_url, api_key, tool_name, tool_input)
            except Exception as exc:
                logger.error("Tool call failed: %s: %s", tool_name, exc)
                result_text = json.dumps({"error": str(exc)}, ensure_ascii=False)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text,
            })

        if choice.finish_reason == "stop":
            final_text = msg.content or "（応答なし）"
            break

    return final_text


# ── LINE Messaging API ────────────────────────────────────────────────────

def send_reply(reply_token: str, message: str, access_token: str) -> bool:
    """LINE Reply API でメッセージを返信する。

    Args:
        reply_token: LINE Webhook イベントの replyToken。
        message: 送信するテキスト。
        access_token: LINE Channel Access Token。

    Returns:
        成功なら True、失敗なら False。
    """
    payload = json.dumps(
        {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": message}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        logger.error("send_reply failed: %s", exc)
        return False


def send_push(user_id: str, message: str, access_token: str) -> bool:
    """LINE Push API でメッセージを送信する。

    Args:
        user_id: 送信先 LINE User ID。
        message: 送信するテキスト。
        access_token: LINE Channel Access Token。

    Returns:
        成功なら True、失敗なら False。
    """
    payload = json.dumps(
        {
            "to": user_id,
            "messages": [{"type": "text", "text": message}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        logger.error("send_push failed: %s", exc)
        return False
