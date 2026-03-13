"""tests/control/test_window_position.py — 窓推定位置テスト"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agriha.control.window_position import (
    calibrate_closed,
    calibrate_open,
    compute_move,
    load_position,
    save_position,
    update_position,
)


class TestLoadSavePosition:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        pos = load_position(str(tmp_path / "no.json"))
        assert pos["north"] == 0.0
        assert pos["south"] == 0.0

    def test_save_and_load(self, tmp_path: Path) -> None:
        path = str(tmp_path / "pos.json")
        pos = {"north": 0.5, "south": 0.3, "last_calibrated_at": None, "last_updated_at": None}
        save_position(pos, path)
        loaded = load_position(path)
        assert loaded["north"] == 0.5
        assert loaded["south"] == 0.3
        assert loaded["last_updated_at"] is not None


class TestUpdatePosition:
    def test_open_north(self) -> None:
        pos = {"north": 0.0, "south": 0.0}
        pos = update_position(pos, "北側窓", "open", 32.5, 65, 50)
        assert abs(pos["north"] - 0.5) < 0.01

    def test_close_south(self) -> None:
        pos = {"north": 0.0, "south": 1.0}
        pos = update_position(pos, "南側窓", "close", 25.0, 65, 50)
        assert abs(pos["south"] - 0.5) < 0.01

    def test_clamp_max(self) -> None:
        pos = {"north": 0.9, "south": 0.0}
        pos = update_position(pos, "北側窓", "open", 65, 65, 50)
        assert pos["north"] == 1.0

    def test_clamp_min(self) -> None:
        pos = {"north": 0.0, "south": 0.1}
        pos = update_position(pos, "南側窓", "close", 50, 65, 50)
        assert pos["south"] == 0.0


class TestCalibrate:
    def test_calibrate_closed(self) -> None:
        pos = {"north": 0.5, "south": 0.3, "last_calibrated_at": None}
        pos = calibrate_closed(pos, "北側窓")
        assert pos["north"] == 0.0
        assert pos["last_calibrated_at"] is not None

    def test_calibrate_open(self) -> None:
        pos = {"north": 0.5, "south": 0.3, "last_calibrated_at": None}
        pos = calibrate_open(pos, "南側窓")
        assert pos["south"] == 1.0


class TestComputeMove:
    def test_no_move_in_deadband(self) -> None:
        direction, dur = compute_move(0.5, 0.53, 65, 50, deadband=0.05)
        assert direction is None
        assert dur == 0

    def test_open_direction(self) -> None:
        direction, dur = compute_move(0.0, 0.5, 65, 50)
        assert direction == "open"
        assert abs(dur - 32.5) < 0.01  # 0.5 * 65

    def test_close_direction(self) -> None:
        direction, dur = compute_move(1.0, 0.5, 65, 50)
        assert direction == "close"
        assert abs(dur - 25.0) < 0.01  # 0.5 * 50

    def test_asymmetric_travel(self) -> None:
        """開と閉で走行時間が異なることを確認"""
        _, open_dur = compute_move(0.0, 1.0, 65, 50)
        _, close_dur = compute_move(1.0, 0.0, 65, 50)
        assert abs(open_dur - 65) < 0.01
        assert abs(close_dur - 50) < 0.01
        assert open_dur > close_dur
