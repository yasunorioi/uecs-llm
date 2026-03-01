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

# ──────────────────────────────────────────────
# 定数・デフォルトパス（環境変数で上書き可能）
# ──────────────────────────────────────────────
_JST = ZoneInfo("Asia/Tokyo")

DEFAULT_CONFIG_PATH = os.environ.get(
    "LAYER2_CONFIG_PATH", "/etc/agriha/layer2_config.yaml"
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
) -> dict[str, Any]:
    """
    ルール評価。各ルールの適用結果と更新後の solar_acc を返す。

    Returns:
        {
            "relay_actions": [(ch, value, duration_sec_or_None), ...],
            "solar_acc": updated accumulator dict,
            "triggered_rules": [str, ...],
        }
    """
    now = now or datetime.now(tz=_JST)
    relay_actions: list[tuple[int, int, int | None]] = []
    triggered_rules: list[str] = []

    temp_cfg = cfg["temperature"]
    wind_cfg = cfg["wind"]
    rain_cfg = cfg["rain"]
    window_chs: list[int] = temp_cfg["window_channels"]

    misol = get_misol(sensors)
    rainfall = misol.get("rainfall", 0.0) or 0.0
    wind_speed = misol.get("wind_speed_ms", 0.0) or 0.0
    wind_dir = misol.get("wind_direction", 0) or 0
    indoor_temp = get_indoor_temp(sensors)
    insolar = get_insolar(sensors)

    nighttime = is_nighttime(cfg, now)
    target_temp = temp_cfg["target_night"] if nighttime else temp_cfg["target_day"]

    # Layer 3 計画が有効かどうか
    layer3_active = current_plan is not None

    # ── Rule 6a: 降雨チェック ───────────────────────────
    if rainfall > rain_cfg["threshold_mm_h"]:
        triggered_rules.append("rain_close_all")
        for ch in window_chs:
            relay_actions.append((ch, 0, None))
        logger.info("Rule 6a: rainfall=%.2f > %.2f → 全窓閉", rainfall, rain_cfg["threshold_mm_h"])
        # 降雨時は以降の窓制御をスキップ（灌水のみ評価継続）
        _eval_irrigation(
            cfg, crop_cfg, insolar, solar_acc, relay_actions, triggered_rules
        )
        return {
            "relay_actions": relay_actions,
            "solar_acc": solar_acc,
            "triggered_rules": triggered_rules,
        }

    # ── Rule 6b: 強風チェック ──────────────────────────
    if wind_speed > wind_cfg["strong_wind_threshold_ms"]:
        triggered_rules.append("strong_wind")
        if wind_dir in wind_cfg["north_directions"]:
            logger.info("Rule 6b: 北風 %.1fm/s → 北側窓閉 ch%s", wind_speed, wind_cfg["north_channels"])
            for ch in wind_cfg["north_channels"]:
                relay_actions.append((ch, 0, None))
        elif wind_dir in wind_cfg["south_directions"]:
            logger.info("Rule 6b: 南風 %.1fm/s → 南側窓閉 ch%s", wind_speed, wind_cfg["south_channels"])
            for ch in wind_cfg["south_channels"]:
                relay_actions.append((ch, 0, None))
        else:
            logger.info("Rule 6b: 強風 %.1fm/s dir=%d → 全窓閉", wind_speed, wind_dir)
            for ch in window_chs:
                relay_actions.append((ch, 0, None))

    # ── Rule 6c: 時間帯制御 ───────────────────────────
    if nighttime:
        triggered_rules.append("nighttime_close")
        logger.info("Rule 6c: 夜間 → 全窓閉")
        for ch in window_chs:
            if not any(a[0] == ch for a in relay_actions):
                relay_actions.append((ch, 0, None))

    # ── Rule 6d: 温度制御（Layer 3 計画があれば委譲） ──
    if not layer3_active:
        if indoor_temp is not None:
            margin_open = temp_cfg["margin_open"]
            margin_close = temp_cfg["margin_close"]
            if indoor_temp > target_temp + margin_open:
                triggered_rules.append("temp_high_open")
                logger.info(
                    "Rule 6d: 高温 %.1f℃ > %.1f+%.1f → 側窓開",
                    indoor_temp, target_temp, margin_open,
                )
                for ch in window_chs:
                    if not any(a[0] == ch for a in relay_actions):
                        relay_actions.append((ch, 1, None))
            elif indoor_temp < target_temp - margin_close:
                triggered_rules.append("temp_low_close")
                logger.info(
                    "Rule 6d: 低温 %.1f℃ < %.1f-%.1f → 側窓閉",
                    indoor_temp, target_temp, margin_close,
                )
                for ch in window_chs:
                    if not any(a[0] == ch for a in relay_actions):
                        relay_actions.append((ch, 0, None))
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
# 状態保存
# ──────────────────────────────────────────────

def save_state(state_path: str, result: dict[str, Any]) -> None:
    state = {
        "last_run_at": datetime.now(tz=_JST).isoformat(),
        "triggered_rules": result.get("triggered_rules", []),
        "relay_actions": [
            {"channel": a[0], "value": a[1], "duration_sec": a[2]}
            for a in result.get("relay_actions", [])
        ],
    }
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps(state, ensure_ascii=False, indent=2))


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
) -> int:
    """
    rule_engine のメイン処理。
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

    # Step 3: センサーデータ取得 + CommandGate ロックアウト確認
    try:
        with httpx.Client(timeout=timeout) as client:
            sensors = fetch_sensors(client, api_base)
            status = fetch_status(client, api_base)

            # CommandGate ロックアウト確認
            if status.get("locked_out", False):
                logger.info("CommandGate ロックアウト中 → スキップ")
                return 1

            # Step 4: 日の出/日没計算は evaluate_rules 内で行う

            # Step 5: Layer 3 計画確認
            current_plan = load_current_plan(plan_path)

            # Step 6: ルール評価
            solar_acc = load_solar_accumulator(solar_acc_path)
            result = evaluate_rules(cfg, crop_cfg, sensors, status, solar_acc, current_plan)

            # Step 7: アクション実行（変更がある場合のみ）
            relay_actions = result["relay_actions"]
            if relay_actions:
                # 重複チャンネルは最後の設定を優先
                seen: dict[int, tuple[int, int | None]] = {}
                for ch, val, dur in relay_actions:
                    seen[ch] = (val, dur)
                for ch, (val, dur) in seen.items():
                    post_relay(client, api_base, ch, val, dur)
            else:
                logger.info("アクションなし")

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


if __name__ == "__main__":
    sys.exit(run())
