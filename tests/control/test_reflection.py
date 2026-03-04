"""tests/control/test_reflection.py — reflection.py テスト

テスト対象: load_config / select_candidates / generate_question /
            create_reflection_memos / process_answer / check_nag_status
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from agriha.control.reflection import (
    check_nag_status,
    create_reflection_memos,
    generate_question,
    load_config,
    process_answer,
    run_reflection,
    select_candidates,
)

_JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _make_candidate(
    cid: str,
    frequency: int = 5,
    status: str = "pending",
    query: str = "spring_morning_warm_clear",
) -> dict[str, Any]:
    return {
        "id": cid,
        "query": query,
        "pattern": f"パターン_{cid}",
        "frequency": frequency,
        "confidence": 0.9,
        "status": status,
        "typical_actions": [{"relay_ch": 5, "value": 1}],
        "created_at": "2026-03-01T00:00:00+09:00",
        "expires_at": "2026-04-01T00:00:00+09:00",
    }


def _make_db_with_table(db_path: Path) -> sqlite3.Connection:
    """reflection_memo テーブル付きDB作成。"""
    db = sqlite3.connect(str(db_path))
    db.execute("""CREATE TABLE IF NOT EXISTS reflection_memo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        source_candidate_id TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        context TEXT NOT NULL,
        answer TEXT,
        answered_at TEXT,
        promoted_to_rule INTEGER DEFAULT 0,
        expired INTEGER DEFAULT 0
    )""")
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Test 1: load_config — YAML読み込み
# ---------------------------------------------------------------------------


def test_load_config_from_yaml(tmp_path: Path) -> None:
    """YAML ファイルから設定を正しく読み込む。"""
    cfg_file = tmp_path / "reflection.yaml"
    cfg_file.write_text(
        yaml.dump({
            "reflection": {
                "frequency": "monthly",
                "max_items": 5,
                "nag_after_weeks": 3,
            }
        }),
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg["reflection"]["frequency"] == "monthly"
    assert cfg["reflection"]["max_items"] == 5
    assert cfg["reflection"]["nag_after_weeks"] == 3
    # デフォルト値で補完されること
    assert cfg["reflection"]["downgrade_after_weeks"] == 4


def test_load_config_file_not_found() -> None:
    """ファイルなし時はデフォルト値を返す。"""
    cfg = load_config("/nonexistent/path/reflection.yaml")
    assert cfg["reflection"]["frequency"] == "weekly"
    assert cfg["reflection"]["max_items"] == 3


# ---------------------------------------------------------------------------
# Test 2-3: select_candidates — frequency降順+未質問優先+max_items
# ---------------------------------------------------------------------------


def test_select_candidates_frequency_order(tmp_path: Path) -> None:
    """frequency降順で選定し、max_itemsで切り詰める。"""
    candidates = [
        _make_candidate("rc_001", frequency=3),
        _make_candidate("rc_002", frequency=10),
        _make_candidate("rc_003", frequency=7),
        _make_candidate("rc_004", frequency=15),
    ]
    candidates_yaml = tmp_path / "rule_candidates.yaml"
    candidates_yaml.write_text(
        yaml.dump({"candidates": candidates}), encoding="utf-8"
    )
    db = _make_db_with_table(tmp_path / "control_log.db")

    result = select_candidates(
        max_items=2,
        candidates_path=str(candidates_yaml),
        db=db,
    )
    db.close()

    assert len(result) == 2
    assert result[0]["id"] == "rc_004"  # frequency=15
    assert result[1]["id"] == "rc_002"  # frequency=10


def test_select_candidates_excludes_already_answered(tmp_path: Path) -> None:
    """reflection_memoに登録済みの候補は除外される。"""
    candidates = [
        _make_candidate("rc_001", frequency=10),
        _make_candidate("rc_002", frequency=8),
    ]
    candidates_yaml = tmp_path / "rule_candidates.yaml"
    candidates_yaml.write_text(
        yaml.dump({"candidates": candidates}), encoding="utf-8"
    )
    db = _make_db_with_table(tmp_path / "control_log.db")
    # rc_001 を既回答としてINSERT
    db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
        "VALUES (?, ?, ?, ?, ?)",
        ("2026-03-01T07:00:00", "rc_001", "q", "[]", "{}"),
    )
    db.commit()

    result = select_candidates(
        max_items=3,
        candidates_path=str(candidates_yaml),
        db=db,
    )
    db.close()

    assert len(result) == 1
    assert result[0]["id"] == "rc_002"


def test_select_candidates_all_answered_returns_empty(tmp_path: Path) -> None:
    """全候補が回答済みなら空リストを返す。"""
    candidates = [_make_candidate("rc_001"), _make_candidate("rc_002")]
    candidates_yaml = tmp_path / "rule_candidates.yaml"
    candidates_yaml.write_text(
        yaml.dump({"candidates": candidates}), encoding="utf-8"
    )
    db = _make_db_with_table(tmp_path / "control_log.db")
    for cid in ["rc_001", "rc_002"]:
        db.execute(
            "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-03-01T07:00:00", cid, "q", "[]", "{}"),
        )
    db.commit()

    result = select_candidates(
        candidates_path=str(candidates_yaml),
        db=db,
    )
    db.close()

    assert result == []


# ---------------------------------------------------------------------------
# Test 4: generate_question — A/B/C選択肢構造
# ---------------------------------------------------------------------------


def test_generate_question_abc_structure() -> None:
    """A/B/C選択肢が生成される。"""
    candidate = _make_candidate("rc_001", frequency=12)
    result = generate_question(candidate)

    assert "question" in result
    assert "options" in result
    options = result["options"]
    assert len(options) == 3
    labels = [o["label"] for o in options]
    assert labels == ["A", "B", "C"]
    # question に pattern と frequency が含まれる
    assert "パターン_rc_001" in result["question"]
    assert "12" in result["question"]
    # 各optionに "text" キーがある
    for opt in options:
        assert "text" in opt
        assert len(opt["text"]) > 0


def test_generate_question_no_actions() -> None:
    """典型アクションがない候補でもクラッシュしない。"""
    candidate = {
        "id": "rc_001",
        "query": "winter_night_cold_unknown",
        "frequency": 5,
        "status": "pending",
    }
    result = generate_question(candidate)
    assert len(result["options"]) == 3
    assert "アクションなし" in result["options"][0]["text"]


# ---------------------------------------------------------------------------
# Test 5: create_reflection_memos — DB INSERT確認
# ---------------------------------------------------------------------------


def test_create_reflection_memos_db_insert(tmp_path: Path) -> None:
    """DBにreflection_memo行が挿入される。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 3, 7, 0, tzinfo=_JST)
    candidates = [_make_candidate("rc_001", frequency=8)]

    memos = create_reflection_memos(candidates, db=db, now=now)
    db.close()

    assert len(memos) == 1
    assert memos[0]["candidate_id"] == "rc_001"
    assert memos[0]["memo_id"] is not None
    assert len(memos[0]["options"]) == 3
    assert memos[0]["context"]["frequency"] == 8


def test_create_reflection_memos_multiple(tmp_path: Path) -> None:
    """複数候補を渡すと複数行挿入される。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 3, 7, 0, tzinfo=_JST)
    candidates = [
        _make_candidate("rc_001"),
        _make_candidate("rc_002"),
    ]

    memos = create_reflection_memos(candidates, db=db, now=now)

    rows = db.execute("SELECT id FROM reflection_memo").fetchall()
    db.close()

    assert len(memos) == 2
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Test 6-7: process_answer — A→approve+promote / B→reject
# ---------------------------------------------------------------------------


def test_process_answer_A_approve_promote(tmp_path: Path) -> None:
    """回答AでDB更新 + rule_manager.approve_candidate が呼ばれる。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 3, 7, 0, tzinfo=_JST)
    # memoを挿入
    cursor = db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
        "VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(), "rc_001", "テスト質問", "[]", "{}"),
    )
    db.commit()
    memo_id = cursor.lastrowid

    # candidates_yaml 準備
    candidates_yaml = tmp_path / "rule_candidates.yaml"
    candidates = [_make_candidate("rc_001")]
    candidates_yaml.write_text(yaml.dump({"candidates": candidates}), encoding="utf-8")

    # rules.yaml 準備
    rules_yaml = tmp_path / "rules.yaml"
    rules_yaml.write_text(yaml.dump({}), encoding="utf-8")

    result = process_answer(
        memo_id=memo_id,
        answer="A",
        db=db,
        candidates_path=str(candidates_yaml),
        rules_path=str(rules_yaml),
        now=now,
    )
    db.close()

    assert result["status"] == "approved"
    assert result["candidate_id"] == "rc_001"
    # candidates.yamlのステータスが approved になる
    updated_candidates = yaml.safe_load(
        candidates_yaml.read_text(encoding="utf-8")
    )["candidates"]
    assert updated_candidates[0]["status"] == "approved"
    # rules.yaml に custom_rules が追加される
    rules = yaml.safe_load(rules_yaml.read_text(encoding="utf-8"))
    assert len(rules.get("custom_rules", [])) >= 1


def test_process_answer_B_reject(tmp_path: Path) -> None:
    """回答Bで却下。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 3, 7, 0, tzinfo=_JST)
    cursor = db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
        "VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(), "rc_002", "テスト質問", "[]", "{}"),
    )
    db.commit()
    memo_id = cursor.lastrowid

    candidates_yaml = tmp_path / "rule_candidates.yaml"
    candidates = [_make_candidate("rc_002")]
    candidates_yaml.write_text(yaml.dump({"candidates": candidates}), encoding="utf-8")

    result = process_answer(
        memo_id=memo_id,
        answer="B",
        db=db,
        candidates_path=str(candidates_yaml),
        now=now,
    )
    db.close()

    assert result["status"] == "rejected"
    assert result["candidate_id"] == "rc_002"
    assert result["promoted"] == []
    updated = yaml.safe_load(candidates_yaml.read_text(encoding="utf-8"))["candidates"]
    assert updated[0]["status"] == "rejected"


def test_process_answer_not_found(tmp_path: Path) -> None:
    """存在しないmemo_idはnot_foundを返す。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    result = process_answer(memo_id=999, answer="A", db=db)
    db.close()
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Test 8-9: check_nag_status — 無反応チェック
# ---------------------------------------------------------------------------


def test_check_nag_status_ok_when_recently_answered(tmp_path: Path) -> None:
    """最近回答があれば ok を返す。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 10, 7, 0, tzinfo=_JST)
    # 3日前に回答
    answered_at = (now - timedelta(days=3)).isoformat()
    db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context, answer) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (answered_at, "rc_001", "q", "[]", "{}", "A"),
    )
    db.commit()

    config = {"reflection": {"nag_after_weeks": 2, "downgrade_after_weeks": 4}}
    result = check_nag_status(config, db=db, now=now)
    db.close()
    assert result == "ok"


def test_check_nag_status_2weeks_no_answer(tmp_path: Path) -> None:
    """2週以上無回答でnagを返す。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 20, 7, 0, tzinfo=_JST)
    # 15日前（2週超）に作成、未回答
    created_at = (now - timedelta(days=15)).isoformat()
    db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
        "VALUES (?, ?, ?, ?, ?)",
        (created_at, "rc_001", "q", "[]", "{}"),
    )
    db.commit()

    config = {"reflection": {"nag_after_weeks": 2, "downgrade_after_weeks": 4}}
    result = check_nag_status(config, db=db, now=now)
    db.close()
    assert result == "nag"


def test_check_nag_status_4weeks_no_answer(tmp_path: Path) -> None:
    """4週以上無回答でdowngradeを返す。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 30, 7, 0, tzinfo=_JST)
    # 29日前（4週超）に作成、未回答
    created_at = (now - timedelta(days=29)).isoformat()
    db.execute(
        "INSERT INTO reflection_memo (created_at, source_candidate_id, question, options, context) "
        "VALUES (?, ?, ?, ?, ?)",
        (created_at, "rc_001", "q", "[]", "{}"),
    )
    db.commit()

    config = {"reflection": {"nag_after_weeks": 2, "downgrade_after_weeks": 4}}
    result = check_nag_status(config, db=db, now=now)
    db.close()
    assert result == "downgrade"


def test_check_nag_status_no_memos(tmp_path: Path) -> None:
    """メモが1件もなければ ok を返す。"""
    db = _make_db_with_table(tmp_path / "control_log.db")
    now = datetime(2026, 3, 10, 7, 0, tzinfo=_JST)
    config = {"reflection": {"nag_after_weeks": 2, "downgrade_after_weeks": 4}}
    result = check_nag_status(config, db=db, now=now)
    db.close()
    assert result == "ok"


# ---------------------------------------------------------------------------
# Test 10-14: run_reflection() E2E 統合テスト（LINE送信モック）
# ---------------------------------------------------------------------------


def _setup_candidates_yaml(tmp_path: Path, cids: list[str]) -> Path:
    """candidates.yaml を tmp_path に作成してパスを返す。"""
    candidates = [_make_candidate(cid) for cid in cids]
    p = tmp_path / "rule_candidates.yaml"
    p.write_text(yaml.dump({"candidates": candidates}), encoding="utf-8")
    return p


def test_run_reflection_send_reflection_called(tmp_path: Path) -> None:
    """run_reflection() が memos で send_reflection を呼び出す。"""
    candidates_yaml = _setup_candidates_yaml(tmp_path, ["rc_001"])
    db_path = tmp_path / "control_log.db"
    _make_db_with_table(db_path).close()

    with patch("agriha.control.reflection.send_reflection") as mock_send, \
         patch("agriha.control.reflection.send_nag_message") as mock_nag, \
         patch("agriha.control.reflection.send_downgrade_notice") as mock_dg, \
         patch.dict(os.environ, {"LINE_FARMER_USER_ID": "U123"}):
        memos = run_reflection(
            candidates_path=str(candidates_yaml),
            db_path=str(db_path),
        )

    assert len(memos) == 1
    mock_send.assert_called_once_with(memos)
    mock_nag.assert_not_called()
    mock_dg.assert_not_called()


def test_run_reflection_skip_if_no_user_id(tmp_path: Path) -> None:
    """LINE_FARMER_USER_ID 未設定時は send_reflection を呼び出さない。"""
    candidates_yaml = _setup_candidates_yaml(tmp_path, ["rc_001"])
    db_path = tmp_path / "control_log.db"
    _make_db_with_table(db_path).close()

    env = {k: v for k, v in os.environ.items() if k != "LINE_FARMER_USER_ID"}
    with patch("agriha.control.reflection.send_reflection") as mock_send, \
         patch.dict(os.environ, env, clear=True):
        memos = run_reflection(
            candidates_path=str(candidates_yaml),
            db_path=str(db_path),
        )

    assert len(memos) == 1
    mock_send.assert_not_called()


def test_run_reflection_nag_sends_nag_message(tmp_path: Path) -> None:
    """check_nag_status が 'nag' を返した場合、send_nag_message が呼ばれる。"""
    candidates_yaml = _setup_candidates_yaml(tmp_path, ["rc_001"])
    db_path = tmp_path / "control_log.db"
    _make_db_with_table(db_path).close()

    with patch("agriha.control.reflection.check_nag_status", return_value="nag"), \
         patch("agriha.control.reflection.send_reflection"), \
         patch("agriha.control.reflection.send_nag_message") as mock_nag, \
         patch("agriha.control.reflection.send_downgrade_notice") as mock_dg, \
         patch.dict(os.environ, {"LINE_FARMER_USER_ID": "U123"}):
        run_reflection(
            candidates_path=str(candidates_yaml),
            db_path=str(db_path),
        )

    mock_nag.assert_called_once()
    mock_dg.assert_not_called()


def test_run_reflection_downgrade_sends_downgrade_notice(tmp_path: Path) -> None:
    """check_nag_status が 'downgrade' を返した場合、send_downgrade_notice が呼ばれる。"""
    candidates_yaml = _setup_candidates_yaml(tmp_path, ["rc_001"])
    db_path = tmp_path / "control_log.db"
    _make_db_with_table(db_path).close()

    with patch("agriha.control.reflection.check_nag_status", return_value="downgrade"), \
         patch("agriha.control.reflection.send_reflection"), \
         patch("agriha.control.reflection.send_nag_message") as mock_nag, \
         patch("agriha.control.reflection.send_downgrade_notice") as mock_dg, \
         patch.dict(os.environ, {"LINE_FARMER_USER_ID": "U123"}):
        run_reflection(
            candidates_path=str(candidates_yaml),
            db_path=str(db_path),
        )

    mock_dg.assert_called_once()
    mock_nag.assert_not_called()


def test_run_reflection_no_candidates_skips_send_reflection(tmp_path: Path) -> None:
    """候補なしの場合、send_reflection を呼び出さない。"""
    # 全候補が非pendingなら select_candidates は空を返す
    candidates = [_make_candidate("rc_001", status="approved")]
    p = tmp_path / "rule_candidates.yaml"
    p.write_text(yaml.dump({"candidates": candidates}), encoding="utf-8")
    db_path = tmp_path / "control_log.db"
    _make_db_with_table(db_path).close()

    with patch("agriha.control.reflection.send_reflection") as mock_send, \
         patch.dict(os.environ, {"LINE_FARMER_USER_ID": "U123"}):
        memos = run_reflection(
            candidates_path=str(p),
            db_path=str(db_path),
        )

    assert memos == []
    mock_send.assert_not_called()
