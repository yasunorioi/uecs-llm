#!/usr/bin/env python3
"""AgriHA LLM制御ループ — LFM2.5 (llama-server) + unipi-daemon REST API.

cron (*/5) から起動され、1回の制御判断を行って終了する。

Flow:
  1. REST API で全センサーデータ取得
  2. llama-server (OpenAI互換API) にシステムプロンプト + 履歴 + センサーデータを送信
  3. LLM が tool_calls (get_sensors / get_status / set_relay) を返す
  4. tool_calls を REST API 経由で実行
  5. 判断ログを SQLite に保存

LLM: LFM2.5 1.2B (lfm2.5-1.2b-instruct-q4_k_m.gguf)
推論サーバー: llama-server (llama.cpp) — OpenAI互換 /v1/chat/completions

設計書: docs/llm_control_loop_design.md §2.3
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from astral import LocationInfo
from astral.sun import sun as astral_sun

logger = logging.getLogger("agriha_control")

# ---------------------------------------------------------------------------
# デフォルト設定（環境変数 or config で上書き可能）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "llama_server_url": "http://localhost:8081",
    "unipi_api": "http://10.10.0.10:8080",
    "api_key": "",
    "db_path": "/var/lib/agriha/control_log.db",
    "system_prompt_path": "/etc/agriha/system_prompt.txt",
    "max_tool_rounds": 5,
    "temperature": 0.1,
    "max_tokens": 512,
    "inference_timeout_sec": 60,
    # 座標（恵庭市近郊: ArSprout node_config より）
    "latitude": 42.888,
    "longitude": 141.603,
    "elevation": 21,
}

# ---------------------------------------------------------------------------
# ツール定義 (OpenAI互換 tools 形式)
# ---------------------------------------------------------------------------

TOOLS = [
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
            "description": (
                "UniPiリレー制御。ch=チャンネル(1-8), value=1(ON)/0(OFF), "
                "duration_sec=自動OFF秒数(灌水等は必須指定), reason=操作理由。"
                "ch4=灌水電磁弁, ch5-8=側窓開閉。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ch": {"type": "integer", "minimum": 1, "maximum": 8},
                    "value": {"type": "integer", "enum": [0, 1]},
                    "duration_sec": {"type": "number", "default": 0},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["ch", "value"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# DB ヘルパー
# ---------------------------------------------------------------------------

def init_db(db_path: str | Path) -> sqlite3.Connection:
    """制御ログ DB を初期化して接続を返す。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        summary TEXT,
        actions_taken TEXT,
        raw_response TEXT,
        sensor_snapshot TEXT
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_ts "
        "ON decisions(timestamp DESC)"
    )
    db.commit()
    return db


def load_recent_history(db: sqlite3.Connection, n: int = 3) -> str:
    """直近 n 回の判断履歴をテキストで返す。"""
    rows = db.execute(
        "SELECT timestamp, summary, actions_taken "
        "FROM decisions ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        return "（過去の判断履歴なし — 初回起動）"
    lines = []
    for ts, summary, actions in reversed(rows):
        lines.append(f"[{ts}] {summary} → {actions}")
    return "\n".join(lines)


def save_decision(
    db: sqlite3.Connection,
    summary: str,
    actions: str,
    raw_response: str,
    sensor_snapshot: str,
) -> None:
    """判断ログを SQLite に保存。"""
    db.execute(
        "INSERT INTO decisions "
        "(timestamp, summary, actions_taken, raw_response, sensor_snapshot) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            summary,
            actions,
            raw_response,
            sensor_snapshot,
        ),
    )
    db.commit()


# ---------------------------------------------------------------------------
# 日の出/日没ヘルパー
# ---------------------------------------------------------------------------

_JST = ZoneInfo("Asia/Tokyo")


def get_sun_times(
    lat: float,
    lon: float,
    elevation: float = 0,
    dt: datetime | None = None,
) -> tuple[datetime, datetime]:
    """指定座標の当日の日の出/日没時刻を返す（JST）。

    Args:
        lat: 緯度。
        lon: 経度。
        elevation: 標高 (m)。
        dt: 基準日時。None の場合は現在時刻（JST）を使用。

    Returns:
        (sunrise, sunset) — どちらも JST の aware datetime。
    """
    ref = dt if dt is not None else datetime.now(_JST)
    location = LocationInfo(latitude=lat, longitude=lon)
    s = astral_sun(location.observer, date=ref.date(), tzinfo=_JST)
    return s["sunrise"], s["sunset"]


def get_time_period(now: datetime, sunrise: datetime, sunset: datetime) -> str:
    """現在時刻から時間帯ラベルを返す。

    Returns:
        "日の出前" / "日中（日の出後〜日没前1時間）" / "日没前1時間" / "日没後"
    """
    if now < sunrise:
        return "日の出前"
    if now >= sunset:
        return "日没後"
    if now >= sunset - timedelta(hours=1):
        return "日没前1時間"
    return "日中（日の出後〜日没前1時間）"


# ---------------------------------------------------------------------------
# REST API 呼び出し（unipi-daemon）
# ---------------------------------------------------------------------------

def call_tool(
    http_client: Any,
    base_url: str,
    api_key: str,
    name: str,
    args: dict,
) -> str:
    """ツール名に応じて unipi-daemon REST API を呼ぶ。"""
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
        ch = args.get("ch", 1)
        payload = {
            "value": args.get("value", 0),
            "duration_sec": args.get("duration_sec", 0),
            "reason": args.get("reason", "LLM auto"),
        }
        r = http_client.post(
            f"{base_url}/api/relay/{ch}",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
        return r.text

    return json.dumps({"error": f"unknown tool: {name}"})


# ---------------------------------------------------------------------------
# LLM 推論 (llama-server OpenAI互換API)
# ---------------------------------------------------------------------------

def llm_chat(
    llm_client: Any,
    llm_url: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float = 0.1,
    max_tokens: int = 512,
    tool_choice: str = "auto",
) -> dict[str, Any]:
    """llama-server の /v1/chat/completions を呼び出す。

    Args:
        llm_client: httpx.Client (または互換のモック)。
        llm_url: llama-server のベース URL (例: http://localhost:8081)。
        messages: OpenAI形式のメッセージリスト。
        tools: OpenAI形式のツール定義リスト。
        temperature: 推論温度。
        max_tokens: 最大生成トークン数。
        tool_choice: "auto", "required", or "none"。

    Returns:
        OpenAI互換レスポンスから抽出した message dict。
        キー: "content" (str|None), "tool_calls" (list|None)
    """
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    resp = llm_client.post(
        f"{llm_url}/v1/chat/completions",
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    message = choice["message"]

    # tool_calls の arguments が JSON 文字列の場合をパース
    raw_tool_calls = message.get("tool_calls") or []
    parsed_tool_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        if isinstance(args_raw, str):
            try:
                args_parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                logger.warning("Failed to parse tool arguments: %r", args_raw)
                args_parsed = {}
        else:
            args_parsed = args_raw

        parsed_tool_calls.append({
            "id": tc.get("id", ""),
            "function": {
                "name": fn.get("name", ""),
                "arguments": args_parsed,
            },
        })

    return {
        "content": message.get("content"),
        "tool_calls": parsed_tool_calls if parsed_tool_calls else None,
    }


# ---------------------------------------------------------------------------
# メイン制御ループ
# ---------------------------------------------------------------------------

def run_control_loop(
    config: dict[str, Any] | None = None,
    *,
    llm_client: Any = None,
    http_client: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """メイン制御ループ（1 回実行、cron から呼ばれる）。

    Args:
        config: 設定辞書。None の場合は DEFAULT_CONFIG を使用。
        llm_client: llama-server 用 HTTP クライアント（テスト用 DI）。
        http_client: unipi-daemon REST API 用 HTTP クライアント（テスト用 DI）。

    Returns:
        判断結果の辞書 (summary, actions, sensor_snapshot)。
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    llm_url = cfg["llama_server_url"]
    base_url = cfg["unipi_api"]
    api_key = cfg["api_key"]
    max_rounds = int(cfg["max_tool_rounds"])
    temperature = float(cfg["temperature"])
    max_tokens = int(cfg["max_tokens"])
    inference_timeout = int(cfg["inference_timeout_sec"])

    # llama-server 用 HTTP クライアント（DI or デフォルト）
    own_llm_client = False
    if llm_client is None:
        llm_client = httpx.Client(timeout=inference_timeout)
        own_llm_client = True

    # unipi-daemon REST API 用 HTTP クライアント（DI or デフォルト）
    own_http_client = False
    if http_client is None:
        http_client = httpx.Client(timeout=30)
        own_http_client = True

    # DB
    db = init_db(cfg["db_path"])

    try:
        # システムプロンプト
        prompt_path = Path(cfg["system_prompt_path"])
        if prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = (
                "あなたは温室環境制御AIです。"
                "センサーデータを確認し、必要に応じてリレーを制御してください。"
            )
            logger.warning("system_prompt not found: %s (using default)", prompt_path)

        # 履歴
        history = load_recent_history(db, n=3)

        # 現在日時（JST、タイムゾーン付き）
        now_jst = (now if now is not None else datetime.now(_JST)).astimezone(_JST)

        # 日の出/日没計算
        sunrise, sunset = get_sun_times(
            lat=float(cfg["latitude"]),
            lon=float(cfg["longitude"]),
            elevation=float(cfg["elevation"]),
            dt=now_jst,
        )
        time_period = get_time_period(now_jst, sunrise, sunset)

        # メッセージ組み立て
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"## 直近の判断履歴\n{history}\n\n"
                    f"## 指示\n"
                    f"現在日時: {now_jst.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                    f"日の出: {sunrise.strftime('%H:%M')} / 日没: {sunset.strftime('%H:%M')}\n"
                    f"現在の時間帯: {time_period}\n"
                    f"センサーデータを確認し、5分後の気象変化を予測し、"
                    f"目標値に近づける制御アクションを実行せよ。\n"
                    f"アクションが不要なら「現状維持」と報告せよ。"
                ),
            },
        ]

        # Tool calling ループ
        sensor_snapshot = ""
        actions_taken: list[str] = []
        msg: dict[str, Any] = {}

        for round_num in range(max_rounds):
            # 初回は tool_choice="required" でセンサーデータ取得を強制
            # (LFM2.5 は auto だとツール呼び出しをスキップする傾向がある)
            choice = "required" if round_num == 0 else "auto"
            msg = llm_chat(
                llm_client,
                llm_url,
                messages,
                TOOLS,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice=choice,
            )

            # ツール呼び出しがなければ最終応答
            if not msg.get("tool_calls"):
                break

            # assistant メッセージを追加（tool_calls 付き）
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if msg.get("content"):
                assistant_msg["content"] = msg["content"]
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": json.dumps(
                            tc["function"]["arguments"], ensure_ascii=False
                        ),
                    },
                }
                for tc in msg["tool_calls"]
            ]
            messages.append(assistant_msg)

            # 各ツール呼び出しを実行
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                tool_name = fn["name"]
                tool_args = fn["arguments"]

                logger.info(
                    "Tool call [%d]: %s(%s)", round_num, tool_name, tool_args
                )

                try:
                    result_text = call_tool(
                        http_client, base_url, api_key, tool_name, tool_args
                    )
                except Exception as exc:
                    logger.error("Tool call failed: %s: %s", tool_name, exc)
                    result_text = json.dumps(
                        {"error": str(exc)}, ensure_ascii=False
                    )

                if tool_name in ("get_sensors", "get_status"):
                    sensor_snapshot += f"\n--- {tool_name} ---\n{result_text}"

                if tool_name == "set_relay":
                    actions_taken.append(
                        f"relay ch{tool_args.get('ch')}="
                        f"{'ON' if tool_args.get('value') else 'OFF'}"
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })

        # 最終応答
        final_text = msg.get("content", "（応答なし）") or "（応答なし）"

        result = {
            "summary": final_text[:500],
            "actions": "; ".join(actions_taken) if actions_taken else "現状維持",
            "sensor_snapshot": sensor_snapshot[:2000],
        }

        # DB 保存
        save_decision(
            db,
            summary=result["summary"],
            actions=result["actions"],
            raw_response=json.dumps(msg, ensure_ascii=False),
            sensor_snapshot=result["sensor_snapshot"],
        )

        logger.info("Decision: %s | Actions: %s", final_text[:200], actions_taken)
        return result

    finally:
        if own_llm_client:
            llm_client.close()
        if own_http_client:
            http_client.close()
        db.close()


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI エントリポイント。cron から呼ばれる。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # オプション: --config <path> で設定ファイル指定
    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        import yaml  # type: ignore[import-untyped]

        config_path = Path(sys.argv[2])
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
            config.update(user_config)

    result = run_control_loop(config)
    print(f"Result: {result['actions']}")


if __name__ == "__main__":
    main()
