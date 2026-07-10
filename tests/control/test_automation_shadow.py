"""tests/control/test_automation_shadow.py — shadow-run アダプタのテスト

rule_engine の helper に依存する build_sensors_dict / build_primitives は
統合テスト側 (env に httpx 等が揃った状態) で verify すべきなので、
ここでは pure な部分 (schema load、active state 永続化、compare_results、log) を
中心にテストする。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agriha.control.automation_interpreter import Result as InterpResult
from agriha.control.automation_shadow import (
    compare_results,
    load_active_state,
    load_schema,
    log_shadow_result,
    save_active_state,
)


# ══════════════════════════════════════════════
# schema load
# ══════════════════════════════════════════════

def test_load_schema_missing_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    assert load_schema(str(missing)) == []


def test_load_schema_reads_automations_list(tmp_path: Path) -> None:
    p = tmp_path / "automations.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "automations": [
                    {
                        "id": "test_auto",
                        "priority": 10,
                        "trigger": "x > 0",
                        "action": {"kind": "close_all_windows"},
                    }
                ]
            }
        )
    )
    schema = load_schema(str(p))
    assert len(schema) == 1
    assert schema[0]["id"] == "test_auto"


def test_load_schema_empty_yaml_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert load_schema(str(p)) == []


# ══════════════════════════════════════════════
# active state roundtrip
# ══════════════════════════════════════════════

def test_active_state_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    save_active_state({"humidity_vent", "co2_vent"}, str(p))
    assert load_active_state(str(p)) == {"humidity_vent", "co2_vent"}


def test_active_state_missing_returns_empty(tmp_path: Path) -> None:
    assert load_active_state(str(tmp_path / "does_not_exist.json")) == set()


def test_active_state_corrupt_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.json"
    p.write_text("{ not valid json")
    assert load_active_state(str(p)) == set()


def test_active_state_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "dir" / "state.json"
    save_active_state({"x"}, str(p))
    assert p.exists()
    assert load_active_state(str(p)) == {"x"}


# ══════════════════════════════════════════════
# compare_results
# ══════════════════════════════════════════════

def _interp_result(
    triggered: list[str] | None = None,
    target_opening_pct: int = 0,
    close_all: bool = False,
    close_by_wind: bool = False,
) -> InterpResult:
    r = InterpResult()
    r.triggered = triggered or []
    r.target_opening_pct = target_opening_pct
    r.close_all_windows = close_all
    r.close_by_wind_direction = close_by_wind
    return r


def test_compare_no_delta_when_identical() -> None:
    interp = _interp_result(triggered=["humidity_vent"], target_opening_pct=30)
    live: dict[str, Any] = {
        "triggered_rules": ["humidity_vent"],
        "target_opening_pct": 30,
    }
    assert compare_results(interp, live) == {}


def test_compare_detects_opening_pct_divergence() -> None:
    interp = _interp_result(target_opening_pct=30)
    live: dict[str, Any] = {
        "triggered_rules": [],
        "target_opening_pct": 60,
    }
    delta = compare_results(interp, live)
    assert delta["target_opening_pct"] == {"interp": 30, "live": 60}


def test_compare_detects_close_all_divergence() -> None:
    interp = _interp_result(triggered=["rain_close_all"], close_all=True)
    live: dict[str, Any] = {
        "triggered_rules": [],  # live didn't fire rain
        "target_opening_pct": 0,
    }
    delta = compare_results(interp, live)
    assert "close_all_windows" in delta
    assert delta["close_all_windows"] == {"interp": True, "live": False}


def test_compare_detects_solar_irrigation_divergence() -> None:
    interp = _interp_result(triggered=["solar_irrigation"])
    live: dict[str, Any] = {
        "triggered_rules": [],
        "target_opening_pct": 0,
    }
    delta = compare_results(interp, live)
    assert delta["solar_irrigation"] == {"interp": True, "live": False}


def test_compare_ignores_p6_marker_rules() -> None:
    """live 側の nighttime_close / temp_schedule 等の marker rule は
    interp との比較で無視される (target_opening_pct が一致すればよい)。"""
    interp = _interp_result(target_opening_pct=25)
    live: dict[str, Any] = {
        "triggered_rules": ["temp_schedule", "window_open"],
        "target_opening_pct": 25,
    }
    assert compare_results(interp, live) == {}


# ══════════════════════════════════════════════
# log_shadow_result
# ══════════════════════════════════════════════

def test_log_shadow_result_appends_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "shadow.jsonl"
    interp = _interp_result(
        triggered=["humidity_vent"], target_opening_pct=30
    )
    live: dict[str, Any] = {
        "triggered_rules": ["humidity_vent"],
        "target_opening_pct": 30,
    }
    log_shadow_result(interp, live, {}, str(log))
    log_shadow_result(interp, live, {"foo": "bar"}, str(log))

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2
    entry1 = json.loads(lines[0])
    entry2 = json.loads(lines[1])
    assert entry1["match"] is True
    assert entry1["delta"] == {}
    assert entry2["match"] is False
    assert entry2["delta"] == {"foo": "bar"}
    assert entry1["interp"]["triggered"] == ["humidity_vent"]
    assert entry1["live"]["target_opening_pct"] == 30


def test_log_shadow_result_creates_parent_dir(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "dir" / "shadow.jsonl"
    interp = _interp_result()
    log_shadow_result(interp, {"triggered_rules": [], "target_opening_pct": 0}, {}, str(log))
    assert log.exists()


# ══════════════════════════════════════════════
# エンドツーエンド (mock 経由)
# ══════════════════════════════════════════════

def test_run_shadow_missing_schema_returns_without_error(
    tmp_path: Path,
) -> None:
    """schema が無いだけなら interpreter を呼ばず静かに return する。"""
    from agriha.control.automation_shadow import run_shadow

    # rule_engine を import せず missing schema パスで抜けさせる
    run_shadow(
        cfg={},
        crop_cfg={},
        sensors={},
        solar_acc={},
        temp_history=[],
        live_result={"triggered_rules": [], "target_opening_pct": 0},
        schema_path=str(tmp_path / "no_such.yaml"),
        active_state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "log.jsonl"),
    )
    # log にも state にも書かれない
    assert not (tmp_path / "log.jsonl").exists()
    assert not (tmp_path / "state.json").exists()


def test_run_shadow_survives_internal_error(tmp_path: Path) -> None:
    """rule_engine の helper が無い環境で schema があっても run_shadow は raise しない
    (build_sensors_dict の import 失敗を non-fatal に呑む)。"""
    from agriha.control.automation_shadow import run_shadow

    schema = tmp_path / "automations.yaml"
    schema.write_text(
        yaml.safe_dump(
            {
                "automations": [
                    {
                        "id": "test",
                        "priority": 10,
                        "trigger": "x > 0",
                        "action": {"kind": "close_all_windows"},
                    }
                ]
            }
        )
    )
    # rule_engine 経由の import が httpx 依存で失敗しても、
    # run_shadow は例外を raise せず warning ログのみ、で戻ってくること
    run_shadow(
        cfg={"rain": {"threshold_mm_h": 0.5}},
        crop_cfg={},
        sensors={},
        solar_acc={},
        temp_history=[],
        live_result={"triggered_rules": [], "target_opening_pct": 0},
        schema_path=str(schema),
        active_state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "log.jsonl"),
    )
    # ここまで到達すれば non-fatal 保証 OK
