"""
Ollama API クライアント

http://ollama:11434/api/chat に httpx でリクエストを送る。
モデル: qwen3:8b（think: false で thinking 無効化）
stream: false（LINE reply は一括応答）

ツール呼び出し対応:
  sensor_status / relay_test / actuator_control の 3 ツールを使用する。
  LLM がツールを呼び出した場合、結果を送り返して最大 MAX_TOOL_ROUNDS 往復まで繰り返す。
"""

import asyncio
import logging
import os

import httpx

from system_prompt import get_system_prompt
from tools import TOOLS, execute_tool_call

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3:8b")
TIMEOUT_SEC = 60.0
MAX_RETRIES = 1
MAX_TOOL_ROUNDS = 3  # ツール呼び出しの最大往復回数


def _base_payload(messages: list) -> dict:
    """共通リクエストペイロードを生成する。"""
    return {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
            "num_predict": 512,
        },
    }


def _post_sync(client: httpx.Client, payload: dict) -> dict:
    """同期 POST を MAX_RETRIES 回リトライして JSON を返す。"""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            if attempt < MAX_RETRIES:
                continue
            raise
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama API error: {e.response.status_code}") from e
    return {}  # unreachable


def generate_response_sync(user_message: str) -> str:
    """同期版: ツール呼び出しループ付き応答生成。

    WebhookHandler（同期ハンドラ）から呼び出す。
    LLM がツールを呼び出した場合は実行して結果を送り返す。
    """
    messages: list[dict] = [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": user_message},
    ]

    with httpx.Client(timeout=TIMEOUT_SEC) as client:
        for _round in range(MAX_TOOL_ROUNDS + 1):
            data = _post_sync(client, _base_payload(messages))
            message = data.get("message", {})
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                return message.get("content", "").strip()

            if _round >= MAX_TOOL_ROUNDS:
                logger.warning("ツール呼び出し上限(%d)に達した", MAX_TOOL_ROUNDS)
                return message.get("content", "").strip() or "ツール呼び出し上限に達しました。"

            # アシスタントメッセージ（tool_calls 付き）を履歴に追加
            messages.append(message)

            # 各ツールを実行して tool メッセージを追加
            for tc in tool_calls:
                tool_result = execute_tool_call(tc)
                messages.append({"role": "tool", "content": tool_result})

    return ""  # unreachable


async def generate_response(user_message: str) -> str:
    """非同期版: ツール呼び出しループ付き応答生成。"""
    messages: list[dict] = [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": user_message},
    ]

    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        for _round in range(MAX_TOOL_ROUNDS + 1):
            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"{OLLAMA_URL}/api/chat",
                        json=_base_payload(messages),
                    )
                    resp.raise_for_status()
                    break
                except httpx.TimeoutException:
                    if attempt < MAX_RETRIES:
                        continue
                    raise
                except httpx.HTTPStatusError as e:
                    raise RuntimeError(f"Ollama API error: {e.response.status_code}") from e

            data = resp.json()
            message = data.get("message", {})
            tool_calls = message.get("tool_calls")

            if not tool_calls:
                return message.get("content", "").strip()

            if _round >= MAX_TOOL_ROUNDS:
                return message.get("content", "").strip() or "ツール呼び出し上限に達しました。"

            messages.append(message)

            for tc in tool_calls:
                # rpi_client は同期なのでスレッドプールで実行
                tool_result = await asyncio.get_event_loop().run_in_executor(
                    None, execute_tool_call, tc
                )
                messages.append({"role": "tool", "content": tool_result})

    return ""  # unreachable


async def check_ollama_health() -> bool:
    """Ollama が起動しているか確認する。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
