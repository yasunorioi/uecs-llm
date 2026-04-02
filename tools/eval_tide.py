"""eval_tide.py — TiDE モデル精度評価スクリプト。

SavedModel と test_data.json を読み込み、以下を評価する:
  1. RMSE/MAE（InAirTemp/InAirHumid/InAirCO2）
  2. 27℃超過アラート検知率（何ステップ前に予測できたか）
  3. 朝方(5:00-8:00 JST)湿度急変予測精度
  4. 共変量あり/なし精度比較（共変量をゼロマスクして再評価）

Usage:
  python3 tools/eval_tide.py --model models/agriha_tide_v1/
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent))

import numpy as np
import tensorflow as tf
from tensorflow import keras
# Import custom classes for deserialization
from train_tide import TiDE, ResidualBlock  # noqa: F401

logger = logging.getLogger(__name__)

TEMP_THRESHOLD = 27.0  # ℃ 高温アラート閾値
HUMIDITY_RAPID_CHANGE = 10.0  # %/h 朝方湿度急変閾値


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def evaluate_threshold_detection(
    targets_norm: np.ndarray,
    preds_norm: np.ndarray,
    target_idx: int,
    threshold_norm: float,
    pred_len: int,
) -> dict:
    """27℃超過をN時間前に検知できた率を評価。

    Args:
        targets_norm: [N, pred_len] 正規化済み実測値
        preds_norm:   [N, pred_len] 正規化済み予測値
        target_idx:   対象変数インデックス（InAirTemp=0）
        threshold_norm: 正規化済み閾値
        pred_len:     予測ホライゾン

    Returns:
        dict: lead_time別の検知率
    """
    # 実際に閾値を超えたサンプル
    actual_breach = targets_norm >= threshold_norm  # [N, pred_len]
    pred_breach = preds_norm >= threshold_norm

    results = {}
    for h in range(pred_len):
        # h ステップ後に実際に超過 → h ステップ前（現在）に予測できた率
        actual_at_h = actual_breach[:, h]
        pred_at_h = pred_breach[:, h]
        n_actual = actual_at_h.sum()
        if n_actual == 0:
            results[f"lead_{h+1}h"] = {"n_actual": 0, "detection_rate": None}
            continue
        tp = (actual_at_h & pred_at_h).sum()
        results[f"lead_{h+1}h"] = {
            "n_actual": int(n_actual),
            "n_detected": int(tp),
            "detection_rate": float(tp / n_actual),
        }
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Evaluate TiDE model accuracy")
    parser.add_argument("--model", type=Path, default=Path("models/agriha_tide_v1"),
                        help="SavedModel directory")
    parser.add_argument("--report", type=Path, default=None,
                        help="Output JSON report path (default: model_dir/eval_report.json)")
    args = parser.parse_args()

    if args.report is None:
        args.report = args.model / "eval_report.json"

    # ── 1. Load model and normalization params ─────────────────────────
    logger.info("Loading model: %s", args.model)
    keras_path = args.model / "model.keras"
    saved_model_path = args.model / "saved_model"
    model = None
    infer = None
    if keras_path.exists():
        model = tf.keras.models.load_model(
            str(keras_path),
            custom_objects={"TiDE": TiDE, "ResidualBlock": ResidualBlock},
            compile=False,
        )
    elif saved_model_path.exists():
        sm = tf.saved_model.load(str(saved_model_path))
        infer = sm.signatures.get("serving_default") or sm

    norm_path = args.model / "norm_params.json"
    with open(norm_path) as f:
        norm = json.load(f)

    mean = np.array(norm["mean"], dtype=np.float32)
    std = np.array(norm["std"], dtype=np.float32)
    targets = norm["targets"]
    n_targets = norm["n_targets"]
    lookback = norm["lookback"]
    pred_len = norm["pred_len"]
    n_future_cov = norm["n_future_cov"]

    logger.info("Targets: %s, lookback=%d, pred_len=%d", targets, lookback, pred_len)

    # ── 2. Load test data ─────────────────────────────────────────────
    test_path = args.model / "test_data.json"
    with open(test_path) as f:
        test = json.load(f)

    past_x = np.array(test["past_x"], dtype=np.float32)   # [N, lookback, n_feat]
    future_cov = np.array(test["future_cov"], dtype=np.float32)  # [N, pred_len, n_fut]
    tg_true = np.array(test["targets"], dtype=np.float32)  # [N, pred_len, n_targets]

    logger.info("Test samples: %d", len(past_x))

    # ── 3. Predict ────────────────────────────────────────────────────
    # Use Keras model directly if saved as keras format
    if model is not None:
        model_keras = model
        tg_pred = model_keras((past_x, future_cov), training=False).numpy()
    else:
        result = infer(tf.constant(past_x), tf.constant(future_cov))
        tg_pred = list(result.values())[0].numpy()
        model_keras = None

    # tg_pred: [N, pred_len, n_targets]

    # ── 4. Denormalize targets only ───────────────────────────────────
    target_mean = mean[:n_targets]
    target_std = std[:n_targets]

    tg_true_real = tg_true * target_std + target_mean    # [N, pred_len, n_targets]
    tg_pred_real = tg_pred * target_std + target_mean

    # ── 5. RMSE/MAE per target ────────────────────────────────────────
    metrics = {}
    for i, tname in enumerate(targets):
        y_true_i = tg_true_real[:, :, i]  # [N, pred_len]
        y_pred_i = tg_pred_real[:, :, i]
        metrics[tname] = {
            "rmse": rmse(y_true_i, y_pred_i),
            "mae": mae(y_true_i, y_pred_i),
            "n_samples": int(len(y_true_i)),
        }
        logger.info("%-12s RMSE=%.3f  MAE=%.3f", tname, metrics[tname]["rmse"], metrics[tname]["mae"])

    # ── 6. 27℃超過アラート検知率 ──────────────────────────────────────
    temp_alert = {}
    if "InAirTemp" in targets:
        ti = targets.index("InAirTemp")
        threshold_norm = (TEMP_THRESHOLD - target_mean[ti]) / target_std[ti]
        temp_alert = evaluate_threshold_detection(
            tg_true[:, :, ti], tg_pred[:, :, ti],
            target_idx=ti,
            threshold_norm=threshold_norm,
            pred_len=pred_len,
        )
        total_breaches = sum(v["n_actual"] for v in temp_alert.values() if v["n_actual"] > 0)
        logger.info("27℃超過アラート: total_breach_samples=%d", total_breaches)
        for h_key, v in temp_alert.items():
            if v["n_actual"] > 0:
                logger.info("  %s: n=%d, detection_rate=%.1f%%",
                            h_key, v["n_actual"], (v["detection_rate"] or 0) * 100)

    # ── 7. 朝方(5-8時 JST)湿度急変予測精度 ─────────────────────────────
    morning_humidity = {}
    if "InAirHumid" in targets:
        hi = targets.index("InAirHumid")
        y_true_h = tg_true_real[:, :, hi]   # [N, pred_len]
        y_pred_h = tg_pred_real[:, :, hi]

        # 急変サンプル: pred_len内での最大変化量 >= HUMIDITY_RAPID_CHANGE
        rapid_mask = np.abs(y_true_h.max(axis=1) - y_true_h.min(axis=1)) >= HUMIDITY_RAPID_CHANGE
        n_rapid = rapid_mask.sum()
        if n_rapid > 0:
            rmse_rapid = rmse(y_true_h[rapid_mask], y_pred_h[rapid_mask])
            mae_rapid = mae(y_true_h[rapid_mask], y_pred_h[rapid_mask])
            rmse_normal = rmse(y_true_h[~rapid_mask], y_pred_h[~rapid_mask])
            morning_humidity = {
                "n_rapid_change_samples": int(n_rapid),
                "rmse_on_rapid_change": rmse_rapid,
                "mae_on_rapid_change": mae_rapid,
                "rmse_on_normal": rmse_normal,
                "rapid_change_threshold_pct": HUMIDITY_RAPID_CHANGE,
            }
            logger.info("湿度急変サンプル: n=%d, RMSE=%.3f (normal: %.3f)",
                        n_rapid, rmse_rapid, rmse_normal)
        else:
            morning_humidity = {"n_rapid_change_samples": 0}
            logger.info("湿度急変サンプル: なし（閾値%g%%）", HUMIDITY_RAPID_CHANGE)

    # ── 8. 共変量あり/なし比較 ────────────────────────────────────────
    cov_comparison = {}
    if n_future_cov > 0 and model_keras is not None:
        # 共変量をゼロにして予測
        zero_cov = np.zeros_like(future_cov)
        tg_pred_nocov = model_keras((past_x, zero_cov), training=False).numpy()

        tg_pred_nocov_real = tg_pred_nocov * target_std + target_mean
        for i, tname in enumerate(targets):
            y_true_i = tg_true_real[:, :, i]
            y_nocov_i = tg_pred_nocov_real[:, :, i]
            cov_comparison[tname] = {
                "rmse_with_cov": metrics[tname]["rmse"],
                "rmse_without_cov": rmse(y_true_i, y_nocov_i),
                "mae_with_cov": metrics[tname]["mae"],
                "mae_without_cov": mae(y_true_i, y_nocov_i),
            }
        logger.info("共変量あり/なし比較:")
        for tname, v in cov_comparison.items():
            logger.info("  %-12s with=%.3f  without=%.3f (RMSE)",
                        tname, v["rmse_with_cov"], v["rmse_without_cov"])

    # ── 9. TFLite変換確認 ─────────────────────────────────────────────
    tflite_info = {}
    try:
        sm_path = args.model / "saved_model"
        if not sm_path.exists():
            raise FileNotFoundError(f"saved_model not found: {sm_path}")
        converter = tf.lite.TFLiteConverter.from_saved_model(str(sm_path))
        tflite_model = converter.convert()
        tflite_path = args.model / "agriha_tide.tflite"
        with open(tflite_path, "wb") as f:
            f.write(tflite_model)
        size_kb = len(tflite_model) / 1024
        tflite_info = {
            "conversion": "success",
            "path": str(tflite_path),
            "size_kb": round(size_kb, 1),
        }
        logger.info("TFLite変換: 成功 size=%.1f KB → %s", size_kb, tflite_path)
    except Exception as e:
        tflite_info = {"conversion": "failed", "error": str(e)}
        logger.warning("TFLite変換失敗: %s", e)

    # ── 10. Report ────────────────────────────────────────────────────
    report = {
        "model": str(args.model),
        "n_test_samples": int(len(past_x)),
        "pred_len": pred_len,
        "targets": targets,
        "metrics": metrics,
        "temp_27c_alert": temp_alert,
        "morning_humidity_rapid_change": morning_humidity,
        "covariate_comparison": cov_comparison,
        "tflite": tflite_info,
    }

    with open(args.report, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info("Report saved: %s", args.report)

    # ── サマリ出力 ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("TiDE 精度評価レポート")
    print("=" * 60)
    print(f"テストサンプル数: {len(past_x)}")
    print(f"予測ホライゾン: {pred_len}時間")
    print()
    print("RMSE / MAE:")
    for tname, m in metrics.items():
        print(f"  {tname:<14} RMSE={m['rmse']:.3f}  MAE={m['mae']:.3f}")

    if temp_alert:
        print("\n27℃超過アラート検知率:")
        for h_key, v in temp_alert.items():
            if v["n_actual"] > 0:
                print(f"  {h_key}: {v['detection_rate']*100:.1f}% (n={v['n_actual']})")

    if morning_humidity.get("n_rapid_change_samples", 0) > 0:
        print(f"\n湿度急変サンプル(≥{HUMIDITY_RAPID_CHANGE}%/h): n={morning_humidity['n_rapid_change_samples']}")
        print(f"  急変時 RMSE={morning_humidity['rmse_on_rapid_change']:.3f}"
              f"  通常時 RMSE={morning_humidity['rmse_on_normal']:.3f}")

    if cov_comparison:
        print("\n共変量あり/なし (RMSE):")
        for tname, v in cov_comparison.items():
            diff = v["rmse_without_cov"] - v["rmse_with_cov"]
            print(f"  {tname:<14} with={v['rmse_with_cov']:.3f}  without={v['rmse_without_cov']:.3f}  diff={diff:+.3f}")

    print(f"\nTFLite変換: {tflite_info.get('conversion', 'n/a')}"
          + (f" ({tflite_info['size_kb']} KB)" if tflite_info.get("size_kb") else ""))
    print("=" * 60)


if __name__ == "__main__":
    main()
