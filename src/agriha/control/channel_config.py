"""channel_config.py — channel_map.yaml ローダー

全Python制御スクリプト共通。テスト時はpath引数で差し替え可能。
設計書: docs/v2_three_layer_design.md §9.3
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEPLOY_PATH = Path("/etc/agriha/channel_map.yaml")
_REPO_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "channel_map.yaml"
)


def load_channel_map(path: str | Path | None = None) -> dict[str, Any]:
    """channel_map.yaml を読み込む。テスト時はpath引数で差し替え可能。

    優先順: 引数path > /etc/agriha/channel_map.yaml > リポジトリ config/
    """
    if path:
        p = Path(path)
    elif _DEPLOY_PATH.exists():
        p = _DEPLOY_PATH
    else:
        p = _REPO_PATH
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_window_groups(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """側窓グループ設定（open/closeチャンネル分離）を返す。

    各グループは以下のキーを持つ:
        name: str — グループ名
        open_channel: int — 窓を開けるリレーch番号
        close_channel: int — 窓を閉めるリレーch番号
        wind_close_directions: list[int] — この窓を閉める強風方角コード
    """
    if config is None:
        config = load_channel_map()
    return config["side_window"]["groups"]


def get_window_channels(config: dict[str, Any] | None = None) -> list[int]:
    """全窓チャンネル（全グループのopen+close両方）を返す。"""
    if config is None:
        config = load_channel_map()
    groups = config["side_window"]["groups"]
    chs: list[int] = []
    for g in groups:
        chs.append(g["open_channel"])
        chs.append(g["close_channel"])
    return chs


def get_irrigation_channel(config: dict[str, Any] | None = None) -> int:
    """灌水チャンネル番号を返す。"""
    if config is None:
        config = load_channel_map()
    return config["irrigation"]["channel"]


def get_relay_labels(config: dict[str, Any] | None = None) -> dict[int, str]:
    """リレーチャンネルラベル辞書を返す。"""
    if config is None:
        config = load_channel_map()
    return config.get("relay_labels", {})


def get_valid_channel_range(config: dict[str, Any] | None = None) -> tuple[int, int]:
    """有効チャンネル範囲 (min, max) を返す。"""
    if config is None:
        config = load_channel_map()
    vc = config.get("valid_channels", {"min": 1, "max": 8})
    return vc["min"], vc["max"]
