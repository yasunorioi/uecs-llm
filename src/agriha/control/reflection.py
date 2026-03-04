#!/usr/bin/env python3
"""reflection.py — 反省会モード: rule_candidates.yaml → 質問生成 → reflection_memo → 回答処理

農家への週次フィードバックループ。rule_candidatesのpending候補をLINEで農家に提示し、
承認/却下を受け付ける。回答をrule_manager経由でrules.yamlに反映する。

設計書: docs/v2_three_layer_design.md §7
LINE送信: subtask_738（reflection_sender.py）が担当。
本モジュールはmemo作成+送信用データ返却まで。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from agriha.control import rule_manager

logger = logging.getLogger("reflection")

_JST = timezone(timedelta(hours=9))

REFLECTION_CONFIG_PATH = os.environ.get(
    "REFLECTION_CONFIG_PATH", "/etc/agriha/reflection.yaml"
)
DB_PATH = os.environ.get("CONTROL_LOG_DB", "/var/lib/agriha/control_log.db")


# ---------------------------------------------------------------------------
# 設定読み込み
# ---------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """reflection.yaml を読み込む。ファイルなしはデフォルト値を返す。"""
    defaults: dict[str, Any] = {
        "reflection": {
            "frequency": "weekly",
            "day_of_week": 1,
            "time": "07:00",
            "max_items": 3,
            "nag_after_weeks": 2,
            "downgrade_after_weeks": 4,
            "expiry_days": 30,
        }
    }
    p = Path(path or REFLECTION_CONFIG_PATH)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        merged: dict[str, Any] = dict(defaults)
        if "reflection" in data:
            merged["reflection"] = {**defaults["reflection"], **data["reflection"]}
        return merged
    except FileNotFoundError:
        logger.warning("reflection.yaml not found: %s (using defaults)", p)
        return defaults
    except Exception as exc:
        logger.error("reflection.yaml 読み込みエラー: %s", exc)
        return defaults


# ---------------------------------------------------------------------------
# 候補選定
# ---------------------------------------------------------------------------


def _get_answered_candidate_ids(db: sqlite3.Connection) -> set[str]:
    """reflection_memo に登録済みの source_candidate_id を返す。"""
    rows = db.execute(
        "SELECT DISTINCT source_candidate_id FROM reflection_memo"
    ).fetchall()
    return {row[0] for row in rows}


def select_candidates(
    max_items: int = 3,
    candidates_path: str | Path | None = None,
    db: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """rule_candidates.yaml から pending 候補を選定する。

    - status=pending かつ reflection_memo に未登録のもの
    - frequency 降順にソート
    - max_items 件を返す

    Args:
        max_items: 最大返却件数。
        candidates_path: rule_candidates.yaml のパス（テスト用DI）。
        db: DB接続（テスト用DI）。
        db_path: DBパス（テスト用DI）。

    Returns:
        選定された候補リスト。
    """
    candidates = rule_manager.load_candidates(candidates_path)
    pending = [c for c in candidates if c.get("status") == "pending"]

    _own_db = db is None
    if _own_db:
        _db_path = Path(db_path or DB_PATH)
        if not _db_path.exists():
            # DB未作成 = 全候補が未質問
            return sorted(
                pending,
                key=lambda c: c.get("frequency", 0),
                reverse=True,
            )[:max_items]
        _db: sqlite3.Connection = sqlite3.connect(str(_db_path))
    else:
        _db = db

    try:
        answered_ids = _get_answered_candidate_ids(_db)
    except Exception:
        answered_ids = set()
    finally:
        if _own_db:
            _db.close()

    unanswered = [c for c in pending if c.get("id") not in answered_ids]
    sorted_candidates = sorted(
        unanswered,
        key=lambda c: c.get("frequency", 0),
        reverse=True,
    )
    return sorted_candidates[:max_items]


# ---------------------------------------------------------------------------
# 質問生成
# ---------------------------------------------------------------------------


def generate_question(candidate: dict[str, Any]) -> dict[str, Any]:
    """候補から A/B/C 選択肢付き質問を生成する。

    Args:
        candidate: rule_candidates.yaml の候補エントリ。

    Returns:
        {"question": str, "options": [{"label": "A"/"B"/"C", "text": str}, ...]}
    """
    pattern = candidate.get("pattern") or candidate.get("query", "(パターン不明)")
    typical_actions = candidate.get("typical_actions") or candidate.get("actions", [])
    freq = candidate.get("frequency", 0)

    if typical_actions:
        if isinstance(typical_actions[0], dict):
            acts = ", ".join(
                f"ch{a.get('relay_ch', '?')}→{'ON' if a.get('value') else 'OFF'}"
                for a in typical_actions[:3]
            )
        else:
            acts = ", ".join(str(a) for a in typical_actions[:3])
    else:
        acts = "（アクションなし）"

    question = (
        f"【反省会】パターン「{pattern}」が過去{freq}回発生し、"
        f"自動判断「{acts}」が実施されました。このルール化を承認しますか？"
    )
    options = [
        {"label": "A", "text": f"承認: 「{acts}」を正しい判断としてルール化する"},
        {"label": "B", "text": "改善: アクションを調整してルール化する"},
        {"label": "C", "text": "却下: このパターンはルール化しない"},
    ]
    return {"question": question, "options": options}


# ---------------------------------------------------------------------------
# reflection_memo 作成
# ---------------------------------------------------------------------------


def create_reflection_memos(
    candidates: list[dict[str, Any]],
    db: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """DBに reflection_memo 行を INSERT して、送信用データのリストを返す。

    Args:
        candidates: select_candidates() が返した候補リスト。
        db: DB接続（テスト用DI）。
        db_path: DBパス（テスト用DI）。
        now: 現在時刻（テスト用DI）。

    Returns:
        送信用データのリスト。各要素:
        {"memo_id": int, "candidate_id": str, "question": str,
         "options": list, "context": dict}
    """
    _now = now if now is not None else datetime.now(_JST)
    _own_db = db is None
    if _own_db:
        _db_path = Path(db_path or DB_PATH)
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        _db: sqlite3.Connection = sqlite3.connect(str(_db_path))
    else:
        _db = db

    try:
        memos: list[dict[str, Any]] = []
        for candidate in candidates:
            q = generate_question(candidate)
            context: dict[str, Any] = {
                "timestamp": _now.isoformat(),
                "pattern": candidate.get("pattern") or candidate.get("query", ""),
                "frequency": candidate.get("frequency", 0),
                "action_taken": str(
                    candidate.get("typical_actions") or candidate.get("actions", [])
                ),
            }
            options_json = json.dumps(q["options"], ensure_ascii=False)
            context_json = json.dumps(context, ensure_ascii=False)
            cursor = _db.execute(
                """INSERT INTO reflection_memo
                   (created_at, source_candidate_id, question, options, context)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    _now.isoformat(),
                    candidate.get("id", ""),
                    q["question"],
                    options_json,
                    context_json,
                ),
            )
            _db.commit()
            memo_id = cursor.lastrowid
            memos.append({
                "memo_id": memo_id,
                "candidate_id": candidate.get("id", ""),
                "question": q["question"],
                "options": q["options"],
                "context": context,
            })
            logger.info(
                "reflection_memo 作成: id=%d candidate_id=%s",
                memo_id,
                candidate.get("id"),
            )
        return memos
    finally:
        if _own_db:
            _db.close()


# ---------------------------------------------------------------------------
# 回答処理
# ---------------------------------------------------------------------------


def process_answer(
    memo_id: int,
    answer: str,
    db: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    candidates_path: str | Path | None = None,
    rules_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """農家の回答を処理する。

    - answer "A": 承認 → approve_candidate + promote_to_rules
    - answer "B" or "C": 却下 → reject_candidate

    Args:
        memo_id: reflection_memo の id。
        answer: "A" / "B" / "C"。
        db: DB接続（テスト用DI）。
        db_path: DBパス（テスト用DI）。
        candidates_path: rule_candidates.yaml のパス（テスト用DI）。
        rules_path: rules.yaml のパス（テスト用DI）。
        now: 現在時刻（テスト用DI）。

    Returns:
        {"status": "approved"|"rejected"|"not_found",
         "candidate_id": str, "promoted": list[str]}
    """
    _now = now if now is not None else datetime.now(_JST)
    _own_db = db is None
    if _own_db:
        _db_path = Path(db_path or DB_PATH)
        _db: sqlite3.Connection = sqlite3.connect(str(_db_path))
    else:
        _db = db

    try:
        row = _db.execute(
            "SELECT source_candidate_id FROM reflection_memo WHERE id = ?",
            (memo_id,),
        ).fetchone()
        if not row:
            logger.warning("reflection_memo id=%d が見つかりません", memo_id)
            return {"status": "not_found", "candidate_id": "", "promoted": []}

        candidate_id = row[0]

        _db.execute(
            "UPDATE reflection_memo SET answer = ?, answered_at = ? WHERE id = ?",
            (answer, _now.isoformat(), memo_id),
        )
        _db.commit()

        candidates = rule_manager.load_candidates(candidates_path)
        promoted: list[str] = []

        if answer == "A":
            ok = rule_manager.approve_candidate(candidate_id, candidates, now=_now)
            if ok:
                promoted = rule_manager.promote_to_rules(candidates, rules_path=rules_path)
                rule_manager.save_candidates(candidates, candidates_path)
                if promoted:
                    _db.execute(
                        "UPDATE reflection_memo SET promoted_to_rule = 1 WHERE id = ?",
                        (memo_id,),
                    )
                    _db.commit()
            logger.info("回答A: 候補 %s 承認+昇格=%s", candidate_id, promoted)
            return {"status": "approved", "candidate_id": candidate_id, "promoted": promoted}
        else:
            ok = rule_manager.reject_candidate(candidate_id, candidates, now=_now)
            if ok:
                rule_manager.save_candidates(candidates, candidates_path)
            logger.info("回答%s: 候補 %s 却下", answer, candidate_id)
            return {"status": "rejected", "candidate_id": candidate_id, "promoted": []}

    finally:
        if _own_db:
            _db.close()


# ---------------------------------------------------------------------------
# 無反応チェック
# ---------------------------------------------------------------------------


def check_nag_status(
    config: dict[str, Any],
    db: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> str:
    """無反応チェック。直近メモへの未回答週数を計算する。

    Args:
        config: load_config() の戻り値。
        db: DB接続（テスト用DI）。
        db_path: DBパス（テスト用DI）。
        now: 現在時刻（テスト用DI）。

    Returns:
        "ok": 正常
        "nag": nag_after_weeks 超過 → ナッジ必要
        "downgrade": downgrade_after_weeks 超過 → frequency を monthly に変更推奨
    """
    _now = now if now is not None else datetime.now(_JST)
    refl_cfg = config.get("reflection", {})
    nag_weeks = int(refl_cfg.get("nag_after_weeks", 2))
    downgrade_weeks = int(refl_cfg.get("downgrade_after_weeks", 4))

    _own_db = db is None
    if _own_db:
        _db_path = Path(db_path or DB_PATH)
        if not _db_path.exists():
            return "ok"
        _db: sqlite3.Connection = sqlite3.connect(str(_db_path))
    else:
        _db = db

    try:
        # 未回答メモを全件取得し、最古の未回答メモからの経過週数で判定
        rows = _db.execute(
            "SELECT created_at FROM reflection_memo "
            "WHERE answer IS NULL ORDER BY created_at ASC",
        ).fetchall()

        if not rows:
            return "ok"

        try:
            oldest_ts = rows[0][0]
            oldest_dt = datetime.fromisoformat(oldest_ts)
            if oldest_dt.tzinfo is None:
                oldest_dt = oldest_dt.replace(tzinfo=_JST)
            _now_aware = _now if _now.tzinfo else _now.replace(tzinfo=_JST)
            weeks_since_last = (
                _now_aware - oldest_dt
            ).total_seconds() / (7 * 86400)
        except ValueError:
            weeks_since_last = downgrade_weeks

        if weeks_since_last >= downgrade_weeks:
            logger.info(
                "無反応 %.1f週 → downgrade (閾値: %d週)",
                weeks_since_last,
                downgrade_weeks,
            )
            return "downgrade"
        elif weeks_since_last >= nag_weeks:
            logger.info(
                "無反応 %.1f週 → nag (閾値: %d週)",
                weeks_since_last,
                nag_weeks,
            )
            return "nag"
        else:
            return "ok"

    finally:
        if _own_db:
            _db.close()


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------


def run_reflection(
    config_path: str | Path | None = None,
    candidates_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """メインエントリポイント（cron 月曜07:00）。

    Returns:
        送信用memoリスト（LINE送信はsubtask_738/reflection_sender.pyが実装）。
    """
    config = load_config(config_path)
    refl_cfg = config.get("reflection", {})
    max_items = int(refl_cfg.get("max_items", 3))

    nag = check_nag_status(config, db_path=db_path)
    if nag == "downgrade":
        logger.warning(
            "反省会無反応が続いています（>=%d週）。"
            "frequency を monthly に変更することを推奨します。",
            refl_cfg.get("downgrade_after_weeks", 4),
        )
    elif nag == "nag":
        logger.info("反省会ナッジ: 未回答週数が閾値超過")

    candidates = select_candidates(
        max_items=max_items,
        candidates_path=candidates_path,
        db_path=db_path,
    )
    if not candidates:
        logger.info("反省会候補なし — スキップ")
        return []

    memos = create_reflection_memos(candidates, db_path=db_path)
    logger.info("反省会メモ作成: %d件", len(memos))
    return memos


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
    result = run_reflection()
    print(f"反省会メモ: {len(result)}件作成")


if __name__ == "__main__":
    main()
