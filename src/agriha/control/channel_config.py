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


def get_window_channels(config: dict[str, Any] | None = None) -> list[int]:
    """全窓チャンネル（南+北）を返す。"""
    if config is None:
        config = load_channel_map()
    south = config["side_window"]["south"]["channels"]
    north = config["side_window"]["north"]["channels"]
    return south + north


def get_south_channels(config: dict[str, Any] | None = None) -> list[int]:
    """南側窓チャンネルを返す。"""
    if config is None:
        config = load_channel_map()
    return config["side_window"]["south"]["channels"]


def get_north_channels(config: dict[str, Any] | None = None) -> list[int]:
    """北側窓チャンネルを返す。"""
    if config is None:
        config = load_channel_map()
    return config["side_window"]["north"]["channels"]


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
