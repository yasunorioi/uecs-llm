"""tests/control/test_automation_interpreter.py — DSL インタプリタ prototype テスト

docs/dsl_prototype.md の schema を実装した automation_interpreter の動作検証。
既存の evaluate_rules と競合しない純ライブラリのため、外部 I/O は無し。

カバー範囲:
  - 式サンドボックス (whitelist 拒否 / 通常評価)
  - 個別 automation 発火 (P1-P7 相当)
  - Priority + max compose semantics
  - hard_stop の group スコープ (windows / pulse 分離)
  - P4 ヒステリシス (release による持続 active)
  - P7 on_fire 副作用
  - P6 value_expr (window_step_lookup)
"""

from __future__ import annotations

from typing import Any

import pytest

from agriha.control.automation_interpreter import (
    DotDict,
    ExpressionError,
    Interpreter,
    compile_expr,
    eval_expr,
    load_automations,
)


# ══════════════════════════════════════════════
# フィクスチャ — 共通 config / 全 P1-P7 automation 定義
# ══════════════════════════════════════════════

SAMPLE_CONFIG: dict[str, Any] = {
    "rain": {"threshold_mm_h": 0.5},
    "wind": {"strong_wind_threshold_ms": 5.0},
    "morning_ventilation": {"max_rise_rate_per_hour": 3.0},
    "tide": {"humidity_prevent_threshold": 80.0},
    "humidity": {"ventilation_start": 85, "ventilation_stop": 75},
    "co2": {
        "critical_low": 300,
        "ventilation_trigger": 350,
        "outdoor_baseline": 450,
    },
    "irrigation": {"channel": 4},
}


P1_TO_P7_AUTOMATIONS: list[dict[str, Any]] = [
    {
        "id": "rain_close_all",
        "priority": 100,
        "trigger": "rainfall > config.rain.threshold_mm_h",
        "action": {"kind": "close_all_windows"},
        "hard_stop": True,
    },
    {
        "id": "strong_wind",
        "priority": 95,
        "trigger": "wind_speed > config.wind.strong_wind_threshold_ms",
        "action": {"kind": "close_windows_by_wind_direction"},
        "hard_stop": True,
    },
    {
        "id": "rapid_rise_vent",
        "priority": 80,
        "trigger": (
            "rise_rate() > config.morning_ventilation.max_rise_rate_per_hour"
        ),
        "action": {"kind": "set_min_opening", "value": 60},
    },
    {
        "id": "tide_preemptive_vent",
        "priority": 75,
        "trigger": (
            "tide_forecast_humidity_1h() > "
            "config.tide.humidity_prevent_threshold"
        ),
        "action": {"kind": "set_min_opening", "value": 30},
    },
    {
        "id": "humidity_vent",
        "priority": 60,
        "trigger": "indoor_humidity >= config.humidity.ventilation_start",
        "release": "indoor_humidity <= config.humidity.ventilation_stop",
        "action": {"kind": "set_min_opening", "value": 30},
    },
    {
        "id": "co2_critical_vent",
        "priority": 55,
        "trigger": "indoor_co2 <= config.co2.critical_low",
        "action": {"kind": "set_min_opening", "value": 60},
    },
    {
        "id": "co2_vent",
        "priority": 50,
        "trigger": "indoor_co2 <= config.co2.ventilation_trigger",
        "action": {"kind": "set_min_opening", "value": 30},
    },
    {
        "id": "daytime_temp_too_warm_vent",
        "priority": 40,
        "trigger": "not is_night() and indoor_temp > schedule_target_temp()",
        "action": {
            "kind": "set_min_opening",
            "value_expr": "window_step_lookup(outdoor_temp)",
        },
    },
    {
        "id": "solar_irrigation",
        "priority": 30,
        "trigger": "solar_accumulated_mj() >= solar_threshold()",
        "action": {
            "kind": "pulse_relay",
            "ch_expr": "config.irrigation.channel",
            "duration_expr": "irrigation_duration()",
        },
        "on_fire": {
            "reset": ["solar_acc.accumulated_mj"],
            "increment": ["solar_acc.irrigations_today"],
            "set_timestamp": ["solar_acc.last_irrigation_at"],
        },
    },
]


@pytest.fixture
def interpreter() -> Interpreter:
    return Interpreter(load_automations(P1_TO_P7_AUTOMATIONS))


def _stub_primitives(**overrides: Any) -> dict[str, Any]:
    """名前付き primitive のデフォルトスタブ (発火しない値)。上書き可。"""
    defaults: dict[str, Any] = {
        "rise_rate": lambda: 0.0,
        "solar_accumulated_mj": lambda: 0.0,
        "schedule_period": lambda: "morning",
        "schedule_target_temp": lambda: 25.0,
        "is_night": lambda: False,
        "sunset_offset_min": lambda: 120,
        "tide_forecast_humidity_1h": lambda: 50.0,
        "window_step_lookup": lambda ot: 25 if ot < 25 else 100,
        "solar_threshold": lambda: 1.5,
        "irrigation_duration": lambda: 120,
    }
    defaults.update(overrides)
    return defaults


def _base_sensors(**overrides: Any) -> dict[str, Any]:
    """発火しない基準センサ値。上書き可。"""
    defaults: dict[str, Any] = {
        "rainfall": 0.0,
        "wind_speed": 2.0,
        "wind_direction": 0,
        "indoor_temp": 24.0,
        "outdoor_temp": 20.0,
        "indoor_humidity": 60,
        "indoor_co2": 400,
        "insolar": 200,
    }
    defaults.update(overrides)
    return defaults


# ══════════════════════════════════════════════
# 式サンドボックス
# ══════════════════════════════════════════════

def test_compile_expr_rejects_walrus_assignment() -> None:
    with pytest.raises(ExpressionError):
        compile_expr("(x := 1) > 0")


def test_compile_expr_rejects_lambda() -> None:
    with pytest.raises(ExpressionError):
        compile_expr("(lambda: 1)()")


def test_compile_expr_rejects_list_comprehension() -> None:
    with pytest.raises(ExpressionError):
        compile_expr("[i for i in range(3)]")


def test_compile_expr_rejects_subscript_blocking_classic_escape() -> None:
    """`x.__class__.__bases__[0].__subclasses__()` 経由の古典的サンドボックス
    escape は ast.Subscript が whitelist 外なのでコンパイル時に落ちる。
    これが __class__ 自体を wrap で塞ぐより tight な protection。"""
    with pytest.raises(ExpressionError, match="Subscript"):
        compile_expr("x.__class__.__bases__[0].__subclasses__()")


def test_eval_expr_undefined_builtins_are_unavailable() -> None:
    """__builtins__ を空にしているので __import__ 等は NameError で拾える。"""
    with pytest.raises(NameError):
        eval_expr("__import__('os')", {})


def test_eval_expr_simple_comparison() -> None:
    assert eval_expr("a > 1", {"a": 2}) is True
    assert eval_expr("a > 1", {"a": 0}) is False


def test_eval_expr_dotted_config_access() -> None:
    ctx = {"config": DotDict({"foo": {"bar": 42}})}
    assert eval_expr("config.foo.bar", ctx) == 42


def test_dotdict_blocks_underscore_prefixed_dict_keys() -> None:
    """DotDict.__getattr__ は underscore-prefix の dict キーへの読みを拒否する
    (ユーザ側で `_secret` みたいなキー名の露出を防ぐ簡易 hardening)。
    class-level dunder (__class__ 等) は __getattr__ を経由しないため
    ここでは blocked にならないが、ast.Subscript 遮断でエスケープ経路は塞がっている。"""
    d = DotDict({"foo": 1, "_secret": "hidden"})
    assert d.foo == 1
    with pytest.raises(AttributeError):
        d._secret


def test_eval_expr_primitive_call() -> None:
    ctx = {"my_func": lambda: 5}
    assert eval_expr("my_func() > 3", ctx) is True


def test_eval_expr_bool_composition() -> None:
    ctx = {"a": 10, "b": 5}
    assert eval_expr("a > 3 and b < 10", ctx) is True
    assert eval_expr("a > 3 and b > 10", ctx) is False
    assert eval_expr("not (a > 100)", ctx) is True


# ══════════════════════════════════════════════
# 基本発火 / 非発火
# ══════════════════════════════════════════════

def test_no_automations_fire_under_normal_conditions(
    interpreter: Interpreter,
) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(),
        state={"solar_acc": {"accumulated_mj": 0.5}},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert result.triggered == []
    assert result.target_opening_pct == 0
    assert not result.close_all_windows
    assert not result.close_by_wind_direction
    assert result.relay_pulses == []


def test_p1_rain_fires_and_hard_stops_windows(
    interpreter: Interpreter,
) -> None:
    """降雨で全窓閉、下位の湿度・CO2 は発火しない (hard_stop windows)。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(rainfall=1.0, indoor_humidity=90, indoor_co2=280),
        state={"solar_acc": {"accumulated_mj": 0.5}},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert "rain_close_all" in result.triggered
    assert result.close_all_windows
    assert result.target_opening_pct == 0
    assert "windows" in result.hard_stopped_groups
    assert "humidity_vent" not in result.triggered
    assert "co2_critical_vent" not in result.triggered


def test_p2_wind_hard_stops_windows_but_not_pulse(
    interpreter: Interpreter,
) -> None:
    """hard_stop は group="windows" 限定、P7 灌水 (pulse) は独立に発火する。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(wind_speed=10.0),
        state={"solar_acc": {"accumulated_mj": 2.0}},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(solar_accumulated_mj=lambda: 2.0),
    )
    assert "strong_wind" in result.triggered
    assert result.close_by_wind_direction
    assert "solar_irrigation" in result.triggered
    assert len(result.relay_pulses) == 1
    assert result.relay_pulses[0]["ch"] == 4
    assert result.relay_pulses[0]["duration_sec"] == 120


def test_p3_rapid_rise_sets_60_percent(interpreter: Interpreter) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(rise_rate=lambda: 4.0),
    )
    assert "rapid_rise_vent" in result.triggered
    assert result.target_opening_pct == 60


def test_p3_5_tide_forecast_sets_30_percent(
    interpreter: Interpreter,
) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(tide_forecast_humidity_1h=lambda: 85.0),
    )
    assert "tide_preemptive_vent" in result.triggered
    assert result.target_opening_pct >= 30


def test_p4_humidity_fires_at_threshold(interpreter: Interpreter) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=85),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert "humidity_vent" in result.triggered
    assert result.target_opening_pct >= 30


def test_p5_co2_critical_wins_over_vent_via_priority_and_max(
    interpreter: Interpreter,
) -> None:
    """CO2 が critical と vent 両方の条件を満たすとき、両方発火するが
    target は max で 60 (critical) になる。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_co2=280),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert set(result.triggered) >= {"co2_critical_vent", "co2_vent"}
    assert result.target_opening_pct == 60


# ══════════════════════════════════════════════
# max compose / 複数発火
# ══════════════════════════════════════════════

def test_max_compose_takes_highest_floor(interpreter: Interpreter) -> None:
    """P4 humidity(30) と P5 critical(60) を同時発火させ、target=60 になる。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=88, indoor_co2=280),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert set(result.triggered) >= {"humidity_vent", "co2_critical_vent"}
    assert result.target_opening_pct == 60


# ══════════════════════════════════════════════
# ヒステリシス (P4 の release セマンティクス)
# ══════════════════════════════════════════════

def test_p4_hysteresis_activates_and_persists_in_deadband(
    interpreter: Interpreter,
) -> None:
    """Cycle 1: 湿度 90 で発火・active に登録。
    Cycle 2: 湿度 80 (75<x<85) でも active なので発火継続 = 真ヒステリシス。"""
    r1 = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=90),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert "humidity_vent" in r1.triggered
    assert "humidity_vent" in r1.active_after

    r2 = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=80),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
        active_before=r1.active_after,
    )
    assert "humidity_vent" in r2.triggered
    assert "humidity_vent" in r2.active_after


def test_p4_hysteresis_releases_below_vent_stop(
    interpreter: Interpreter,
) -> None:
    """active 状態から湿度 70 (<=75) → release 発火、以降 active から外れる。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=70),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
        active_before={"humidity_vent"},
    )
    assert "humidity_vent" not in result.triggered
    assert "humidity_vent" not in result.active_after


def test_p4_no_activation_in_deadband_when_not_previously_active(
    interpreter: Interpreter,
) -> None:
    """初期 non-active、湿度 80 で trigger も release も真じゃない → 何も起こらない。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_humidity=80),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
    )
    assert "humidity_vent" not in result.triggered
    assert "humidity_vent" not in result.active_after


# ══════════════════════════════════════════════
# P6 value_expr (window_step_lookup)
# ══════════════════════════════════════════════

def test_p6_daytime_hot_uses_step_lookup_via_value_expr(
    interpreter: Interpreter,
) -> None:
    """indoor_temp > target のとき value_expr で window_step_lookup が使われる。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_temp=28, outdoor_temp=15),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(
            schedule_target_temp=lambda: 25.0,
            window_step_lookup=lambda ot: 5 if ot == 15 else 100,
        ),
    )
    assert "daytime_temp_too_warm_vent" in result.triggered
    assert result.target_opening_pct == 5


def test_p6_night_does_not_fire(interpreter: Interpreter) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(indoor_temp=28, outdoor_temp=15),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(is_night=lambda: True),
    )
    assert "daytime_temp_too_warm_vent" not in result.triggered


# ══════════════════════════════════════════════
# P7 solar_irrigation の on_fire 副作用
# ══════════════════════════════════════════════

def test_p7_solar_irrigation_emits_pulse_and_side_effects(
    interpreter: Interpreter,
) -> None:
    result = interpreter.evaluate(
        sensors=_base_sensors(),
        state={"solar_acc": {"accumulated_mj": 2.0}},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(solar_accumulated_mj=lambda: 2.0),
    )
    assert "solar_irrigation" in result.triggered
    assert len(result.relay_pulses) == 1
    pulse = result.relay_pulses[0]
    assert pulse["ch"] == 4
    assert pulse["duration_sec"] == 120
    assert pulse["from_id"] == "solar_irrigation"
    assert "solar_acc.accumulated_mj" in result.state_resets
    assert "solar_acc.irrigations_today" in result.state_increments
    assert "solar_acc.last_irrigation_at" in result.state_timestamps


# ══════════════════════════════════════════════
# 全部乗せシナリオ
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
# strict / 非 strict モード (センサ欠損許容)
# ══════════════════════════════════════════════

def test_missing_sensor_raises_in_strict_mode(
    interpreter: Interpreter,
) -> None:
    """strict=True でセンサ dict にキーがないと ExpressionError で raise。"""
    with pytest.raises(ExpressionError):
        interpreter.evaluate(
            sensors={},  # 全欠損
            state={},
            config=SAMPLE_CONFIG,
            primitives=_stub_primitives(),
            strict=True,
        )


def test_missing_sensor_swallowed_in_non_strict_mode(
    interpreter: Interpreter,
) -> None:
    """strict=False では失敗した automation は skipped に、他は評価継続。"""
    result = interpreter.evaluate(
        sensors={},  # 全欠損
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(),
        strict=False,
    )
    # センサ参照 automation は skipped (P1/P2/P4/P5a/P5b/P6)
    skipped_ids = {sid for sid, _ in result.skipped}
    assert {
        "rain_close_all",
        "strong_wind",
        "humidity_vent",
        "co2_critical_vent",
        "co2_vent",
        "daytime_temp_too_warm_vent",
    } <= skipped_ids
    # センサ非依存 (primitive only) の P3/P3.5/P7 は評価される (今回は発火せず)
    assert result.triggered == []


def test_none_primitive_result_skipped_in_non_strict(
    interpreter: Interpreter,
) -> None:
    """rise_rate() が None を返しても strict=False なら interpreter は生存。"""
    result = interpreter.evaluate(
        sensors=_base_sensors(),
        state={},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(rise_rate=lambda: None),
        strict=False,
    )
    # rapid_rise_vent は skipped (None > 3.0 は TypeError)
    skipped_ids = {sid for sid, _ in result.skipped}
    assert "rapid_rise_vent" in skipped_ids


# ══════════════════════════════════════════════
# 全部乗せシナリオ
# ══════════════════════════════════════════════

def test_full_scenario_rain_dominates_everything_except_irrigation(
    interpreter: Interpreter,
) -> None:
    """降雨中でも湿度・CO2・急昇温・日射灌水条件が揃った状態を評価:
    - windows 側は全て hard_stop で rain_close_all のみ生存
    - pulse 側は独立で solar_irrigation 発火
    """
    result = interpreter.evaluate(
        sensors=_base_sensors(
            rainfall=1.0,
            indoor_humidity=95,
            indoor_co2=250,
            indoor_temp=30,
            outdoor_temp=28,
        ),
        state={"solar_acc": {"accumulated_mj": 3.0}},
        config=SAMPLE_CONFIG,
        primitives=_stub_primitives(
            rise_rate=lambda: 5.0,
            solar_accumulated_mj=lambda: 3.0,
            tide_forecast_humidity_1h=lambda: 90.0,
        ),
    )
    assert "rain_close_all" in result.triggered
    assert result.close_all_windows
    assert result.target_opening_pct == 0
    assert "windows" in result.hard_stopped_groups
    # 他 windows automation は全部 skip されるはず
    for skipped in [
        "rapid_rise_vent",
        "tide_preemptive_vent",
        "humidity_vent",
        "co2_critical_vent",
        "daytime_temp_too_warm_vent",
    ]:
        assert skipped not in result.triggered
    # pulse は独立
    assert "solar_irrigation" in result.triggered
