"""tests/control/test_channel_config.py — channel_config.py ユニットテスト

設計書 §9.3 の全7関数をテスト。
tmp_pathにYAMLを生成するfixture方式でファイルI/Oをテスト。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agriha.control.channel_config import (
    get_irrigation_channel,
    get_north_channels,
    get_relay_labels,
    get_south_channels,
    get_valid_channel_range,
    get_window_channels,
    load_channel_map,
)

# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

SAMPLE_MAP: dict[str, Any] = {
    "irrigation": {
        "channel": 4,
        "label": "灌水ポンプ",
    },
    "side_window": {
        "south": {
            "channels": [5, 6],
            "label": "南側窓",
        },
        "north": {
            "channels": [7, 8],
            "label": "北側窓",
        },
    },
    "relay_labels": {
        1: "暖房",
        2: "循環扇",
        3: "CO2発生器",
        4: "灌水ポンプ",
        5: "南側窓",
        6: "南側窓",
        7: "北側窓",
        8: "北側窓",
    },
    "valid_channels": {
        "min": 1,
        "max": 8,
    },
}


@pytest.fixture
def channel_map_file(tmp_path: Path) -> Path:
    """テスト用 channel_map.yaml を tmp_path に生成して Path を返す。"""
    p = tmp_path / "channel_map.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(SAMPLE_MAP, f, allow_unicode=True, default_flow_style=False)
    return p


# ---------------------------------------------------------------------------
# 1. load_channel_map
# ---------------------------------------------------------------------------

def test_load_channel_map_with_path(channel_map_file: Path) -> None:
    """path引数でYAMLを読み込めること。"""
    result = load_channel_map(channel_map_file)
    assert result["irrigation"]["channel"] == 4
    assert result["side_window"]["south"]["channels"] == [5, 6]
    assert result["side_window"]["north"]["channels"] == [7, 8]


def test_load_channel_map_fallback_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """リポジトリ内 config/channel_map.yaml のフォールバック読み込みが動作すること。
    /etc/agriha/channel_map.yaml は存在しないことを仮定。
    """
    import agriha.control.channel_config as cc
    # /etc/agriha/channel_map.yaml が存在しない状況をシミュレート
    monkeypatch.setattr(cc, "_DEPLOY_PATH", Path("/nonexistent/path/channel_map.yaml"))
    result = load_channel_map()
    # リポジトリ config/channel_map.yaml が存在すれば読み込めるはず
    assert "irrigation" in result
    assert "side_window" in result


def test_load_channel_map_raises_if_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全パスが存在しない場合 FileNotFoundError が発生すること。"""
    import agriha.control.channel_config as cc
    monkeypatch.setattr(cc, "_DEPLOY_PATH", tmp_path / "no_deploy.yaml")
    monkeypatch.setattr(cc, "_REPO_PATH", tmp_path / "no_repo.yaml")
    with pytest.raises(FileNotFoundError):
        load_channel_map()


# ---------------------------------------------------------------------------
# 2. get_window_channels
# ---------------------------------------------------------------------------

def test_get_window_channels_returns_south_and_north(channel_map_file: Path) -> None:
    """全窓チャンネル（南+北）を正しく返すこと。"""
    cfg = load_channel_map(channel_map_file)
    result = get_window_channels(cfg)
    assert result == [5, 6, 7, 8]


def test_get_window_channels_order(channel_map_file: Path) -> None:
    """南側チャンネルが先、北側チャンネルが後の順であること。"""
    cfg = load_channel_map(channel_map_file)
    result = get_window_channels(cfg)
    assert result[:2] == [5, 6]
    assert result[2:] == [7, 8]


# ---------------------------------------------------------------------------
# 3. get_south_channels
# ---------------------------------------------------------------------------

def test_get_south_channels(channel_map_file: Path) -> None:
    """南側窓チャンネルを正しく返すこと。"""
    cfg = load_channel_map(channel_map_file)
    assert get_south_channels(cfg) == [5, 6]


# ---------------------------------------------------------------------------
# 4. get_north_channels
# ---------------------------------------------------------------------------

def test_get_north_channels(channel_map_file: Path) -> None:
    """北側窓チャンネルを正しく返すこと。"""
    cfg = load_channel_map(channel_map_file)
    assert get_north_channels(cfg) == [7, 8]


# ---------------------------------------------------------------------------
# 5. get_irrigation_channel
# ---------------------------------------------------------------------------

def test_get_irrigation_channel(channel_map_file: Path) -> None:
    """灌水チャンネル番号を正しく返すこと。"""
    cfg = load_channel_map(channel_map_file)
    assert get_irrigation_channel(cfg) == 4


# ---------------------------------------------------------------------------
# 6. get_relay_labels
# ---------------------------------------------------------------------------

def test_get_relay_labels_returns_dict(channel_map_file: Path) -> None:
    """リレーラベル辞書を返すこと。"""
    cfg = load_channel_map(channel_map_file)
    labels = get_relay_labels(cfg)
    assert isinstance(labels, dict)
    assert labels[4] == "灌水ポンプ"
    assert labels[5] == "南側窓"
    assert labels[7] == "北側窓"


def test_get_relay_labels_empty_when_missing(tmp_path: Path) -> None:
    """relay_labelsキーがない場合は空辞書を返すこと。"""
    p = tmp_path / "minimal.yaml"
    minimal: dict[str, Any] = {
        "irrigation": {"channel": 4, "label": "灌水ポンプ"},
        "side_window": {
            "south": {"channels": [5, 6], "label": "南"},
            "north": {"channels": [7, 8], "label": "北"},
        },
        "valid_channels": {"min": 1, "max": 8},
    }
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(minimal, f)
    cfg = load_channel_map(p)
    assert get_relay_labels(cfg) == {}


# ---------------------------------------------------------------------------
# 7. get_valid_channel_range
# ---------------------------------------------------------------------------

def test_get_valid_channel_range(channel_map_file: Path) -> None:
    """有効チャンネル範囲 (min, max) タプルを返すこと。"""
    cfg = load_channel_map(channel_map_file)
    min_ch, max_ch = get_valid_channel_range(cfg)
    assert min_ch == 1
    assert max_ch == 8


def test_get_valid_channel_range_default(tmp_path: Path) -> None:
    """valid_channelsキーがない場合はデフォルト (1, 8) を返すこと。"""
    p = tmp_path / "no_vc.yaml"
    no_vc: dict[str, Any] = {
        "irrigation": {"channel": 4, "label": "灌水ポンプ"},
        "side_window": {
            "south": {"channels": [5, 6], "label": "南"},
            "north": {"channels": [7, 8], "label": "北"},
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(no_vc, f)
    cfg = load_channel_map(p)
    assert get_valid_channel_range(cfg) == (1, 8)


# ---------------------------------------------------------------------------
# 統合: config/channel_map.yaml のリポジトリ値確認
# ---------------------------------------------------------------------------

def test_repo_channel_map_values() -> None:
    """リポジトリ内 config/channel_map.yaml の値が仕様通りであること。
    ch4=灌水、ch5,6=南側窓、ch7,8=北側窓、valid 1-8。
    """
    import agriha.control.channel_config as cc
    cfg = load_channel_map(cc._REPO_PATH)
    assert get_irrigation_channel(cfg) == 4
    assert get_south_channels(cfg) == [5, 6]
    assert get_north_channels(cfg) == [7, 8]
    assert get_window_channels(cfg) == [5, 6, 7, 8]
    assert get_valid_channel_range(cfg) == (1, 8)
