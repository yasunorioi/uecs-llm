#!/usr/bin/env python3
"""plan_executor.py — Layer 3 補助: アクション計画実行

cron (* * * * *) から起動。current_plan.json の予定時刻到来アクションを
REST API 経由で実行し、実行済みマークを書き込む。

設計書: docs/v2_three_layer_design.md §1.4
殿裁定: MAJOR-3 — 下層が上層を黙らせる原則（降雨/強風で側窓操作を抑制）
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger("plan_executor")

_JST = ZoneInfo("Asia/Tokyo")

# ---------------------------------------------------------------------------
# デフォルト設定
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "plan_path": "/var/lib/agriha/current_plan.json",
    "lockout_path": "/var/lib/agriha/lockout_state.json",
    "layer2_config_path": "/etc/agriha/layer2_config.yaml",
    "unipi_api": "http://localhost:8080",
    "api_key": "",
    "timeout_sec": 10,
}

# バリデーション定数
RELAY_CH_MIN = 1
RELAY_CH_MAX = 8
DURATION_SEC_MAX = 3600


# ---------------------------------------------------------------------------
# ロックアウト判定
# ---------------------------------------------------------------------------

def is_layer1_locked(path: str | Path) -> bool:
    """Layer 1 ロックアウト中かどうかを判定する。

    ファイルなし or パースエラーの場合は False（ロックアウトなし）を返す。
    """
    try:
        with open(path) as f:
            data = json.load(f)
        until_str = data.get("layer1_lockout_until", "")
        if not until_str:
            return False
        until = datetime.fromisoformat(until_str)
        return datetime.now(_JST) < until
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# Layer 2 設定読み込み（降雨/強風閾値・側窓チャンネル）
# ---------------------------------------------------------------------------

def load_layer2_config(path: str | Path) -> dict[str, Any]:
    """layer2_config.yaml から降雨/強風閾値と側窓チャンネルを読み込む。

    ファイルなし or パースエラー時はデフォルト値を返す（重複定義回避のため
    設計書 §6.2 と同じデフォルト値を使用）。
    """
    defaults: dict[str, Any] = {
        "rainfall_threshold": 0.5,
        "wind_threshold": 5.0,
        "window_channels": [5, 6, 7, 8],
    }
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return {
            "rainfall_threshold": cfg.get("rain", {}).get(
                "threshold_mm_h", defaults["rainfall_threshold"]
            ),
            "wind_threshold": cfg.get("wind", {}).get(
                "strong_wind_threshold_ms", defaults["wind_threshold"]
            ),
            "window_channels": cfg.get("temperature", {}).get(
                "window_channels", defaults["window_channels"]
            ),
        }
    except Exception:
        logger.warning("layer2_config.yaml 読み込み失敗 — デフォルト値を使用")
        return defaults


# ---------------------------------------------------------------------------
# センサーデータから降雨・風速を抽出
# ---------------------------------------------------------------------------

def _extract_weather(sensors: dict[str, Any]) -> tuple[float, float]:
    """センサー辞書から rainfall と wind_speed_ms を抽出する。

    Args:
        sensors: /api/sensors の "sensors" キーの値（MQTT トピック→データの辞書）

    Returns:
        (rainfall, wind_speed_ms) のタプル。値が見つからない場合は 0.0。
    """
    rainfall = 0.0
    wind_speed = 0.0
    for sensor_data in sensors.values():
        if not isinstance(sensor_data, dict):
            continue
        # rainfall: "rainfall" または "rainfall_mm" どちらも対応
        for key in ("rainfall", "rainfall_mm"):
            if key in sensor_data:
                try:
                    rainfall = float(sensor_data[key])
                except (TypeError, ValueError):
                    pass
                break
        # wind_speed: "wind_speed_ms" または "wind_speed" どちらも対応
        for key in ("wind_speed_ms", "wind_speed"):
            if key in sensor_data:
                try:
                    wind_speed = float(sensor_data[key])
                except (TypeError, ValueError):
                    pass
                break
    return rainfall, wind_speed


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def run_executor(
    config: dict[str, Any] | None = None,
    *,
    http_client: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """計画実行メイン処理。

    Args:
        config: 設定辞書。None の場合は DEFAULT_CONFIG を使用。
        http_client: httpx.Client 互換オブジェクト（テスト用 DI）。
        now: 現在時刻（テスト用 DI）。None の場合は JST 現在時刻。

    Returns:
        実行結果サマリ辞書:
            executed:           実行した relay_ch リスト
            skipped_weather:    天候スキップした relay_ch リスト
            skipped_not_due:    未到来でスキップした relay_ch リスト
            skipped_already_done: 実行済みでスキップした relay_ch リスト
            skipped_lockout:    ロックアウトでスキップ（文字列リスト）
            skipped_invalid:    バリデーション失敗でスキップした relay_ch リスト
            no_plan:            計画なし/期限切れフラグ
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    plan_path = Path(cfg["plan_path"])
    lockout_path = Path(cfg["lockout_path"])
    layer2_config_path = Path(cfg["layer2_config_path"])
    base_url = str(cfg["unipi_api"])
    api_key = str(cfg.get("api_key", ""))
    timeout = float(cfg["timeout_sec"])

    _now = now if now is not None else datetime.now(_JST)

    result: dict[str, Any] = {
        "executed": [],
        "skipped_weather": [],
        "skipped_not_due": [],
        "skipped_already_done": [],
        "skipped_lockout": [],
        "skipped_invalid": [],
        "no_plan": False,
    }

    # -----------------------------------------------------------------------
    # Step 1: current_plan.json 読み込み
    # -----------------------------------------------------------------------
    if not plan_path.exists():
        logger.info("current_plan.json なし → 終了")
        result["no_plan"] = True
        return result

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("current_plan.json 読み込みエラー: %s", exc)
        result["no_plan"] = True
        return result

    # valid_until チェック
    try:
        valid_until = datetime.fromisoformat(plan["valid_until"])
        if _now > valid_until:
            logger.info("current_plan.json 期限切れ (valid_until=%s) → 終了", valid_until)
            result["no_plan"] = True
            return result
    except (KeyError, ValueError) as exc:
        logger.error("valid_until パースエラー: %s → 終了", exc)
        result["no_plan"] = True
        return result

    # -----------------------------------------------------------------------
    # Step 2: ロックアウト確認
    # -----------------------------------------------------------------------
    if is_layer1_locked(lockout_path):
        logger.info("Layer 1 ロックアウト中 → 終了")
        result["skipped_lockout"].append("layer1")
        return result

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    _own_client = http_client is None
    if _own_client:
        http_client = httpx.Client(timeout=timeout, headers=headers)

    try:
        # CommandGate ロックアウト確認
        try:
            status_r = http_client.get(f"{base_url}/api/status")
            if status_r.json().get("locked_out", False):
                logger.info("CommandGate ロックアウト中 → 終了")
                result["skipped_lockout"].append("commandgate")
                return result
        except Exception as exc:
            logger.warning("GET /api/status 失敗: %s — ロックアウトなしと見なす", exc)

        # -----------------------------------------------------------------------
        # Step 3: 降雨/強風チェック
        # -----------------------------------------------------------------------
        weather_cfg = load_layer2_config(layer2_config_path)
        rainfall_threshold = float(weather_cfg["rainfall_threshold"])
        wind_threshold = float(weather_cfg["wind_threshold"])
        window_channels = set(weather_cfg["window_channels"])

        rainfall = 0.0
        wind_speed = 0.0
        try:
            sensors_r = http_client.get(f"{base_url}/api/sensors")
            sensors_r.raise_for_status()
            sensors = sensors_r.json().get("sensors", {})
            rainfall, wind_speed = _extract_weather(sensors)
        except Exception as exc:
            logger.warning("GET /api/sensors 失敗: %s — 天候チェックスキップ", exc)

        bad_weather = (rainfall > rainfall_threshold) or (wind_speed > wind_threshold)
        if bad_weather:
            logger.info(
                "降雨/強風検知 (rainfall=%.2f, wind=%.1f) → 側窓操作スキップ",
                rainfall,
                wind_speed,
            )

        # -----------------------------------------------------------------------
        # Step 4 → 6: アクション抽出・実行・更新
        # -----------------------------------------------------------------------
        actions: list[dict[str, Any]] = plan.get("actions", [])
        modified = False

        for action in actions:
            # ---- バリデーション: relay_ch 範囲チェック ----
            relay_ch = action.get("relay_ch")
            if not isinstance(relay_ch, int) or not (RELAY_CH_MIN <= relay_ch <= RELAY_CH_MAX):
                logger.warning("relay_ch=%r 範囲外 [1-8] → スキップ", relay_ch)
                result["skipped_invalid"].append(relay_ch)
                continue

            # ---- executed 済みチェック ----
            executed_val = action.get("executed")
            if executed_val is True or executed_val == "skipped_weather":
                result["skipped_already_done"].append(relay_ch)
                continue

            # ---- execute_at 到来チェック ----
            try:
                execute_at = datetime.fromisoformat(action["execute_at"])
            except (KeyError, ValueError) as exc:
                logger.warning("ch%s: execute_at パースエラー: %s → スキップ", relay_ch, exc)
                result["skipped_invalid"].append(relay_ch)
                continue

            if _now < execute_at:
                result["skipped_not_due"].append(relay_ch)
                continue

            # ---- Step 3: 天候スキップ（側窓のみ）----
            if bad_weather and relay_ch in window_channels:
                logger.info(
                    "ch%s 側窓操作スキップ (rainfall=%.2f > %.2f or wind=%.1f > %.1f)",
                    relay_ch,
                    rainfall,
                    rainfall_threshold,
                    wind_speed,
                    wind_threshold,
                )
                action["executed"] = "skipped_weather"
                result["skipped_weather"].append(relay_ch)
                modified = True
                continue

            # ---- duration_sec クランプ ----
            duration_sec = action.get("duration_sec", 0)
            if duration_sec > DURATION_SEC_MAX:
                logger.warning(
                    "ch%s: duration_sec=%s > %s → %s に切り詰め",
                    relay_ch,
                    duration_sec,
                    DURATION_SEC_MAX,
                    DURATION_SEC_MAX,
                )
                duration_sec = DURATION_SEC_MAX
                action["duration_sec"] = DURATION_SEC_MAX

            # ---- Step 5: アクション実行 ----
            payload = {
                "value": action.get("value", 0),
                "duration_sec": duration_sec,
                "reason": action.get("reason", "plan_executor"),
            }
            try:
                relay_r = http_client.post(
                    f"{base_url}/api/relay/{relay_ch}",
                    json=payload,
                )
                if relay_r.status_code == 423:
                    logger.info("ch%s: 423 ロックアウト → スキップ（次回リトライ）", relay_ch)
                    result["skipped_lockout"].append(f"relay_ch{relay_ch}")
                    continue
                relay_r.raise_for_status()
                logger.info(
                    "ch%s: value=%s duration=%s → 実行完了",
                    relay_ch,
                    payload["value"],
                    duration_sec,
                )
                action["executed"] = True
                result["executed"].append(relay_ch)
                modified = True

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 423:
                    result["skipped_lockout"].append(f"relay_ch{relay_ch}")
                else:
                    logger.error("ch%s: POST エラー %s", relay_ch, exc)
            except Exception as exc:
                logger.error("ch%s: POST エラー %s", relay_ch, exc)

        # ---- Step 6: current_plan.json 更新 ----
        if modified:
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("current_plan.json 更新完了")

        return result

    finally:
        if _own_client:
            http_client.close()


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI エントリポイント。cron から起動される。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config: dict[str, Any] = dict(DEFAULT_CONFIG)

    # オプション: --config <path>
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        cfg_path = Path(sys.argv[2])
        if cfg_path.exists():
            with open(cfg_path) as f:
                user_cfg = yaml.safe_load(f) or {}
            config.update(user_cfg)

    result = run_executor(config)
    logger.info(
        "完了: executed=%s skipped_weather=%s skipped_lockout=%s",
        result["executed"],
        result["skipped_weather"],
        result["skipped_lockout"],
    )


if __name__ == "__main__":
    main()
