"""test_rule_manager.py — rule_manager.py のユニットテスト (7件)

テストケース:
    1. load/save_candidates: YAML読み書きのラウンドトリップ
    2. expire_stale_candidates: 30日超過→expired
    3. expire_stale_candidates: 29日→まだpending
    4. approve_candidate: status+approved_at設定
    5. reject_candidate: status+rejected_at設定
    6. promote_to_rules: approved→rules.yaml custom_rules追加
    7. cleanup_expired: expired候補削除
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
import pytest

from agriha.control.rule_manager import (
    approve_candidate,
    cleanup_expired,
    expire_stale_candidates,
    load_candidates,
    promote_to_rules,
    reject_candidate,
    save_candidates,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_JST = timezone(timedelta(hours=9))
_NOW = datetime(2026, 3, 5, 10, 0, 0, tzinfo=_JST)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _make_candidate(
    cid: str = "rc_001",
    status: str = "pending",
    days_old: int = 0,
) -> dict[str, Any]:
    """テスト用候補辞書を生成する。"""
    created_at = _NOW - timedelta(days=days_old)
    expires_at = created_at + timedelta(days=30)
    return {
        "id": cid,
        "pattern": f"test_pattern_{cid}",
        "frequency": 10,
        "window_days": 30,
        "first_seen": created_at.isoformat(),
        "last_seen": created_at.isoformat(),
        "typical_actions": [
            {"relay_ch": 5, "value": 0, "reason": "テスト"}
        ],
        "confidence": 0.80,
        "status": status,
        "created_at": created_at.isoformat(),
        "approved_at": None,
        "rejected_at": None,
        "expires_at": expires_at.isoformat(),
        "reflection_item_id": None,
        "farmer_answer": None,
    }


def _make_rules_yaml(tmp_path: Path, custom_rules: list | None = None) -> Path:
    """テスト用 rules.yaml を作成する。"""
    p = tmp_path / "rules.yaml"
    data: dict[str, Any] = {
        "temperature": {"target_day": 26.0, "window_channels": [5, 6, 7, 8]},
        "custom_rules": custom_rules or [],
    }
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestLoadSave:
    """テスト 1: load/save ラウンドトリップ。"""

    def test_01_round_trip(self, tmp_path: Path) -> None:
        """1. save→load でデータが一致する。"""
        candidates_path = tmp_path / "rule_candidates.yaml"
        candidates = [_make_candidate("rc_001"), _make_candidate("rc_002")]

        save_candidates(candidates, path=candidates_path)
        loaded = load_candidates(path=candidates_path)

        assert len(loaded) == 2
        assert loaded[0]["id"] == "rc_001"
        assert loaded[1]["id"] == "rc_002"
        assert loaded[0]["status"] == "pending"

    def test_01b_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """1b. ファイルなし → 空リストを返す。"""
        result = load_candidates(path=tmp_path / "nonexistent.yaml")
        assert result == []


class TestExpire:
    """テスト 2, 3: 腐敗チェック。"""

    def test_02_expired_after_30_days(self) -> None:
        """2. 30日超過(31日前作成) → expired に変更。"""
        candidates = [_make_candidate("rc_001", days_old=31)]
        result = expire_stale_candidates(candidates, now=_NOW)

        assert result[0]["status"] == "expired"

    def test_03_not_expired_before_30_days(self) -> None:
        """3. 29日前作成 → まだ pending のまま。"""
        candidates = [_make_candidate("rc_001", days_old=29)]
        result = expire_stale_candidates(candidates, now=_NOW)

        assert result[0]["status"] == "pending"

    def test_03b_already_approved_not_touched(self) -> None:
        """3b. approved候補は腐敗チェックの対象外。"""
        candidates = [_make_candidate("rc_001", status="approved", days_old=60)]
        result = expire_stale_candidates(candidates, now=_NOW)

        assert result[0]["status"] == "approved"


class TestApproveReject:
    """テスト 4, 5: 承認/却下。"""

    def test_04_approve_sets_status_and_timestamp(self) -> None:
        """4. 承認 → status=approved, approved_atが設定される。"""
        candidates = [_make_candidate("rc_001")]
        success = approve_candidate("rc_001", candidates, now=_NOW)

        assert success is True
        assert candidates[0]["status"] == "approved"
        assert candidates[0]["approved_at"] == _NOW.isoformat()

    def test_04b_approve_nonexistent_returns_false(self) -> None:
        """4b. 存在しないIDの承認 → False。"""
        candidates = [_make_candidate("rc_001")]
        result = approve_candidate("rc_999", candidates, now=_NOW)
        assert result is False

    def test_05_reject_sets_status_and_timestamp(self) -> None:
        """5. 却下 → status=rejected, rejected_atが設定される。"""
        candidates = [_make_candidate("rc_001")]
        success = reject_candidate("rc_001", candidates, now=_NOW)

        assert success is True
        assert candidates[0]["status"] == "rejected"
        assert candidates[0]["rejected_at"] == _NOW.isoformat()

    def test_05b_reject_nonexistent_returns_false(self) -> None:
        """5b. 存在しないIDの却下 → False。"""
        candidates = [_make_candidate("rc_001")]
        result = reject_candidate("rc_999", candidates, now=_NOW)
        assert result is False


class TestPromote:
    """テスト 6: rules.yaml 昇格。"""

    def test_06_approved_candidate_promoted_to_custom_rules(self, tmp_path: Path) -> None:
        """6. approved候補 → rules.yaml custom_rulesに追加される。"""
        rules_path = _make_rules_yaml(tmp_path)
        candidates = [_make_candidate("rc_001", status="approved")]
        candidates[0]["approved_at"] = _NOW.isoformat()

        promoted = promote_to_rules(candidates, rules_path=rules_path)

        assert "rc_001" in promoted

        # rules.yaml に追加されていること
        data = yaml.safe_load(rules_path.read_text())
        custom_rules = data["custom_rules"]
        assert len(custom_rules) == 1
        assert custom_rules[0]["source_candidate"] == "rc_001"
        assert custom_rules[0]["id"] == "cr_rc_001"
        assert custom_rules[0]["actions"] == candidates[0]["typical_actions"]

    def test_06b_pending_candidate_not_promoted(self, tmp_path: Path) -> None:
        """6b. pending候補は昇格されない。"""
        rules_path = _make_rules_yaml(tmp_path)
        candidates = [_make_candidate("rc_001", status="pending")]

        promoted = promote_to_rules(candidates, rules_path=rules_path)

        assert promoted == []
        data = yaml.safe_load(rules_path.read_text())
        assert data["custom_rules"] == []

    def test_06c_already_promoted_is_skipped(self, tmp_path: Path) -> None:
        """6c. 既に昇格済みの候補は二重昇格されない。"""
        rules_path = _make_rules_yaml(
            tmp_path,
            custom_rules=[{"id": "cr_rc_001", "source_candidate": "rc_001"}],
        )
        candidates = [_make_candidate("rc_001", status="approved")]
        candidates[0]["approved_at"] = _NOW.isoformat()

        promoted = promote_to_rules(candidates, rules_path=rules_path)

        assert promoted == []
        data = yaml.safe_load(rules_path.read_text())
        assert len(data["custom_rules"]) == 1  # 増えていない


class TestCleanup:
    """テスト 7: expired 候補削除。"""

    def test_07_cleanup_removes_expired(self) -> None:
        """7. expired候補がリストから削除される。"""
        candidates = [
            _make_candidate("rc_001", status="pending"),
            _make_candidate("rc_002", status="expired"),
            _make_candidate("rc_003", status="approved"),
        ]
        result = cleanup_expired(candidates)

        assert len(result) == 2
        ids = [c["id"] for c in result]
        assert "rc_001" in ids
        assert "rc_003" in ids
        assert "rc_002" not in ids

    def test_07b_no_expired_returns_all(self) -> None:
        """7b. expiredなし → 全件そのまま返す。"""
        candidates = [_make_candidate("rc_001"), _make_candidate("rc_002")]
        result = cleanup_expired(candidates)
        assert len(result) == 2
