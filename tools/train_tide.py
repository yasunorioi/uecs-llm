"""train_tide.py — TiDE モデル学習スクリプト。

TiDE (Time-series Dense Encoder) を ArSprout 前年データで学習し、
SavedModel 形式で保存する。

Usage:
  python3 tools/train_tide.py --input data/sensor_hourly.csv --output models/agriha_tide_v1/

References:
  Das et al. (2023) "Long-term Forecasting with TiDE: Time-series Dense Encoder"
  https://arxiv.org/abs/2304.08424
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow import keras

logger = logging.getLogger(__name__)

# ── デフォルトパラメータ（設計書§3.3） ────────────────────────────────
TARGETS = ["InAirTemp", "InAirHumid", "InAirCO2"]
HIDDEN_DIMS = 128
NUM_ENCODER_LAYERS = 2
PRED_LEN = 6
LOOKBACK = 48
BATCH_SIZE = 32
EPOCHS = 100
DROPOUT_RATE = 0.1
LEARNING_RATE = 1e-3
PATIENCE = 15  # early stopping patience


# ────────────────────────────────────────────────────────────────────────
# TiDE モデル定義
# ────────────────────────────────────────────────────────────────────────

class ResidualBlock(keras.layers.Layer):
    """Dense + LayerNorm + Dropout + Skip connection."""

    def __init__(self, hidden_dim: int, output_dim: int, dropout_rate: float = 0.1, **kwargs):
        super().__init__(**kwargs)
        self.dense1 = keras.layers.Dense(hidden_dim, activation="relu")
        self.dense2 = keras.layers.Dense(output_dim)
        self.norm = keras.layers.LayerNormalization()
        self.dropout = keras.layers.Dropout(dropout_rate)
        self.skip = keras.layers.Dense(output_dim, use_bias=False)

    def get_config(self):
        config = super().get_config()
        config.update({"hidden_dim": self.dense1.units,
                        "output_dim": self.dense2.units,
                        "dropout_rate": self.dropout.rate})
        return config

    def call(self, x: tf.Tensor, training: bool = False) -> tf.Tensor:
        h = self.dense1(x)
        h = self.dropout(h, training=training)
        h = self.dense2(h)
        return self.norm(h + self.skip(x))


class TiDE(keras.Model):
    """Time-series Dense Encoder (TiDE).

    Args:
        lookback:          過去観測ウィンドウ長（タイムステップ数）
        n_features:        入力フィーチャー数（targets + 共変量）
        pred_len:          予測ホライゾン（タイムステップ数）
        n_targets:         予測対象変数数
        n_future_cov:      未来共変量数（時間特徴量等）
        hidden_dims:       隠れ層次元数
        num_encoder_layers:エンコーダ層数
        dropout_rate:      ドロップアウト率
    """

    def __init__(
        self,
        lookback: int,
        n_features: int,
        pred_len: int,
        n_targets: int,
        n_future_cov: int = 4,
        hidden_dims: int = HIDDEN_DIMS,
        num_encoder_layers: int = NUM_ENCODER_LAYERS,
        dropout_rate: float = DROPOUT_RATE,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.pred_len = pred_len
        self.n_targets = n_targets

        # Encoder: MLP on flattened past window
        self.encoder_blocks = [
            ResidualBlock(hidden_dims, hidden_dims, dropout_rate,
                          name=f"encoder_{i}")
            for i in range(num_encoder_layers)
        ]
        self.encoder_proj = keras.layers.Dense(hidden_dims, name="encoder_proj")

        # Temporal decoder: one block per prediction step
        self.decoder_blocks = [
            ResidualBlock(hidden_dims, hidden_dims, dropout_rate,
                          name=f"decoder_{t}")
            for t in range(pred_len)
        ]

        # Store config for serialization
        self._init_config = dict(
            lookback=lookback, n_features=n_features, pred_len=pred_len,
            n_targets=n_targets, n_future_cov=n_future_cov,
            hidden_dims=hidden_dims, num_encoder_layers=num_encoder_layers,
            dropout_rate=dropout_rate,
        )

        # Output projection per step
        self.output_proj = keras.layers.Dense(n_targets, name="output_proj")

        # Residual (temporal) skip: direct from past targets to prediction
        self.residual_proj = keras.layers.Dense(n_targets, name="residual_proj")

        # Future covariate projection
        self.future_proj = keras.layers.Dense(hidden_dims // 2, activation="relu",
                                               name="future_proj")

    def get_config(self):
        config = super().get_config()
        config.update(self._init_config)
        return config

    def call(
        self,
        inputs: tuple,          # (past_x, future_cov)
        training: bool = False,
    ) -> tf.Tensor:
        past_x, future_cov = inputs[0], inputs[1]
        batch = tf.shape(past_x)[0]

        # ── Encoder ───────────────────────────────────────────────────
        # Flatten: [batch, lookback × n_features]
        h = tf.reshape(past_x, [batch, -1])
        h = self.encoder_proj(h)
        for block in self.encoder_blocks:
            h = block(h, training=training)
        # h: [batch, hidden_dims]

        # ── Temporal decoder ─────────────────────────────────────────
        # Future covariates: [batch, pred_len, hidden//2]
        future_h = self.future_proj(future_cov)

        preds = []
        for t in range(self.pred_len):
            # Concat encoded + future cov at step t
            h_t = tf.concat([h, future_h[:, t, :]], axis=-1)
            h_t = self.decoder_blocks[t](h_t, training=training)
            y_t = self.output_proj(h_t)  # [batch, n_targets]
            preds.append(y_t)

        output = tf.stack(preds, axis=1)  # [batch, pred_len, n_targets]

        # ── Residual skip: last step of past targets → all pred steps ─
        # past_targets: [batch, lookback, n_targets] → last value
        past_last = past_x[:, -1:, :self.n_targets]  # [batch, 1, n_targets]
        residual = self.residual_proj(
            tf.reshape(past_last, [batch, self.n_targets])
        )  # [batch, n_targets]
        residual = tf.tile(tf.expand_dims(residual, 1), [1, self.pred_len, 1])

        return output + residual


# ────────────────────────────────────────────────────────────────────────
# データ前処理
# ────────────────────────────────────────────────────────────────────────

def make_windows(
    data: np.ndarray,
    lookback: int,
    pred_len: int,
    n_targets: int,
    n_future_cov: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """スライディングウィンドウでサンプル生成。

    Returns:
        past_x:     [N, lookback, n_features]
        future_cov: [N, pred_len, n_future_cov]  (time features only)
        targets:    [N, pred_len, n_targets]
    """
    n_features = data.shape[1]
    n_time_feat_start = n_features - n_future_cov

    past_x_list, future_cov_list, target_list = [], [], []
    total = len(data) - lookback - pred_len + 1
    for i in range(total):
        window = data[i: i + lookback]               # [lookback, n_features]
        future = data[i + lookback: i + lookback + pred_len]  # [pred_len, n_features]

        past_x_list.append(window)
        future_cov_list.append(future[:, n_time_feat_start:])  # time features
        target_list.append(future[:, :n_targets])

    return (
        np.array(past_x_list, dtype=np.float32),
        np.array(future_cov_list, dtype=np.float32),
        np.array(target_list, dtype=np.float32),
    )


def split_train_val_test(
    past_x, future_cov, targets, train_ratio=0.70, val_ratio=0.15
):
    """時間順に train/val/test 分割（シャッフルなし）。"""
    n = len(past_x)
    n_train = int(n * train_ratio)
    n_val = int(n * (train_ratio + val_ratio))

    def _split(arr):
        return arr[:n_train], arr[n_train:n_val], arr[n_val:]

    return _split(past_x), _split(future_cov), _split(targets)


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Train TiDE model on ArSprout sensor data")
    parser.add_argument("--input", type=Path, default=Path("data/sensor_hourly.csv"),
                        help="Input CSV from export_sensor_csv.py")
    parser.add_argument("--output", type=Path, default=Path("models/agriha_tide_v1"),
                        help="Output SavedModel directory")
    parser.add_argument("--lookback", type=int, default=LOOKBACK)
    parser.add_argument("--pred-len", type=int, default=PRED_LEN)
    parser.add_argument("--hidden-dims", type=int, default=HIDDEN_DIMS)
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    # ── 1. Load data ──────────────────────────────────────────────────
    df = pd.read_csv(args.input, index_col="datetime_utc", parse_dates=True)
    logger.info("Loaded: %d rows, %d columns from %s", len(df), len(df.columns), args.input)

    available_targets = [c for c in TARGETS if c in df.columns]
    if not available_targets:
        raise RuntimeError(f"No target columns found. Available: {list(df.columns)}")

    n_targets = len(available_targets)
    time_feat_cols = ["hour_sin", "hour_cos", "doy_sin", "doy_cos"]
    available_time_feat = [c for c in time_feat_cols if c in df.columns]
    n_future_cov = len(available_time_feat)

    # Column order: targets first, then other features, then time features last
    other_cols = [c for c in df.columns if c not in available_targets + available_time_feat]
    col_order = available_targets + other_cols + available_time_feat
    df = df[col_order]
    df = df.ffill().bfill()

    logger.info("Targets: %s", available_targets)
    logger.info("Features: %d total (%d targets + %d other + %d time)",
                len(col_order), n_targets, len(other_cols), n_future_cov)

    # ── 2. Normalize ──────────────────────────────────────────────────
    data = df.values.astype(np.float32)
    mean = data.mean(axis=0)
    std = data.std(axis=0)
    std[std < 1e-8] = 1.0  # avoid division by zero
    data_norm = (data - mean) / std

    n_features = data_norm.shape[1]

    # ── 3. Create windows ─────────────────────────────────────────────
    past_x, future_cov, targets = make_windows(
        data_norm, args.lookback, args.pred_len, n_targets, n_future_cov
    )
    logger.info("Windows: %d total", len(past_x))

    (px_tr, px_va, px_te), (fc_tr, fc_va, fc_te), (tg_tr, tg_va, tg_te) = \
        split_train_val_test(past_x, future_cov, targets)
    logger.info("Split: train=%d val=%d test=%d", len(px_tr), len(px_va), len(px_te))

    # ── 4. Build model ────────────────────────────────────────────────
    model = TiDE(
        lookback=args.lookback,
        n_features=n_features,
        pred_len=args.pred_len,
        n_targets=n_targets,
        n_future_cov=n_future_cov,
        hidden_dims=args.hidden_dims,
        num_encoder_layers=NUM_ENCODER_LAYERS,
    )

    optimizer = keras.optimizers.Adam(learning_rate=LEARNING_RATE)
    model.compile(optimizer=optimizer, loss="mse", metrics=["mae"])

    # Build with dummy input
    dummy_past = tf.zeros([1, args.lookback, n_features])
    dummy_cov = tf.zeros([1, args.pred_len, n_future_cov])
    _ = model((dummy_past, dummy_cov))
    model.summary(print_fn=logger.info)

    # ── 5. Train ──────────────────────────────────────────────────────
    callbacks = [
        keras.callbacks.EarlyStopping(
            monitor="val_loss", patience=PATIENCE, restore_best_weights=True
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=7, min_lr=1e-5
        ),
    ]

    history = model.fit(
        (px_tr, fc_tr), tg_tr,
        validation_data=((px_va, fc_va), tg_va),
        batch_size=args.batch_size,
        epochs=args.epochs,
        callbacks=callbacks,
        verbose=1,
    )

    best_epoch = np.argmin(history.history["val_loss"]) + 1
    best_val_loss = min(history.history["val_loss"])
    logger.info("Training complete: best_epoch=%d, best_val_loss=%.4f", best_epoch, best_val_loss)

    # ── 6. Save ───────────────────────────────────────────────────────
    args.output.mkdir(parents=True, exist_ok=True)
    # Keras 3 native format (.keras)
    keras_path = args.output / "model.keras"
    model.save(keras_path)
    logger.info("Keras model saved: %s", keras_path)
    # SavedModel format (for TFLite conversion)
    model.export(str(args.output / "saved_model"))
    logger.info("SavedModel exported: %s", args.output / "saved_model")

    # Save normalization parameters for inference
    norm_params = {
        "mean": mean.tolist(),
        "std": std.tolist(),
        "columns": col_order,
        "targets": available_targets,
        "lookback": args.lookback,
        "pred_len": args.pred_len,
        "n_targets": n_targets,
        "n_future_cov": n_future_cov,
        "best_epoch": int(best_epoch),
        "best_val_loss": float(best_val_loss),
    }
    norm_path = args.output / "norm_params.json"
    with open(norm_path, "w") as f:
        json.dump(norm_params, f, indent=2)
    logger.info("Normalization params saved: %s", norm_path)

    # Save test set for eval_tide.py
    # Evaluate on test set
    test_loss = model.evaluate((px_te, fc_te), tg_te, verbose=0)
    logger.info("Test loss: %.4f", test_loss[0])

    test_data = {
        "past_x": px_te.tolist(),
        "future_cov": fc_te.tolist(),
        "targets": tg_te.tolist(),
    }
    test_path = args.output / "test_data.json"
    with open(test_path, "w") as f:
        json.dump(test_data, f)
    logger.info("Test data saved: %s (%d samples)", test_path, len(px_te))


if __name__ == "__main__":
    main()
