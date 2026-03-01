"""LINE Bot ツール定義と実行エンジン

OpenAI互換 tool calling 用のスキーマと、
ツール実行ロジックを提供する。

ツール一覧:
  relay_test       リレー ON/OFF 動作テスト
  sensor_status    ハウスセンサーデータ取得
  actuator_control アクチュエータ（灌水/換気窓等）制御
"""

from __future__ import annotations

import json
import logging
from typing import Any

from rpi_client import get_sensors, set_relay

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ツールスキーマ定義 (OpenAI 互換 JSON Schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "relay_test",
            "description": (
                "リレーチャンネルを ON/OFF する動作テストを行う。"
                "「リレー3 ON」「リレー5 OFF」「1から8番まで順番に動かして」などに使う。"
                "duration_sec を指定すると自動で OFF になる。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "リレーチャンネル番号 (1〜8)",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "value": {
                        "type": "integer",
                        "description": "0=OFF, 1=ON",
                        "enum": [0, 1],
                    },
                    "duration_sec": {
                        "type": "number",
                        "description": "指定秒後に自動 OFF。0 または省略でタイマーなし。",
                        "default": 0.0,
                    },
                },
                "required": ["channel", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sensor_status",
            "description": (
                "ハウスの最新センサーデータを取得する。"
                "「今の温度は？」「気象データ見せて」「センサーの状態は？」「ハウスの状況教えて」などに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actuator_control",
            "description": (
                "アクチュエータ（灌水ポンプ・換気窓モーター等）を制御する。"
                "「灌水 ON」「換気窓開けて」「灌水を 30 秒だけ ON」「リレー2 OFF」などに使う。"
                "relay_test との違い: 実運用目的の制御で reason が必ずログに記録される。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "リレーチャンネル番号 (1〜8)",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "action": {
                        "type": "string",
                        "description": "on=ON, off=OFF",
                        "enum": ["on", "off"],
                    },
                    "duration_sec": {
                        "type": "number",
                        "description": "動作時間（秒）。0 または省略で手動停止まで継続。",
                        "default": 0.0,
                    },
                    "reason": {
                        "type": "string",
                        "description": "制御理由（ログに記録される）。例: '灌水', '換気窓開放', '換気扇 ON'",
                    },
                },
                "required": ["channel", "action"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# ツール実行エンジン
# ---------------------------------------------------------------------------

def execute_tool_call(tool_call: dict[str, Any]) -> str:
    """Ollama ツールコールを実行し、結果を JSON 文字列で返す。

    Args:
        tool_call: Ollama message.tool_calls の 1 エントリ
                   {"function": {"name": "...", "arguments": {...}}}

    Returns:
        ツール実行結果の JSON 文字列（LLM の tool メッセージの content として使用）
    """
    func = tool_call.get("function", {})
    name = func.get("name", "")
    args = func.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    logger.info("ツール実行: %s args=%s", name, args)

    if name == "sensor_status":
        result = get_sensors()

    elif name == "relay_test":
        ch = int(args.get("channel", 1))
        value = int(args.get("value", 0))
        duration = float(args.get("duration_sec", 0.0))
        result = set_relay(
            ch=ch,
            value=value,
            duration_sec=duration,
            reason="LINE Bot リレーテスト",
        )

    elif name == "actuator_control":
        ch = int(args.get("channel", 1))
        action = args.get("action", "off")
        value = 1 if action == "on" else 0
        duration = float(args.get("duration_sec", 0.0))
        reason = str(args.get("reason", f"LINE Bot ch{ch} {action}"))
        result = set_relay(ch=ch, value=value, duration_sec=duration, reason=reason)

    else:
        logger.warning("未知のツール: %s", name)
        result = {"error": "unknown_tool", "message": f"未知のツール: {name}"}

    logger.info("ツール結果: %s → %s", name, str(result)[:120])
    return json.dumps(result, ensure_ascii=False)
