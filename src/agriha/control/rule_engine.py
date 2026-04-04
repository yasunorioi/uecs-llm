"""
rule_engine.py — Layer 2: ガムテ制御 (v5拡張)

cron 10分毎に実行。ルールベースで側窓・灌水を制御する。
v5: 変温管理・CO2・湿度・外気温ベース開度テーブル追加。PID廃止。

設計書: docs/layer3_compiler_design.md
データ根拠: ArSprout 2025実績 + 道央農業振興公社研修資料

依存: httpx, pyyaml, astral
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml
from astral import LocationInfo
from astral.sun import sun

from agriha.control.channel_config import load_channel_map, load_window_groups, get_window_channels

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
DEFAULT_SOLAR_ACC_PATH = os.environ.get(
    "SOLAR_ACCUMULATOR_PATH", "/var/lib/agriha/solar_accumulator.json"
)
DEFAULT_STATE_PATH = os.environ.get(
    "RULE_ENGINE_STATE_PATH", "/var/lib/agriha/rule_engine_state.json"
)
DEFAULT_TEMP_HISTORY_PATH = os.environ.get(
    "TEMP_HISTORY_PATH", "/var/lib/agriha/temp_history.json"
)
DEFAULT_WINDOW_POS_PATH = os.environ.get(
    "WINDOW_POSITION_PATH", "/var/lib/agriha/window_position.json"
)
DEFAULT_API_BASE = os.environ.get("UNIPI_API_BASE", "http://localhost:8080")
LOG_PATH = os.environ.get("RULE_ENGINE_LOG", "/var/log/agriha/rule_engine.log")
FLAG_DIR = os.environ.get("AGRIHA_FLAG_DIR", "/var/lib/agriha")
DEFAULT_TIDE_FORECAST_PATH = os.environ.get(
    "TIDE_FORECAST_PATH", "/var/lib/agriha/tide_forecast.json"
)
TIDE_FORECAST_MAX_AGE_SEC = 1800  # 30分以上古い予測はスキップ
TIDE_HUMIDITY_PREVENT_THRESHOLD = 80.0  # % — 先行換気トリガー湿度閾値

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
        pass


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
    if isinstance(threshold, list):
        return float(min(threshold))
    return float(threshold)


def get_irrigation_duration(crop_cfg: dict[str, Any]) -> int:
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
    if isinstance(ml_per_plant, list):
        ml = min(ml_per_plant)
    else:
        ml = ml_per_plant
    return max(60, int(ml))


# ──────────────────────────────────────────────
# ロックアウト確認
# ──────────────────────────────────────────────

def is_layer1_locked_out(lockout_path: str = DEFAULT_LOCKOUT_PATH) -> bool:
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
    resp = client.get(f"{base_url}/api/sensors")
    resp.raise_for_status()
    return resp.json()


def fetch_status(client: httpx.Client, base_url: str) -> dict[str, Any]:
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
    now = dt or datetime.now(tz=_JST)
    sun_times = get_sun_times(cfg, now)
    return now < sun_times["sunrise"] or now > sun_times["sunset"]


# ──────────────────────────────────────────────
# v5: 変温管理 — 時間帯別目標温度
# ──────────────────────────────────────────────

def get_schedule_target(
    cfg: dict[str, Any], now: datetime | None = None
) -> tuple[float, str]:
    """現在時刻から適用すべき温度スケジュールの目標温度と時間帯名を返す。"""
    now = now or datetime.now(tz=_JST)
    schedule = cfg.get("temperature_schedule")
    if not schedule:
        # v4互換: temperature_schedule がなければ旧方式
        temp_cfg = cfg.get("temperature", {})
        night = is_nighttime(cfg, now)
        target = temp_cfg.get("target_night", 17.0) if night else temp_cfg.get("target_day", 26.0)
        return float(target), "night" if night else "day"

    sun_times = get_sun_times(cfg, now)
    sunrise = sun_times["sunrise"]
    sunset = sun_times["sunset"]

    pre_dawn_offset = schedule.get("pre_dawn", {}).get("offset_min", -60)
    pre_dawn_start = sunrise + timedelta(minutes=pre_dawn_offset)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    afternoon_end = now.replace(hour=14, minute=0, second=0, microsecond=0)

    if now < pre_dawn_start:
        return float(schedule["night"]["target"]), "night"
    elif now < sunrise:
        return float(schedule["pre_dawn"]["target"]), "pre_dawn"
    elif now < noon:
        return float(schedule["morning"]["target"]), "morning"
    elif now < afternoon_end:
        return float(schedule["afternoon"]["target"]), "afternoon"
    elif now < sunset:
        return float(schedule["evening"]["target"]), "evening"
    else:
        return float(schedule["night"]["target"]), "night"


# ──────────────────────────────────────────────
# v5: 外気温ベース開度テーブル
# ──────────────────────────────────────────────

def get_target_opening(outdoor_temp: float, step_table: dict[int, int]) -> int:
    """外気温から目標開度(%)を線形補間で算出。"""
    temps = sorted(step_table.keys())
    if not temps:
        return 0
    if outdoor_temp <= temps[0]:
        return step_table[temps[0]]
    if outdoor_temp >= temps[-1]:
        return step_table[temps[-1]]

    for i in range(len(temps) - 1):
        t_low = temps[i]
        t_high = temps[i + 1]
        if t_low <= outdoor_temp < t_high:
            ratio = (outdoor_temp - t_low) / (t_high - t_low)
            pct_low = step_table[t_low]
            pct_high = step_table[t_high]
            return int(pct_low + ratio * (pct_high - pct_low))

    return 0


def opening_to_relay_duration(
    current_pct: int, target_pct: int, cfg: dict[str, Any]
) -> tuple[str, int]:
    """開度差からリレー駆動方向("open"/"close"/"none")と時間(秒)を算出。"""
    motor_cfg = cfg.get("window_motor", {})
    full_duration = motor_cfg.get("full_open_duration_sec", 120)
    min_change = motor_cfg.get("min_change_pct", 5)

    diff = target_pct - current_pct
    if abs(diff) < min_change:
        return ("none", 0)

    direction = "open" if diff > 0 else "close"
    duration = int(abs(diff) / 100 * full_duration)
    return (direction, max(1, duration))


# ──────────────────────────────────────────────
# TiDE 予測 JSON 読み込み
# ──────────────────────────────────────────────

def load_tide_forecast(
    path: str = DEFAULT_TIDE_FORECAST_PATH,
    max_age_sec: int = TIDE_FORECAST_MAX_AGE_SEC,
) -> dict[str, Any] | None:
    """
    tide_forecast.json を読み込む。

    Returns:
        dict: 予測データ
        None: ファイルが存在しない、JSON 不正、または max_age_sec 以上古い場合
    """
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    generated_at_str = data.get("generated_at")
    if not generated_at_str:
        return None

    try:
        generated_at = datetime.fromisoformat(generated_at_str)
        now = datetime.now(tz=generated_at.tzinfo or ZoneInfo("Asia/Tokyo"))
        age_sec = (now - generated_at).total_seconds()
        if age_sec > max_age_sec:
            logger.info(
                "TiDE予測が古い (%.0f秒 > %d秒) → スキップ", age_sec, max_age_sec
            )
            return None
    except (ValueError, TypeError):
        return None

    return data


# ──────────────────────────────────────────────
# v5: 朝換気（急昇温監視）
# ──────────────────────────────────────────────

def load_temp_history(path: str = DEFAULT_TEMP_HISTORY_PATH) -> list[dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_temp_history(history: list[dict[str, Any]], path: str = DEFAULT_TEMP_HISTORY_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(history, ensure_ascii=False))


def update_temp_history(
    history: list[dict[str, Any]], indoor_temp: float, now: datetime
) -> list[dict[str, Any]]:
    """温度履歴に追加し、60分より古いデータを削除。"""
    history.append({"t": now.isoformat(), "temp": indoor_temp})
    cutoff = now - timedelta(minutes=60)
    return [h for h in history if datetime.fromisoformat(h["t"]) > cutoff]


def get_rise_rate_per_hour(history: list[dict[str, Any]]) -> float | None:
    """直近の温度履歴から1時間あたりの上昇率(℃/h)を算出。"""
    if len(history) < 2:
        return None
    oldest = history[0]
    newest = history[-1]
    dt_diff = (
        datetime.fromisoformat(newest["t"]) - datetime.fromisoformat(oldest["t"])
    ).total_seconds()
    if dt_diff < 300:  # 5分未満のデータでは判定しない
        return None
    temp_diff = newest["temp"] - oldest["temp"]
    return temp_diff / dt_diff * 3600  # ℃/hour


# ──────────────────────────────────────────────
# v5: CO2取得
# ──────────────────────────────────────────────

def get_indoor_co2(sensors: dict[str, Any]) -> float | None:
    """CCMのCO2センサー値(ppm)を取得。"""
    val = _sensor_val(sensors, "agriha/h01/ccm/InAirCO2")
    return float(val) if val is not None else None


# ──────────────────────────────────────────────
# v5: 湿度取得
# ──────────────────────────────────────────────

def get_indoor_humidity(sensors: dict[str, Any]) -> float | None:
    """CCMの湿度センサー値(%)を取得。"""
    val = _sensor_val(sensors, "agriha/h01/ccm/InAirHumid")
    return float(val) if val is not None else None


# ──────────────────────────────────────────────
# v5: 窓ポジション管理（ソフトウェア推定）
# ──────────────────────────────────────────────

def load_window_position(path: str = DEFAULT_WINDOW_POS_PATH) -> int:
    """現在の推定窓開度(%)を読み込む。"""
    try:
        data = json.loads(Path(path).read_text())
        return int(data.get("opening_pct", 0))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def save_window_position(opening_pct: int, path: str = DEFAULT_WINDOW_POS_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "opening_pct": opening_pct,
        "updated_at": datetime.now(tz=_JST).isoformat(),
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False))


# ──────────────────────────────────────────────
# センサー値取得ヘルパー
# ──────────────────────────────────────────────

def _sensor_val(sensors: dict[str, Any], key: str) -> float | None:
    entry = sensors.get("sensors", {}).get(key)
    if entry is None:
        return None
    return entry.get("value")


def get_indoor_temp(sensors: dict[str, Any]) -> float | None:
    return _sensor_val(sensors, "agriha/h01/ccm/InAirTemp")


def get_outdoor_temp(sensors: dict[str, Any]) -> float | None:
    """外気温を取得。Misol気象ステーション or CCM。"""
    misol = get_misol(sensors)
    val = misol.get("temperature_c")
    if val is not None:
        return float(val)
    # フォールバック: CCM外気温
    val = _sensor_val(sensors, "agriha/h01/ccm/WAirTemp")
    return float(val) if val is not None else None


def get_misol(sensors: dict[str, Any]) -> dict[str, Any]:
    entry = sensors.get("sensors", {}).get("agriha/farm/weather/misol", {})
    return entry


def get_insolar(sensors: dict[str, Any]) -> float:
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
    now: datetime | None = None,
    channel_map_path: str | Path | None = None,
    temp_history: list[dict[str, Any]] | None = None,
    current_window_pct: int = 0,
    tide_forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    ルール評価（v5: priority chain順）。

    Returns:
        {
            "relay_actions": [(ch, value, duration_sec_or_None), ...],
            "solar_acc": updated accumulator dict,
            "triggered_rules": [str, ...],
            "target_opening_pct": int,
            "schedule_period": str,
            "temp_history": list,
        }
    """
    now = now or datetime.now(tz=_JST)
    relay_actions: list[tuple[int, int, int | None]] = []
    triggered_rules: list[str] = []

    wind_cfg = cfg["wind"]
    rain_cfg = cfg["rain"]
    _ch_config = load_channel_map(channel_map_path)
    groups: list[dict] = load_window_groups(_ch_config)

    misol = get_misol(sensors)
    rainfall = misol.get("rainfall", 0.0) or 0.0
    wind_speed = misol.get("wind_speed_ms", 0.0) or 0.0
    wind_dir = misol.get("wind_direction", 0) or 0
    indoor_temp = get_indoor_temp(sensors)
    outdoor_temp = get_outdoor_temp(sensors)
    indoor_co2 = get_indoor_co2(sensors)
    indoor_humidity = get_indoor_humidity(sensors)
    insolar = get_insolar(sensors)

    # 変温管理: 時間帯別目標温度
    target_temp, schedule_period = get_schedule_target(cfg, now)

    # 温度履歴更新
    if temp_history is None:
        temp_history = []
    if indoor_temp is not None:
        temp_history = update_temp_history(temp_history, indoor_temp, now)
    rise_rate = get_rise_rate_per_hour(temp_history)

    # 目標開度の決定（v5: 外気温ベースステップテーブル）
    target_opening_pct = 0  # デフォルト: 全閉
    window_skip = False  # 上位ルールで窓制御済みフラグ

    def _acted_chs() -> set[int]:
        return {a[0] for a in relay_actions}

    def _close_group(g: dict) -> None:
        relay_actions.append((g["close_channel"], 1, None))
        relay_actions.append((g["open_channel"], 0, None))

    def _open_group(g: dict) -> None:
        relay_actions.append((g["open_channel"], 1, None))
        relay_actions.append((g["close_channel"], 0, None))

    def _group_acted(g: dict) -> bool:
        chs = _acted_chs()
        return g["open_channel"] in chs or g["close_channel"] in chs

    # ═══════════════════════════════════════════
    # Priority 1: 降雨 → 全閉
    # ═══════════════════════════════════════════
    if rainfall > rain_cfg["threshold_mm_h"]:
        triggered_rules.append("rain_close_all")
        target_opening_pct = 0
        window_skip = True
        for g in groups:
            _close_group(g)
        logger.info("P1 rain: rainfall=%.2f > %.2f → 全窓閉", rainfall, rain_cfg["threshold_mm_h"])

    # ═══════════════════════════════════════════
    # Priority 2: 強風 → 風向別閉鎖
    # ═══════════════════════════════════════════
    if not window_skip and wind_speed > wind_cfg["strong_wind_threshold_ms"]:
        triggered_rules.append("strong_wind")
        window_skip = True
        matched = [g for g in groups if wind_dir in g["wind_close_directions"]]
        if matched:
            for g in matched:
                logger.info("P2 wind: %.1fm/s dir=%d → %s閉", wind_speed, wind_dir, g["name"])
                _close_group(g)
        else:
            logger.info("P2 wind: %.1fm/s dir=%d → 方角不明 → 全窓閉", wind_speed, wind_dir)
            for g in groups:
                _close_group(g)

    # ═══════════════════════════════════════════
    # Priority 3: 急昇温 → 強制換気
    # ═══════════════════════════════════════════
    if not window_skip and rise_rate is not None:
        morning_cfg = cfg.get("morning_ventilation", {})
        max_rate = morning_cfg.get("max_rise_rate_per_hour", 3.0)
        if rise_rate > max_rate:
            triggered_rules.append("rapid_rise_vent")
            target_opening_pct = 60  # 急昇温時は60%以上
            logger.info(
                "P3 rapid_rise: %.1f℃/h > %.1f → 強制換気 開度%d%%",
                rise_rate, max_rate, target_opening_pct,
            )

    # ═══════════════════════════════════════════
    # Priority 3.5: TiDE 予測先行換気
    # 1時間後の湿度予測が TIDE_HUMIDITY_PREVENT_THRESHOLD(80%) 超なら先行換気
    # ═══════════════════════════════════════════
    if not window_skip and tide_forecast is not None:
        humid_preds = (
            tide_forecast.get("predictions", {}).get("InAirHumid", [])
        )
        if len(humid_preds) >= 1 and humid_preds[0] > TIDE_HUMIDITY_PREVENT_THRESHOLD:
            triggered_rules.append("tide_preemptive_vent")
            target_opening_pct = max(target_opening_pct, 30)
            logger.info(
                "P3.5 TiDE予測: 1h後湿度%.1f%% > %.0f%% → 先行換気 開度≥%d%%",
                humid_preds[0],
                TIDE_HUMIDITY_PREVENT_THRESHOLD,
                target_opening_pct,
            )

    # ═══════════════════════════════════════════
    # Priority 4: 湿度 → 換気
    # ═══════════════════════════════════════════
    if not window_skip and indoor_humidity is not None:
        humidity_cfg = cfg.get("humidity", {})
        vent_start = humidity_cfg.get("ventilation_start", 85)
        vent_stop = humidity_cfg.get("ventilation_stop", 75)
        if indoor_humidity >= vent_start:
            triggered_rules.append("humidity_vent")
            target_opening_pct = max(target_opening_pct, 30)
            logger.info(
                "P4 humidity: %.1f%% >= %d%% → 換気 開度≥%d%%",
                indoor_humidity, vent_start, target_opening_pct,
            )
        elif indoor_humidity <= vent_stop:
            # 湿度十分低い → 湿度制約は解除（他ルールに任せる）
            logger.info("P4 humidity: %.1f%% <= %d%% → 湿度制約なし", indoor_humidity, vent_stop)

    # ═══════════════════════════════════════════
    # Priority 5: CO2 → 換気
    # ═══════════════════════════════════════════
    if not window_skip and indoor_co2 is not None:
        co2_cfg = cfg.get("co2", {})
        critical_low = co2_cfg.get("critical_low", 300)
        vent_trigger = co2_cfg.get("ventilation_trigger", 350)
        if indoor_co2 <= critical_low:
            triggered_rules.append("co2_critical_vent")
            target_opening_pct = max(target_opening_pct, 60)
            logger.info(
                "P5 CO2: %.0fppm <= %d → 強制換気 開度≥%d%%",
                indoor_co2, critical_low, target_opening_pct,
            )
        elif indoor_co2 <= vent_trigger:
            triggered_rules.append("co2_vent")
            target_opening_pct = max(target_opening_pct, 30)
            logger.info(
                "P5 CO2: %.0fppm <= %d → CO2補充換気 開度≥%d%%",
                indoor_co2, vent_trigger, target_opening_pct,
            )

    # ═══════════════════════════════════════════
    # Priority 6: 時間帯+温度制御（変温管理+開度テーブル）
    # ═══════════════════════════════════════════
    if not window_skip:
        nighttime = schedule_period == "night"

        if nighttime:
            triggered_rules.append("nighttime_close")
            # 夜間は基本全閉だが、CO2/湿度ルールが上書きしている場合はそちらを尊重
            if target_opening_pct == 0:
                logger.info("P6 night: 夜間 → 全窓閉")
            else:
                logger.info(
                    "P6 night: 夜間だがCO2/湿度ルールで開度%d%%を維持",
                    target_opening_pct,
                )
        elif indoor_temp is not None and outdoor_temp is not None:
            triggered_rules.append("temp_schedule")
            logger.info(
                "P6 temp: period=%s target=%.1f℃ indoor=%.1f℃ outdoor=%.1f℃",
                schedule_period, target_temp, indoor_temp, outdoor_temp,
            )

            if indoor_temp > target_temp:
                # 室温が目標超過 → 外気温ベースの開度テーブルで開ける
                step_table = cfg.get("window_step_table", {})
                if step_table:
                    # YAMLのキーは文字列になりうるのでint変換
                    table = {int(k): int(v) for k, v in step_table.items()}
                    table_opening = get_target_opening(outdoor_temp, table)
                    target_opening_pct = max(target_opening_pct, table_opening)
                    logger.info(
                        "P6 step_table: outdoor=%.1f℃ → table開度=%d%% → target=%d%%",
                        outdoor_temp, table_opening, target_opening_pct,
                    )
                else:
                    # テーブルなし → 全開
                    target_opening_pct = max(target_opening_pct, 100)
            elif indoor_temp < target_temp - 2.0:
                # 室温が目標-2℃未満 → 閉める（ただしCO2/湿度が上書きしてる場合を尊重）
                if target_opening_pct == 0:
                    logger.info(
                        "P6 temp_low: %.1f℃ < %.1f-2 → 全窓閉",
                        indoor_temp, target_temp,
                    )
                else:
                    logger.info(
                        "P6 temp_low: %.1f℃ < %.1f-2 だがCO2/湿度で開度%d%%維持",
                        indoor_temp, target_temp, target_opening_pct,
                    )
            else:
                # 適温域 → CO2/湿度ルールの開度を維持
                logger.info(
                    "P6 temp_ok: %.1f℃ (目標%.1f±2) 適温域 → 開度%d%%維持",
                    indoor_temp, target_temp, target_opening_pct,
                )

    # ═══════════════════════════════════════════
    # 窓開度 → リレー駆動変換（雨・風でskipされてなければ）
    # ═══════════════════════════════════════════
    if not window_skip:
        direction, duration = opening_to_relay_duration(
            current_window_pct, target_opening_pct, cfg
        )
        if direction != "none":
            triggered_rules.append(f"window_{direction}")
            for g in groups:
                if not _group_acted(g):
                    if direction == "open":
                        relay_actions.append((g["open_channel"], 1, duration))
                        relay_actions.append((g["close_channel"], 0, None))
                    else:
                        relay_actions.append((g["close_channel"], 1, duration))
                        relay_actions.append((g["open_channel"], 0, None))
            logger.info(
                "Window: %d%% → %d%% (%s %ds)",
                current_window_pct, target_opening_pct, direction, duration,
            )
        else:
            logger.info("Window: %d%% → %d%% (変化なし)", current_window_pct, target_opening_pct)

    # ═══════════════════════════════════════════
    # Priority 7: 日射比例灌水
    # ═══════════════════════════════════════════
    _eval_irrigation(cfg, crop_cfg, insolar, solar_acc, relay_actions, triggered_rules)

    return {
        "relay_actions": relay_actions,
        "solar_acc": solar_acc,
        "triggered_rules": triggered_rules,
        "target_opening_pct": target_opening_pct,
        "schedule_period": schedule_period,
        "target_temp": target_temp,
        "temp_history": temp_history,
    }


def _eval_irrigation(
    cfg: dict[str, Any],
    crop_cfg: dict[str, Any],
    insolar: float,
    solar_acc: dict[str, Any],
    relay_actions: list[tuple[int, int, int | None]],
    triggered_rules: list[str],
) -> None:
    irr_cfg = cfg["irrigation"]
    irr_ch = irr_cfg["channel"]
    solar_threshold = get_solar_threshold(crop_cfg)
    duration_sec = get_irrigation_duration(crop_cfg)

    # 10分間の日射積算量（cron 10分毎）
    solar_mj_10min = insolar * 600.0 / 1_000_000.0
    solar_acc["accumulated_mj"] = solar_acc.get("accumulated_mj", 0.0) + solar_mj_10min
    logger.info(
        "P7 irrigation: InSolar=%.1fW/m² → +%.4f MJ → 累積=%.4f MJ (閾値=%.2f)",
        insolar, solar_mj_10min, solar_acc["accumulated_mj"], solar_threshold,
    )

    if solar_acc["accumulated_mj"] >= solar_threshold:
        triggered_rules.append("solar_irrigation")
        logger.info(
            "P7 irrigation: 日射積算 %.4f >= %.2f → 灌水実行 ch%d %dsec",
            solar_acc["accumulated_mj"], solar_threshold, irr_ch, duration_sec,
        )
        relay_actions.append((irr_ch, 1, duration_sec))
        solar_acc["accumulated_mj"] = 0.0
        solar_acc["irrigations_today"] = solar_acc.get("irrigations_today", 0) + 1
        solar_acc["last_irrigation_at"] = datetime.now(tz=_JST).isoformat()


# ──────────────────────────────────────────────
# 状態保存
# ──────────────────────────────────────────────

def save_state(state_path: str, result: dict[str, Any]) -> None:
    state = {
        "last_run_at": datetime.now(tz=_JST).isoformat(),
        "triggered_rules": result.get("triggered_rules", []),
        "schedule_period": result.get("schedule_period", ""),
        "target_temp": result.get("target_temp", 0),
        "target_opening_pct": result.get("target_opening_pct", 0),
        "relay_actions": [
            {"channel": a[0], "value": a[1], "duration_sec": a[2]}
            for a in result.get("relay_actions", [])
        ],
    }
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# weather flag 更新
# ──────────────────────────────────────────────

def update_weather_flags(
    cfg: dict[str, Any],
    sensors: dict[str, Any],
    flag_dir: str = FLAG_DIR,
) -> None:
    flag_path = Path(flag_dir)
    flag_path.mkdir(parents=True, exist_ok=True)
    rain_cfg = cfg["rain"]
    wind_cfg = cfg["wind"]

    misol = get_misol(sensors)
    rainfall = misol.get("rainfall", 0.0) or 0.0
    wind_speed = misol.get("wind_speed_ms", 0.0) or 0.0
    now_str = datetime.now(tz=_JST).isoformat()

    rain_flag = flag_path / "rain_flag"
    if rainfall > rain_cfg["threshold_mm_h"]:
        rain_flag.write_text(now_str)
    elif rain_flag.exists():
        rain_flag.unlink()

    wind_flag = flag_path / "wind_flag"
    if wind_speed > wind_cfg["strong_wind_threshold_ms"]:
        wind_flag.write_text(json.dumps({"timestamp": now_str, "wind_speed_ms": wind_speed}))
    elif wind_flag.exists():
        wind_flag.unlink()


# ──────────────────────────────────────────────
# メインエントリポイント
# ──────────────────────────────────────────────

def run(
    config_path: str = DEFAULT_CONFIG_PATH,
    crop_config_path: str = DEFAULT_CROP_CONFIG_PATH,
    lockout_path: str = DEFAULT_LOCKOUT_PATH,
    solar_acc_path: str = DEFAULT_SOLAR_ACC_PATH,
    state_path: str = DEFAULT_STATE_PATH,
    api_base: str = DEFAULT_API_BASE,
    channel_map_path: str | None = None,
    flag_dir: str = FLAG_DIR,
    temp_history_path: str = DEFAULT_TEMP_HISTORY_PATH,
    window_pos_path: str = DEFAULT_WINDOW_POS_PATH,
) -> int:
    """
    rule_engine のメイン処理。
    Returns: 0=正常終了, 1=スキップ/エラー
    """
    _setup_logging()
    logger.info("rule_engine.py v5 起動")

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

    # Step 3: センサーデータ取得
    try:
        with httpx.Client(timeout=timeout) as client:
            sensors = fetch_sensors(client, api_base)
            status = fetch_status(client, api_base)

            # weather flag 更新
            update_weather_flags(cfg, sensors, flag_dir=flag_dir)

            # CommandGate ロックアウト確認
            if status.get("locked_out", False):
                logger.info("CommandGate ロックアウト中 → スキップ")
                return 1

            # Step 4: 状態読み込み
            solar_acc = load_solar_accumulator(solar_acc_path)
            temp_history = load_temp_history(temp_history_path)
            current_window_pct = load_window_position(window_pos_path)
            tide_forecast = load_tide_forecast()

            # Step 5: ルール評価
            result = evaluate_rules(
                cfg, crop_cfg, sensors, status, solar_acc,
                channel_map_path=channel_map_path,
                temp_history=temp_history,
                current_window_pct=current_window_pct,
                tide_forecast=tide_forecast,
            )

            # Step 6: アクション実行
            relay_actions = result["relay_actions"]
            if relay_actions:
                seen: dict[int, tuple[int, int | None]] = {}
                for ch, val, dur in relay_actions:
                    seen[ch] = (val, dur)
                for ch, (val, dur) in seen.items():
                    post_relay(client, api_base, ch, val, dur)
            else:
                logger.info("アクションなし")

        # Step 7: 状態保存
        save_solar_accumulator(result["solar_acc"], solar_acc_path)
        save_temp_history(result["temp_history"], temp_history_path)
        save_window_position(result["target_opening_pct"], window_pos_path)
        save_state(state_path, result)
        logger.info(
            "完了: period=%s target_temp=%.1f℃ opening=%d%% rules=%s actions=%d",
            result["schedule_period"],
            result["target_temp"],
            result["target_opening_pct"],
            result["triggered_rules"],
            len(relay_actions),
        )
        return 0

    except httpx.HTTPError as e:
        logger.error("REST API エラー: %s → 安全側（操作しない）", e)
        return 1
    except Exception as e:
        logger.error("予期しないエラー: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
