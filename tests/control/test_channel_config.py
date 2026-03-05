"""tests/control/test_channel_config.py — channel_config.py ユニットテスト

設計書 §9.3 の全関数をテスト。
tmp_pathにYAMLを生成するfixture方式でファイルI/Oをテスト。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agriha.control.channel_config import (
    get_irrigation_channel,
    get_relay_labels,
    get_valid_channel_range,
    get_window_channels,
    load_channel_map,
    load_window_groups,
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
        "groups": [
            {
                "name": "北側窓",
                "open_channel": 5,
                "close_channel": 6,
                "wind_close_directions": [1, 2, 16],
            },
            {
                "name": "南側窓",
                "open_channel": 8,
                "close_channel": 7,
                "wind_close_directions": [8, 9, 10],
            },
        ],
    },
    "relay_labels": {
        1: "暖房",
        2: "循環扇",
        3: "CO2発生器",
        4: "灌水ポンプ",
        5: "北側窓(開)",
        6: "北側窓(閉)",
        7: "南側窓(閉)",
        8: "南側窓(開)",
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
    assert len(result["side_window"]["groups"]) == 2


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
# 2. load_window_groups
# ---------------------------------------------------------------------------

def test_load_window_groups_returns_list(channel_map_file: Path) -> None:
    """グループリストを返すこと。"""
    cfg = load_channel_map(channel_map_file)
    groups = load_window_groups(cfg)
    assert isinstance(groups, list)
    assert len(groups) == 2


def test_load_window_groups_north_group(channel_map_file: Path) -> None:
    """北側窓グループの open/close チャンネルと風向が正しいこと。"""
    cfg = load_channel_map(channel_map_file)
    groups = load_window_groups(cfg)
    north = groups[0]
    assert north["name"] == "北側窓"
    assert north["open_channel"] == 5
    assert north["close_channel"] == 6
    assert north["wind_close_directions"] == [1, 2, 16]


def test_load_window_groups_south_group(channel_map_file: Path) -> None:
    """南側窓グループの open/close チャンネルと風向が正しいこと。"""
    cfg = load_channel_map(channel_map_file)
    groups = load_window_groups(cfg)
    south = groups[1]
    assert south["name"] == "南側窓"
    assert south["open_channel"] == 8
    assert south["close_channel"] == 7
    assert south["wind_close_directions"] == [8, 9, 10]


# ---------------------------------------------------------------------------
# 3. get_window_channels
# ---------------------------------------------------------------------------

def test_get_window_channels_returns_all_channels(channel_map_file: Path) -> None:
    """全窓チャンネル（open+close両方）を返すこと。"""
    cfg = load_channel_map(channel_map_file)
    result = get_window_channels(cfg)
    assert set(result) == {5, 6, 7, 8}
    assert len(result) == 4


def test_get_window_channels_includes_open_and_close(channel_map_file: Path) -> None:
    """open_channel と close_channel の両方が含まれること。"""
    cfg = load_channel_map(channel_map_file)
    result = get_window_channels(cfg)
    assert 5 in result  # 北open
    assert 6 in result  # 北close
    assert 7 in result  # 南close
    assert 8 in result  # 南open


# ---------------------------------------------------------------------------
# 4. get_irrigation_channel
# ---------------------------------------------------------------------------

def test_get_irrigation_channel(channel_map_file: Path) -> None:
    """灌水チャンネル番号を正しく返すこと。"""
    cfg = load_channel_map(channel_map_file)
    assert get_irrigation_channel(cfg) == 4


# ---------------------------------------------------------------------------
# 5. get_relay_labels
# ---------------------------------------------------------------------------

def test_get_relay_labels_returns_dict(channel_map_file: Path) -> None:
    """リレーラベル辞書を返すこと。"""
    cfg = load_channel_map(channel_map_file)
    labels = get_relay_labels(cfg)
    assert isinstance(labels, dict)
    assert labels[4] == "灌水ポンプ"
    assert labels[5] == "北側窓(開)"
    assert labels[6] == "北側窓(閉)"
    assert labels[7] == "南側窓(閉)"
    assert labels[8] == "南側窓(開)"


def test_get_relay_labels_empty_when_missing(tmp_path: Path) -> None:
    """relay_labelsキーがない場合は空辞書を返すこと。"""
    p = tmp_path / "minimal.yaml"
    minimal: dict[str, Any] = {
        "irrigation": {"channel": 4, "label": "灌水ポンプ"},
        "side_window": {
            "groups": [
                {"name": "北側窓", "open_channel": 5, "close_channel": 6, "wind_close_directions": [1, 2, 16]},
                {"name": "南側窓", "open_channel": 8, "close_channel": 7, "wind_close_directions": [8, 9, 10]},
            ],
        },
        "valid_channels": {"min": 1, "max": 8},
    }
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(minimal, f)
    cfg = load_channel_map(p)
    assert get_relay_labels(cfg) == {}


# ---------------------------------------------------------------------------
# 6. get_valid_channel_range
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
            "groups": [
                {"name": "北側窓", "open_channel": 5, "close_channel": 6, "wind_close_directions": [1, 2, 16]},
            ],
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
    ch4=灌水、北側窓open=5/close=6、南側窓open=8/close=7、valid 1-8。
    """
    import agriha.control.channel_config as cc
    cfg = load_channel_map(cc._REPO_PATH)
    assert get_irrigation_channel(cfg) == 4
    groups = load_window_groups(cfg)
    assert len(groups) == 2
    north = groups[0]
    south = groups[1]
    assert north["open_channel"] == 5
    assert north["close_channel"] == 6
    assert south["open_channel"] == 8
    assert south["close_channel"] == 7
    assert set(get_window_channels(cfg)) == {5, 6, 7, 8}
    assert get_valid_channel_range(cfg) == (1, 8)
