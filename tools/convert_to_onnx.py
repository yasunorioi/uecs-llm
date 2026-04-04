"""convert_to_onnx.py — TFLite モデルを ONNX に変換するスクリプト。

RPi5 (Python 3.13) では tflite-runtime の aarch64 wheel が存在しないため、
ONNX Runtime 推論に切り替えている。本スクリプトで変換を行う。

前提:
  - 学習済みの agriha_tide.tflite が models/ 配下に存在すること
  - tf2onnx がインストール済みであること (pip install tf2onnx)

Usage:
  python3 tools/convert_to_onnx.py
  python3 tools/convert_to_onnx.py --model models/agriha_tide_v1/
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def convert(model_dir: Path) -> Path:
    tflite_path = model_dir / "agriha_tide.tflite"
    onnx_path = model_dir / "agriha_tide.onnx"

    if not tflite_path.exists():
        raise FileNotFoundError(f"TFLite モデルが見つかりません: {tflite_path}")

    if onnx_path.exists():
        logger.info("ONNX モデルが既に存在します: %s → スキップ", onnx_path)
        return onnx_path

    logger.info("変換開始: %s → %s", tflite_path, onnx_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "tf2onnx.convert",
            "--tflite", str(tflite_path),
            "--output", str(onnx_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("変換失敗:\n%s", result.stderr)
        raise RuntimeError(f"tf2onnx 変換失敗 (exit={result.returncode})")

    logger.info("変換完了: %s", onnx_path)
    return onnx_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="TFLite → ONNX 変換")
    parser.add_argument(
        "--model", type=Path, default=Path("models/agriha_tide_v1"),
        help="モデルディレクトリ (default: models/agriha_tide_v1)",
    )
    args = parser.parse_args()

    try:
        onnx_path = convert(args.model)
        print(f"OK: {onnx_path}")
    except Exception as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
