"""
rule_engine.py — Layer 2: ガムテ制御

cron 5分毎に実行。ルールベースで側窓・灌水を制御する。
設計書: v2_three_layer_design.md §1.2, §6.2, §6.4

依存: httpx, pyyaml, astral
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml
from astral import LocationInfo
from astral.sun import sun

from agriha.control.channel_config import load_channel_map, load_window_groups, get_window_channels
from agriha.control.retry_helper import RETRY_DELAYS_LOCAL_SEC, retry_with_backoff
from agriha.control.window_position import (
    load_position, save_position, update_position,
    calibrate_closed, compute_move,
)

# ──────────────────────────────────────────────
# 定数・デフォルトパス（環境変数で上書き可能）
# ──────────────────────────────────────────────
_JST = ZoneInfo("Asia/Tokyo")

DEFAULT_CONFIG_PATH = os.environ.get(
    "RULES_CONFIG_PATH", "/etc/agriha/rules.yaml"
)
DEFAULT_CROP_CONFIG_PATH = os.environ.get(
    "CROP_IRRIGATION_PATH", "/etc/agriha/crop_irrigation.yaml"
)
DEFAULT_LOCKOUT_PATH = os.environ.get(
    "LOCKOUT_STATE_PATH", "/var/lib/agriha/lockout_state.json"
)
DEFAULT_PLAN_PATH = os.environ.get(
    "CURRENT_PLAN_PATH", "/var/lib/agriha/current_plan.json"
)
DEFAULT_SOLAR_ACC_PATH = os.environ.get(
    "SOLAR_ACCUMULATOR_PATH", "/var/lib/agriha/solar_accumulator.json"
)
DEFAULT_STATE_PATH = os.environ.get(
    "RULE_ENGINE_STATE_PATH", "/var/lib/agriha/rule_engine_state.json"
)
DEFAULT_API_BASE = os.environ.get("UNIPI_API_BASE", "http://localhost:8080")
LOG_PATH = os.environ.get("RULE_ENGINE_LOG", "/var/log/agriha/rule_engine.log")
FLAG_DIR = os.environ.get("AGRIHA_FLAG_DIR", "/var/lib/agriha")
DEFAULT_TEMP_HISTORY_PATH = os.environ.get(
    "TEMP_HISTORY_PATH", "/var/lib/agriha/temp_history.json"
)
DEFAULT_THRESHOLD_HINT_PATH = os.environ.get(
    "THRESHOLD_HINT_PATH", "/var/lib/agriha/threshold_hint.json"
)

# 温度履歴の最大保持件数（5分毎 × 12 = 1時間分）
TEMP_HISTORY_MAX_POINTS = 12

# 閾値（上限/下限）
TEMP_THRESHOLD_HIGH = 32.0   # W3-A改善: 27℃→32℃ (P95相当、実データ日中48.4%超過のため)
TEMP_THRESHOLD_LOW = 16.0

# ──────────────────────────────────────────────
# ロガー設定
# ──────────────────────────────────────────────
logger = logging.getLogger("rule_engine")


def _setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    try:
        log_dir = Path(LOG_PATH).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(logging.Formatter(fmt))
        logger.addHandler(fh)
    except OSError:
        pass  # ログファイルが書けなくてもコア機能は動かす


# ──────────────────────────────────────────────
# 設定読み込み
# ──────────────────────────────────────────────

def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_crop_config(crop_path: str = DEFAULT_CROP_CONFIG_PATH) -> dict[str, Any]:
    with open(crop_path) as f:
        return yaml.safe_load(f)


def get_solar_threshold(crop_cfg: dict[str, Any]) -> float:
    """crop_irrigation.yaml から現在ステージの solar_threshold_mj を取得する。"""
    house = crop_cfg.get("house", {})
    crop_name = house.get("crop", "nasu_naga")
    stage_name = house.get("current_stage", "harvest_peak")
    threshold = (
        crop_cfg.get("crops", {})
        .get(crop_name, {})
        .get("stages", {})
        .get(stage_name, {})
        .get("defaults", {})
        .get("solar_threshold_mj", 0.9)
    )
    # リストの場合は最小値を使う
    if isinstance(threshold, list):
        return float(min(threshold))
    return float(threshold)


def get_irrigation_duration(crop_cfg: dict[str, Any]) -> int:
    """灌水時間を秒数で返す（デフォルト60秒）。"""
    house = crop_cfg.get("house", {})
    crop_name = house.get("crop", "nasu_naga")
    stage_name = house.get("current_stage", "harvest_peak")
    ml_per_plant = (
        crop_cfg.get("crops", {})
        .get(crop_name, {})
        .get("stages", {})
        .get(stage_name, {})
        .get("defaults", {})
        .get("irrigation_ml_per_plant", 270)
    )
    # ml から秒数に変換（1ml/秒 と仮定、最小60秒）
    if isinstance(ml_per_plant, list):
        ml = min(ml_per_plant)
    else:
        ml = ml_per_plant
    return max(60, int(ml))


# ──────────────────────────────────────────────
# ロックアウト確認
# ──────────────────────────────────────────────

def is_layer1_locked_out(lockout_path: str = DEFAULT_LOCKOUT_PATH) -> bool:
    """lockout_state.json を読み、Layer 1 ロックアウト中なら True を返す。"""
    try:
        data = json.loads(Path(lockout_path).read_text())
        until_str = data.get("layer1_lockout_until")
        if not until_str:
            return False
        until = datetime.fromisoformat(until_str)
        return datetime.now(tz=_JST) < until
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return False


# ──────────────────────────────────────────────
# API アクセス
# ──────────────────────────────────────────────

def fetch_sensors(client: httpx.Client, base_url: str) -> dict[str, Any]:
    """GET /api/sensors → センサーデータ辞書を返す。"""
    resp = client.get(f"{base_url}/api/sensors")
    resp.raise_for_status()
    return resp.json()


def fetch_status(client: httpx.Client, base_url: str) -> dict[str, Any]:
    """GET /api/status → ステータス辞書を返す。"""
    resp = client.get(f"{base_url}/api/status")
    resp.raise_for_status()
    return resp.json()


def post_relay(
    client: httpx.Client,
    base_url: str,
    channel: int,
    value: int,
    duration_sec: int | None = None,
) -> None:
    """POST /api/relay/{ch} でリレー制御。"""
    payload: dict[str, Any] = {"value": value}
    if duration_sec is not None:
        payload["duration_sec"] = duration_sec
    resp = client.post(f"{base_url}/api/relay/{channel}", json=payload)
    resp.raise_for_status()
    logger.info("relay ch%d → %d (duration=%s)", channel, value, duration_sec)


# ──────────────────────────────────────────────
# astral: 日の出/日没判定
# ──────────────────────────────────────────────

def get_sun_times(cfg: dict[str, Any], dt: datetime | None = None) -> dict[str, datetime]:
    """astral で日の出・日没時刻を計算して返す。"""
    loc_cfg = cfg.get("location", {})
    location = LocationInfo(
        "Greenhouse",
        "Japan",
        "Asia/Tokyo",
        loc_cfg.get("latitude", 42.888),
        loc_cfg.get("longitude", 141.603),
    )
    target_date = (dt or datetime.now(tz=_JST)).date()
    return sun(location.observer, date=target_date, tzinfo=_JST)


def is_nighttime(cfg: dict[str, Any], dt: datetime | None = None) -> bool:
    """日没後または日の出前なら True を返す。"""
    now = dt or datetime.now(tz=_JST)
    sun_times = get_sun_times(cfg, now)
    return now < sun_times["sunrise"] or now > sun_times["sunset"]


# ──────────────────────────────────────────────
# Layer 3 計画確認
# ──────────────────────────────────────────────

def load_current_plan(plan_path: str = DEFAULT_PLAN_PATH) -> dict[str, Any] | None:
    """current_plan.json を読み込む。存在しないか期限切れなら None を返す。"""
    try:
        data = json.loads(Path(plan_path).read_text())
        valid_until_str = data.get("valid_until")
        if not valid_until_str:
            return None
        valid_until = datetime.fromisoformat(valid_until_str)
        if datetime.now(tz=_JST) > valid_until:
            logger.info("current_plan.json expired at %s", valid_until_str)
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


# ──────────────────────────────────────────────
# センサー値取得ヘルパー
# ──────────────────────────────────────────────

def _sensor_val(sensors: dict[str, Any], key: str) -> float | None:
    """sensors辞書からネストされたvalue値を取得する。"""
    entry = sensors.get("sensors", {}).get(key)
    if entry is None:
        return None
    return entry.get("value")


def get_indoor_temp(sensors: dict[str, Any]) -> float | None:
    val = _sensor_val(sensors, "agriha/h01/ccm/InAirTemp")
    return val


def get_misol(sensors: dict[str, Any]) -> dict[str, Any]:
    entry = sensors.get("sensors", {}).get("agriha/farm/weather/misol", {})
    return entry


def get_insolar(sensors: dict[str, Any]) -> float:
    """CCM 日射量 (W/m²)。なければ 0。"""
    val = _sensor_val(sensors, "agriha/h01/ccm/InSolar")
    return float(val) if val is not None else 0.0


# ──────────────────────────────────────────────
# 日射積算器
# ──────────────────────────────────────────────

def load_solar_accumulator(acc_path: str = DEFAULT_SOLAR_ACC_PATH) -> dict[str, Any]:
    today = date.today().isoformat()
    try:
        data = json.loads(Path(acc_path).read_text())
        if data.get("date") != today:
            logger.info("solar_accumulator: date changed, resetting")
            return {"date": today, "accumulated_mj": 0.0, "irrigations_today": 0}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": today, "accumulated_mj": 0.0, "irrigations_today": 0}


def save_solar_accumulator(acc: dict[str, Any], acc_path: str = DEFAULT_SOLAR_ACC_PATH) -> None:
    acc["last_updated_at"] = datetime.now(tz=_JST).isoformat()
    Path(acc_path).parent.mkdir(parents=True, exist_ok=True)
    Path(acc_path).write_text(json.dumps(acc, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# ルール評価
# ──────────────────────────────────────────────

def evaluate_rules(
    cfg: dict[str, Any],
    crop_cfg: dict[str, Any],
    sensors: dict[str, Any],
    status: dict[str, Any],
    solar_acc: dict[str, Any],
    current_plan: dict[str, Any] | None,
    now: datetime | None = None,
    channel_map_path: str | Path | None = None,
    prev_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    ルール評価。各ルールの適用結果と更新後の solar_acc を返す。

    Args:
        prev_state: load_state() の戻り値。cron再起動後の状態引き継ぎに使用。

    Returns:
        {
            "relay_actions": [(ch, value, duration_sec_or_None), ...],
            "solar_acc": updated accumulator dict,
            "triggered_rules": [str, ...],
            "window_state": "open" | "closed" | "unknown",
            "temperature_stage": "low" | "normal" | "high" | "critical",
            "last_irrigation_at": ISO8601 str | None,
        }
    """
    now = now or datetime.now(tz=_JST)
    relay_actions: list[tuple[int, int, int | None]] = []
    triggered_rules: list[str] = []
    _prev_state = prev_state or {}

    temp_cfg = cfg["temperature"]
    wind_cfg = cfg["wind"]
    rain_cfg = cfg["rain"]
    _ch_config = load_channel_map(channel_map_path)
    groups: list[dict] = load_window_groups(_ch_config)

    misol = get_misol(sensors)
    rainfall = misol.get("rainfall", 0.0) or 0.0
    wind_speed = misol.get("wind_speed_ms", 0.0) or 0.0
    wind_dir = misol.get("wind_direction", 0) or 0
    indoor_temp = get_indoor_temp(sensors)
    insolar = get_insolar(sensors)

    nighttime = is_nighttime(cfg, now)
    target_temp = temp_cfg["target_night"] if nighttime else temp_cfg["target_day"]

    # Layer 3 計画が有効かどうか + 天気予報フィールド取得
    layer3_active = current_plan is not None
    plan_rain_probability: float | None = None
    if current_plan is not None:
        _rp = current_plan.get("rain_probability")
        if _rp is not None:
            try:
                plan_rain_probability = float(_rp)
            except (TypeError, ValueError):
                pass

    def _acted_chs() -> set[int]:
        return {a[0] for a in relay_actions}

    def _close_group(g: dict) -> None:
        """グループを閉める: close_channel ON, open_channel OFF"""
        relay_actions.append((g["close_channel"], 1, None))
        relay_actions.append((g["open_channel"], 0, None))

    def _open_group(g: dict) -> None:
        """グループを開ける: open_channel ON, close_channel OFF"""
        relay_actions.append((g["open_channel"], 1, None))
        relay_actions.append((g["close_channel"], 0, None))

    def _group_acted(g: dict) -> bool:
        chs = _acted_chs()
        return g["open_channel"] in chs or g["close_channel"] in chs

    # ── Rule 6a: 降雨チェック（実測 + 予報確率）────────
    _forecast_rain = (
        plan_rain_probability is not None and plan_rain_probability >= 70
        and rainfall <= rain_cfg["threshold_mm_h"]
    )
    if rainfall > rain_cfg["threshold_mm_h"]:
        triggered_rules.append("rain_close_all")
        for g in groups:
            _close_group(g)
        logger.info("Rule 6a: rainfall=%.2f > %.2f → 全窓閉", rainfall, rain_cfg["threshold_mm_h"])
        # 降雨時は以降の窓制御をスキップ（灌水のみ評価継続）
        _eval_irrigation(
            cfg, crop_cfg, insolar, solar_acc, relay_actions, triggered_rules
        )
        return {
            "relay_actions": relay_actions,
            "solar_acc": solar_acc,
            "triggered_rules": triggered_rules,
            "window_state": "closed",
            "temperature_stage": _get_temperature_stage(indoor_temp),
            "last_irrigation_at": solar_acc.get("last_irrigation_at"),
        }
    if _forecast_rain:
        triggered_rules.append("forecast_rain_close_all")
        for g in groups:
            _close_group(g)
        logger.info(
            "Rule 6a(予報): rain_probability=%.0f%% >= 70 → 予防的全窓閉",
            plan_rain_probability,
        )
        _eval_irrigation(
            cfg, crop_cfg, insolar, solar_acc, relay_actions, triggered_rules
        )
        return {
            "relay_actions": relay_actions,
            "solar_acc": solar_acc,
            "triggered_rules": triggered_rules,
            "window_state": "closed",
            "temperature_stage": _get_temperature_stage(indoor_temp),
            "last_irrigation_at": solar_acc.get("last_irrigation_at"),
        }

    # ── Rule 6b: 強風チェック ──────────────────────────
    if wind_speed > wind_cfg["strong_wind_threshold_ms"]:
        triggered_rules.append("strong_wind")
        matched = [g for g in groups if wind_dir in g["wind_close_directions"]]
        if matched:
            for g in matched:
                logger.info(
                    "Rule 6b: 強風 %.1fm/s dir=%d → %s閉 (close_ch=%d)",
                    wind_speed, wind_dir, g["name"], g["close_channel"],
                )
                _close_group(g)
        else:
            logger.info("Rule 6b: 強風 %.1fm/s dir=%d → 方角不明 → 全窓閉", wind_speed, wind_dir)
            for g in groups:
                _close_group(g)

    # ── Rule 6c: 夜間全閉 + キャリブレーション ─────────
    pitagorasu_cfg = cfg.get("pitagorasu", {})
    if nighttime:
        triggered_rules.append("nighttime_close")
        if pitagorasu_cfg.get("enabled"):
            win_pos = load_position()
            close_travel = pitagorasu_cfg.get("close_travel_sec", 50)
            calibration_dur = int(close_travel * 1.1)  # +10%余裕
            for g in groups:
                if not _group_acted(g):
                    relay_actions.append((g["close_channel"], 1, calibration_dur))
                    relay_actions.append((g["open_channel"], 0, None))
                    calibrate_closed(win_pos, g["name"])
            save_position(win_pos)
            logger.info("Rule 6c: 夜間全閉 + キャリブレーション (%d秒)", calibration_dur)
        else:
            logger.info("Rule 6c: 夜間 → 全窓閉")
            for g in groups:
                if not _group_acted(g):
                    _close_group(g)

    # ── Rule 6d: 温度制御（Layer 3 計画があれば委譲） ──
    if not layer3_active:
        if indoor_temp is not None:
            if pitagorasu_cfg.get("enabled"):
                # ピタゴラススイッチ: 段階的窓開放
                temp_history = load_temp_history()
                trend = compute_temperature_trend(temp_history)
                hour = now.hour
                pitagorasu = _compute_pitagorasu_stage(
                    indoor_temp, trend, hour,
                    early_offset=temp_cfg.get("early_morning_offset", -1.0),
                    stages=pitagorasu_cfg.get("stages"),
                    predictive_cfg=pitagorasu_cfg.get("predictive"),
                )
                triggered_rules.append(f"pitagorasu_stage_{pitagorasu['stage']}")
                logger.info("Rule 6d: %s", pitagorasu["reason"])

                win_pos = load_position()
                open_travel = pitagorasu_cfg.get("open_travel_sec", 65)
                close_travel = pitagorasu_cfg.get("close_travel_sec", 50)
                deadband = pitagorasu_cfg.get("deadband", 0.05)

                for g in groups:
                    if _group_acted(g):
                        continue
                    is_south = "南" in g["name"]
                    key = "south" if is_south else "north"
                    current = win_pos.get(key, 0.0)
                    target = pitagorasu[f"{key}_target"]

                    direction, dur = compute_move(
                        current, target, open_travel, close_travel, deadband,
                    )
                    if direction is None:
                        logger.info(
                            "Rule 6d: %s 不感帯内 (%.0f%%→%.0f%%) → 維持",
                            g["name"], current * 100, target * 100,
                        )
                        continue

                    if direction == "open":
                        relay_actions.append((g["open_channel"], 1, int(dur)))
                        relay_actions.append((g["close_channel"], 0, None))
                    else:
                        relay_actions.append((g["close_channel"], 1, int(dur)))
                        relay_actions.append((g["open_channel"], 0, None))

                    logger.info(
                        "Rule 6d: %s %s %.0f%%→%.0f%% (%s %d秒)",
                        g["name"], direction, current * 100, target * 100,
                        direction, int(dur),
                    )
                    update_position(
                        win_pos, g["name"], direction, dur,
                        open_travel, close_travel,
                    )

                save_position(win_pos)
            else:
                # フォールバック: 従来のバイナリ開閉
                margin_open = temp_cfg["margin_open"]
                margin_close = temp_cfg["margin_close"]
                if indoor_temp > target_temp + margin_open:
                    triggered_rules.append("temp_high_open")
                    logger.info(
                        "Rule 6d: 高温 %.1f℃ > %.1f+%.1f → 側窓開",
                        indoor_temp, target_temp, margin_open,
                    )
                    for g in groups:
                        if not _group_acted(g):
                            _open_group(g)
                elif indoor_temp < target_temp - margin_close:
                    triggered_rules.append("temp_low_close")
                    logger.info(
                        "Rule 6d: 低温 %.1f℃ < %.1f-%.1f → 側窓閉",
                        indoor_temp, target_temp, margin_close,
                    )
                    for g in groups:
                        if not _group_acted(g):
                            _close_group(g)
    else:
        logger.info("Rule 6d: Layer 3 計画有効 → 温度制御を Layer 3 に委譲")

    # ── Rule 6e: 日射比例灌水 ─────────────────────────
    _eval_irrigation(
        cfg, crop_cfg, insolar, solar_acc, relay_actions, triggered_rules
    )

    return {
        "relay_actions": relay_actions,
        "solar_acc": solar_acc,
        "triggered_rules": triggered_rules,
        "window_state": _compute_window_state(
            relay_actions, groups, _prev_state.get("window_state", "unknown")
        ),
        "temperature_stage": _get_temperature_stage(indoor_temp),
        "last_irrigation_at": solar_acc.get("last_irrigation_at"),
    }


def _eval_irrigation(
    cfg: dict[str, Any],
    crop_cfg: dict[str, Any],
    insolar: float,
    solar_acc: dict[str, Any],
    relay_actions: list[tuple[int, int, int | None]],
    triggered_rules: list[str],
) -> None:
    """Rule 6e: 日射比例灌水を評価し、必要なら relay_actions に追加する。"""
    irr_cfg = cfg["irrigation"]
    irr_ch = irr_cfg["channel"]
    solar_threshold = get_solar_threshold(crop_cfg)
    duration_sec = get_irrigation_duration(crop_cfg)

    # 5分間の日射積算量を計算
    solar_mj_5min = insolar * 300.0 / 1_000_000.0
    solar_acc["accumulated_mj"] = solar_acc.get("accumulated_mj", 0.0) + solar_mj_5min
    logger.info(
        "Rule 6e: InSolar=%.1fW/m² → +%.4f MJ → 累積=%.4f MJ (閾値=%.2f)",
        insolar, solar_mj_5min, solar_acc["accumulated_mj"], solar_threshold,
    )

    if solar_acc["accumulated_mj"] >= solar_threshold:
        triggered_rules.append("solar_irrigation")
        logger.info(
            "Rule 6e: 日射積算 %.4f >= %.2f → 灌水実行 ch%d %dsec",
            solar_acc["accumulated_mj"], solar_threshold, irr_ch, duration_sec,
        )
        relay_actions.append((irr_ch, 1, duration_sec))
        solar_acc["accumulated_mj"] = 0.0
        solar_acc["irrigations_today"] = solar_acc.get("irrigations_today", 0) + 1
        solar_acc["last_irrigation_at"] = datetime.now(tz=_JST).isoformat()
    else:
        logger.info("Rule 6e: 日射積算 %.4f < %.2f → 灌水スキップ", solar_acc["accumulated_mj"], solar_threshold)


# ──────────────────────────────────────────────
# ピタゴラススイッチ（段階的窓開放）
# 設計書: predictive_ventilation_design.md
# ──────────────────────────────────────────────

_DEFAULT_PITAGORASU_STAGES = [
    {"max_temp": 25.0, "south": 0.0, "north": 0.0, "name": "closed"},
    {"max_temp": 27.0, "south": 0.3, "north": 0.0, "name": "south_micro"},
    {"max_temp": 30.0, "south": 0.5, "north": 0.5, "name": "both_medium"},
    {"max_temp": 32.0, "south": 0.8, "north": 0.8, "name": "both_wide"},
    {"max_temp": 999,  "south": 1.0, "north": 1.0, "name": "full_open"},
]


def _compute_pitagorasu_stage(
    temp: float,
    trend: float | None,
    hour: int,
    early_offset: float = -1.0,
    stages: list[dict] | None = None,
    predictive_cfg: dict | None = None,
) -> dict:
    """ピタゴラススイッチの段階と目標開度を返す。

    温度トレンドに基づく先読み補正と早朝オフセットを適用し、
    実効温度から段階を判定する。

    Returns:
        {
            "stage": 0-4,
            "stage_name": str,
            "south_target": 0.0-1.0,
            "north_target": 0.0-1.0,
            "effective_temp": float,
            "reason": str,
        }
    """
    if stages is None:
        stages = _DEFAULT_PITAGORASU_STAGES

    # 早朝オフセット（5-8時は-1℃シフト → 換気を先行）
    offset = early_offset if 5 <= hour < 8 else 0.0

    # 先読み補正: トレンドに基づき実効温度を嵩上げ
    predictive_bonus = 0.0
    if trend is not None and trend > 0:
        p = predictive_cfg or {}
        rapid = p.get("rapid_trend_threshold", 3.0)
        mild = p.get("mild_trend_threshold", 1.5)
        if trend > rapid:
            predictive_bonus = p.get("rapid_bonus", 2.0)
        elif trend > mild:
            predictive_bonus = p.get("mild_bonus", 1.0)

    effective_temp = temp + predictive_bonus + offset

    for i, s in enumerate(stages):
        if effective_temp < s["max_temp"]:
            return {
                "stage": i,
                "stage_name": s["name"],
                "south_target": s["south"],
                "north_target": s["north"],
                "effective_temp": effective_temp,
                "reason": (
                    f"T_eff={effective_temp:.1f}℃ "
                    f"(実測{temp:.1f} +予測{predictive_bonus:+.1f} +朝{offset:+.1f}) "
                    f"→ {s['name']} (S:{s['south']:.0%} N:{s['north']:.0%})"
                ),
            }

    last = stages[-1]
    return {
        "stage": len(stages) - 1,
        "stage_name": last["name"],
        "south_target": last["south"],
        "north_target": last["north"],
        "effective_temp": effective_temp,
        "reason": f"T_eff={effective_temp:.1f}℃ → {last['name']} (フォールバック)",
    }


# ──────────────────────────────────────────────
# 温度段階判定・窓状態導出ヘルパー
# ──────────────────────────────────────────────

def _get_temperature_stage(temp: float | None) -> str:
    """現在の室内温度からピタゴラスイッチの段階を返す。

    Returns:
        "critical": 閾値超過（≥32℃ または <16℃）
        "high":     高温注意（≥26℃）
        "low":      低温注意（<16.5℃）
        "normal":   正常範囲
    """
    if temp is None:
        return "normal"
    if temp >= TEMP_THRESHOLD_HIGH or temp < TEMP_THRESHOLD_LOW:
        return "critical"
    if temp >= 26.0:
        return "high"
    if temp < 16.5:
        return "low"
    return "normal"


def _compute_window_state(
    relay_actions: list[tuple[int, int, int | None]],
    groups: list[dict],
    prev_window_state: str,
) -> str:
    """relay_actions から現在の窓状態を導出する。

    アクションがない場合は prev_window_state を引き継ぐ。
    """
    action_map = {ch: val for ch, val, _ in relay_actions}
    if any(action_map.get(g["open_channel"]) == 1 for g in groups):
        return "open"
    if any(action_map.get(g["close_channel"]) == 1 for g in groups):
        return "closed"
    return prev_window_state


# ──────────────────────────────────────────────
# 状態保存・読み込み
# ──────────────────────────────────────────────

def load_state(state_path: str = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """rule_engine_state.json を読み込む。

    ファイルなし・パースエラー時はデフォルト値を返す。

    Returns:
        {
            "window_state": "open" | "closed" | "unknown",
            "last_irrigation_at": ISO8601 str | None,
            "temperature_stage": "low" | "normal" | "high" | "critical",
        }
    """
    defaults: dict[str, Any] = {
        "window_state": "unknown",
        "last_irrigation_at": None,
        "temperature_stage": "normal",
    }
    try:
        data = json.loads(Path(state_path).read_text())
        return {
            "window_state": data.get("window_state", defaults["window_state"]),
            "last_irrigation_at": data.get("last_irrigation_at"),
            "temperature_stage": data.get("temperature_stage", defaults["temperature_stage"]),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(defaults)


def save_state(state_path: str, result: dict[str, Any]) -> None:
    state = {
        "last_run_at": datetime.now(tz=_JST).isoformat(),
        "triggered_rules": result.get("triggered_rules", []),
        "relay_actions": [
            {"channel": a[0], "value": a[1], "duration_sec": a[2]}
            for a in result.get("relay_actions", [])
        ],
        "window_state": result.get("window_state", "unknown"),
        "last_irrigation_at": result.get("last_irrigation_at"),
        "temperature_stage": result.get("temperature_stage", "normal"),
    }
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# 温度履歴・閾値到達予測（先読みヒント生成）
# ──────────────────────────────────────────────

def load_temp_history(path: str = DEFAULT_TEMP_HISTORY_PATH) -> dict[str, Any]:
    """温度履歴ファイルを読み込む。存在しなければ空の履歴を返す。"""
    try:
        data = json.loads(Path(path).read_text())
        if "points" not in data or not isinstance(data["points"], list):
            return {"points": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"points": []}


def append_temp_history(
    history: dict[str, Any],
    temp_c: float,
    timestamp: datetime | None = None,
    max_points: int = TEMP_HISTORY_MAX_POINTS,
    path: str = DEFAULT_TEMP_HISTORY_PATH,
) -> dict[str, Any]:
    """温度履歴に1点追加し、max_points を超えた古い点を削除してファイルに保存する。"""
    ts = (timestamp or datetime.now(tz=_JST)).isoformat()
    points: list[dict[str, Any]] = history.get("points", [])
    points.append({"timestamp": ts, "temp_c": temp_c})
    if len(points) > max_points:
        points = points[-max_points:]
    updated = {"points": points}
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(updated, ensure_ascii=False, indent=2))
    except OSError as e:
        logger.warning("温度履歴保存失敗: %s", e)
    return updated


def compute_temperature_trend(history: dict[str, Any]) -> float | None:
    """過去の温度履歴から上昇/下降速度(℃/h)を計算する。

    点が2点未満の場合は None を返す。
    最新点と最古点の差分を時間差で割る（単純差分）。
    """
    points = history.get("points", [])
    if len(points) < 2:
        return None
    oldest = points[0]
    newest = points[-1]
    try:
        t0 = datetime.fromisoformat(oldest["timestamp"])
        t1 = datetime.fromisoformat(newest["timestamp"])
        dt_hours = (t1 - t0).total_seconds() / 3600.0
        if dt_hours <= 0:
            return None
        delta_temp = float(newest["temp_c"]) - float(oldest["temp_c"])
        return delta_temp / dt_hours
    except (KeyError, ValueError, TypeError):
        return None


def compute_threshold_hint(
    history: dict[str, Any],
    outdoor_temp_forecast_c: float | None = None,
) -> dict[str, str]:
    """閾値到達予測ヒントを計算する。

    Args:
        history: load_temp_history() の戻り値。
        outdoor_temp_forecast_c: 天気予報外気温（あれば精度向上。なくても動く）。

    Returns:
        {
          "temperature_trend": "+2.3℃/h" などの文字列,
          "threshold_eta": "27℃到達まで約35分" or "到達予測なし",
          "recommendation": "先読み開放を検討" or "",
        }
    """
    points = history.get("points", [])
    hint: dict[str, str] = {
        "temperature_trend": "データ不足",
        "threshold_eta": "到達予測なし",
        "recommendation": "",
    }

    if not points:
        return hint

    try:
        current_temp = float(points[-1]["temp_c"])
    except (KeyError, ValueError, TypeError):
        return hint

    trend = compute_temperature_trend(history)

    if trend is None:
        hint["temperature_trend"] = f"現在{current_temp:.1f}℃（履歴不足）"
        return hint

    # 外気温予報があれば補正（簡易: 外気温との差分で速度を補正）
    if outdoor_temp_forecast_c is not None:
        # 外気温が現在より高ければ上昇傾向を強化、低ければ弱化（重み0.2）
        correction = (outdoor_temp_forecast_c - current_temp) * 0.2
        trend = trend + correction

    sign = "+" if trend >= 0 else ""
    hint["temperature_trend"] = f"{sign}{trend:.1f}℃/h"

    # 閾値到達予測（線形外挿）
    if trend > 0:
        # 上昇中 → 27℃上限到達まで
        remaining = TEMP_THRESHOLD_HIGH - current_temp
        if remaining <= 0:
            hint["threshold_eta"] = f"現在{current_temp:.1f}℃ — 既に{TEMP_THRESHOLD_HIGH:.0f}℃超過"
            hint["recommendation"] = "即時開放を検討"
        else:
            eta_hours = remaining / trend
            eta_min = int(eta_hours * 60)
            if eta_min <= 120:
                hint["threshold_eta"] = f"{TEMP_THRESHOLD_HIGH:.0f}℃到達まで約{eta_min}分"
                if eta_min <= 40:
                    hint["recommendation"] = "先読み開放を検討"
                else:
                    hint["recommendation"] = ""
            else:
                hint["threshold_eta"] = "到達予測なし（2時間超）"
    elif trend < 0:
        # 下降中 → 16℃下限到達まで
        remaining = current_temp - TEMP_THRESHOLD_LOW
        if remaining <= 0:
            hint["threshold_eta"] = f"現在{current_temp:.1f}℃ — 既に{TEMP_THRESHOLD_LOW:.0f}℃以下"
            hint["recommendation"] = "即時閉窓を検討"
        else:
            eta_hours = remaining / (-trend)
            eta_min = int(eta_hours * 60)
            if eta_min <= 120:
                hint["threshold_eta"] = f"{TEMP_THRESHOLD_LOW:.0f}℃到達まで約{eta_min}分"
                if eta_min <= 40:
                    hint["recommendation"] = "先読み閉窓を検討"
                else:
                    hint["recommendation"] = ""
            else:
                hint["threshold_eta"] = "到達予測なし（2時間超）"
    else:
        # 横ばい
        hint["threshold_eta"] = "到達予測なし（横ばい）"

    return hint


def save_threshold_hint(
    hint: dict[str, str],
    path: str = DEFAULT_THRESHOLD_HINT_PATH,
) -> None:
    """閾値到達予測ヒントをファイルに保存する。"""
    data = dict(hint)
    data["generated_at"] = datetime.now(tz=_JST).isoformat()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        logger.warning("threshold_hint保存失敗: %s", e)


# ──────────────────────────────────────────────
# weather flag 更新
# ──────────────────────────────────────────────

def update_weather_flags(
    cfg: dict[str, Any],
    sensors: dict[str, Any],
    flag_dir: str = FLAG_DIR,
) -> None:
    """rain_flag / wind_flag ファイルを書き出し／削除する。

    rule_engine 実行毎に呼ばれ、plan_executor が参照するフラグを更新する。
    - 条件成立: タイムスタンプをファイルに書き出す
    - 条件解除: ファイルを削除する
    """
    flag_path = Path(flag_dir)
    flag_path.mkdir(parents=True, exist_ok=True)
    rain_cfg = cfg["rain"]
    wind_cfg = cfg["wind"]

    misol = get_misol(sensors)
    rainfall = misol.get("rainfall", 0.0) or 0.0
    wind_speed = misol.get("wind_speed_ms", 0.0) or 0.0
    now_str = datetime.now(tz=_JST).isoformat()

    # rain_flag
    rain_flag = flag_path / "rain_flag"
    if rainfall > rain_cfg["threshold_mm_h"]:
        rain_flag.write_text(now_str)
        logger.info("rain_flag 書き出し: rainfall=%.2f > %.2f", rainfall, rain_cfg["threshold_mm_h"])
    elif rain_flag.exists():
        rain_flag.unlink()
        logger.info("rain_flag 削除: rainfall=%.2f", rainfall)

    # wind_flag
    wind_flag = flag_path / "wind_flag"
    if wind_speed > wind_cfg["strong_wind_threshold_ms"]:
        wind_flag.write_text(json.dumps({"timestamp": now_str, "wind_speed_ms": wind_speed}))
        logger.info("wind_flag 書き出し: wind_speed=%.1fm/s > %.1f", wind_speed, wind_cfg["strong_wind_threshold_ms"])
    elif wind_flag.exists():
        wind_flag.unlink()
        logger.info("wind_flag 削除: wind_speed=%.1fm/s", wind_speed)


# ──────────────────────────────────────────────
# メインエントリポイント
# ──────────────────────────────────────────────

def run(
    config_path: str = DEFAULT_CONFIG_PATH,
    crop_config_path: str = DEFAULT_CROP_CONFIG_PATH,
    lockout_path: str = DEFAULT_LOCKOUT_PATH,
    plan_path: str = DEFAULT_PLAN_PATH,
    solar_acc_path: str = DEFAULT_SOLAR_ACC_PATH,
    state_path: str = DEFAULT_STATE_PATH,
    api_base: str = DEFAULT_API_BASE,
    channel_map_path: str | None = None,
    flag_dir: str = FLAG_DIR,
    temp_history_path: str = DEFAULT_TEMP_HISTORY_PATH,
    threshold_hint_path: str = DEFAULT_THRESHOLD_HINT_PATH,
    dry_run: bool = False,
) -> int:
    """
    rule_engine のメイン処理。
    Args:
        dry_run: True の場合、センサー取得+ルール評価のみ。リレー操作しない。
    Returns: 0=正常終了, 1=スキップ/エラー
    """
    _setup_logging()
    logger.info("rule_engine.py 起動")

    # Step 1: Layer 1 ロックアウト確認
    if is_layer1_locked_out(lockout_path):
        logger.info("Layer 1 ロックアウト中 → スキップ")
        return 1

    # Step 2: 設定読み込み
    try:
        cfg = load_config(config_path)
        crop_cfg = load_crop_config(crop_config_path)
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.error("設定ファイル読み込みエラー: %s", e)
        return 1

    api_cfg = cfg.get("unipi_api", {})
    api_base = api_cfg.get("base_url", api_base)
    timeout = api_cfg.get("timeout_sec", 10)

    # Step 2.5: 前回状態読み込み + Layer 3 計画（早期ロード: threshold_hint計算に活用）
    prev_state = load_state(state_path)
    current_plan = load_current_plan(plan_path)
    logger.info(
        "前回状態: window=%s stage=%s",
        prev_state["window_state"], prev_state["temperature_stage"],
    )

    # Step 3: センサーデータ取得 + CommandGate ロックアウト確認（リトライ付き）
    try:
        with httpx.Client(timeout=timeout) as client:
            sensors = retry_with_backoff(
                lambda: fetch_sensors(client, api_base),
                delays=RETRY_DELAYS_LOCAL_SEC,
                error_label="センサーデータ取得",
                notify_on_exceeded=False,
            )
            status = retry_with_backoff(
                lambda: fetch_status(client, api_base),
                delays=RETRY_DELAYS_LOCAL_SEC,
                error_label="ステータス取得",
                notify_on_exceeded=False,
            )

            # Step 3.5: weather flag 更新（ロックアウト状態によらず実行）
            update_weather_flags(cfg, sensors, flag_dir=flag_dir)

            # Step 3.6: 温度履歴更新 + 閾値到達予測ヒント生成（ロックアウト状態によらず実行）
            indoor_temp_for_hint = get_indoor_temp(sensors)
            if indoor_temp_for_hint is not None:
                temp_history = load_temp_history(temp_history_path)
                temp_history = append_temp_history(
                    temp_history, indoor_temp_for_hint, path=temp_history_path
                )
                # forecast_engine が計画に書いた予報外気温を優先、なければ Misol 現在値を使用
                outdoor_temp_for_hint: float | None = None
                if current_plan is not None:
                    _ot = current_plan.get("outdoor_temp_forecast_c")
                    if _ot is not None:
                        try:
                            outdoor_temp_for_hint = float(_ot)
                        except (TypeError, ValueError):
                            pass
                if outdoor_temp_for_hint is None:
                    try:
                        outdoor_temp_for_hint = float(
                            get_misol(sensors).get("temperature_c") or 0.0
                        ) or None
                    except (TypeError, ValueError):
                        pass
                hint = compute_threshold_hint(temp_history, outdoor_temp_for_hint)
                save_threshold_hint(hint, threshold_hint_path)
                logger.info(
                    "閾値到達予測: trend=%s eta=%s",
                    hint["temperature_trend"], hint["threshold_eta"],
                )

            # CommandGate ロックアウト確認
            if status.get("locked_out", False):
                logger.info("CommandGate ロックアウト中 → スキップ")
                return 1

            # Step 4: 日の出/日没計算は evaluate_rules 内で行う

            # Step 6: ルール評価（prev_state + current_plan を渡す）
            solar_acc = load_solar_accumulator(solar_acc_path)
            result = evaluate_rules(
                cfg, crop_cfg, sensors, status, solar_acc, current_plan,
                channel_map_path=channel_map_path,
                prev_state=prev_state,
            )

            # Step 7: アクション実行（変更がある場合のみ）
            relay_actions = result["relay_actions"]
            if relay_actions:
                # 重複チャンネルは最後の設定を優先
                seen: dict[int, tuple[int, int | None]] = {}
                for ch, val, dur in relay_actions:
                    seen[ch] = (val, dur)
                if dry_run:
                    logger.info("DRY-RUN: リレー操作スキップ (%d アクション)", len(seen))
                    print(json.dumps({
                        "dry_run": True,
                        "triggered_rules": result["triggered_rules"],
                        "relay_actions": [
                            {"channel": ch, "value": val, "duration_sec": dur}
                            for ch, (val, dur) in seen.items()
                        ],
                    }, ensure_ascii=False, indent=2))
                else:
                    for ch, (val, dur) in seen.items():
                        retry_with_backoff(
                            lambda _ch=ch, _val=val, _dur=dur: post_relay(
                                client, api_base, _ch, _val, _dur
                            ),
                            delays=RETRY_DELAYS_LOCAL_SEC,
                            error_label=f"リレー制御(ch{ch})",
                            notify_on_exceeded=False,
                        )
            else:
                logger.info("アクションなし")
                if dry_run:
                    print(json.dumps({
                        "dry_run": True,
                        "triggered_rules": result["triggered_rules"],
                        "relay_actions": [],
                    }, ensure_ascii=False, indent=2))

        # Step 8: 状態保存
        save_solar_accumulator(result["solar_acc"], solar_acc_path)
        save_state(state_path, result)
        logger.info(
            "完了: rules=%s, actions=%d",
            result["triggered_rules"],
            len(result.get("relay_actions", [])),
        )
        return 0

    except httpx.HTTPError as e:
        logger.error("REST API エラー: %s → 安全側（操作しない）", e)
        return 1
    except Exception as e:
        logger.error("予期しないエラー: %s", e)
        return 1


def main() -> None:
    """CLI エントリポイント。argparse でオプションを処理する。"""
    import argparse

    parser = argparse.ArgumentParser(description="Layer 2: ガムテ制御 rule_engine")
    parser.add_argument("--dry-run", action="store_true", help="センサー取得+ルール評価のみ。リレー操作しない")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG レベルログ有効化")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_PATH, help="rules.yaml パス")
    parser.add_argument("--show-state", action="store_true", help="現在の rule_engine_state.json を表示して終了")
    args = parser.parse_args()

    if args.show_state:
        try:
            data = json.loads(Path(DEFAULT_STATE_PATH).read_text())
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"State file not found or invalid: {e}")
        sys.exit(0)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    sys.exit(run(config_path=args.config, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
