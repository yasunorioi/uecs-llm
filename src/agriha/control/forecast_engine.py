#!/usr/bin/env python3
"""Layer 3: LLM 1時間予報エンジン — Claude Haiku + unipi-daemon REST API.

cron (0 * * * *) から起動され、1時間分のアクション計画を生成して終了する。
**リレー操作は一切行わない。** 計画の実行は plan_executor.py が担当。

設計書: docs/v2_three_layer_design.md §1.3

Flow:
  1. ロックアウト確認（Layer 1 / CommandGate）→ lockout中は即終了
  2. 設定読み込み (forecast.yaml + system_prompt.txt)
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
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger("forecast_engine")

_JST = ZoneInfo("Asia/Tokyo")

VC_CACHE_PATH = os.environ.get("VC_CACHE_PATH", "/tmp/vc_cache.json")
VC_CACHE_TTL = 3600  # TTL: 1時間（秒）

# ---------------------------------------------------------------------------
# デフォルト設定（forecast.yaml で上書き）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "nullclaw",
        "model": "nullclaw-local",
        "base_url": "http://localhost:3001/v1/",
        "api_key_env": "NULLCLAW_API_KEY",
        "max_tokens": 1024,
        "max_tool_rounds": 5,
        "api_timeout_sec": 30.0,
    },
    # 後方互換性: "claude" キーが渡された場合は "llm" にマージされる
    "claude": {},
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
# ツール定義 (OpenAI tools 形式) — set_relay は除外
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
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
    db.execute("""CREATE TABLE IF NOT EXISTS reflection_memo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        source_candidate_id TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        context TEXT NOT NULL,
        answer TEXT,
        answered_at TEXT,
        promoted_to_rule INTEGER DEFAULT 0,
        expired INTEGER DEFAULT 0
    )""")
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
# Visual Crossing API 連携 / キャッシュ / 接続確認 / 検索クエリ (§3.3)
# ---------------------------------------------------------------------------

def fetch_weather_forecast(
    lat: float = 42.888, lon: float = 141.603
) -> dict[str, Any] | None:
    """Visual Crossing Timeline API から24時間天気予報を取得する。

    Returns:
        APIレスポンスのdict、またはNone（キー未設定・エラー時）。
    """
    api_key = os.environ.get("VC_API_KEY", "")
    if not api_key:
        logger.warning("VC_API_KEY未設定、天気予報スキップ")
        return None
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
        f"timeline/{lat},{lon}?unitGroup=metric&include=hours&key={api_key}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except urllib.error.HTTPError as exc:
        logger.warning("VC API HTTP error: %s %s", exc.code, exc.reason)
        return None
    except Exception as exc:
        logger.warning("VC API fetch failed: %s", exc)
        return None


def get_weather_with_cache(
    lat: float = 42.888, lon: float = 141.603
) -> dict[str, Any] | None:
    """キャッシュ付き天気予報取得。TTL 1時間。

    1. キャッシュ確認（TTL内なら返却）
    2. API呼び出し
    3. 成功 → キャッシュ更新して返却
    4. 失敗 → 古いキャッシュにフォールバック返却
    5. キャッシュもなし → None返却
    """
    cache_path = Path(VC_CACHE_PATH)

    # 1. キャッシュ確認
    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_at_str = cached.get("_cached_at", "")
            if cached_at_str:
                cached_at = datetime.fromisoformat(cached_at_str)
                age = datetime.now(timezone.utc) - cached_at.astimezone(timezone.utc)
                if age.total_seconds() < VC_CACHE_TTL:
                    logger.debug("VC cache hit (age=%.0fs)", age.total_seconds())
                    return cached
    except Exception:
        pass  # キャッシュ読み込み失敗は無視してAPI呼び出しへ

    # 2. API呼び出し
    data = fetch_weather_forecast(lat, lon)

    if data is not None:
        # 3. キャッシュ更新
        data["_cached_at"] = datetime.now(timezone.utc).isoformat()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("VC cache write failed: %s", exc)
        return data

    # 4. API失敗 → 古いキャッシュにフォールバック
    try:
        if cache_path.exists():
            logger.warning("VC API failed, using stale cache")
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    # 5. キャッシュもなし
    return None


def build_search_query(
    sensor_data: dict[str, Any],
    weather_data: dict[str, Any] | None,
) -> str:
    """高札API検索用クエリを生成する。

    形式: ``{季節}_{時間帯}_{温度バンド}_{天気}``

    - 季節: spring(3-5月) / summer(6-8月) / autumn(9-11月) / winter(12-2月)
    - 時間帯: dawn(4-8h) / morning(8-12h) / afternoon(12-17h) / evening(17-21h) / night
    - 温度バンド: cold(<5℃) / cool(5-15℃) / warm(15-25℃) / hot(≥25℃)
    - 天気: clear / cloudy / rain / snow / unknown
    """
    now = datetime.now(_JST)

    # 季節（月で判定）
    month = now.month
    if month in (3, 4, 5):
        season = "spring"
    elif month in (6, 7, 8):
        season = "summer"
    elif month in (9, 10, 11):
        season = "autumn"
    else:
        season = "winter"

    # 時間帯（時で判定）
    hour = now.hour
    if 4 <= hour < 8:
        time_of_day = "dawn"
    elif 8 <= hour < 12:
        time_of_day = "morning"
    elif 12 <= hour < 17:
        time_of_day = "afternoon"
    elif 17 <= hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "night"

    # 外気温バンド（sensorデータ優先、なければweather_data）
    outside_temp: float | None = None
    try:
        raw = sensor_data.get("misol", {}).get("temperature_c")
        if raw is None and weather_data:
            raw = (weather_data.get("currentConditions") or {}).get("temp")
        if raw is not None:
            outside_temp = float(raw)
    except (TypeError, ValueError):
        pass

    if outside_temp is None:
        temp_band = "unknown"
    elif outside_temp < 5.0:
        temp_band = "cold"
    elif outside_temp < 15.0:
        temp_band = "cool"
    elif outside_temp < 25.0:
        temp_band = "warm"
    else:
        temp_band = "hot"

    # 天気（weather_dataのconditions文字列から判定）
    weather = "unknown"
    if weather_data:
        conditions = (
            (weather_data.get("currentConditions") or {}).get("conditions") or ""
        ).lower()
        if any(w in conditions for w in ("rain", "drizzle", "shower")):
            weather = "rain"
        elif any(w in conditions for w in ("snow", "sleet")):
            weather = "snow"
        elif any(w in conditions for w in ("cloud", "overcast")):
            weather = "cloudy"
        elif any(w in conditions for w in ("clear", "sunny", "sun")):
            weather = "clear"

    return f"{season}_{time_of_day}_{temp_band}_{weather}"


def check_connectivity(host: str = "8.8.8.8", timeout: int = 3) -> bool:
    """インターネット接続確認（Starlink断時のフォールバック用）。

    Args:
        host: pingの宛先ホスト。
        timeout: タイムアウト秒数。

    Returns:
        True: 疎通OK / False: 疎通NG / 例外時もFalse。
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            capture_output=True,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# NullClawFallbackClient — APIキー未設定/オフライン時のフォールバック (§3.3)
# ---------------------------------------------------------------------------


class NullClawFallbackClient:
    """OpenAI SDK互換クライアントラッパー。

    APIキー未設定またはオフライン時はNullClaw (localhost:3001/v1) にフォールバックする。
    毎回check_connectivity()で判定するため、回線復帰時は自動でAPI優先に戻る。
    """

    def __init__(
        self,
        primary_client: Any | None,
        nullclaw_base_url: str = "http://localhost:3001/v1/",
        timeout: float = 30.0,
    ) -> None:
        self._primary = primary_client
        self._nullclaw_base_url = nullclaw_base_url
        self._timeout = timeout
        self._using_fallback = False

    def _get_nullclaw_client(self) -> Any:
        from openai import OpenAI  # type: ignore[import]
        return OpenAI(base_url=self._nullclaw_base_url, api_key="local", timeout=self._timeout)

    @property
    def chat(self) -> "NullClawFallbackClient":
        return self

    @property
    def completions(self) -> "NullClawFallbackClient":
        return self

    def create(self, **kwargs: Any) -> Any:
        if self._primary is None:
            # APIキー未設定 → NullClaw直行
            self._using_fallback = True
            fallback_kwargs = {k: v for k, v in kwargs.items() if k != "tools"}
            return self._get_nullclaw_client().chat.completions.create(**fallback_kwargs)

        try:
            if not check_connectivity():
                raise ConnectionError("オフライン検知")
            result = self._primary.chat.completions.create(**kwargs)
            self._using_fallback = False
            return result
        except Exception as exc:
            logger.warning("API失敗→NullClawフォールバック: %s", exc)
            self._using_fallback = True
            fallback_kwargs = {k: v for k, v in kwargs.items() if k != "tools"}
            return self._get_nullclaw_client().chat.completions.create(**fallback_kwargs)


# ---------------------------------------------------------------------------
# 高札API検索 + LLMスキップ判定 (§3.3 + §7)
# ---------------------------------------------------------------------------

KOUSATSU_URL = os.environ.get("KOUSATSU_URL", "http://localhost:8080")
SEARCH_LOG_PATH = os.environ.get("SEARCH_LOG_PATH", "/var/lib/agriha/search_log.jsonl")
PID_OVERRIDE_PATH = os.environ.get("PID_OVERRIDE_PATH", "/var/lib/agriha/pid_override.json")


def search_kousatsu(query: str, timeout: int = 5) -> dict[str, Any]:
    """高札APIで過去の類似判断を検索する。

    Args:
        query: 検索クエリ文字列。
        timeout: タイムアウト秒数。

    Returns:
        {"total_hits": N, "results": [...]} 形式の辞書。
        エラー時は {"total_hits": 0, "results": []}。
    """
    empty: dict[str, Any] = {"total_hits": 0, "results": []}
    try:
        url = f"{KOUSATSU_URL}/search?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as exc:
        logger.warning("高札API検索失敗 (query=%r): %s", query, exc)
        return empty


def should_skip_llm(search_results: dict[str, Any], threshold: int = 3) -> bool:
    """類似判断が閾値以上あればLLMをスキップする。

    Args:
        search_results: search_kousatsu() の戻り値。
        threshold: スキップする最小ヒット数（デフォルト3件）。

    Returns:
        True: LLMスキップ / False: LLM呼び出し継続。
    """
    return int(search_results.get("total_hits", 0)) >= threshold


def build_plan_from_search_results(
    search_results: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """高札検索結果からcurrent_plan.json形式のplanを生成する。

    最スコア順（rank=1）のsnippetからJSONブロックを抽出し、
    validate_actionsでアクションを検証して返す。
    抽出失敗時は空アクションのplanを返す。

    Args:
        search_results: search_kousatsu() の戻り値。
        now: 現在時刻（テスト用DI）。

    Returns:
        current_plan.json形式の辞書。
    """
    _now = now if now is not None else datetime.now(_JST)
    results = search_results.get("results", [])
    # rankでソート（rank=1が最高）
    sorted_results = sorted(results, key=lambda r: r.get("rank", 999))

    actions: list[dict[str, Any]] = []
    summary = "高札検索結果から生成"
    for item in sorted_results:
        snippet = item.get("snippet", "")
        plan_data = extract_plan_json(snippet)
        if plan_data and "actions" in plan_data:
            actions = validate_actions(plan_data["actions"])
            summary = plan_data.get("summary", summary)
            break

    return {
        "generated_at": _now.isoformat(),
        "valid_until": (_now + timedelta(hours=1)).isoformat(),
        "summary": summary,
        "actions": actions,
        "co2_advisory": "",
        "dewpoint_risk": "unknown",
        "next_check_note": "高札検索によりLLMスキップ",
    }


def convert_llm_to_pid_override(plan: dict[str, Any]) -> None:
    """plan内のdewpoint_riskをPIDオーバーライドパラメータに変換してファイルに書き込む。

    dewpoint_riskが"high"の場合、湿度上限を下げるPIDオーバーライドを生成する。
    ファイル書き込みエラーは警告ログのみで継続（サイレント失敗）。

    Args:
        plan: current_plan.json形式の辞書。
    """
    dewpoint_risk = plan.get("dewpoint_risk", "unknown")
    humidity_max = 80 if dewpoint_risk == "high" else 90

    co2_map = {"ventilate": 400, "accumulate": 700, "neutral": 550}
    co2_setpoint = co2_map.get(plan.get("co2_mode", "neutral"), 550)

    override = {
        "generated_at": datetime.now(_JST).isoformat(),
        "humidity_max": humidity_max,
        "dewpoint_risk": dewpoint_risk,
        "co2_setpoint": co2_setpoint,
    }
    try:
        pid_path = Path(PID_OVERRIDE_PATH)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            json.dumps(override, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("pid_override.json 更新: humidity_max=%s", humidity_max)
    except Exception as exc:
        logger.warning("pid_override.json 書き込み失敗: %s", exc)


def log_search(
    query: str,
    hits: int,
    skipped_llm: bool,
    plan_source: str,
) -> None:
    """検索ログをsearch_log.jsonlに追記する。

    Args:
        query: 検索クエリ文字列。
        hits: ヒット件数。
        skipped_llm: LLMをスキップしたか。
        plan_source: "kousatsu" または "llm"。
    """
    entry = {
        "timestamp": datetime.now(_JST).isoformat(),
        "query": query,
        "hits": hits,
        "skipped_llm": skipped_llm,
        "source": plan_source,
    }
    try:
        log_path = Path(SEARCH_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("search_log.jsonl 書き込み失敗: %s", exc)


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
    llm_client: Any = None,
    anthropic_client: Any = None,  # 後方互換性エイリアス
    http_client: Any = None,
) -> dict[str, Any]:
    """メイン予報生成ループ（1回実行、cron から呼ばれる）。

    Args:
        config: 設定辞書。None の場合は DEFAULT_CONFIG を使用。
        llm_client: OpenAI互換クライアント（テスト用 DI）。
        anthropic_client: 後方互換性のためのエイリアス。llm_client が None の場合に使用。
        http_client: unipi-daemon REST API 用 HTTP クライアント（テスト用 DI）。

    Returns:
        結果辞書。lockout中は {"status": "skipped", "reason": ...}。
        正常時は {"status": "ok", "plan_path": ..., "summary": ...}。
    """
    cfg = _merge_config(DEFAULT_CONFIG, config or {})

    # 後方互換性: cfg["claude"] が設定されていれば cfg["llm"] にマージ
    if cfg.get("claude"):
        cfg["llm"] = _merge_config(cfg["llm"], cfg["claude"])

    # 後方互換性: anthropic_client エイリアスをサポート
    if llm_client is None and anthropic_client is not None:
        llm_client = anthropic_client

    llm_cfg = cfg["llm"]
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

        # Step 4.5: 高札API検索 + LLMスキップ判定 (§3.3)
        try:
            sensor_resp = http_client.get(
                f"{base_url}/api/sensors",
                timeout=unipi_cfg.get("timeout_sec", 10),
            )
            _sensor_data: dict[str, Any] = sensor_resp.json().get("sensors", {})
        except Exception as exc:
            logger.warning("センサー先行取得失敗 (検索クエリ生成用): %s", exc)
            _sensor_data = {}
        _weather_data = get_weather_with_cache(
            lat=loc_cfg.get("latitude", 42.888),
            lon=loc_cfg.get("longitude", 141.603),
        )
        search_query = build_search_query(_sensor_data, _weather_data)
        search_results = search_kousatsu(search_query)
        skip_llm = should_skip_llm(search_results)
        plan_source = "kousatsu" if skip_llm else "llm"
        logger.info(
            "高札検索: query=%r hits=%d skip_llm=%s",
            search_query,
            search_results.get("total_hits", 0),
            skip_llm,
        )

        sensor_snapshot = ""
        final_text = ""

        if skip_llm:
            # Step 5 スキップ: 高札検索結果からplan生成
            logger.info("LLMスキップ — 高札検索結果からplan生成")
            plan_output = build_plan_from_search_results(search_results, now=now)

        else:
            # Step 5: LLM API 呼び出し (OpenAI SDK互換 + NullClawフォールバック)
            if llm_client is None:
                from openai import OpenAI  # type: ignore[import]

                api_key = os.environ.get(llm_cfg.get("api_key_env", "NULLCLAW_API_KEY"), "")
                nullclaw_base_url = "http://localhost:3001/v1/"
                timeout = llm_cfg.get("api_timeout_sec", 30.0)

                if api_key:
                    # APIキーあり → API優先クライアントを作成しフォールバックラッパーで包む
                    client_kwargs: dict[str, Any] = {
                        "api_key": api_key,
                        "timeout": timeout,
                    }
                    if llm_cfg.get("base_url") and llm_cfg["base_url"] != nullclaw_base_url:
                        client_kwargs["base_url"] = llm_cfg["base_url"]
                    primary = OpenAI(**client_kwargs)
                else:
                    primary = None  # APIキー未設定 → NullClaw直行

                llm_client = NullClawFallbackClient(
                    primary_client=primary,
                    nullclaw_base_url=nullclaw_base_url,
                    timeout=timeout,
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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            max_rounds = llm_cfg.get("max_tool_rounds", 5)

            try:
                for round_num in range(max_rounds):
                    response = llm_client.chat.completions.create(
                        model=llm_cfg["model"],
                        max_tokens=llm_cfg.get("max_tokens", 1024),
                        tools=TOOLS,
                        messages=messages,
                    )

                    # レスポンス解析
                    choice = response.choices[0]
                    msg = choice.message
                    has_tool_calls = bool(msg.tool_calls)

                    # アシスタントメッセージを追加
                    messages.append(msg.model_dump(exclude_unset=False))

                    if not has_tool_calls:
                        # 最終テキスト応答
                        final_text = msg.content or ""
                        break

                    # ツール呼び出し処理
                    for tc in msg.tool_calls:
                        tool_name = tc.function.name
                        try:
                            tool_input = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            tool_input = {}
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

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })

                    # stop チェック
                    if choice.finish_reason == "stop":
                        final_text = msg.content or ""
                        break

            except Exception as exc:
                logger.error("LLM API error: %s", exc)
                return {
                    "status": "error",
                    "reason": f"api_error: {exc}",
                }

            # Step 6: 計画 JSON 抽出 + スキーマ検証
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
        plan_path = Path(state_cfg["plan_path"])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(plan_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Plan written to %s (%d actions)", plan_path, len(plan_output["actions"]))

        # Step 7.5: 検索ログ + PIDオーバーライド更新
        log_search(
            query=search_query,
            hits=search_results.get("total_hits", 0),
            skipped_llm=skip_llm,
            plan_source=plan_source,
        )
        convert_llm_to_pid_override(plan_output)

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
