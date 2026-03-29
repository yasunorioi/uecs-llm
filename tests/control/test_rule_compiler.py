"""
tests/control/test_rule_compiler.py — rule_compiler.py テスト

矛盾チェッカー、YAML抽出、diff生成、プロンプト構築をテスト。
LLM呼び出しはモックで検証。
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from agriha.control.rule_compiler import (
    build_compile_prompt,
    build_review_prompt,
    check_contradictions,
    extract_yaml_from_response,
    generate_diff,
)


# ──────────────────────────────────────────────
# フィクスチャ
# ──────────────────────────────────────────────

@pytest.fixture
def valid_rules() -> dict[str, Any]:
    """矛盾のない正常なルール。"""
    return {
        "temperature_schedule": {
            "pre_dawn": {"target": 20, "offset_min": -60},
            "morning": {"target": 24},
            "afternoon": {"target": 27},
            "evening": {"target": 23},
            "night": {"target": 19},
        },
        "morning_ventilation": {
            "max_rise_rate_per_hour": 3.0,
        },
        "co2": {
            "outdoor_baseline": 450,
            "ventilation_trigger": 350,
            "critical_low": 300,
        },
        "humidity": {
            "ventilation_start": 85,
            "ventilation_stop": 75,
        },
        "window_step_table": {
            10: 0, 15: 5, 20: 25, 25: 85, 30: 100,
        },
        "window_motor": {
            "full_open_duration_sec": 120,
            "min_change_pct": 5,
        },
    }


# ──────────────────────────────────────────────
# 矛盾チェッカーテスト
# ──────────────────────────────────────────────

def test_valid_rules_no_contradictions(valid_rules):
    """正常なルール → 矛盾なし（温度ジャンプ<5℃に調整済み）"""
    errors = check_contradictions(valid_rules)
    assert len(errors) == 0


def test_co2_critical_gte_trigger():
    """critical_low >= ventilation_trigger → エラー"""
    rules = {
        "co2": {
            "outdoor_baseline": 450,
            "ventilation_trigger": 300,
            "critical_low": 350,  # 逆転
        }
    }
    errors = check_contradictions(rules)
    assert any("critical_low" in e for e in errors)


def test_co2_trigger_gte_baseline():
    """ventilation_trigger >= outdoor_baseline → エラー"""
    rules = {
        "co2": {
            "outdoor_baseline": 350,
            "ventilation_trigger": 400,  # 逆転
            "critical_low": 300,
        }
    }
    errors = check_contradictions(rules)
    assert any("ventilation_trigger" in e for e in errors)


def test_humidity_hysteresis_inverted():
    """ventilation_stop >= ventilation_start → ヒステリシス逆転"""
    rules = {
        "humidity": {
            "ventilation_start": 75,
            "ventilation_stop": 85,  # 逆転
        }
    }
    errors = check_contradictions(rules)
    assert any("ヒステリシス" in e for e in errors)


def test_window_step_table_gap():
    """テーブルに大きな隙間 → 警告"""
    rules = {
        "window_step_table": {
            10: 0,
            30: 100,  # 20℃の隙間
        }
    }
    errors = check_contradictions(rules)
    assert any("隙間" in e for e in errors)


def test_window_step_table_out_of_range():
    """開度が0-100範囲外 → エラー"""
    rules = {
        "window_step_table": {
            10: 0,
            20: 150,  # 範囲外
        }
    }
    errors = check_contradictions(rules)
    assert any("範囲外" in e for e in errors)


def test_temp_schedule_large_jump():
    """隣接時間帯の温度ジャンプ >5℃ → 警告"""
    rules = {
        "temperature_schedule": {
            "pre_dawn": {"target": 15},
            "morning": {"target": 28},  # 13℃ジャンプ
            "afternoon": {"target": 28},
            "evening": {"target": 23},
            "night": {"target": 17},
        }
    }
    errors = check_contradictions(rules)
    assert any("ジャンプ" in e for e in errors)


def test_motor_invalid_values():
    """window_motor の値が0以下 → エラー"""
    rules = {
        "window_motor": {
            "full_open_duration_sec": 0,
            "min_change_pct": -1,
        }
    }
    errors = check_contradictions(rules)
    assert len(errors) >= 2


def test_morning_vent_invalid():
    """max_rise_rate_per_hour <= 0 → エラー"""
    rules = {
        "morning_ventilation": {
            "max_rise_rate_per_hour": 0,
        }
    }
    errors = check_contradictions(rules)
    assert any("max_rise_rate" in e for e in errors)


# ──────────────────────────────────────────────
# YAML抽出テスト
# ──────────────────────────────────────────────

def test_extract_yaml_fenced():
    """```yaml ... ``` ブロックから抽出"""
    response = "Here is the config:\n```yaml\nfoo: bar\nbaz: 42\n```\nDone."
    result = extract_yaml_from_response(response)
    parsed = yaml.safe_load(result)
    assert parsed["foo"] == "bar"
    assert parsed["baz"] == 42


def test_extract_yaml_no_fence():
    """フェンスなし — 全体をYAML扱い"""
    response = "foo: bar\nbaz: 42"
    result = extract_yaml_from_response(response)
    parsed = yaml.safe_load(result)
    assert parsed["foo"] == "bar"


def test_extract_yaml_generic_fence():
    """``` ... ``` (yaml指定なし)"""
    response = "Output:\n```\nfoo: bar\n```"
    result = extract_yaml_from_response(response)
    parsed = yaml.safe_load(result)
    assert parsed["foo"] == "bar"


# ──────────────────────────────────────────────
# diff生成テスト
# ──────────────────────────────────────────────

def test_generate_diff_shows_changes():
    """変更ありのdiffが生成される"""
    old = "foo: 1\nbar: 2\n"
    new = "foo: 1\nbar: 3\n"
    diff = generate_diff(old, new)
    assert "-bar: 2" in diff
    assert "+bar: 3" in diff


def test_generate_diff_no_changes():
    """変更なし → 空文字列"""
    text = "foo: 1\nbar: 2\n"
    diff = generate_diff(text, text)
    assert diff == ""


# ──────────────────────────────────────────────
# プロンプト構築テスト
# ──────────────────────────────────────────────

def test_compile_prompt_structure():
    """compileプロンプトにsystem/userメッセージが含まれる"""
    messages = build_compile_prompt("怒り: 暑い！", "crop:\n  name: ナス", "temperature_schedule:\n  morning:\n    target: 25")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "農学データベース" in messages[1]["content"]
    assert "現行ルール" in messages[1]["content"]
    assert "怒り" in messages[1]["content"]


def test_review_prompt_structure():
    """reviewプロンプトにsystem/userメッセージが含まれる"""
    messages = build_review_prompt("rules here", "log summary", "knowledge here")
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "改善" in messages[0]["content"]
    assert "制御ログ" in messages[1]["content"]
