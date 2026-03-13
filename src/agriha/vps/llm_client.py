"""
Claude API クライアント (Anthropic SDK)

Anthropic Messages API に接続してツール呼び出しループを実行する。
sensor_status / relay_test / actuator_control / control_history の各ツールに対応。
"""

import asyncio
import logging
import os

import anthropic

from system_prompt import get_system_prompt
from tools import TOOLS, execute_tool_call

logger = logging.getLogger(__name__)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
MAX_TOOL_ROUNDS = 3


def _to_anthropic_tools(tools: list) -> list:
    """OpenAI互換ツール定義を Anthropic 形式に変換する。"""
    result = []
    for t in tools:
        f = t["function"]
        result.append({
            "name": f["name"],
            "description": f["description"],
            "input_schema": f["parameters"],
        })
    return result


def _execute_tool_call_anthropic(tool_use) -> str:
    """Anthropic ToolUseBlock を execute_tool_call() 形式に変換して実行する。"""
    tc = {
        "function": {
            "name": tool_use.name,
            "arguments": tool_use.input,
        }
    }
    return execute_tool_call(tc)


def generate_response_sync(user_message: str) -> str:
    """Claude API 同期版: ツール呼び出しループ付き応答生成。

    ANTHROPIC_API_KEY 環境変数が必要。
    WebhookHandler（同期ハンドラ）から呼び出す。
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = get_system_prompt()
    messages: list[dict] = [
        {"role": "user", "content": user_message},
    ]
    anthropic_tools = _to_anthropic_tools(TOOLS)

    for _round in range(MAX_TOOL_ROUNDS + 1):
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=anthropic_tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return ""

        if _round >= MAX_TOOL_ROUNDS:
            logger.warning("Claude ツール呼び出し上限(%d)に達した", MAX_TOOL_ROUNDS)
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return "ツール呼び出し上限に達しました。"

        # アシスタントメッセージ（tool_use 付き）を履歴に追加
        messages.append({"role": "assistant", "content": response.content})

        # ツールを実行して tool_result を追加
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool_call_anthropic(block)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    return ""  # unreachable


async def generate_response(user_message: str) -> str:
    """Claude API 非同期ラッパー。同期版を executor で実行する。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, generate_response_sync, user_message)


async def check_llm_health() -> bool:
    """Anthropic API が利用可能か確認する（APIキー設定チェック）。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
