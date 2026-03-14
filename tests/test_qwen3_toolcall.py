"""tests/test_qwen3_toolcall.py
Phase 0-A: MBPでQwen3 1.7B tool calling検証スクリプト

目的: RPi5エッジLLM導入前のMBP上でのtool calling精度・速度検証
対象モデル: qwen3:1.7b (ollama)
接続先: $OLLAMA_BASE_URL (default: http://localhost:11434/v1/)

使い方:
    # MBP上で実行
    python3 tests/test_qwen3_toolcall.py

    # リモート実行（VPSから）
    ssh mbp.local 'cd ~/uecs-llm && python3 tests/test_qwen3_toolcall.py'

    # pytest経由（verboseモード）
    pytest tests/test_qwen3_toolcall.py -v -s

cmd_id: cmd_403 / subtask_id: subtask_892
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# openai ライブラリ（dev依存: pyproject.toml [dev] に openai>=1.0 あり）
try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai パッケージが必要です: pip install openai")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
MODEL = os.getenv("QWEN3_MODEL", "qwen3:1.7b")

# Qwen3のthinkingモード制御:
#   True  = /no_think をシステムプロンプトに追加（高速・本番想定）
#   False = デフォルト（thinkingありの場合もある）
DISABLE_THINKING = os.getenv("DISABLE_THINKING", "true").lower() == "true"

# ---------------------------------------------------------------------------
# ツール定義（タスク指定の3ツール）
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_sensors",
            "description": (
                "ハウスのセンサーデータ（温度・湿度・CO2等）を取得する。"
                "「温度は？」「何度？」「暑い？」「おんど」「ハウスNo1の状況」などに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "house_id": {
                        "type": "string",
                        "description": "ハウスID（例: '1', '2'）。省略時はデフォルトハウス。",
                    },
                },
                "required": ["house_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": (
                "全体ステータス（センサー・リレー・アクチュエータの状態）を取得する。"
                "「状態見せて」「どうなってる？」「全部見して」「今の状態は」などに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_relay",
            "description": (
                "リレー（換気窓・灌水ポンプ等のアクチュエータ）を制御する。\n"
                "チャンネル割当: ch5=北側窓(開), ch6=北側窓(閉), ch7=南側窓(閉), ch8=南側窓(開), ch4=灌水ポンプ\n"
                "有効チャンネル: 1〜8\n"
                "「開けて」「閉めて」「南側を50%にして」「北側窓閉めて」「灌水ON」などに使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "リレーチャンネル番号（1〜8）",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "value": {
                        "type": "integer",
                        "description": "制御値: 0=OFF, 1=ON, 1〜100=開度%",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "duration_sec": {
                        "type": "integer",
                        "description": "動作時間（秒）。省略時は手動停止まで継続。",
                    },
                },
                "required": ["channel", "value"],
            },
        },
    },
]

# システムプロンプト（AgriHA LINEBotの役割）
SYSTEM_PROMPT = """あなたは農業ハウス管理AI「AgriHA」です。
農家からの日本語メッセージを解釈し、適切なツールを呼び出してください。

チャンネル割当（覚えておくこと）:
- ch4: 灌水ポンプ
- ch5: 北側窓(開)
- ch6: 北側窓(閉)
- ch7: 南側窓(閉)
- ch8: 南側窓(開)
- 有効チャンネル: 1〜8

ツール呼び出しが不要な質問（天気予報・雑談等）には、ツールなしで返答してください。
""" + ("/no_think" if DISABLE_THINKING else "")

# ---------------------------------------------------------------------------
# テストケース定義（17件）
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    prompt: str
    expected_tool: str | None        # None=ツール不要, "multi"=複数ツール
    expected_args: dict | None       # None=argsチェック不要
    category: str                    # normal / fuzzy / edge
    edge_type: str = ""              # out_of_range / no_tool / multi_tool
    description: str = ""

TEST_CASES: list[TestCase] = [
    # --- 正常系 (5件) ---
    TestCase(
        prompt="いまのハウスNo1の温度おしえて",
        expected_tool="get_sensors",
        expected_args={"house_id": "1"},
        category="normal",
        description="ハウスIDつきセンサー取得",
    ),
    TestCase(
        prompt="みなみがわだけ、開度50%にして",
        expected_tool="set_relay",
        expected_args={"channel": 8, "value": 50},
        category="normal",
        description="南側窓(開)=ch8 開度50%",
    ),
    TestCase(
        prompt="今の状態見せて",
        expected_tool="get_status",
        expected_args={},
        category="normal",
        description="全体ステータス取得",
    ),
    TestCase(
        prompt="ハウス2号の温度は？",
        expected_tool="get_sensors",
        expected_args={"house_id": "2"},
        category="normal",
        description="ハウス2号のセンサー取得",
    ),
    TestCase(
        prompt="北側を閉めて",
        expected_tool="set_relay",
        expected_args={"channel": 6},
        category="normal",
        description="北側窓(閉)=ch6",
    ),
    # --- 揺れ表現 (7件) ---
    TestCase(
        prompt="暑い？",
        expected_tool="get_sensors",
        expected_args=None,
        category="fuzzy",
        description="感覚表現→センサー取得",
    ),
    TestCase(
        prompt="何度？",
        expected_tool="get_sensors",
        expected_args=None,
        category="fuzzy",
        description="省略表現→センサー取得",
    ),
    TestCase(
        prompt="おんど",
        expected_tool="get_sensors",
        expected_args=None,
        category="fuzzy",
        description="ひらがな→センサー取得",
    ),
    TestCase(
        prompt="温度は",
        expected_tool="get_sensors",
        expected_args=None,
        category="fuzzy",
        description="短縮表現→センサー取得",
    ),
    TestCase(
        prompt="開けて",
        expected_tool="set_relay",
        expected_args=None,
        category="fuzzy",
        description="動詞のみ→リレー制御",
    ),
    TestCase(
        prompt="閉めて",
        expected_tool="set_relay",
        expected_args=None,
        category="fuzzy",
        description="動詞のみ→リレー制御",
    ),
    TestCase(
        prompt="全部見して",
        expected_tool="get_status",
        expected_args=None,
        category="fuzzy",
        description="方言風→ステータス取得",
    ),
    TestCase(
        prompt="どうなってる？",
        expected_tool="get_status",
        expected_args=None,
        category="fuzzy",
        description="口語表現→ステータス取得",
    ),
    TestCase(
        prompt="ちょっとだけ開けて",
        expected_tool="set_relay",
        expected_args=None,
        category="fuzzy",
        description="程度表現→リレー制御",
    ),
    # --- エッジケース (3件) ---
    TestCase(
        prompt="ch99をONにして",
        expected_tool="set_relay",
        expected_args={"channel": 99},
        category="edge",
        edge_type="out_of_range",
        description="範囲外チャンネル指定（ch99）",
    ),
    TestCase(
        prompt="明日の天気は？",
        expected_tool=None,
        expected_args=None,
        category="edge",
        edge_type="no_tool",
        description="天気予報→ツール不要",
    ),
    TestCase(
        prompt="温度見てから南側開けて",
        expected_tool="multi",
        expected_args=None,
        category="edge",
        edge_type="multi_tool",
        description="マルチツール: get_sensors→set_relay",
    ),
]

# ---------------------------------------------------------------------------
# 結果データクラス
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    case: TestCase
    actual_tools: list[str] = field(default_factory=list)
    actual_args: list[dict] = field(default_factory=list)
    latency_sec: float = 0.0
    tool_correct: bool = False
    args_correct: bool = False
    raw_response: Any = None
    error: str = ""

    def overall_pass(self) -> bool:
        """tool + args の両方が正解ならTrue（argsチェックがNoneの場合はtoolのみ）"""
        if self.error:
            return False
        if self.case.expected_tool == "multi":
            return len(self.actual_tools) >= 2
        if self.case.expected_tool is None:
            return len(self.actual_tools) == 0
        if not self.tool_correct:
            return False
        if self.case.expected_args is not None:
            return self.args_correct
        return True

# ---------------------------------------------------------------------------
# 判定ロジック
# ---------------------------------------------------------------------------

def judge_result(result: TestResult) -> None:
    """actual_tools/actual_argsを期待値と比較してtool_correct/args_correctをセット"""
    case = result.case

    # ツール選択の正否
    if case.expected_tool == "multi":
        result.tool_correct = len(result.actual_tools) >= 2
    elif case.expected_tool is None:
        result.tool_correct = len(result.actual_tools) == 0
    else:
        result.tool_correct = (
            len(result.actual_tools) >= 1 and
            result.actual_tools[0] == case.expected_tool
        )

    # 引数の正否（expected_argsがNoneの場合はスキップ）
    if case.expected_args is None:
        result.args_correct = True
        return

    if not result.actual_args:
        result.args_correct = False
        return

    actual = result.actual_args[0]
    for k, v in case.expected_args.items():
        if actual.get(k) != v:
            result.args_correct = False
            return
    result.args_correct = True

# ---------------------------------------------------------------------------
# メイン実行
# ---------------------------------------------------------------------------

def run_single_case(client: OpenAI, case: TestCase) -> TestResult:
    """1テストケースを実行して結果を返す"""
    result = TestResult(case=case)
    try:
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": case.prompt},
            ],
            tools=TOOLS,
            tool_choice="auto",
        )
        result.latency_sec = time.perf_counter() - start
        result.raw_response = response

        msg = response.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                result.actual_tools.append(tc.function.name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result.actual_args.append(args)

    except Exception as e:
        result.error = str(e)

    judge_result(result)
    return result


def print_result(idx: int, r: TestResult) -> None:
    """1件の結果を表示"""
    status = "✅ PASS" if r.overall_pass() else "❌ FAIL"
    tools_str = ", ".join(r.actual_tools) if r.actual_tools else "(なし)"
    args_str = json.dumps(r.actual_args[0], ensure_ascii=False) if r.actual_args else "(なし)"
    print(f"\n[{idx:02d}] {status} [{r.case.category}/{r.case.edge_type or '-'}]")
    print(f"     入力: {r.case.prompt!r}")
    print(f"     期待: tool={r.case.expected_tool}, args={r.case.expected_args}")
    print(f"     実際: tool={tools_str}, args={args_str}")
    print(f"     速度: {r.latency_sec:.2f}s", end="")
    if r.error:
        print(f"  ERROR: {r.error}", end="")
    print()


def main() -> None:
    print("=" * 60)
    print(f"Phase 0-A: Qwen3 1.7B tool calling検証")
    print(f"Model  : {MODEL}")
    print(f"Endpoint: {OLLAMA_BASE_URL}")
    print(f"Thinking: {'OFF (/no_think)' if DISABLE_THINKING else 'ON (default)'}")
    print(f"Cases  : {len(TEST_CASES)}")
    print("=" * 60)

    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

    # 接続テスト
    try:
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        if MODEL not in model_ids:
            print(f"WARNING: {MODEL} がollama modelリストにありません。利用可能: {model_ids}")
    except Exception as e:
        print(f"ERROR: ollama接続失敗: {e}")
        sys.exit(1)

    results: list[TestResult] = []
    for idx, case in enumerate(TEST_CASES, 1):
        r = run_single_case(client, case)
        results.append(r)
        print_result(idx, r)

    # --- 集計 ---
    total = len(results)
    passed = sum(1 for r in results if r.overall_pass())
    failed = total - passed
    latencies = [r.latency_sec for r in results if not r.error]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0

    # カテゴリ別集計
    categories = {}
    for r in results:
        cat = r.case.category
        if cat not in categories:
            categories[cat] = {"pass": 0, "total": 0}
        categories[cat]["total"] += 1
        if r.overall_pass():
            categories[cat]["pass"] += 1

    print("\n" + "=" * 60)
    print("■ 集計結果")
    print(f"  総合正答率: {passed}/{total} = {passed/total*100:.1f}%")
    print(f"  推論速度  : avg={avg_latency:.2f}s, min={min_latency:.2f}s, max={max_latency:.2f}s")
    print()
    print("  カテゴリ別:")
    for cat, d in categories.items():
        pct = d["pass"] / d["total"] * 100
        print(f"    {cat:8s}: {d['pass']}/{d['total']} ({pct:.1f}%)")

    # 失敗ケース一覧
    failed_cases = [r for r in results if not r.overall_pass()]
    if failed_cases:
        print()
        print("  ❌ 失敗ケース:")
        for r in failed_cases:
            tools_str = ", ".join(r.actual_tools) if r.actual_tools else "(なし)"
            print(f"    - {r.case.prompt!r}")
            print(f"      期待={r.case.expected_tool}, 実際={tools_str}")
            if r.error:
                print(f"      ERROR={r.error}")

    # 問題点サマリ
    print()
    print("■ 日本語理解の問題点（観察）")
    no_tool_errors = [r for r in results
                      if not r.overall_pass() and r.case.expected_tool is None
                      and r.actual_tools]
    if no_tool_errors:
        print(f"  - ツール不要判定ミス: {len(no_tool_errors)}件 "
              f"({[r.case.prompt for r in no_tool_errors]})")
    else:
        print("  - ツール不要判定: 正常")

    fuzzy_fails = [r for r in results
                   if not r.overall_pass() and r.case.category == "fuzzy"]
    if fuzzy_fails:
        print(f"  - 揺れ表現失敗: {len(fuzzy_fails)}件 "
              f"({[r.case.prompt for r in fuzzy_fails]})")
    else:
        print("  - 揺れ表現: 全件正常")

    args_fails = [r for r in results
                  if r.tool_correct and not r.args_correct
                  and r.case.expected_args is not None]
    if args_fails:
        print(f"  - ツール選択OK・引数NG: {len(args_fails)}件")
        for r in args_fails:
            print(f"    {r.case.prompt!r}: 期待={r.case.expected_args}, 実際={r.actual_args}")
    else:
        print("  - 引数精度: 正常（チェック対象内）")

    # 最終判定
    print()
    accuracy = passed / total
    if accuracy >= 0.80:
        print(f"✅ Phase 0-A 合格: 正答率 {accuracy*100:.1f}% ≥ 80%")
        print("   → Phase 1 (RPi5デプロイ) に進んでよい")
    else:
        print(f"❌ Phase 0-A 不合格: 正答率 {accuracy*100:.1f}% < 80%")
        print("   → QLoRAファインチューン（§2.2）またはPrompt改善を検討")

    print("=" * 60)

    # pytest互換: 失敗があればnon-zero exit
    if failed > 0 and os.getenv("PYTEST_FAIL_ON_ERROR", "false").lower() == "true":
        sys.exit(1)


# ---------------------------------------------------------------------------
# pytest互換テスト関数（pytest -v でも実行可能）
# ---------------------------------------------------------------------------

def test_phase0a_toolcalling() -> None:
    """pytest互換エントリポイント: 全テストケースの正答率が80%以上であること"""
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

    results: list[TestResult] = []
    for case in TEST_CASES:
        r = run_single_case(client, case)
        results.append(r)

    total = len(results)
    passed = sum(1 for r in results if r.overall_pass())
    accuracy = passed / total

    # 各ケースの結果をprintで出力（pytest -s で確認可能）
    for idx, r in enumerate(results, 1):
        print_result(idx, r)

    assert accuracy >= 0.80, (
        f"tool calling正答率 {accuracy*100:.1f}% < 80%。"
        f"失敗: {[r.case.prompt for r in results if not r.overall_pass()]}"
    )


if __name__ == "__main__":
    main()
