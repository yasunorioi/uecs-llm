#!/usr/bin/env python3
"""蒸留パイプライン: search_log.jsonl → 頻度分析 → ルール候補生成.

週次バッチ(月曜 03:00 cron)として実行。
search_log.jsonlを読み込み、クエリ別に頻度・確度を分析し、
閾値(N≥7, confidence≥0.80)を満たすパターンをrule_candidates.yamlに出力する。

設計書: docs/v2_three_layer_design.md §7
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("distiller")

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SEARCH_LOG_PATH = os.environ.get("SEARCH_LOG_PATH", "/var/lib/agriha/search_log.jsonl")
CANDIDATES_PATH = os.environ.get("CANDIDATES_PATH", "config/rule_candidates.yaml")
MIN_FREQUENCY = 7
MIN_CONFIDENCE = 0.80
WINDOW_DAYS = 30
EXPIRY_DAYS = 30


# ---------------------------------------------------------------------------
# ログ読み込み
# ---------------------------------------------------------------------------

def load_search_log(
    path: str | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """search_log.jsonlから直近window_days分のエントリを読み込む。

    Args:
        path: ファイルパス。Noneの場合はSEARCH_LOG_PATHを使用。
        window_days: 読み込む日数（デフォルト30日）。

    Returns:
        エントリのリスト。ファイルなし・読み込みエラー時は空リスト。
    """
    target = Path(path or SEARCH_LOG_PATH)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    entries: list[dict[str, Any]] = []

    try:
        with open(target, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts.astimezone(timezone.utc) >= cutoff:
                            entries.append(entry)
                    else:
                        entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
    except FileNotFoundError:
        logger.warning("search_log not found: %s", target)
    except OSError as exc:
        logger.warning("search_log read error: %s", exc)

    return entries


# ---------------------------------------------------------------------------
# 頻度分析
# ---------------------------------------------------------------------------

def analyze_frequency(
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """query別にグループ化し、頻度+source統計を算出する。

    Args:
        entries: load_search_log()が返すエントリのリスト。

    Returns:
        {query: {"count": N, "kousatsu_rate": float, "entries": [...]}} の辞書。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        query = entry.get("query", "")
        if not query:
            continue
        groups.setdefault(query, []).append(entry)

    result: dict[str, dict[str, Any]] = {}
    for query, group_entries in groups.items():
        count = len(group_entries)
        kousatsu_count = sum(
            1 for e in group_entries if e.get("skipped_llm") is True
        )
        kousatsu_rate = kousatsu_count / count if count > 0 else 0.0
        result[query] = {
            "count": count,
            "kousatsu_rate": kousatsu_rate,
            "entries": group_entries,
        }
    return result


# ---------------------------------------------------------------------------
# 典型アクション抽出（確度算出）
# ---------------------------------------------------------------------------

def extract_typical_actions(
    query_group: dict[str, Any],
) -> dict[str, Any]:
    """同一queryの判断から確度を算出する。

    search_log.jsonlのskipped_llmフラグからkousatsu_rateを確度として使用する
    （B案簡易版: control_log.dbとのtimestamp結合は将来拡張）。

    Args:
        query_group: analyze_frequency()のvalueエントリ
                     {"count": N, "kousatsu_rate": float, "entries": [...]}.

    Returns:
        {"actions": [], "confidence": float}
    """
    return {
        "actions": [],
        "confidence": round(query_group.get("kousatsu_rate", 0.0), 4),
    }


# ---------------------------------------------------------------------------
# 既存候補読み込み
# ---------------------------------------------------------------------------

def load_existing_candidates(path: str | None = None) -> list[dict[str, Any]]:
    """rule_candidates.yamlから既存候補を読み込む。

    Args:
        path: ファイルパス。Noneの場合はCANDIDATES_PATHを使用。

    Returns:
        候補リスト。ファイルなし時は空リスト。
    """
    target = Path(path or CANDIDATES_PATH)
    try:
        with open(target, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("candidates", [])
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("candidates YAML read error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# 候補ID採番
# ---------------------------------------------------------------------------

def _next_candidate_id(existing_candidates: list[dict[str, Any]]) -> str:
    """既存候補のmax id + 1 を返す。既存なしなら "rc_001" から開始。"""
    if not existing_candidates:
        return "rc_001"
    ids = [
        int(c["id"].replace("rc_", ""))
        for c in existing_candidates
        if c.get("id", "").startswith("rc_")
    ]
    return f"rc_{max(ids) + 1:03d}" if ids else "rc_001"


# ---------------------------------------------------------------------------
# 候補生成
# ---------------------------------------------------------------------------

def generate_candidates(
    analysis: dict[str, dict[str, Any]],
    existing_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """frequency≥MIN_FREQUENCY & confidence≥MIN_CONFIDENCE のパターンを候補化する。

    - 新規: status="pending" で追加
    - 既存(閾値クリア): frequency/confidence を更新
    - 既存(閾値未達または今回分析対象外): 期限チェック後に保持/expired化

    Args:
        analysis: analyze_frequency() の戻り値。
        existing_candidates: 既存の候補リスト（Noneの場合は空）。

    Returns:
        更新済みの候補リスト。
    """
    existing = {c["query"]: c for c in (existing_candidates or [])}
    now = datetime.now(timezone.utc)
    updated: dict[str, dict[str, Any]] = {}

    for query, stats in analysis.items():
        typical = extract_typical_actions(stats)
        frequency = stats["count"]
        confidence = typical["confidence"]

        if frequency < MIN_FREQUENCY or confidence < MIN_CONFIDENCE:
            continue

        if query in existing:
            cand = dict(existing[query])
            cand["frequency"] = frequency
            cand["confidence"] = confidence
            cand["last_updated"] = now.isoformat()
        else:
            cand = {
                "id": _next_candidate_id(list(updated.values()) + list(existing.values())),
                "query": query,
                "frequency": frequency,
                "confidence": confidence,
                "status": "pending",
                "first_seen": now.isoformat(),
                "last_updated": now.isoformat(),
                "expires_at": (now + timedelta(days=EXPIRY_DAYS)).isoformat(),
                "actions": typical["actions"],
            }
        updated[query] = cand

    # 既存候補で今回更新されなかったもの: 期限超過をexpired化
    expiry_cutoff = now - timedelta(days=EXPIRY_DAYS)
    for query, cand in existing.items():
        if query in updated:
            continue
        try:
            last_updated = datetime.fromisoformat(cand.get("last_updated", ""))
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            if last_updated.astimezone(timezone.utc) < expiry_cutoff:
                cand = dict(cand)
                cand["status"] = "expired"
        except (ValueError, KeyError):
            pass
        updated[query] = cand

    return list(updated.values())


# ---------------------------------------------------------------------------
# YAML書き出し
# ---------------------------------------------------------------------------

def save_candidates(
    candidates: list[dict[str, Any]],
    path: str | None = None,
) -> None:
    """rule_candidates.yamlに書き出す。

    Args:
        candidates: 候補リスト。
        path: 出力先パス。Noneの場合はCANDIDATES_PATHを使用。
    """
    target = Path(path or CANDIDATES_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": now_str(),
        "min_frequency": MIN_FREQUENCY,
        "min_confidence": MIN_CONFIDENCE,
        "candidates": candidates,
    }
    with open(target, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    logger.info("candidates YAML written: %s (%d件)", target, len(candidates))


def now_str() -> str:
    """現在時刻をISO文字列で返す（テスト用に分離）。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------

def run_distill(
    search_log_path: str | None = None,
    candidates_path: str | None = None,
) -> dict[str, Any]:
    """蒸留パイプラインのメインエントリポイント（cron実行用）。

    Args:
        search_log_path: search_log.jsonlのパス（Noneで環境変数参照）。
        candidates_path: rule_candidates.yamlのパス（Noneで環境変数参照）。

    Returns:
        {"status": "ok", "total_analyzed": N, "queries_found": M, "total_candidates": K}
    """
    entries = load_search_log(search_log_path)
    logger.info("search_log読込: %d件", len(entries))

    analysis = analyze_frequency(entries)
    logger.info("クエリ種別: %d種", len(analysis))

    existing = load_existing_candidates(candidates_path)
    candidates = generate_candidates(analysis, existing)

    save_candidates(candidates, candidates_path)
    logger.info("蒸留完了: %d件の候補", len(candidates))

    return {
        "status": "ok",
        "total_analyzed": len(entries),
        "queries_found": len(analysis),
        "total_candidates": len(candidates),
    }


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI エントリポイント。cron から呼ばれる。"""
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    result = run_distill()
    print(f"Result: {result}")


if __name__ == "__main__":
    main()
