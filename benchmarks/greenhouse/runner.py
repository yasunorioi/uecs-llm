#!/usr/bin/env python3
"""runner.py — 温室制御LLMベンチマークランナー

OpenAI SDK互換のLLM（NullClaw proxy, Claude, OpenAI等）に対して
7シナリオを実行し、evaluate.py でスコアリングする。

Usage:
  # NullClaw (デフォルト)
  python runner.py

  # Claude Haiku
  python runner.py --base-url https://api.anthropic.com/v1 \\
                   --model claude-haiku-4-5-20251001 \\
                   --api-key $ANTHROPIC_API_KEY

  # 特定シナリオのみ
  python runner.py --scenarios S01,S03

  # 結果をJSONファイルに保存
  python runner.py --output results/nullclaw_$(date +%Y%m%d).json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai パッケージが必要です: pip install openai", file=sys.stderr)
    sys.exit(1)

from evaluate import EvalResult, RelayCall, evaluate, format_result_table

# ── ツール定義（forecast_engine.py と同形式）────────────────────────────
BENCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_sensors",
            "description": (
                "全センサーデータ取得。"
                "CCM内気象(温度/湿度/CO2) + Misol外気象(気温/風速/風向/降雨) + リレー状態を返す。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_relay",
            "description": "リレーチャンネルのON/OFFを制御する。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "リレーチャンネル番号(1-8)",
                    },
                    "value": {
                        "type": "integer",
                        "description": "0=OFF, 1=ON",
                    },
                    "duration_sec": {
                        "type": "integer",
                        "description": "自動OFF秒数(0=永続、CO2パルス換気は300〜600推奨)",
                    },
                },
                "required": ["channel", "value"],
            },
        },
    },
]


# ── センサーデータをLLMへの入力テキストに変換 ──────────────────────────
def format_sensor_message(scenario: dict) -> str:
    s = scenario["sensors"]
    f = scenario["weather_forecast_1h"]
    dt_str = scenario["datetime"]
    sun = scenario.get("sun", {})
    relay = s["relay_states"]

    def rs(ch: int) -> str:
        return "ON" if relay.get(f"ch{ch}", 0) else "OFF"

    lines: list[str] = [f"現在時刻: {dt_str}"]
    if sun:
        day_night = "昼間" if sun.get("is_daytime") else "夜間"
        lines.append(
            f"日出: {sun.get('sunrise', '?')} / 日没: {sun.get('sunset', '?')} / {day_night}"
        )
        mins = sun.get("minutes_since_sunrise", 0)
        if sun.get("is_daytime") and mins >= 0:
            lines.append(f"日出後: {mins}分")

    lines += [
        "",
        "=== センサーデータ ===",
        "【ハウス内】",
        f"温度: {s['inside_temp_c']}℃ / 湿度: {s['inside_humidity_pct']}% / CO2: {s['co2_ppm']}ppm",
        "【ハウス外（Misol）】",
        (
            f"気温: {s['outside_temp_c']}℃ / 湿度: {s['outside_humidity_pct']}% / "
            f"風速: {s['wind_speed_ms']}m/s / 風向: {s['wind_direction']} / "
            f"降雨: {'あり' if s['rain'] else 'なし'}"
        ),
        "【リレー状態】",
        f"ch1:暖房={rs(1)}  ch2:循環扇={rs(2)}  ch3:CO2={rs(3)}  ch4:灌水={rs(4)}",
        f"ch5:南窓A={rs(5)}  ch6:南窓B={rs(6)}  ch7:北窓A={rs(7)}  ch8:北窓B={rs(8)}",
        "",
        "=== 1時間気象予報 ===",
        (
            f"気温: {f['temp_c']}℃ / 降水確率: {f['rain_probability_pct']}% / "
            f"風速: {f['wind_speed_ms']}m/s"
        ),
        "",
        "向こう1時間のハウス管理計画を立て、必要なリレー操作をset_relayで実行してください。",
    ]
    return "\n".join(lines)


def sensors_to_json(scenario: dict) -> str:
    """get_sensors ツールコールへのモック応答を生成する。"""
    s = scenario["sensors"]
    return json.dumps(
        {
            "inside": {
                "temperature": s["inside_temp_c"],
                "humidity": s["inside_humidity_pct"],
                "co2_ppm": s["co2_ppm"],
            },
            "outside": {
                "temperature": s["outside_temp_c"],
                "humidity": s["outside_humidity_pct"],
                "wind_speed_ms": s["wind_speed_ms"],
                "wind_direction": s["wind_direction"],
                "rain": s["rain"],
            },
            "relay_states": s["relay_states"],
        },
        ensure_ascii=False,
    )


# ── 1シナリオの実行 ───────────────────────────────────────────────────
def run_scenario(
    client: OpenAI,
    scenario: dict,
    system_prompt: str,
    model: str,
    max_rounds: int = 6,
    verbose: bool = False,
) -> dict:
    """1シナリオを実行し、set_relay 呼び出し一覧と最終テキストを返す。

    Returns:
        {
            "scenario_id": str,
            "relay_calls": [{"channel": int, "value": int, "duration_sec": int}, ...],
            "response_text": str,
            "rounds": int,
            "error": str | None,
        }
    """
    sid = scenario["id"]
    relay_calls: list[dict] = []
    final_text = ""
    error: str | None = None

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": format_sensor_message(scenario)},
    ]

    try:
        for round_num in range(max_rounds):
            if verbose:
                print(f"  [{sid}] round {round_num + 1}", file=sys.stderr)

            response = client.chat.completions.create(
                model=model,
                max_tokens=1024,
                tools=BENCH_TOOLS,
                messages=messages,
            )
            choice = response.choices[0]
            msg = choice.message
            has_tool_calls = bool(msg.tool_calls)

            messages.append(msg.model_dump(exclude_unset=False))

            if not has_tool_calls:
                final_text = msg.content or ""
                break

            # ツール処理
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if tool_name == "get_sensors":
                    tool_result = sensors_to_json(scenario)
                elif tool_name == "set_relay":
                    ch = int(args.get("channel", 0))
                    val = int(args.get("value", 0))
                    dur = int(args.get("duration_sec", 0))
                    if verbose:
                        print(
                            f"    set_relay ch{ch}={'ON' if val else 'OFF'}"
                            f"{f' ({dur}s)' if dur else ''}",
                            file=sys.stderr,
                        )
                    relay_calls.append({"channel": ch, "value": val, "duration_sec": dur})
                    tool_result = json.dumps({"ok": True, "channel": ch, "value": val})
                else:
                    tool_result = json.dumps({"error": f"unknown tool: {tool_name}"})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )

            if choice.finish_reason == "stop":
                final_text = msg.content or ""
                break

    except Exception as exc:
        error = str(exc)
        if verbose:
            print(f"  [{sid}] ERROR: {exc}", file=sys.stderr)

    return {
        "scenario_id": sid,
        "relay_calls": relay_calls,
        "response_text": final_text,
        "rounds": len([m for m in messages if m.get("role") == "assistant"]),
        "error": error,
    }


# ── メイン ───────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="温室制御LLMベンチマークランナー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:3001/v1/",
        help="OpenAI互換APIのベースURL (デフォルト: http://localhost:3001/v1/)",
    )
    parser.add_argument(
        "--model",
        default="nullclaw-local",
        help="使用モデル名 (デフォルト: nullclaw-local)",
    )
    parser.add_argument(
        "--api-key",
        default="bench-key",
        help="APIキー (NullClawは不要。Claudeの場合は必須)",
    )
    parser.add_argument(
        "--system-prompt",
        default=str(Path(__file__).parent.parent.parent / "config" / "system_prompt.txt"),
        help="system_prompt.txt のパス",
    )
    parser.add_argument(
        "--scenarios",
        default="",
        help="実行するシナリオID (カンマ区切り例: S01,S03)。省略時は全件",
    )
    parser.add_argument(
        "--output",
        default="",
        help="結果JSONの出力先ファイルパス",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="詳細ログを stderr に出力",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="1リクエストのタイムアウト秒数 (デフォルト: 60)",
    )
    args = parser.parse_args()

    # システムプロンプト読み込み
    prompt_path = Path(args.system_prompt)
    if not prompt_path.exists():
        print(f"ERROR: system_prompt not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # シナリオ読み込み
    scenarios_dir = Path(__file__).parent / "scenarios"
    scenario_files = sorted(scenarios_dir.glob("S*.json"))
    if not scenario_files:
        print(f"ERROR: シナリオファイルが見つかりません: {scenarios_dir}", file=sys.stderr)
        sys.exit(1)

    scenarios: list[dict] = []
    filter_ids = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    for f in scenario_files:
        sc = json.loads(f.read_text(encoding="utf-8"))
        if not filter_ids or sc["id"] in filter_ids:
            scenarios.append(sc)

    if not scenarios:
        print(f"ERROR: 指定されたシナリオが見つかりません: {args.scenarios}", file=sys.stderr)
        sys.exit(1)

    # LLMクライアント初期化
    client = OpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )

    print(f"\n温室制御LLMベンチマーク開始", file=sys.stderr)
    print(f"  モデル   : {args.model}", file=sys.stderr)
    print(f"  ベースURL: {args.base_url}", file=sys.stderr)
    print(f"  シナリオ : {len(scenarios)}件", file=sys.stderr)
    print(f"  プロンプト: {prompt_path}", file=sys.stderr)
    print("", file=sys.stderr)

    # 実行
    run_results: list[dict] = []
    eval_results: list[EvalResult] = []

    for sc in scenarios:
        print(f"  実行中: {sc['id']} - {sc['name']}", file=sys.stderr)
        t0 = time.time()
        run_result = run_scenario(
            client=client,
            scenario=sc,
            system_prompt=system_prompt,
            model=args.model,
            verbose=args.verbose,
        )
        elapsed = time.time() - t0

        ev = evaluate(
            scenario=sc,
            relay_calls=run_result["relay_calls"],
            response_text=run_result["response_text"],
        )

        run_result["elapsed_sec"] = round(elapsed, 2)
        run_results.append(run_result)
        eval_results.append(ev)

        status = f"{ev.grade:<8} {ev.score}/{ev.max_score}点"
        if ev.violations:
            status += f"  違反: {'; '.join(ev.violations)}"
        print(f"    → {status}  ({elapsed:.1f}s)", file=sys.stderr)

    # 結果テーブル表示
    print(format_result_table(eval_results))

    # JSON出力
    output_data = {
        "meta": {
            "run_at": datetime.now().isoformat(),
            "model": args.model,
            "base_url": args.base_url,
            "system_prompt": str(prompt_path),
            "scenario_count": len(scenarios),
        },
        "summary": {
            "total_score": sum(r.score for r in eval_results),
            "max_score": sum(r.max_score for r in eval_results),
            "pass_count": sum(1 for r in eval_results if r.passed),
            "fail_count": sum(1 for r in eval_results if not r.passed),
            "pct": round(
                sum(r.score for r in eval_results)
                / sum(r.max_score for r in eval_results)
                * 100,
                1,
            ) if eval_results else 0,
        },
        "results": [
            {
                "scenario_id": ev.scenario_id,
                "score": ev.score,
                "max_score": ev.max_score,
                "grade": ev.grade,
                "matched_criterion": ev.matched_criterion,
                "violations": ev.violations,
                "timeline_linear": ev.timeline_linear,
                "timeline_note": ev.timeline_note,
                "json_valid": ev.json_valid,
                "relay_calls": [
                    {"channel": rc.channel, "value": rc.value, "duration_sec": rc.duration_sec}
                    for rc in ev.relay_calls
                ],
                "elapsed_sec": rr["elapsed_sec"],
                "error": rr.get("error"),
            }
            for ev, rr in zip(eval_results, run_results)
        ],
    }

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"結果を保存: {out_path}", file=sys.stderr)
    else:
        print(json.dumps(output_data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
