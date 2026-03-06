"""evaluate.py — 温室制御LLMベンチマーク評価ロジック

評価3軸:
1. JSON構文検証    - LLM応答テキストにJSONが含まれればパース可能か
2. 論理整合性チェック - set_relayのアクションが期待通りか (シナリオのgradingで定義)
3. 時間軸リニア性  - 複数ステップのプランのタイムスタンプが単調増加か
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class RelayCall:
    channel: int
    value: int          # 0=OFF, 1=ON
    duration_sec: int = 0


@dataclass
class EvalResult:
    scenario_id: str
    score: int = 0
    max_score: int = 10
    json_valid: bool = True          # 応答にJSONが含まれていればTrue
    relay_calls: list[RelayCall] = field(default_factory=list)
    matched_criterion: str = ""
    violations: list[str] = field(default_factory=list)
    timeline_linear: bool = True
    timeline_note: str = ""
    notes: str = ""

    @property
    def passed(self) -> bool:
        return self.score >= int(self.max_score * 0.6)

    @property
    def grade(self) -> str:
        ratio = self.score / self.max_score if self.max_score else 0
        if ratio >= 1.0:
            return "PASS"
        if ratio >= 0.6:
            return "PARTIAL"
        return "FAIL"


def evaluate(
    scenario: dict,
    relay_calls: list[dict],
    response_text: str = "",
) -> EvalResult:
    """シナリオ定義とset_relay呼び出し結果から評価スコアを算出する。

    Args:
        scenario:      scenarios/*.json の内容
        relay_calls:   runner が収集した set_relay 呼び出しリスト
                       [{"channel": int, "value": int, "duration_sec": int}, ...]
        response_text: LLM の最終テキスト応答（時間軸リニア性検証に使用）

    Returns:
        EvalResult
    """
    result = EvalResult(scenario_id=scenario["id"])

    # ── 1. RelayCall オブジェクト化 ─────────────────────────────────────
    result.relay_calls = [
        RelayCall(
            channel=int(c["channel"]),
            value=int(c["value"]),
            duration_sec=int(c.get("duration_sec", 0)),
        )
        for c in relay_calls
    ]

    # チャンネル別の最終状態（後から来た set_relay が優先）
    final_states: dict[int, int] = {}
    has_duration: dict[int, bool] = {}
    for rc in result.relay_calls:
        final_states[rc.channel] = rc.value
        if rc.duration_sec > 0:
            has_duration[rc.channel] = True

    # ── 2. JSON構文検証 ──────────────────────────────────────────────────
    result.json_valid = _check_json_in_text(response_text)

    # ── 3. 論理整合性チェック ─────────────────────────────────────────────
    grading = scenario.get("grading", {})
    criteria = grading.get("criteria", [])

    best_score = 0
    for criterion in criteria:
        pts = criterion.get("points", 0)
        cond = criterion.get("condition", {})
        if _check_condition(cond, final_states, has_duration):
            if pts > best_score:
                best_score = pts
                result.matched_criterion = criterion.get("description", "")

    result.score = best_score

    # 違反チェック（ペナルティ）
    for forbidden in grading.get("forbidden_conditions", []):
        cond = forbidden.get("condition", {})
        if cond and _check_condition(cond, final_states, has_duration):
            note = forbidden.get("note", "forbidden action detected")
            penalty = forbidden.get("penalty", 0)
            result.violations.append(note)
            result.score = max(0, result.score - penalty)

    # ── 4. 時間軸リニア性検証 ─────────────────────────────────────────────
    result.timeline_linear, result.timeline_note = check_timeline_linearity(response_text)

    return result


# ── 内部ヘルパー ─────────────────────────────────────────────────────────

def _check_condition(
    condition: dict,
    final_states: dict[int, int],
    has_duration: dict[int, bool],
) -> bool:
    """条件が final_states を満たすか検証する。

    Supported keys:
      relay_on:   [ch, ...]  → 全チャンネルが value==1 であること
      relay_off:  [ch, ...]  → 全チャンネルが value==0 であること
      any_on:     [ch, ...]  → いずれかが value==1 であること
      any_off:    [ch, ...]  → いずれかが value==0 であること
      has_duration: true     → いずれかの set_relay に duration_sec>0 があること
    """
    if not condition:
        return True  # 空条件 = 「何もしなかった」= always match as fallback

    for ch in condition.get("relay_on", []):
        if final_states.get(ch, 0) != 1:
            return False

    for ch in condition.get("relay_off", []):
        if final_states.get(ch, 1) != 0:
            return False

    any_on = condition.get("any_on", [])
    if any_on and not any(final_states.get(ch, 0) == 1 for ch in any_on):
        return False

    any_off = condition.get("any_off", [])
    if any_off and not any(final_states.get(ch, 1) == 0 for ch in any_off):
        return False

    if condition.get("has_duration"):
        if not any(has_duration.values()):
            return False

    return True


def _check_json_in_text(text: str) -> bool:
    """応答テキストに有効なJSONオブジェクトまたは配列が含まれるか検証する。"""
    if not text:
        return True  # テキスト応答なし = tool_callsのみ = 問題なし

    # ```json ... ``` ブロックを優先して検索
    code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    for block in code_blocks:
        try:
            json.loads(block.strip())
            return True
        except (json.JSONDecodeError, ValueError):
            pass

    # { ... } または [ ... ] を直接探す
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        for match in re.finditer(pattern, text):
            try:
                json.loads(match.group())
                return True
            except (json.JSONDecodeError, ValueError):
                pass

    # JSONが含まれていなくてもテキスト応答自体は有効
    return True


def check_timeline_linearity(text: str) -> tuple[bool, str]:
    """応答テキスト中のJSONタイムラインが時間順（単調増加）か検証する。

    LLMが複数ステップの計画を出力する場合:
      [{"time": "13:00", ...}, {"time": "13:30", ...}, {"time": "14:00", ...}]
    のような形式を検出し、time フィールドが昇順かチェックする。

    Returns:
        (is_linear: bool, note: str)
    """
    if not text:
        return True, ""

    # JSONアレイを抽出
    json_blocks = re.findall(r"\[\s*\{[\s\S]*?\}\s*\]", text)

    for block in json_blocks:
        try:
            items = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(items, list) or len(items) < 2:
            continue

        times: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("time", "timestamp", "datetime", "at", "scheduled_at"):
                if key in item and isinstance(item[key], str):
                    times.append(item[key])
                    break

        if len(times) < 2:
            continue

        for i in range(len(times) - 1):
            if times[i] > times[i + 1]:
                return (
                    False,
                    f"タイムライン非線形: ステップ{i+1}({times[i]}) > ステップ{i+2}({times[i+1]})",
                )

    return True, ""


def format_result_table(results: list[EvalResult]) -> str:
    """評価結果を人間が読みやすいテキスト表形式にフォーマットする。"""
    lines = [
        "",
        "=" * 72,
        "  温室制御LLMベンチマーク 評価結果",
        "=" * 72,
        f"  {'ID':<6} {'シナリオ':<28} {'スコア':>6} {'判定':<8} {'違反'}",
        "  " + "-" * 68,
    ]

    total = 0
    max_total = 0
    for r in results:
        violations = "; ".join(r.violations) if r.violations else "-"
        grade_display = {
            "PASS":    "PASS   ✓",
            "PARTIAL": "PARTIAL ~",
            "FAIL":    "FAIL   ✗",
        }.get(r.grade, r.grade)
        lines.append(
            f"  {r.scenario_id:<6} {r.matched_criterion[:26]:<28} "
            f"{r.score:>3}/{r.max_score:<3} {grade_display:<10} {violations[:30]}"
        )
        total += r.score
        max_total += r.max_score

    lines += [
        "  " + "-" * 68,
        f"  {'合計':<34} {total:>3}/{max_total:<3} "
        f"({'%.1f' % (total / max_total * 100 if max_total else 0)}%)",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)
