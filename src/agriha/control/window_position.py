"""window_position.py — 側窓推定位置管理

エンコーダなしの巻き上げモーターの開度をソフトウェアで追跡する。
重力の影響で開（巻き上げ）と閉（巻き下ろし）の走行時間が異なる。

設計書: predictive_ventilation_design.md
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")
logger = logging.getLogger(__name__)

DEFAULT_POSITION_PATH = os.environ.get(
    "WINDOW_POSITION_PATH", "/var/lib/agriha/window_position.json"
)


def load_position(path: str = DEFAULT_POSITION_PATH) -> dict:
    """推定位置を読み込む。ファイルなし時は全閉(0.0)を返す。"""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "north": 0.0,
            "south": 0.0,
            "last_calibrated_at": None,
            "last_updated_at": None,
        }


def save_position(pos: dict, path: str = DEFAULT_POSITION_PATH) -> None:
    """推定位置を保存する。"""
    pos["last_updated_at"] = datetime.now(tz=_JST).isoformat()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(pos, ensure_ascii=False, indent=2))


def update_position(
    pos: dict,
    group_name: str,
    direction: str,
    duration_sec: float,
    open_travel_sec: float,
    close_travel_sec: float,
) -> dict:
    """通電実績から推定位置を更新する。

    開と閉で走行時間が異なる（重力の影響）:
    - 開（巻き上げ）: 重力に逆らう → open_travel_sec（長い）
    - 閉（巻き下ろし）: 重力アシスト → close_travel_sec（短い）
    """
    key = "north" if "北" in group_name else "south"
    if direction == "open":
        delta = duration_sec / open_travel_sec
        pos[key] = min(1.0, pos[key] + delta)
    else:
        delta = duration_sec / close_travel_sec
        pos[key] = max(0.0, pos[key] - delta)
    return pos


def calibrate_closed(pos: dict, group_name: str) -> dict:
    """全閉キャリブレーション（リミットスイッチ到達後に呼ぶ）"""
    key = "north" if "北" in group_name else "south"
    pos[key] = 0.0
    pos["last_calibrated_at"] = datetime.now(tz=_JST).isoformat()
    return pos


def calibrate_open(pos: dict, group_name: str) -> dict:
    """全開キャリブレーション"""
    key = "north" if "北" in group_name else "south"
    pos[key] = 1.0
    pos["last_calibrated_at"] = datetime.now(tz=_JST).isoformat()
    return pos


def compute_move(
    current_pos: float,
    target_pos: float,
    open_travel_sec: float,
    close_travel_sec: float,
    deadband: float = 0.05,
) -> tuple[str | None, float]:
    """現在位置→目標位置の移動を計算する。

    開と閉で走行時間が異なる（重力の影響）:
    - 開方向: open_travel_sec を使用（巻き上げ、重力に逆らうので遅い）
    - 閉方向: close_travel_sec を使用（巻き下ろし、重力アシストで速い）

    Returns:
        (direction, duration_sec): ("open", 13.0) or ("close", 10.0) or (None, 0)
    """
    delta = target_pos - current_pos
    if abs(delta) < deadband:
        return None, 0
    if delta > 0:
        return "open", delta * open_travel_sec
    else:
        return "close", abs(delta) * close_travel_sec
