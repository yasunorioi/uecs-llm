"""rule_manager.py — ルール候補管理 (承認/却下/腐敗/昇格)

農家の怒り駆動開発を支えるルール候補ライフサイクル管理。
- pending: 承認待ち
- approved: 承認済み → rules.yaml custom_rules へ昇格
- rejected: 却下済み
- expired: 30日腐敗で自動消去

設計書: docs/v2_three_layer_design.md §3.5
殿の哲学(Memory MCP): 未承認ルール候補は30日で腐敗削除
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("rule_manager")

# ---------------------------------------------------------------------------
# パス定数
# ---------------------------------------------------------------------------

_JST = timezone(timedelta(hours=9))

CANDIDATES_PATH = os.environ.get(
    "CANDIDATES_PATH", str(Path(__file__).resolve().parent.parent.parent.parent / "config" / "rule_candidates.yaml")
)
RULES_PATH = os.environ.get(
    "RULES_CONFIG_PATH", str(Path(__file__).resolve().parent.parent.parent.parent / "config" / "rules.yaml")
)

EXPIRY_DAYS = 30  # 腐敗日数


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def load_candidates(path: str | Path | None = None) -> list[dict[str, Any]]:
    """rule_candidates.yaml からcandidatesリストを読み込む。

    ファイルなし or パースエラーの場合は空リストを返す。
    """
    p = Path(path or CANDIDATES_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data.get("candidates", [])
    except FileNotFoundError:
        logger.warning("rule_candidates.yaml が見つかりません: %s", p)
        return []
    except Exception as exc:
        logger.error("rule_candidates.yaml 読み込みエラー: %s", exc)
        return []


def save_candidates(
    candidates: list[dict[str, Any]],
    path: str | Path | None = None,
) -> None:
    """candidatesリストを rule_candidates.yaml に書き出す。"""
    p = Path(path or CANDIDATES_PATH)
    data: dict[str, Any] = {
        "version": 1,
        "generated_at": datetime.now(_JST).isoformat(),
        "candidates": candidates,
    }
    p.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("rule_candidates.yaml 書き出し完了: %d件", len(candidates))


# ---------------------------------------------------------------------------
# 腐敗 / 承認 / 却下
# ---------------------------------------------------------------------------


def expire_stale_candidates(
    candidates: list[dict[str, Any]],
    expiry_days: int = EXPIRY_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """pending候補のうち expires_at を超過したものを expired に変更する。

    Args:
        candidates: 候補リスト（インプレース変更）
        expiry_days: 腐敗日数（デフォルト30日）
        now: 現在時刻（テスト用DI。Noneなら datetime.now(_JST)）

    Returns:
        変更後のcandidatesリスト（同一オブジェクト）
    """
    _now = now if now is not None else datetime.now(_JST)
    for c in candidates:
        if c.get("status") != "pending":
            continue
        expires_at_str = c.get("expires_at")
        if not expires_at_str:
            # expires_atが未設定の場合はcreated_at+expiry_daysで計算
            created_at_str = c.get("created_at")
            if not created_at_str:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_str)
                expires_at = created_at + timedelta(days=expiry_days)
            except ValueError:
                continue
        else:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
            except ValueError:
                continue

        if _now >= expires_at:
            c["status"] = "expired"
            logger.info("候補 %s が腐敗: expires_at=%s", c.get("id"), expires_at)

    return candidates


def approve_candidate(
    candidate_id: str,
    candidates: list[dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    """指定IDの候補を承認する。

    Args:
        candidate_id: 承認する候補のID
        candidates: 候補リスト（インプレース変更）
        now: 承認日時（テスト用DI）

    Returns:
        True: 承認成功 / False: 候補が見つからない or 承認不可能なステータス
    """
    _now = now if now is not None else datetime.now(_JST)
    for c in candidates:
        if c.get("id") != candidate_id:
            continue
        if c.get("status") not in ("pending",):
            logger.warning("候補 %s は承認できないステータス: %s", candidate_id, c.get("status"))
            return False
        c["status"] = "approved"
        c["approved_at"] = _now.isoformat()
        logger.info("候補 %s を承認", candidate_id)
        return True
    logger.warning("候補 %s が見つかりません", candidate_id)
    return False


def reject_candidate(
    candidate_id: str,
    candidates: list[dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    """指定IDの候補を却下する。

    Args:
        candidate_id: 却下する候補のID
        candidates: 候補リスト（インプレース変更）
        now: 却下日時（テスト用DI）

    Returns:
        True: 却下成功 / False: 候補が見つからない or 却下不可能なステータス
    """
    _now = now if now is not None else datetime.now(_JST)
    for c in candidates:
        if c.get("id") != candidate_id:
            continue
        if c.get("status") not in ("pending",):
            logger.warning("候補 %s は却下できないステータス: %s", candidate_id, c.get("status"))
            return False
        c["status"] = "rejected"
        c["rejected_at"] = _now.isoformat()
        logger.info("候補 %s を却下", candidate_id)
        return True
    logger.warning("候補 %s が見つかりません", candidate_id)
    return False


# ---------------------------------------------------------------------------
# rules.yaml 昇格
# ---------------------------------------------------------------------------


def promote_to_rules(
    candidates: list[dict[str, Any]],
    rules_path: str | Path | None = None,
) -> list[str]:
    """approved候補を rules.yaml の custom_rules セクションに昇格する。

    すでに昇格済み（rules.yamlに同じsource_candidateが存在）はスキップ。
    昇格成功後、候補のstatusは "approved" のまま（promote済みフラグはrules.yaml側で管理）。

    Args:
        candidates: 候補リスト
        rules_path: rules.yaml のパス（テスト用DI）

    Returns:
        昇格した candidate_id のリスト
    """
    p = Path(rules_path or RULES_PATH)
    try:
        rules_data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        rules_data = {}
    except Exception as exc:
        logger.error("rules.yaml 読み込みエラー: %s", exc)
        return []

    custom_rules: list[dict[str, Any]] = rules_data.get("custom_rules", [])
    existing_sources = {r.get("source_candidate") for r in custom_rules}

    promoted: list[str] = []
    for c in candidates:
        if c.get("status") != "approved":
            continue
        cid = c.get("id")
        if cid in existing_sources:
            logger.info("候補 %s は昇格済みスキップ", cid)
            continue

        new_rule: dict[str, Any] = {
            "id": f"cr_{cid}",
            "pattern": c.get("pattern", ""),
            "source_candidate": cid,
            "approved_at": c.get("approved_at", ""),
            "actions": c.get("typical_actions", []),
            "priority": 10,
        }
        custom_rules.append(new_rule)
        promoted.append(cid)
        logger.info("候補 %s を rules.yaml custom_rules に昇格", cid)

    if promoted:
        rules_data["custom_rules"] = custom_rules
        p.write_text(
            yaml.dump(rules_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("rules.yaml 更新完了: %d件昇格", len(promoted))

    return promoted


# ---------------------------------------------------------------------------
# 後処理: expired 候補削除
# ---------------------------------------------------------------------------


def cleanup_expired(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """expired候補をリストから完全削除して返す。

    Args:
        candidates: 候補リスト

    Returns:
        expired を除いた新しいリスト
    """
    before = len(candidates)
    result = [c for c in candidates if c.get("status") != "expired"]
    removed = before - len(result)
    if removed:
        logger.info("expired候補 %d件を削除", removed)
    return result
