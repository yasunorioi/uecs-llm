"""automation_shadow.py — shadow-run adapter for automation_interpreter.

現行 `evaluate_rules()` と並行して interpreter を走らせ、action 一致を検証する。
実際の relay 制御は evaluate_rules が担当、interpreter は **read-only、log-only**。

`rule_engine.run()` から環境変数 `AGRIHA_SHADOW_RUN=1` が立っているときのみ呼ばれる。
interpreter が何らかの理由で失敗しても rule_engine を巻き込まない (non-fatal)。

副作用:
  - `/var/lib/agriha/shadow_log.jsonl` に 1 行追記
  - `/var/lib/agriha/shadow_active_state.json` にヒステリシス state を書き戻す
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from agriha.control.automation_interpreter import (
    Interpreter,
    Result as InterpResult,
    load_automations,
)

logger = logging.getLogger("automation_shadow")
_JST = ZoneInfo("Asia/Tokyo")

# ══════════════════════════════════════════════
# デフォルトパス
# ══════════════════════════════════════════════

DEFAULT_SCHEMA_PATH = os.environ.get(
    "AUTOMATIONS_SCHEMA_PATH", "/etc/agriha/automations.yaml"
)
DEFAULT_ACTIVE_STATE_PATH = os.environ.get(
    "SHADOW_ACTIVE_STATE_PATH", "/var/lib/agriha/shadow_active_state.json"
)
DEFAULT_SHADOW_LOG_PATH = os.environ.get(
    "SHADOW_LOG_PATH", "/var/lib/agriha/shadow_log.jsonl"
)


# ══════════════════════════════════════════════
# I/O helpers
# ══════════════════════════════════════════════

def load_schema(schema_path: str = DEFAULT_SCHEMA_PATH) -> list[dict[str, Any]]:
    """automations.yaml から automation リストを読む。missing ならば []。"""
    p = Path(schema_path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("automations", [])


def load_active_state(path: str = DEFAULT_ACTIVE_STATE_PATH) -> set[str]:
    """前回の active automation ID set を読む。missing / corrupt なら empty。"""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        with open(p, encoding="utf-8") as f:
            return set(json.load(f).get("active", []))
    except Exception as e:
        logger.warning("failed to load shadow active state: %s", e)
        return set()


def save_active_state(
    active: set[str], path: str = DEFAULT_ACTIVE_STATE_PATH
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(
            {
                "active": sorted(active),
                "saved_at": datetime.now(_JST).isoformat(),
            },
            f,
        )


# ══════════════════════════════════════════════
# rule_engine の生センサ辞書 → interpreter 用 flat センサ dict
# ══════════════════════════════════════════════

def build_sensors_dict(sensors: dict[str, Any]) -> dict[str, Any]:
    """rule_engine の getter helper を経由して interpreter に渡す flat dict を作る。

    None 値はそのまま残す (interpreter の strict=False モードで捌く)。"""
    # rule_engine は httpx を import するので、テスト環境で httpx が無いと ImportError。
    # 実行時 (cron 起動時) は rule_engine.run() から呼ばれるので import 済み前提。
    from agriha.control.rule_engine import (
        get_indoor_co2,
        get_indoor_humidity,
        get_indoor_temp,
        get_insolar,
        get_misol,
        get_outdoor_temp,
    )

    misol = get_misol(sensors)
    return {
        "rainfall": misol.get("rainfall", 0.0) or 0.0,
        "wind_speed": misol.get("wind_speed_ms", 0.0) or 0.0,
        "wind_direction": misol.get("wind_direction", 0) or 0,
        "indoor_temp": get_indoor_temp(sensors),
        "outdoor_temp": get_outdoor_temp(sensors),
        "indoor_humidity": get_indoor_humidity(sensors),
        "indoor_co2": get_indoor_co2(sensors),
        "insolar": get_insolar(sensors),
    }


# ══════════════════════════════════════════════
# Primitives registry: rule_engine helper を lambda で wrap
# ══════════════════════════════════════════════

def build_primitives(
    cfg: dict[str, Any],
    crop_cfg: dict[str, Any],
    temp_history: list[dict[str, Any]],
    solar_acc: dict[str, Any],
    tide_forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """automation の trigger/action 式が呼ぶ名前付き関数を Python callable として登録。"""
    from agriha.control.rule_engine import (
        get_irrigation_duration,
        get_rise_rate_per_hour,
        get_schedule_target,
        get_solar_threshold,
        get_sun_times,
        get_target_opening,
        is_nighttime,
    )

    def _tide_1h() -> float | None:
        if not tide_forecast:
            return None
        preds = tide_forecast.get("predictions", {}).get("InAirHumid", [])
        return preds[0] if preds else None

    def _window_step(outdoor_temp: float) -> int:
        step_table = cfg.get("window_step_table", {})
        if not step_table or outdoor_temp is None:
            return 0
        table = {int(k): int(v) for k, v in step_table.items()}
        return get_target_opening(outdoor_temp, table)

    def _sunset_offset_min() -> int:
        sun_times = get_sun_times(cfg)
        sunset = sun_times["sunset"]
        now = datetime.now(_JST)
        return int((sunset - now).total_seconds() / 60.0)

    return {
        "rise_rate": lambda: get_rise_rate_per_hour(temp_history),
        "solar_accumulated_mj": lambda: solar_acc.get("accumulated_mj", 0.0),
        "schedule_period": lambda: get_schedule_target(cfg)[1],
        "schedule_target_temp": lambda: get_schedule_target(cfg)[0],
        "is_night": lambda: is_nighttime(cfg),
        "sunset_offset_min": _sunset_offset_min,
        "tide_forecast_humidity_1h": _tide_1h,
        "window_step_lookup": _window_step,
        "solar_threshold": lambda: get_solar_threshold(crop_cfg),
        "irrigation_duration": lambda: get_irrigation_duration(crop_cfg),
    }


# ══════════════════════════════════════════════
# 比較ロジック
# ══════════════════════════════════════════════

def compare_results(
    interp_result: InterpResult,
    live_result: dict[str, Any],
) -> dict[str, Any]:
    """interpreter の Result と evaluate_rules の返り値 dict を比較し差分を返す。

    比較する主要 output のみ:
      - target_opening_pct
      - close_all_windows (P1 rain)
      - close_by_wind_direction (P2 wind)
      - solar_irrigation fired? (P7)

    triggered の rule 名比較は live 側の "marker rule" (nighttime_close /
    temp_schedule / window_open 等) が noise になるので prototype ではスキップ。
    """
    delta: dict[str, Any] = {}

    interp_opening = interp_result.target_opening_pct
    live_opening = live_result.get("target_opening_pct")
    if interp_opening != live_opening:
        delta["target_opening_pct"] = {
            "interp": interp_opening,
            "live": live_opening,
        }

    live_triggered = set(live_result.get("triggered_rules", []))
    live_close_all = "rain_close_all" in live_triggered
    live_close_by_wind = "strong_wind" in live_triggered

    if interp_result.close_all_windows != live_close_all:
        delta["close_all_windows"] = {
            "interp": interp_result.close_all_windows,
            "live": live_close_all,
        }
    if interp_result.close_by_wind_direction != live_close_by_wind:
        delta["close_by_wind_direction"] = {
            "interp": interp_result.close_by_wind_direction,
            "live": live_close_by_wind,
        }

    interp_solar = "solar_irrigation" in interp_result.triggered
    live_solar = "solar_irrigation" in live_triggered
    if interp_solar != live_solar:
        delta["solar_irrigation"] = {
            "interp": interp_solar,
            "live": live_solar,
        }

    return delta


# ══════════════════════════════════════════════
# jsonl ログ書き
# ══════════════════════════════════════════════

def log_shadow_result(
    interp_result: InterpResult,
    live_result: dict[str, Any],
    delta: dict[str, Any],
    log_path: str = DEFAULT_SHADOW_LOG_PATH,
) -> None:
    """1 サイクル分の結果を jsonl に 1 行追記。"""
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(_JST).isoformat(),
        "match": not delta,
        "interp": {
            "triggered": interp_result.triggered,
            "target_opening_pct": interp_result.target_opening_pct,
            "close_all_windows": interp_result.close_all_windows,
            "close_by_wind_direction": interp_result.close_by_wind_direction,
            "relay_pulses": interp_result.relay_pulses,
            "skipped": interp_result.skipped,
            "active_after": sorted(interp_result.active_after),
        },
        "live": {
            "triggered": live_result.get("triggered_rules", []),
            "target_opening_pct": live_result.get("target_opening_pct"),
            "schedule_period": live_result.get("schedule_period"),
        },
        "delta": delta,
    }
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ══════════════════════════════════════════════
# エントリポイント
# ══════════════════════════════════════════════

def run_shadow(
    cfg: dict[str, Any],
    crop_cfg: dict[str, Any],
    sensors: dict[str, Any],
    solar_acc: dict[str, Any],
    temp_history: list[dict[str, Any]],
    live_result: dict[str, Any],
    tide_forecast: dict[str, Any] | None = None,
    schema_path: str = DEFAULT_SCHEMA_PATH,
    active_state_path: str = DEFAULT_ACTIVE_STATE_PATH,
    log_path: str = DEFAULT_SHADOW_LOG_PATH,
) -> None:
    """rule_engine.run() から呼ばれる shadow-run エントリ。

    非致命: 内部で失敗しても rule_engine を巻き込まないよう全 exception を捕捉する。
    """
    try:
        schema = load_schema(schema_path)
        if not schema:
            logger.info(
                "shadow: no automations at %s → skip", schema_path
            )
            return

        automations = load_automations(schema)
        interp = Interpreter(automations)

        interp_sensors = build_sensors_dict(sensors)
        primitives = build_primitives(
            cfg, crop_cfg, temp_history, solar_acc, tide_forecast
        )
        active_before = load_active_state(active_state_path)

        interp_state: dict[str, Any] = {
            "solar_acc": solar_acc,
            "current_window_pct": live_result.get("target_opening_pct", 0),
        }

        interp_result = interp.evaluate(
            sensors=interp_sensors,
            state=interp_state,
            config=cfg,
            crop_config=crop_cfg,
            primitives=primitives,
            active_before=active_before,
            strict=False,
        )

        delta = compare_results(interp_result, live_result)
        log_shadow_result(interp_result, live_result, delta, log_path)
        save_active_state(interp_result.active_after, active_state_path)

        if delta:
            logger.warning("SHADOW DIVERGENCE: %s", delta)
        else:
            logger.info(
                "SHADOW OK: opening=%d triggered=%s",
                interp_result.target_opening_pct,
                interp_result.triggered,
            )
    except Exception as e:
        logger.warning(
            "shadow_run failed (non-fatal): %s", e, exc_info=True
        )
