#!/usr/bin/env python3
"""
rule_compiler.py — Layer 3: LLMルールコンパイラ

農家の方針(system_prompt.txt) + 農学データ(agri_knowledge.yaml)
+ 現行ルール(rules.yaml) を入力し、LLMが矛盾なきrules.yamlを生成する。

リアルタイム実行ではなく、方針変更時にのみ起動。

設計書: docs/layer3_compiler_design.md §4

起動方法:
  python3 -m agriha.control.rule_compiler compile   # 手動コンパイル
  python3 -m agriha.control.rule_compiler review     # 週次レビュー
  python3 -m agriha.control.rule_compiler validate   # 矛盾チェックのみ
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("rule_compiler")
_JST = ZoneInfo("Asia/Tokyo")

# ──────────────────────────────────────────────
# デフォルトパス
# ──────────────────────────────────────────────

DEFAULT_RULES_PATH = os.environ.get(
    "RULES_CONFIG_PATH", "/etc/agriha/rules.yaml"
)
DEFAULT_KNOWLEDGE_PATH = os.environ.get(
    "AGRI_KNOWLEDGE_PATH", "/etc/agriha/agri_knowledge.yaml"
)
DEFAULT_SYSTEM_PROMPT_PATH = os.environ.get(
    "SYSTEM_PROMPT_PATH", "/etc/agriha/system_prompt.txt"
)
DEFAULT_CANDIDATE_PATH = os.environ.get(
    "RULES_CANDIDATE_PATH", "/var/lib/agriha/rules_candidate.yaml"
)
DEFAULT_CONTROL_LOG_DB = os.environ.get(
    "CONTROL_LOG_DB", "/var/lib/agriha/control_log.db"
)
DEFAULT_STATE_PATH = os.environ.get(
    "RULE_ENGINE_STATE_PATH", "/var/lib/agriha/rule_engine_state.json"
)
NULLCLAW_BASE_URL = os.environ.get(
    "NULLCLAW_BASE_URL", "http://localhost:3001/v1/"
)

# ──────────────────────────────────────────────
# ロガー設定
# ──────────────────────────────────────────────

def _setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)s [rule_compiler] %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
    ])


# ──────────────────────────────────────────────
# ファイル読み込み
# ──────────────────────────────────────────────

def load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_yaml(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ──────────────────────────────────────────────
# 矛盾チェッカー（LLM不要、静的解析）
# ──────────────────────────────────────────────

def check_contradictions(rules: dict[str, Any]) -> list[str]:
    """ルール間の矛盾を検出する。LLM不要、静的解析。"""
    errors: list[str] = []

    # 1. window_step_table: キーが昇順で隙間なし
    step_table = rules.get("window_step_table", {})
    if step_table:
        temps = sorted(int(k) for k in step_table.keys())
        for i in range(len(temps) - 1):
            gap = temps[i + 1] - temps[i]
            if gap > 10:
                errors.append(
                    f"window_step_table: {temps[i]}℃〜{temps[i+1]}℃ に {gap}℃の隙間"
                )
        # 値が0-100の範囲
        for k, v in step_table.items():
            v_int = int(v)
            if v_int < 0 or v_int > 100:
                errors.append(f"window_step_table[{k}] = {v} は範囲外(0-100)")

    # 2. temperature_schedule: 隣接時間帯の温度ジャンプが5℃以上なら警告
    schedule = rules.get("temperature_schedule", {})
    if schedule:
        period_order = ["pre_dawn", "morning", "afternoon", "evening", "night"]
        temps_seq = []
        for p in period_order:
            if p in schedule and "target" in schedule[p]:
                temps_seq.append((p, float(schedule[p]["target"])))
        for i in range(len(temps_seq) - 1):
            p1, t1 = temps_seq[i]
            p2, t2 = temps_seq[i + 1]
            if abs(t2 - t1) > 5:
                errors.append(
                    f"temperature_schedule: {p1}({t1}℃)→{p2}({t2}℃) "
                    f"ジャンプ {abs(t2-t1):.0f}℃ (>5℃警告)"
                )

    # 3. CO2設定: critical_low < ventilation_trigger < outdoor_baseline
    co2 = rules.get("co2", {})
    if co2:
        critical = co2.get("critical_low", 0)
        trigger = co2.get("ventilation_trigger", 0)
        baseline = co2.get("outdoor_baseline", 0)
        if critical >= trigger:
            errors.append(
                f"co2: critical_low({critical}) >= ventilation_trigger({trigger})"
            )
        if trigger >= baseline:
            errors.append(
                f"co2: ventilation_trigger({trigger}) >= outdoor_baseline({baseline})"
            )

    # 4. humidity: ventilation_stop < ventilation_start (ヒステリシス)
    humidity = rules.get("humidity", {})
    if humidity:
        start = humidity.get("ventilation_start", 0)
        stop = humidity.get("ventilation_stop", 0)
        if stop >= start:
            errors.append(
                f"humidity: ventilation_stop({stop}) >= ventilation_start({start}) "
                f"ヒステリシスが逆転"
            )

    # 5. window_motor: 値が正
    motor = rules.get("window_motor", {})
    if motor:
        if motor.get("full_open_duration_sec", 1) <= 0:
            errors.append("window_motor: full_open_duration_sec must be > 0")
        if motor.get("min_change_pct", 1) <= 0:
            errors.append("window_motor: min_change_pct must be > 0")

    # 6. morning_ventilation: max_rise_rate は正の値
    morning = rules.get("morning_ventilation", {})
    if morning:
        rate = morning.get("max_rise_rate_per_hour", 0)
        if rate <= 0:
            errors.append("morning_ventilation: max_rise_rate_per_hour must be > 0")

    return errors


# ──────────────────────────────────────────────
# diff生成
# ──────────────────────────────────────────────

def generate_diff(current_yaml: str, candidate_yaml: str) -> str:
    """現行ルールと候補ルールのdiffを生成。"""
    current_lines = current_yaml.splitlines(keepends=True)
    candidate_lines = candidate_yaml.splitlines(keepends=True)
    diff = difflib.unified_diff(
        current_lines, candidate_lines,
        fromfile="rules.yaml (current)",
        tofile="rules.yaml (candidate)",
        lineterm="",
    )
    return "".join(diff)


# ──────────────────────────────────────────────
# コンパイラ用システムプロンプト生成
# ──────────────────────────────────────────────

COMPILER_SYSTEM_PROMPT = """\
あなたは北海道恵庭の長ナス農家のハウス制御ルールを設計するAIです。

## あなたの仕事

農家が書いた方針・怒り・観察メモを読み取り、
制御ルール（rules.yaml形式）を生成してください。

**あなたはリアルタイムで制御判断をしません。**
あなたが生成したルールが、10分毎に自動実行されます。

## 生成ルールの制約

1. priority_chain の優先順位を厳守すること:
   P1:降雨→全閉 > P2:強風→方向別閉鎖 > P3:急昇温→強制換気 > \
P4:湿度→換気 > P5:CO2→換気 > P6:変温管理+開度テーブル > P7:日射比例灌水
2. ルール間で矛盾がある場合、優先度の高いルールが勝つ
3. 温度・CO2・湿度が同時に閾値を超えた場合の挙動を明示すること
4. CO2施用装置はない。外気(~450ppm)が最高値。換気がCO2補充の唯一の手段
5. 窓はON/OFF制御（比例制御不可）。開度は駆動時間で近似
6. window_step_tableは外気温ベース。室温ではない
7. 出力はYAML形式のみ。説明テキストは出力しないこと
8. コメントで各設定値の根拠を記載すること

## 出力フォーマット

以下のセクションを含むYAMLを出力してください:
- temperature_schedule (5時間帯: pre_dawn/morning/afternoon/evening/night)
- morning_ventilation
- co2
- humidity
- window_step_table (外気温5℃刻み → 開度%)
- window_motor
- wind
- rain
- irrigation
- unipi_api
- location
"""


def build_compile_prompt(
    farmer_policy: str,
    knowledge_yaml: str,
    current_rules_yaml: str,
) -> list[dict[str, str]]:
    """compileモード用のメッセージリストを構築。"""
    user_content = (
        "## 農学データベース\n\n"
        f"```yaml\n{knowledge_yaml}\n```\n\n"
        "## 現行ルール\n\n"
        f"```yaml\n{current_rules_yaml}\n```\n\n"
        "## 農家の方針・怒り・観察メモ\n\n"
        f"{farmer_policy}\n\n"
        "---\n"
        "上記の情報に基づいて、新しいrules.yamlを生成してください。\n"
        "変更点にはコメントで理由を記載してください。\n"
        "YAMLのみ出力し、説明テキストは含めないでください。"
    )
    return [
        {"role": "system", "content": COMPILER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ──────────────────────────────────────────────
# 週次レビュー用プロンプト
# ──────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
あなたは北海道恵庭の長ナス農家のハウス制御ルールを改善するAIです。

## あなたの仕事

直近1週間の制御ログと現行ルールを分析し、改善提案を生成してください。

## 出力フォーマット

以下のJSON形式で出力してください:

```json
{
  "patterns": [
    {"description": "パターンの説明", "frequency": "発生頻度", "suggestion": "提案"}
  ],
  "anomalies": [
    {"timestamp": "日時", "description": "異常内容", "suggestion": "対策提案"}
  ],
  "questions": [
    "農家への質問（3件以内）"
  ],
  "proposed_changes": {
    "section": "変更セクション",
    "current": "現在の値",
    "proposed": "提案値",
    "reason": "理由"
  }
}
```
"""


def build_review_prompt(
    current_rules_yaml: str,
    control_log_summary: str,
    knowledge_yaml: str,
) -> list[dict[str, str]]:
    """reviewモード用のメッセージリストを構築。"""
    user_content = (
        "## 現行ルール\n\n"
        f"```yaml\n{current_rules_yaml}\n```\n\n"
        "## 農学データベース\n\n"
        f"```yaml\n{knowledge_yaml}\n```\n\n"
        "## 直近1週間の制御ログサマリ\n\n"
        f"{control_log_summary}\n\n"
        "---\n"
        "上記を分析し、改善提案をJSON形式で出力してください。"
    )
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ──────────────────────────────────────────────
# 制御ログ読み込み
# ──────────────────────────────────────────────

def load_control_log_summary(db_path: str, days: int = 7) -> str:
    """control_log.dbから直近N日の制御ログサマリを取得。"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM decisions "
            "WHERE created_at > datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 100",
            (f"-{days} days",),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return "(制御ログなし — 直近7日間の判断記録がありません)"

        lines = [f"直近{days}日間の制御判断: {len(rows)}件\n"]
        for row in rows[:20]:  # 最大20件をサマリに含める
            lines.append(
                f"- {row['created_at']}: {row.get('summary', 'N/A')}"
            )
        if len(rows) > 20:
            lines.append(f"... 他{len(rows)-20}件")
        return "\n".join(lines)

    except (sqlite3.OperationalError, FileNotFoundError) as e:
        return f"(制御ログ読み込みエラー: {e})"


# ──────────────────────────────────────────────
# rule_engine_state.json読み込み（レビュー用）
# ──────────────────────────────────────────────

def load_recent_states(state_path: str) -> str:
    """直近のrule_engine実行結果を読み込む。"""
    try:
        data = json.loads(Path(state_path).read_text())
        return json.dumps(data, indent=2, ensure_ascii=False)
    except (FileNotFoundError, json.JSONDecodeError):
        return "(rule_engine状態ファイルなし)"


# ──────────────────────────────────────────────
# LLM呼び出し
# ──────────────────────────────────────────────

def call_llm(
    messages: list[dict[str, str]],
    model: str = "nullclaw-local",
    max_tokens: int = 2048,
) -> str:
    """OpenAI互換APIでLLMを呼び出し、テキスト応答を返す。"""
    from openai import OpenAI  # type: ignore[import]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    nullclaw_url = NULLCLAW_BASE_URL

    if api_key:
        # Anthropic API直接（Claude Haiku等）
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.anthropic.com/v1/",
            timeout=60.0,
        )
        model = "claude-haiku-4-5-20251001"
        logger.info("LLM: Anthropic API (model=%s)", model)
    else:
        # NullClaw（ローカルLLM）
        client = OpenAI(
            api_key="local",
            base_url=nullclaw_url,
            timeout=120.0,
        )
        logger.info("LLM: NullClaw (url=%s, model=%s)", nullclaw_url, model)

    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    content = response.choices[0].message.content or ""
    logger.info("LLM応答: %d文字", len(content))
    return content


# ──────────────────────────────────────────────
# LINE通知
# ──────────────────────────────────────────────

def notify_line(message: str) -> bool:
    """LINE Push APIで農家に通知する。環境変数未設定時はスキップ。"""
    access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not access_token or not user_id:
        logger.info("LINE通知スキップ（環境変数未設定）")
        return False

    try:
        from agriha.chat.linebot_handler import send_push
        result = send_push(user_id, message, access_token)
        if result:
            logger.info("LINE通知送信成功")
        return result
    except ImportError:
        logger.warning("linebot_handler インポート失敗 — LINE通知スキップ")
        return False


def format_review_for_line(response: str) -> str:
    """レビュー結果をLINE向けに整形（5000文字制限）。"""
    # JSON応答をパースしてわかりやすく整形
    try:
        data = json.loads(response)
        lines = ["🌱 AgriHA 週次レビュー\n"]

        patterns = data.get("patterns", [])
        if patterns:
            lines.append("📊 検出パターン:")
            for p in patterns[:3]:
                lines.append(f"  • {p.get('description', '')}")
                if p.get("suggestion"):
                    lines.append(f"    → {p['suggestion']}")

        anomalies = data.get("anomalies", [])
        if anomalies:
            lines.append("\n⚠ 異常検出:")
            for a in anomalies[:3]:
                lines.append(f"  • {a.get('description', '')}")
                if a.get("suggestion"):
                    lines.append(f"    → {a['suggestion']}")

        questions = data.get("questions", [])
        if questions:
            lines.append("\n❓ 確認事項:")
            for i, q in enumerate(questions[:3], 1):
                lines.append(f"  {i}. {q}")

        changes = data.get("proposed_changes")
        if changes:
            lines.append("\n📝 変更提案あり")
            lines.append("  チャットで「ルール更新して」と送ると適用できます")

        result = "\n".join(lines)
        return result[:5000]  # LINE文字数制限

    except (json.JSONDecodeError, TypeError):
        # JSONパース失敗時はそのまま送信
        header = "🌱 AgriHA 週次レビュー\n\n"
        return (header + response)[:5000]


def format_compile_for_line(diff_text: str, errors: list[str]) -> str:
    """コンパイル結果をLINE向けに整形。"""
    lines = ["🔧 AgriHA ルール更新候補を生成しました\n"]

    if errors:
        lines.append(f"⚠ 矛盾警告: {len(errors)}件")
        for err in errors[:3]:
            lines.append(f"  • {err}")

    if diff_text:
        # diffの主要部分のみ
        diff_lines = diff_text.splitlines()
        change_lines = [l for l in diff_lines if l.startswith("+") or l.startswith("-")]
        change_lines = [l for l in change_lines if not l.startswith("+++") and not l.startswith("---")]
        if change_lines:
            lines.append(f"\n変更 {len(change_lines)}行:")
            for l in change_lines[:10]:
                lines.append(f"  {l}")
            if len(change_lines) > 10:
                lines.append(f"  ... 他{len(change_lines)-10}行")
    else:
        lines.append("\n(変更なし)")

    lines.append("\nチャットで「ルール適用」と送ると反映されます")
    return "\n".join(lines)[:5000]


# ──────────────────────────────────────────────
# YAML抽出
# ──────────────────────────────────────────────

def extract_yaml_from_response(response: str) -> str:
    """LLM応答からYAMLブロックを抽出。"""
    import re

    # ```yaml ... ``` ブロックを探す
    match = re.search(r"```ya?ml\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # ``` ... ``` ブロック
    match = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # フェンスなし — 全体をYAMLとして扱う
    return response.strip()


# ──────────────────────────────────────────────
# compileコマンド
# ──────────────────────────────────────────────

def cmd_compile(
    rules_path: str = DEFAULT_RULES_PATH,
    knowledge_path: str = DEFAULT_KNOWLEDGE_PATH,
    prompt_path: str = DEFAULT_SYSTEM_PROMPT_PATH,
    candidate_path: str = DEFAULT_CANDIDATE_PATH,
    dry_run: bool = False,
) -> int:
    """方針からrules_candidate.yamlを生成する。"""
    logger.info("=== compile モード開始 ===")

    # 入力ファイル読み込み
    try:
        farmer_policy = load_text(prompt_path)
        knowledge_yaml = load_text(knowledge_path)
        current_rules_yaml = load_text(rules_path)
    except FileNotFoundError as e:
        logger.error("入力ファイルが見つかりません: %s", e)
        return 1

    # プロンプト構築
    messages = build_compile_prompt(farmer_policy, knowledge_yaml, current_rules_yaml)

    if dry_run:
        logger.info("dry-run: プロンプトを表示して終了")
        for msg in messages:
            print(f"\n--- {msg['role']} ---")
            print(msg["content"][:500] + "..." if len(msg["content"]) > 500 else msg["content"])
        return 0

    # LLM呼び出し
    try:
        response = call_llm(messages, max_tokens=4096)
    except Exception as e:
        logger.error("LLM呼び出しエラー: %s", e)
        return 1

    # YAML抽出・検証
    candidate_yaml_str = extract_yaml_from_response(response)
    try:
        candidate_rules = yaml.safe_load(candidate_yaml_str)
        if not isinstance(candidate_rules, dict):
            logger.error("LLM出力がYAML辞書ではありません")
            print("--- LLM raw output ---")
            print(response[:1000])
            return 1
    except yaml.YAMLError as e:
        logger.error("YAML構文エラー: %s", e)
        print("--- LLM raw output ---")
        print(response[:1000])
        return 1

    # 矛盾チェック
    errors = check_contradictions(candidate_rules)
    if errors:
        logger.warning("矛盾チェックで %d 件の警告:", len(errors))
        for err in errors:
            logger.warning("  - %s", err)
        print("\n⚠ 矛盾チェック警告:")
        for err in errors:
            print(f"  - {err}")

    # diff生成
    diff_text = generate_diff(current_rules_yaml, candidate_yaml_str)
    if diff_text:
        print("\n--- diff (current → candidate) ---")
        print(diff_text)
    else:
        print("\n(変更なし)")

    # 候補ファイル書き出し
    Path(candidate_path).parent.mkdir(parents=True, exist_ok=True)
    with open(candidate_path, "w", encoding="utf-8") as f:
        f.write(f"# Generated by rule_compiler.py at {datetime.now(_JST).isoformat()}\n")
        f.write(f"# Contradictions: {len(errors)}\n")
        f.write(candidate_yaml_str)
    logger.info("候補ルール書き出し: %s", candidate_path)

    print(f"\n✓ 候補ルール生成完了: {candidate_path}")
    if errors:
        print(f"  ⚠ 矛盾警告: {len(errors)}件 — 確認後に apply してください")
    else:
        print("  ✓ 矛盾チェック通過")
    print(f"\n適用するには: python3 -m agriha.control.rule_compiler apply")

    # LINE通知
    line_msg = format_compile_for_line(diff_text, errors)
    notify_line(line_msg)

    return 0


# ──────────────────────────────────────────────
# reviewコマンド
# ──────────────────────────────────────────────

def cmd_review(
    rules_path: str = DEFAULT_RULES_PATH,
    knowledge_path: str = DEFAULT_KNOWLEDGE_PATH,
    db_path: str = DEFAULT_CONTROL_LOG_DB,
    state_path: str = DEFAULT_STATE_PATH,
    dry_run: bool = False,
) -> int:
    """週次レビュー: ログ分析→改善提案。"""
    logger.info("=== review モード開始 ===")

    try:
        current_rules_yaml = load_text(rules_path)
        knowledge_yaml = load_text(knowledge_path)
    except FileNotFoundError as e:
        logger.error("入力ファイルが見つかりません: %s", e)
        return 1

    # 制御ログ読み込み
    control_log = load_control_log_summary(db_path)
    state_info = load_recent_states(state_path)

    log_summary = f"{control_log}\n\n## 直近のrule_engine状態\n{state_info}"

    messages = build_review_prompt(current_rules_yaml, log_summary, knowledge_yaml)

    if dry_run:
        logger.info("dry-run: プロンプトを表示して終了")
        for msg in messages:
            print(f"\n--- {msg['role']} ---")
            print(msg["content"][:500] + "..." if len(msg["content"]) > 500 else msg["content"])
        return 0

    try:
        response = call_llm(messages, max_tokens=2048)
    except Exception as e:
        logger.error("LLM呼び出しエラー: %s", e)
        return 1

    print("\n=== 週次レビュー結果 ===\n")
    print(response)

    # レビュー結果保存
    review_path = Path(DEFAULT_CANDIDATE_PATH).parent / "review_result.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_data = {
        "generated_at": datetime.now(_JST).isoformat(),
        "response": response,
    }
    review_path.write_text(json.dumps(review_data, ensure_ascii=False, indent=2))
    logger.info("レビュー結果保存: %s", review_path)

    # LINE通知
    line_msg = format_review_for_line(response)
    notify_line(line_msg)

    return 0


# ──────────────────────────────────────────────
# validateコマンド
# ──────────────────────────────────────────────

def cmd_validate(rules_path: str = DEFAULT_RULES_PATH) -> int:
    """現行rules.yamlの矛盾チェックのみ実行。LLM不要。"""
    logger.info("=== validate モード ===")

    try:
        rules = load_yaml(rules_path)
    except FileNotFoundError as e:
        logger.error("ファイルが見つかりません: %s", e)
        return 1
    except yaml.YAMLError as e:
        logger.error("YAML構文エラー: %s", e)
        return 1

    errors = check_contradictions(rules)
    if errors:
        print(f"⚠ {len(errors)}件の矛盾/警告:")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print("✓ 矛盾なし — rules.yaml は整合性チェックを通過しました")
        return 0


# ──────────────────────────────────────────────
# applyコマンド
# ──────────────────────────────────────────────

def cmd_apply(
    rules_path: str = DEFAULT_RULES_PATH,
    candidate_path: str = DEFAULT_CANDIDATE_PATH,
    backup: bool = True,
) -> int:
    """候補ルールを現行ルールに適用（バックアップ付き）。"""
    logger.info("=== apply モード ===")

    if not Path(candidate_path).exists():
        logger.error("候補ファイルが見つかりません: %s", candidate_path)
        print("先に compile を実行してください")
        return 1

    # 候補ファイルの検証
    try:
        candidate = load_yaml(candidate_path)
    except yaml.YAMLError as e:
        logger.error("候補YAML構文エラー: %s", e)
        return 1

    errors = check_contradictions(candidate)
    if errors:
        print(f"⚠ 候補ルールに{len(errors)}件の矛盾警告があります:")
        for err in errors:
            print(f"  - {err}")
        confirm = input("\n適用しますか? (y/N): ").strip().lower()
        if confirm != "y":
            print("中止しました")
            return 1

    # バックアップ
    if backup and Path(rules_path).exists():
        timestamp = datetime.now(_JST).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{rules_path}.bak.{timestamp}"
        Path(backup_path).write_text(Path(rules_path).read_text())
        logger.info("バックアップ: %s", backup_path)
        print(f"バックアップ: {backup_path}")

    # 候補をコピー（generated byコメント行は除去）
    candidate_text = Path(candidate_path).read_text()
    lines = candidate_text.splitlines(keepends=True)
    clean_lines = [l for l in lines if not l.startswith("# Generated by") and not l.startswith("# Contradictions:")]
    Path(rules_path).write_text("".join(clean_lines))

    logger.info("適用完了: %s → %s", candidate_path, rules_path)
    print(f"✓ ルール適用完了: {rules_path}")
    print("  次のcron実行（10分以内）から新ルールが有効になります")

    return 0


# ──────────────────────────────────────────────
# CLI エントリポイント
# ──────────────────────────────────────────────

def main() -> int:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="LLM Rule Compiler — 農家の方針をrules.yamlに変換",
    )
    sub = parser.add_subparsers(dest="command")

    # compile
    p_compile = sub.add_parser("compile", help="方針からrules_candidate.yamlを生成")
    p_compile.add_argument("--rules", default=DEFAULT_RULES_PATH, help="現行rules.yaml")
    p_compile.add_argument("--knowledge", default=DEFAULT_KNOWLEDGE_PATH, help="農学データベース")
    p_compile.add_argument("--prompt", default=DEFAULT_SYSTEM_PROMPT_PATH, help="system_prompt.txt")
    p_compile.add_argument("--output", default=DEFAULT_CANDIDATE_PATH, help="候補出力先")
    p_compile.add_argument("--dry-run", action="store_true", help="LLM呼び出しせずプロンプト表示")

    # review
    p_review = sub.add_parser("review", help="週次レビュー: ログ分析→改善提案")
    p_review.add_argument("--rules", default=DEFAULT_RULES_PATH, help="現行rules.yaml")
    p_review.add_argument("--knowledge", default=DEFAULT_KNOWLEDGE_PATH, help="農学データベース")
    p_review.add_argument("--db", default=DEFAULT_CONTROL_LOG_DB, help="制御ログDB")
    p_review.add_argument("--dry-run", action="store_true", help="LLM呼び出しせずプロンプト表示")

    # validate
    p_validate = sub.add_parser("validate", help="矛盾チェックのみ（LLM不要）")
    p_validate.add_argument("--rules", default=DEFAULT_RULES_PATH, help="rules.yaml")

    # apply
    p_apply = sub.add_parser("apply", help="候補ルールを適用")
    p_apply.add_argument("--rules", default=DEFAULT_RULES_PATH, help="適用先rules.yaml")
    p_apply.add_argument("--candidate", default=DEFAULT_CANDIDATE_PATH, help="候補ファイル")
    p_apply.add_argument("--no-backup", action="store_true", help="バックアップ不要")

    args = parser.parse_args()

    if args.command == "compile":
        return cmd_compile(
            rules_path=args.rules,
            knowledge_path=args.knowledge,
            prompt_path=args.prompt,
            candidate_path=args.output,
            dry_run=args.dry_run,
        )
    elif args.command == "review":
        return cmd_review(
            rules_path=args.rules,
            knowledge_path=args.knowledge,
            db_path=args.db,
            dry_run=args.dry_run,
        )
    elif args.command == "validate":
        return cmd_validate(rules_path=args.rules)
    elif args.command == "apply":
        return cmd_apply(
            rules_path=args.rules,
            candidate_path=args.candidate,
            backup=not args.no_backup,
        )
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
