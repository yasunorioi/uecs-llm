#!/usr/bin/env python3
"""Layer 3: LLM 1時間予報エンジン — Claude Haiku + unipi-daemon REST API.

cron (0 * * * *) から起動され、1時間分のアクション計画を生成して終了する。
**リレー操作は一切行わない。** 計画の実行は plan_executor.py が担当。

設計書: docs/v2_three_layer_design.md §1.3

Flow:
  1. ロックアウト確認（Layer 1 / CommandGate）→ lockout中は即終了
  2. 設定読み込み (layer3_config.yaml + system_prompt.txt)
  3. 直近判断履歴読み込み (control_log.db → 直近3件)
  4. 日の出/日没計算 + 時間帯注入 (astral)
  5. Claude Haiku API 呼び出し (tool calling: get_sensors, get_status のみ)
  6. 応答からアクション計画抽出 + スキーマ検証
  7. current_plan.json 書き込み
  8. 判断ログ保存 (control_log.db INSERT)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger("forecast_engine")

_JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# デフォルト設定（layer3_config.yaml で上書き）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "claude": {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "max_tool_rounds": 5,
        "api_timeout_sec": 30.0,
    },
    "system_prompt_path": "/etc/agriha/system_prompt.txt",
    "db": {
        "path": "/var/lib/agriha/control_log.db",
        "history_count": 3,
    },
    "state": {
        "plan_path": "/var/lib/agriha/current_plan.json",
        "last_decision_path": "/var/lib/agriha/last_decision.json",
        "lockout_path": "/var/lib/agriha/lockout_state.json",
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

# ---------------------------------------------------------------------------
# ツール定義 (Anthropic tools 形式) — set_relay は除外
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_sensors",
        "description": (
            "全センサーデータ取得。"
            "CCM内気象(温度/湿度/CO2) + DS18B20 + Misol外気象(気温/風速/風向/降雨) "
            "+ リレー状態を返す。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_status",
        "description": (
            "デーモン状態取得。"
            "リレー状態(ch1-8 ON/OFF) + ロックアウト状態 + 稼働時間を返す。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# ロックアウト判定 (設計書 §2.1)
# ---------------------------------------------------------------------------

def is_layer1_locked(path: str | Path) -> bool:
    """Layer 1 ロックアウト中かどうか判定する。"""
    try:
        with open(path) as f:
            data = json.load(f)
        until = datetime.fromisoformat(data.get("layer1_lockout_until", ""))
        return datetime.now(_JST) < until
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return False


def is_commandgate_locked(
    http_client: httpx.Client, base_url: str, api_key: str
) -> bool:
    """CommandGate ロックアウト中かどうか REST API で確認する。"""
    try:
        headers: dict[str, str] = {}
        if api_key:
            headers["X-API-Key"] = api_key
        r = http_client.get(f"{base_url}/api/status", headers=headers, timeout=5)
        return r.json().get("locked_out", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DB ヘルパー (agriha_control.py から再利用)
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
# REST API 呼び出し (set_relay 除外)
# ---------------------------------------------------------------------------

def call_tool(
    http_client: httpx.Client,
    base_url: str,
    api_key: str,
    name: str,
    _args: dict[str, Any],
) -> str:
    """ツール名に応じて unipi-daemon REST API を呼ぶ (読み取り専用)。"""
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

    return json.dumps({"error": f"unknown tool: {name}"})


# ---------------------------------------------------------------------------
# 日の出/日没計算 (astral)
# ---------------------------------------------------------------------------

def get_sun_times(
    lat: float, lon: float, elevation: float, dt: datetime | None = None
) -> dict[str, datetime]:
    """astralで日の出/日没を計算する。"""
    from astral import LocationInfo
    from astral.sun import sun

    loc = LocationInfo(latitude=lat, longitude=lon)
    loc.timezone = "Asia/Tokyo"
    date = (dt or datetime.now(_JST)).date()
    s = sun(loc.observer, date=date, tzinfo=_JST)
    s["elevation"] = elevation
    return s


def get_time_period(
    now: datetime, sun_times: dict[str, datetime]
) -> str:
    """現在時刻から時間帯4区分を返す。"""
    sunrise = sun_times["sunrise"]
    sunset = sun_times["sunset"]

    if now < sunrise:
        return "pre_dawn"
    elif now < sunrise + timedelta(hours=2):
        return "morning"
    elif now < sunset - timedelta(hours=1):
        return "daytime"
    else:
        return "evening"


# ---------------------------------------------------------------------------
# アクション計画のスキーマ検証 (MEDIUM-3 対応)
# ---------------------------------------------------------------------------

def validate_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """アクション計画の各アクションをバリデーションする。

    不正なアクションはスキップ（ログ記録）、duration_sec > 3600 は切り詰め。
    """
    validated = []
    for i, action in enumerate(actions):
        relay_ch = action.get("relay_ch")
        if not isinstance(relay_ch, int) or relay_ch < 1 or relay_ch > 8:
            logger.warning(
                "Action[%d] skipped: relay_ch=%r out of range [1,8]", i, relay_ch
            )
            continue

        value = action.get("value")
        if value not in (0, 1):
            logger.warning(
                "Action[%d] skipped: value=%r not in [0,1]", i, value
            )
            continue

        duration_sec = action.get("duration_sec", 0)
        if not isinstance(duration_sec, (int, float)):
            logger.warning(
                "Action[%d] skipped: duration_sec=%r not numeric", i, duration_sec
            )
            continue
        if duration_sec > 3600:
            logger.warning(
                "Action[%d]: duration_sec=%d exceeds 3600, clamping", i, duration_sec
            )
            duration_sec = 3600

        execute_at = action.get("execute_at", "")
        try:
            datetime.fromisoformat(str(execute_at))
        except (ValueError, TypeError):
            logger.warning(
                "Action[%d] skipped: execute_at=%r not valid ISO8601", i, execute_at
            )
            continue

        validated.append({
            "execute_at": execute_at,
            "relay_ch": relay_ch,
            "value": value,
            "duration_sec": duration_sec,
            "reason": action.get("reason", ""),
            "executed": False,
        })
    return validated


def extract_plan_json(text: str) -> dict[str, Any] | None:
    """LLM応答テキストからJSONブロックを抽出する。"""
    # ```json ... ``` ブロック
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 生JSON
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# メイン: 1時間予報生成
# ---------------------------------------------------------------------------

def run_forecast(
    config: dict[str, Any] | None = None,
    *,
    anthropic_client: Any = None,
    http_client: Any = None,
) -> dict[str, Any]:
    """メイン予報生成ループ（1回実行、cron から呼ばれる）。

    Args:
        config: 設定辞書。None の場合は DEFAULT_CONFIG を使用。
        anthropic_client: anthropic.Anthropic クライアント（テスト用 DI）。
        http_client: unipi-daemon REST API 用 HTTP クライアント（テスト用 DI）。

    Returns:
        結果辞書。lockout中は {"status": "skipped", "reason": ...}。
        正常時は {"status": "ok", "plan_path": ..., "summary": ...}。
    """
    cfg = _merge_config(DEFAULT_CONFIG, config or {})

    claude_cfg = cfg["claude"]
    db_cfg = cfg["db"]
    state_cfg = cfg["state"]
    unipi_cfg = cfg["unipi_api"]
    loc_cfg = cfg["location"]

    base_url = unipi_cfg["base_url"]
    api_key = unipi_cfg.get("api_key", "")

    own_http = False
    if http_client is None:
        http_client = httpx.Client(timeout=unipi_cfg.get("timeout_sec", 10))
        own_http = True

    try:
        # Step 1: ロックアウト確認 (殿裁定 MAJOR-2)
        lockout_path = state_cfg.get("lockout_path", "/var/lib/agriha/lockout_state.json")
        if is_layer1_locked(lockout_path):
            logger.info("Layer 1 lockout active — skipping forecast generation")
            return {"status": "skipped", "reason": "layer1_lockout"}

        if is_commandgate_locked(http_client, base_url, api_key):
            logger.info("CommandGate lockout active — skipping forecast generation")
            return {"status": "skipped", "reason": "commandgate_lockout"}

        # Step 2: 設定読み込み
        prompt_path = Path(cfg["system_prompt_path"])
        if prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = (
                "あなたは温室環境制御AIです。"
                "センサーデータを確認し、向こう1時間のアクション計画をJSON形式で提案してください。"
            )
            logger.warning("system_prompt not found: %s (using default)", prompt_path)

        # Step 3: 判断履歴
        db = init_db(db_cfg["path"])
        try:
            history = load_recent_history(db, n=db_cfg.get("history_count", 3))
        finally:
            pass  # db kept open for Step 8

        # Step 4: 日の出/日没計算 + 時間帯
        now = datetime.now(_JST)
        try:
            sun_times = get_sun_times(
                loc_cfg["latitude"], loc_cfg["longitude"], loc_cfg.get("elevation", 0),
                dt=now,
            )
            time_period = get_time_period(now, sun_times)
            sunrise_str = sun_times["sunrise"].strftime("%H:%M")
            sunset_str = sun_times["sunset"].strftime("%H:%M")
        except Exception as exc:
            logger.warning("astral calculation failed: %s", exc)
            time_period = "unknown"
            sunrise_str = "N/A"
            sunset_str = "N/A"

        # Step 5: Claude Haiku API 呼び出し
        if anthropic_client is None:
            import anthropic
            anthropic_client = anthropic.Anthropic(
                timeout=claude_cfg.get("api_timeout_sec", 30.0),
            )

        user_message = (
            f"## 直近の判断履歴\n{history}\n\n"
            f"## 現在の状況\n"
            f"現在時刻: {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"時間帯: {time_period}\n"
            f"日の出: {sunrise_str} / 日没: {sunset_str}\n\n"
            f"## 指示\n"
            f"ツールを使ってセンサーデータとリレー状態を確認し、"
            f"向こう1時間のアクション計画をJSON形式で生成してください。\n"
            f"リレー操作は行わないでください。計画のみ生成してください。"
        )

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message},
        ]

        sensor_snapshot = ""
        max_rounds = claude_cfg.get("max_tool_rounds", 5)
        final_text = ""

        try:
            for round_num in range(max_rounds):
                response = anthropic_client.messages.create(
                    model=claude_cfg["model"],
                    max_tokens=claude_cfg.get("max_tokens", 1024),
                    system=system_prompt,
                    tools=TOOLS,
                    messages=messages,
                )

                # レスポンス解析
                assistant_content = response.content
                has_tool_use = any(
                    block.type == "tool_use" for block in assistant_content
                )

                # アシスタントメッセージを追加
                messages.append({"role": "assistant", "content": assistant_content})

                if not has_tool_use:
                    # 最終テキスト応答
                    for block in assistant_content:
                        if block.type == "text":
                            final_text = block.text
                    break

                # ツール呼び出し処理
                tool_results = []
                for block in assistant_content:
                    if block.type != "tool_use":
                        continue
                    tool_name = block.name
                    tool_input = block.input or {}
                    logger.info(
                        "Tool call [round %d]: %s", round_num, tool_name
                    )
                    try:
                        result_text = call_tool(
                            http_client, base_url, api_key,
                            tool_name, tool_input,
                        )
                    except Exception as exc:
                        logger.error("Tool call failed: %s: %s", tool_name, exc)
                        result_text = json.dumps(
                            {"error": str(exc)}, ensure_ascii=False
                        )

                    if tool_name in ("get_sensors", "get_status"):
                        sensor_snapshot += f"\n--- {tool_name} ---\n{result_text}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

                messages.append({"role": "user", "content": tool_results})

                # end_turn チェック
                if response.stop_reason == "end_turn":
                    for block in assistant_content:
                        if block.type == "text":
                            final_text = block.text
                    break

        except Exception as exc:
            logger.error("Claude API error: %s", exc)
            return {
                "status": "error",
                "reason": f"api_error: {exc}",
            }

        # Step 6: 計画 JSON 抽出 + スキーマ検証
        plan_path = Path(state_cfg["plan_path"])
        plan_data = extract_plan_json(final_text)

        if plan_data and "actions" in plan_data:
            validated_actions = validate_actions(plan_data.get("actions", []))
            plan_output = {
                "generated_at": now.isoformat(),
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "summary": plan_data.get("summary", final_text[:200]),
                "actions": validated_actions,
                "co2_advisory": plan_data.get("co2_advisory", ""),
                "dewpoint_risk": plan_data.get("dewpoint_risk", "unknown"),
                "next_check_note": plan_data.get("next_check_note", ""),
            }
        else:
            logger.warning("No valid plan JSON in LLM response, writing empty plan")
            plan_output = {
                "generated_at": now.isoformat(),
                "valid_until": (now + timedelta(hours=1)).isoformat(),
                "summary": final_text[:200] if final_text else "No plan generated",
                "actions": [],
                "co2_advisory": "",
                "dewpoint_risk": "unknown",
                "next_check_note": "",
            }

        # Step 7: current_plan.json 書き込み
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(plan_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Plan written to %s (%d actions)", plan_path, len(plan_output["actions"]))

        # Step 8: 判断ログ保存
        actions_summary = "; ".join(
            f"ch{a['relay_ch']}={'ON' if a['value'] else 'OFF'} @{a['execute_at']}"
            for a in plan_output["actions"]
        ) or "現状維持"

        save_decision(
            db,
            summary=plan_output["summary"][:500],
            actions=actions_summary,
            raw_response=final_text[:2000] if final_text else "",
            sensor_snapshot=sensor_snapshot[:2000],
        )

        # last_decision.json 更新
        last_decision_path = Path(state_cfg["last_decision_path"])
        last_decision_path.parent.mkdir(parents=True, exist_ok=True)
        last_decision_path.write_text(
            json.dumps({
                "timestamp": now.isoformat(),
                "summary": plan_output["summary"],
                "actions_count": len(plan_output["actions"]),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        db.close()

        return {
            "status": "ok",
            "plan_path": str(plan_path),
            "summary": plan_output["summary"][:200],
            "actions_count": len(plan_output["actions"]),
        }

    finally:
        if own_http:
            http_client.close()


def _merge_config(base: dict, override: dict) -> dict:
    """ネストされた辞書をマージする。"""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge_config(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI エントリポイント。cron から呼ばれる。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = dict(DEFAULT_CONFIG)
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        config_path = Path(sys.argv[2])
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
            config = _merge_config(config, user_config)

    result = run_forecast(config)
    print(f"Result: {result}")


if __name__ == "__main__":
    main()
