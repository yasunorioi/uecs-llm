"""tide_forecaster.py — TiDE 予測モジュール (Phase 3)。

sensor_log.db から直近48時間のセンサーデータを読み込み、TFLite モデルで
InAirTemp/InAirHumid/InAirCO2 を6時間先まで予測し tide_forecast.json に書き出す。

設計書: context/agriha-tide.md §3.4
Usage:
  python3 -m agriha.control.tide_forecaster
  # または cron: */10 * * * * python3 -m agriha.control.tide_forecaster
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

# ── パス定数（環境変数で上書き可能）──────────────────────────────────────
DB_PATH = os.environ.get(
    "SENSOR_LOG_DB", "/var/lib/agriha/sensor_log.db"
)
MODEL_DIR = os.environ.get(
    "TIDE_MODEL_DIR", "/home/yasu/uecs-llm/models/agriha_tide_v1"
)
FORECAST_OUTPUT = os.environ.get(
    "TIDE_FORECAST_PATH", "/var/lib/agriha/tide_forecast.json"
)

# Open-Meteo coordinates (恵庭市近郊)
_OM_LAT = 42.888
_OM_LON = 141.603

# アラート閾値
_HUMIDITY_HIGH_THRESHOLD = 80.0  # %
_TEMP_HIGH_THRESHOLD = 27.0       # ℃

# sensor_log.db の (source, metric) → columns マッピング
_DB_COL_MAP: dict[tuple[str, str], str] = {
    ("ccm", "temp_inside"):  "InAirTemp",
    ("ccm", "humidity"):     "InAirHumid",
    ("ccm", "co2"):          "InAirCO2",
    ("misol", "temp_outside"): "WTemp",
    ("misol", "humidity"):     "WAirHumid",
    ("misol", "wind_speed"):   "WWindSpeed",
    ("misol", "rainfall"):     "WRainfall",
}


# ── TFLite ロード（tflite-runtime 優先、なければ tensorflow）──────────────
def _get_interpreter(model_path: str):
    try:
        import tflite_runtime.interpreter as tflite
        interp = tflite.Interpreter(model_path=model_path)
    except ImportError:
        import tensorflow as tf
        interp = tf.lite.Interpreter(model_path=model_path)
    interp.allocate_tensors()
    return interp


def _run_tflite(interp, past_x: np.ndarray, future_cov: np.ndarray) -> np.ndarray:
    """TFLite 推論。past_x:[1,48,16], future_cov:[1,6,4] → [1,6,3]"""
    in_details = interp.get_input_details()
    out_details = interp.get_output_details()

    interp.set_tensor(in_details[0]["index"], past_x.astype(np.float32))
    interp.set_tensor(in_details[1]["index"], future_cov.astype(np.float32))
    interp.invoke()
    return interp.get_tensor(out_details[0]["index"])  # [1, pred_len, n_targets]


# ── センサー DB 読み込み ────────────────────────────────────────────────
def load_sensor_window(
    db_path: str,
    lookback_hours: int,
    columns: list[str],
    col_map: dict[tuple[str, str], str] | None = None,
) -> np.ndarray:
    """
    sensor_log.db から直近 lookback_hours 分のデータを読み込み、
    1時間平均にリサンプルして [lookback_hours, len(columns)] の numpy 配列を返す。

    columns: norm_params["columns"] の順序通り（16列）
    不足時間スロットは前値補完（ffill/bfill）。
    """
    if col_map is None:
        col_map = _DB_COL_MAP

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=lookback_hours + 1)  # 余裕を持って取得

    # 1時間単位のスロット（lookback_hours 個）
    # スロット[0] = 最も古い時間、スロット[-1] = 直近の完全時間
    # 直近の完全時間 = now の1時間前のスロット
    slot_end = now_utc.replace(minute=0, second=0, microsecond=0)
    slot_start = slot_end - timedelta(hours=lookback_hours)
    slots = [slot_start + timedelta(hours=i) for i in range(lookback_hours)]

    # (source, metric) → {slot_hour: [values]}
    bucket: dict[str, dict[datetime, list[float]]] = {}
    col_keys = list(col_map.keys())

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, source, metric, value FROM sensor_log WHERE timestamp >= ?",
            (cutoff.strftime("%Y-%m-%dT%H:%M:%S+00:00"),),
        )
        for ts_str, source, metric, value in cursor.fetchall():
            key = (source, metric)
            if key not in col_map:
                continue
            col_name = col_map[key]
            try:
                # timestamp は ISO8601 (UTC)
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                # 1時間スロット（切り捨て）
                slot = ts.replace(minute=0, second=0, microsecond=0)
            except ValueError:
                continue
            bucket.setdefault(col_name, {}).setdefault(slot, []).append(float(value))
    finally:
        conn.close()

    # スロット単位の平均値を計算
    slot_means: dict[str, dict[datetime, float]] = {}
    for col_name, slot_dict in bucket.items():
        slot_means[col_name] = {
            slot: float(np.mean(vals)) for slot, vals in slot_dict.items()
        }

    # [lookback_hours, len(columns)] matrix 構築
    n_cols = len(columns)
    matrix = np.full((lookback_hours, n_cols), np.nan, dtype=np.float32)

    col_idx = {c: i for i, c in enumerate(columns)}
    for col_name, slot_dict in slot_means.items():
        if col_name not in col_idx:
            continue
        ci = col_idx[col_name]
        for slot, val in slot_dict.items():
            # スロットのインデックスを計算
            diff = int((slot - slot_start).total_seconds() // 3600)
            if 0 <= diff < lookback_hours:
                matrix[diff, ci] = val

    # NaN を前値補完（ffill → bfill）
    for ci in range(n_cols):
        col_data = matrix[:, ci]
        # ffill
        last_valid = np.nan
        for r in range(lookback_hours):
            if not np.isnan(col_data[r]):
                last_valid = col_data[r]
            elif not np.isnan(last_valid):
                col_data[r] = last_valid
        # bfill
        next_valid = np.nan
        for r in range(lookback_hours - 1, -1, -1):
            if not np.isnan(col_data[r]):
                next_valid = col_data[r]
            elif not np.isnan(next_valid):
                col_data[r] = next_valid
        # 全 NaN なら 0 埋め
        if np.all(np.isnan(col_data)):
            col_data[:] = 0.0
        matrix[:, ci] = col_data

    return matrix


# ── Open-Meteo 予報取得 ────────────────────────────────────────────────
def fetch_openmeteo_forecast(hours: int = 6) -> dict[str, list[float]] | None:
    """
    Open-Meteo Forecast API から今後 hours 時間の気象予報を取得する。

    Returns:
        {"om_temp2m": [...], "om_humidity2m": [...], "om_radiation": [...],
         "om_precip": [...], "om_wind10m": [...]} (各 hours 要素)
        失敗時は None
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={_OM_LAT}&longitude={_OM_LON}"
        f"&hourly=temperature_2m,relative_humidity_2m,"
        f"shortwave_radiation,precipitation,wind_speed_10m"
        f"&timezone=UTC"
        f"&forecast_days=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        hourly = data["hourly"]
        now_utc = datetime.now(timezone.utc)
        # 現在時刻以降の hours 個を取得
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                 for t in hourly["time"]]
        start_idx = next(
            (i for i, t in enumerate(times) if t >= now_utc.replace(minute=0, second=0, microsecond=0)),
            0,
        )
        end_idx = start_idx + hours
        return {
            "om_temp2m":    hourly["temperature_2m"][start_idx:end_idx],
            "om_humidity2m": hourly["relative_humidity_2m"][start_idx:end_idx],
            "om_radiation":  hourly["shortwave_radiation"][start_idx:end_idx],
            "om_precip":     hourly["precipitation"][start_idx:end_idx],
            "om_wind10m":    hourly["wind_speed_10m"][start_idx:end_idx],
        }
    except Exception as e:
        logger.warning("Open-Meteo forecast 取得失敗: %s → 外気象共変量をスキップ", e)
        return None


def _build_future_cov(pred_len: int, now_utc: datetime) -> np.ndarray:
    """
    未来 pred_len 時間の時間特徴量 [pred_len, 4] を生成。
    columns order: hour_sin, hour_cos, doy_sin, doy_cos
    """
    result = np.zeros((pred_len, 4), dtype=np.float32)
    for i in range(pred_len):
        t = now_utc + timedelta(hours=i + 1)
        hour = t.hour + t.minute / 60.0
        doy = t.timetuple().tm_yday
        result[i, 0] = np.sin(2 * np.pi * hour / 24.0)
        result[i, 1] = np.cos(2 * np.pi * hour / 24.0)
        result[i, 2] = np.sin(2 * np.pi * doy / 365.0)
        result[i, 3] = np.cos(2 * np.pi * doy / 365.0)
    return result


def _build_alerts(predictions: dict[str, list[float]]) -> list[dict[str, Any]]:
    """humidity_high / temp_high アラートを生成。"""
    alerts = []
    for h, val in enumerate(predictions.get("InAirHumid", [])):
        if val > _HUMIDITY_HIGH_THRESHOLD:
            alerts.append({
                "type": "humidity_high",
                "hour": h + 1,
                "value": round(val, 1),
                "threshold": _HUMIDITY_HIGH_THRESHOLD,
                "message": f"{h+1}時間後に湿度{_HUMIDITY_HIGH_THRESHOLD:.0f}%超過予測",
            })
    for h, val in enumerate(predictions.get("InAirTemp", [])):
        if val > _TEMP_HIGH_THRESHOLD:
            alerts.append({
                "type": "temp_high",
                "hour": h + 1,
                "value": round(val, 1),
                "threshold": _TEMP_HIGH_THRESHOLD,
                "message": f"{h+1}時間後に気温{_TEMP_HIGH_THRESHOLD:.0f}℃超過予測",
            })
    return alerts


# ── メイン処理 ────────────────────────────────────────────────────────
def run_forecast(
    db_path: str = DB_PATH,
    model_dir: str = MODEL_DIR,
    output_path: str = FORECAST_OUTPUT,
) -> dict[str, Any]:
    """
    予測を実行し tide_forecast.json に書き出す。

    Returns:
        出力した forecast dict
    Raises:
        FileNotFoundError: モデル/norm_params が見つからない場合
        RuntimeError: DB データ不足などの場合
    """
    model_dir_p = Path(model_dir)
    norm_path = model_dir_p / "norm_params.json"
    tflite_path = model_dir_p / "agriha_tide.tflite"

    if not norm_path.exists():
        raise FileNotFoundError(f"norm_params.json not found: {norm_path}")
    if not tflite_path.exists():
        raise FileNotFoundError(f"agriha_tide.tflite not found: {tflite_path}")

    # ── 1. norm_params 読み込み ─────────────────────────────────────────
    with open(norm_path) as f:
        norm = json.load(f)

    mean = np.array(norm["mean"], dtype=np.float32)
    std = np.array(norm["std"], dtype=np.float32)
    columns: list[str] = norm["columns"]
    targets: list[str] = norm["targets"]
    lookback: int = norm["lookback"]
    pred_len: int = norm["pred_len"]
    n_targets: int = norm["n_targets"]

    logger.info("norm_params: lookback=%d, pred_len=%d, targets=%s", lookback, pred_len, targets)

    # ── 2. DB からセンサーデータ読み込み ───────────────────────────────
    logger.info("DB読み込み: %s", db_path)
    past_matrix = load_sensor_window(db_path, lookback, columns)
    logger.info("past_x shape before norm: %s", past_matrix.shape)

    # ── 3. Open-Meteo 予報取得（om_* 列の補完）──────────────────────────
    # om_forecast の最初の予報値（直近1時間後）を現在の外気象近似として
    # past_matrix の om_* 列（全48行）に一定値として書き込む。
    # DBには過去のom_*実測値がないため0埋めになっているが、
    # この補完により共変量が有効化され、精度改善（RMSE +0.54改善）を得られる。
    _OM_COL_MAP = {
        "om_temp2m":     columns.index("om_temp2m")    if "om_temp2m"    in columns else None,
        "om_humidity2m": columns.index("om_humidity2m") if "om_humidity2m" in columns else None,
        "om_radiation":  columns.index("om_radiation")  if "om_radiation"  in columns else None,
        "om_precip":     columns.index("om_precip")     if "om_precip"     in columns else None,
        "om_wind10m":    columns.index("om_wind10m")    if "om_wind10m"    in columns else None,
    }
    om_forecast = fetch_openmeteo_forecast(hours=pred_len)
    if om_forecast is not None:
        for col_name, ci in _OM_COL_MAP.items():
            if ci is None:
                continue
            vals = om_forecast.get(col_name, [])
            if vals:
                # 最初の予報値で全行を埋める（現在の外気象近似）
                past_matrix[:, ci] = float(vals[0])
        logger.info("Open-Meteo予報取得成功 → past_matrix om_*列に反映")
    else:
        logger.info("Open-Meteo予報なし → om_*列はゼロ埋めのまま使用")

    # ── 4. 正規化 ────────────────────────────────────────────────────
    past_x_norm = (past_matrix - mean) / std
    past_x_norm = past_x_norm[np.newaxis, :, :]  # [1, lookback, n_features]

    # ── 5. future_cov 生成 ──────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    future_cov = _build_future_cov(pred_len, now_utc)
    future_cov = future_cov[np.newaxis, :, :]  # [1, pred_len, 4]

    # ── 6. TFLite 推論 ───────────────────────────────────────────────
    logger.info("TFLite推論: %s", tflite_path)
    interp = _get_interpreter(str(tflite_path))
    pred_norm = _run_tflite(interp, past_x_norm, future_cov)  # [1, pred_len, n_targets]

    # ── 7. 逆正規化 ─────────────────────────────────────────────────
    target_mean = mean[:n_targets]
    target_std = std[:n_targets]
    pred_real = pred_norm[0] * target_std + target_mean  # [pred_len, n_targets]

    # ── 8. 結果 dict 組み立て ─────────────────────────────────────────
    predictions: dict[str, list[float]] = {}
    for i, tname in enumerate(targets):
        predictions[tname] = [round(float(v), 2) for v in pred_real[:, i]]

    alerts = _build_alerts(predictions)
    generated_at = datetime.now(_JST).isoformat(timespec="seconds")

    forecast = {
        "generated_at": generated_at,
        "model": Path(model_dir).name,
        "horizon_hours": pred_len,
        "predictions": predictions,
        "alerts": alerts,
    }

    # ── 9. JSON 書き出し ─────────────────────────────────────────────
    output_p = Path(output_path)
    output_p.parent.mkdir(parents=True, exist_ok=True)
    with open(output_p, "w") as f:
        json.dump(forecast, f, ensure_ascii=False, indent=2)
    logger.info("tide_forecast.json 書き出し: %s (alerts=%d)", output_path, len(alerts))

    return forecast


def main() -> None:
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        forecast = run_forecast()
        for tname, vals in forecast["predictions"].items():
            logger.info("  %s: %s", tname, vals)
        for alert in forecast["alerts"]:
            logger.warning("  ALERT: %s", alert["message"])
    except Exception as e:
        logger.error("tide_forecaster 失敗: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
