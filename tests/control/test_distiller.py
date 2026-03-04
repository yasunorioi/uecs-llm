"""tests/control/test_distiller.py — distiller.py テスト (cmd_314)

テスト方針:
  - ファイルI/O は tmp_path フィクスチャで隔離
  - 外部依存なし（純粋なロジックテスト）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from agriha.control.distiller import (
    EXPIRY_DAYS,
    MIN_CONFIDENCE,
    MIN_FREQUENCY,
    _next_candidate_id,
    analyze_frequency,
    extract_typical_actions,
    generate_candidates,
    load_existing_candidates,
    load_search_log,
    run_distill,
    save_candidates,
)

_UTC = timezone.utc


def _make_entry(
    query: str = "winter_night_cold_clear",
    hours_ago: float = 1.0,
    skipped_llm: bool = True,
) -> dict[str, Any]:
    ts = (datetime.now(_UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "timestamp": ts,
        "query": query,
        "hits": 3,
        "skipped_llm": skipped_llm,
        "source": "kousatsu" if skipped_llm else "llm",
    }


# ---------------------------------------------------------------------------
# Test 1: load_search_log — window_days フィルタ
# ---------------------------------------------------------------------------

def test_load_search_log_filters_old_entries(tmp_path: Path) -> None:
    """WINDOW_DAYS外の古いエントリがフィルタされる。"""
    log_file = tmp_path / "search_log.jsonl"
    recent = _make_entry(hours_ago=1)
    old = _make_entry(hours_ago=24 * 40)  # 40日前 (>30日)
    log_file.write_text(
        json.dumps(recent) + "\n" + json.dumps(old) + "\n",
        encoding="utf-8",
    )
    entries = load_search_log(str(log_file), window_days=30)
    assert len(entries) == 1
    assert entries[0]["query"] == recent["query"]


# ---------------------------------------------------------------------------
# Test 2: load_search_log — ファイルなし → 空リスト
# ---------------------------------------------------------------------------

def test_load_search_log_no_file(tmp_path: Path) -> None:
    """ファイルが存在しない場合は空リストを返す。"""
    result = load_search_log(str(tmp_path / "nonexistent.jsonl"))
    assert result == []


# ---------------------------------------------------------------------------
# Test 3: load_search_log — 壊れた行をスキップ
# ---------------------------------------------------------------------------

def test_load_search_log_skips_invalid_lines(tmp_path: Path) -> None:
    """JSONパースエラーの行はスキップされる。"""
    log_file = tmp_path / "search_log.jsonl"
    good = _make_entry(hours_ago=1)
    log_file.write_text(
        json.dumps(good) + "\n" + "not-valid-json\n",
        encoding="utf-8",
    )
    entries = load_search_log(str(log_file))
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Test 4: analyze_frequency — グループ化
# ---------------------------------------------------------------------------

def test_analyze_frequency_groups_by_query() -> None:
    """クエリ別にグループ化される。"""
    entries = [
        _make_entry("q1", hours_ago=1),
        _make_entry("q1", hours_ago=2),
        _make_entry("q2", hours_ago=3),
    ]
    result = analyze_frequency(entries)
    assert "q1" in result
    assert "q2" in result
    assert result["q1"]["count"] == 2
    assert result["q2"]["count"] == 1


# ---------------------------------------------------------------------------
# Test 5: analyze_frequency — kousatsu_rate 計算
# ---------------------------------------------------------------------------

def test_analyze_frequency_kousatsu_rate() -> None:
    """kousatsu_rateが正しく計算される(6/7 ≈ 0.857)。"""
    entries = (
        [_make_entry("q", skipped_llm=True)] * 6
        + [_make_entry("q", skipped_llm=False)]
    )
    result = analyze_frequency(entries)
    assert "q" in result
    assert result["q"]["count"] == 7
    assert abs(result["q"]["kousatsu_rate"] - 6 / 7) < 1e-6


# ---------------------------------------------------------------------------
# Test 6: generate_candidates — 閾値判定(N=7, confidence=0.80)
# ---------------------------------------------------------------------------

def test_generate_candidates_threshold() -> None:
    """N≥7かつconfidence≥0.80の場合のみ候補化される。"""
    analysis = {
        "q_ok": {"count": 7, "kousatsu_rate": 6 / 7, "entries": []},
        "q_low_count": {"count": 6, "kousatsu_rate": 1.0, "entries": []},
        "q_low_conf": {"count": 10, "kousatsu_rate": 0.7, "entries": []},
    }
    candidates = generate_candidates(analysis)
    queries = {c["query"] for c in candidates}
    assert "q_ok" in queries
    assert "q_low_count" not in queries
    assert "q_low_conf" not in queries


# ---------------------------------------------------------------------------
# Test 7: generate_candidates — 既存候補更新
# ---------------------------------------------------------------------------

def test_generate_candidates_update_existing() -> None:
    """既存候補があればfrequency/confidenceを更新し、statusは維持する。"""
    existing = [
        {
            "query": "q_existing",
            "frequency": 5,
            "confidence": 0.80,
            "status": "pending",
            "first_seen": datetime.now(_UTC).isoformat(),
            "last_updated": datetime.now(_UTC).isoformat(),
            "expires_at": (datetime.now(_UTC) + timedelta(days=30)).isoformat(),
            "actions": [],
        }
    ]
    analysis = {
        "q_existing": {"count": 10, "kousatsu_rate": 0.90, "entries": []},
    }
    candidates = generate_candidates(analysis, existing)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["query"] == "q_existing"
    assert cand["frequency"] == 10
    assert abs(cand["confidence"] - 0.90) < 1e-6
    assert cand["status"] == "pending"  # ステータスは維持


# ---------------------------------------------------------------------------
# Test 8: generate_candidates — 期限超過 → expired化
# ---------------------------------------------------------------------------

def test_generate_candidates_expired() -> None:
    """分析に出てこなかった古い既存候補はexpiredになる。"""
    old_updated = (datetime.now(_UTC) - timedelta(days=EXPIRY_DAYS + 1)).isoformat()
    existing = [
        {
            "query": "q_stale",
            "frequency": 8,
            "confidence": 0.85,
            "status": "pending",
            "first_seen": old_updated,
            "last_updated": old_updated,
            "expires_at": old_updated,
            "actions": [],
        }
    ]
    candidates = generate_candidates({}, existing)
    assert len(candidates) == 1
    assert candidates[0]["status"] == "expired"


# ---------------------------------------------------------------------------
# Test 9: generate_candidates — 新規候補の status
# ---------------------------------------------------------------------------

def test_generate_candidates_new_status() -> None:
    """新規候補はstatus='pending'で追加される。"""
    analysis = {"q_new": {"count": 8, "kousatsu_rate": 0.875, "entries": []}}
    candidates = generate_candidates(analysis)
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending"
    assert "first_seen" in candidates[0]
    assert "expires_at" in candidates[0]
    assert "actions" in candidates[0]


# ---------------------------------------------------------------------------
# Test 10: extract_typical_actions — confidence 算出
# ---------------------------------------------------------------------------

def test_extract_typical_actions_confidence() -> None:
    """kousatsu_rateがconfidenceとして返される。"""
    group = {"count": 8, "kousatsu_rate": 0.875, "entries": []}
    result = extract_typical_actions(group)
    assert "confidence" in result
    assert abs(result["confidence"] - 0.875) < 1e-4
    assert "actions" in result
    assert isinstance(result["actions"], list)


# ---------------------------------------------------------------------------
# Test 11: save_candidates — YAML出力形式確認
# ---------------------------------------------------------------------------

def test_save_candidates_yaml_format(tmp_path: Path) -> None:
    """rule_candidates.yamlが正しい形式で出力される。"""
    out_path = tmp_path / "rule_candidates.yaml"
    candidates = [
        {
            "query": "winter_night_cold_clear",
            "frequency": 8,
            "confidence": 0.875,
            "status": "pending",
            "first_seen": datetime.now(_UTC).isoformat(),
            "last_updated": datetime.now(_UTC).isoformat(),
            "expires_at": (datetime.now(_UTC) + timedelta(days=30)).isoformat(),
            "actions": [],
        }
    ]
    save_candidates(candidates, str(out_path))
    assert out_path.exists()
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert "candidates" in data
    assert "generated_at" in data
    assert data["min_frequency"] == MIN_FREQUENCY
    assert data["min_confidence"] == MIN_CONFIDENCE
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["query"] == "winter_night_cold_clear"


# ---------------------------------------------------------------------------
# Test 12: load_existing_candidates — ファイルなし → 空リスト
# ---------------------------------------------------------------------------

def test_load_existing_candidates_no_file(tmp_path: Path) -> None:
    """ファイルが存在しない場合は空リストを返す。"""
    result = load_existing_candidates(str(tmp_path / "nonexistent.yaml"))
    assert result == []


# ---------------------------------------------------------------------------
# Test 13: load_existing_candidates — 正常読み込み
# ---------------------------------------------------------------------------

def test_load_existing_candidates_normal(tmp_path: Path) -> None:
    """既存candidates.yamlから候補リストを正しく読み込む。"""
    yaml_file = tmp_path / "rule_candidates.yaml"
    data = {
        "generated_at": datetime.now(_UTC).isoformat(),
        "candidates": [
            {"query": "spring_morning_warm_clear", "frequency": 9, "status": "pending"}
        ],
    }
    yaml_file.write_text(
        yaml.dump(data, allow_unicode=True), encoding="utf-8"
    )
    result = load_existing_candidates(str(yaml_file))
    assert len(result) == 1
    assert result[0]["query"] == "spring_morning_warm_clear"


# ---------------------------------------------------------------------------
# Test 14: generate_candidates — 期限内の既存候補は保持
# ---------------------------------------------------------------------------

def test_generate_candidates_recent_existing_preserved() -> None:
    """今回分析に出てこなかった最近の既存候補は保持される（expired化しない）。"""
    recent_updated = datetime.now(_UTC).isoformat()
    existing = [
        {
            "query": "q_recent",
            "frequency": 8,
            "confidence": 0.85,
            "status": "pending",
            "first_seen": recent_updated,
            "last_updated": recent_updated,
            "expires_at": (datetime.now(_UTC) + timedelta(days=30)).isoformat(),
            "actions": [],
        }
    ]
    # 空のanalysis（q_recent は分析に含まれない）
    candidates = generate_candidates({}, existing)
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending"  # expiredにならない


# ---------------------------------------------------------------------------
# Test 15: run_distill — エンドツーエンド
# ---------------------------------------------------------------------------

def test_run_distill_end_to_end(tmp_path: Path) -> None:
    """run_distill() が search_log を読み込み candidates を生成する。"""
    log_file = tmp_path / "search_log.jsonl"
    candidates_file = tmp_path / "rule_candidates.yaml"

    # 7件のエントリ(同一query、6/7がkousatsu) を書き込む
    entries = (
        [_make_entry("winter_night_cold_clear", skipped_llm=True)] * 6
        + [_make_entry("winter_night_cold_clear", skipped_llm=False)]
    )
    log_file.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )

    result = run_distill(str(log_file), str(candidates_file))

    assert result["status"] == "ok"
    assert result["total_analyzed"] == 7
    assert result["total_candidates"] == 1
    assert candidates_file.exists()
    data = yaml.safe_load(candidates_file.read_text(encoding="utf-8"))
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["query"] == "winter_night_cold_clear"


# ---------------------------------------------------------------------------
# Test 16: _next_candidate_id — 空リスト → rc_001
# ---------------------------------------------------------------------------

def test_next_candidate_id_empty() -> None:
    """既存候補なし → rc_001 を返す。"""
    result = _next_candidate_id([])
    assert result == "rc_001"


# ---------------------------------------------------------------------------
# Test 17: _next_candidate_id — 既存あり → 連番インクリメント
# ---------------------------------------------------------------------------

def test_next_candidate_id_increments() -> None:
    """既存候補のmax id + 1 が返される。"""
    existing = [
        {"id": "rc_001", "query": "q1"},
        {"id": "rc_003", "query": "q3"},
    ]
    result = _next_candidate_id(existing)
    assert result == "rc_004"


# ---------------------------------------------------------------------------
# Test 18: generate_candidates — 新規候補に id フィールドが付与される
# ---------------------------------------------------------------------------

def test_generate_candidates_new_has_id() -> None:
    """新規候補にidフィールドが付与される。"""
    analysis = {"q_new": {"count": 8, "kousatsu_rate": 0.875, "entries": []}}
    candidates = generate_candidates(analysis)
    assert len(candidates) == 1
    assert "id" in candidates[0]
    assert candidates[0]["id"].startswith("rc_")


# ---------------------------------------------------------------------------
# Test 19: generate_candidates — 複数新規候補のid連番確認
# ---------------------------------------------------------------------------

def test_generate_candidates_multiple_ids_are_unique() -> None:
    """複数の新規候補が生成された場合、idが一意になる。"""
    analysis = {
        "q_a": {"count": 8, "kousatsu_rate": 0.875, "entries": []},
        "q_b": {"count": 9, "kousatsu_rate": 0.90, "entries": []},
    }
    candidates = generate_candidates(analysis)
    assert len(candidates) == 2
    ids = [c["id"] for c in candidates]
    assert len(set(ids)) == 2, "idが重複してはならない"
    for cid in ids:
        assert cid.startswith("rc_")


# ---------------------------------------------------------------------------
# Test 20: generate_candidates — 既存候補ありのid連番確認
# ---------------------------------------------------------------------------

def test_generate_candidates_id_increments_from_existing() -> None:
    """既存候補がある場合、新規idは既存max+1から始まる。"""
    existing = [
        {
            "id": "rc_005",
            "query": "q_existing",
            "frequency": 5,
            "confidence": 0.80,
            "status": "pending",
            "first_seen": datetime.now(_UTC).isoformat(),
            "last_updated": datetime.now(_UTC).isoformat(),
            "expires_at": (datetime.now(_UTC) + timedelta(days=30)).isoformat(),
            "actions": [],
        }
    ]
    analysis = {
        "q_new": {"count": 8, "kousatsu_rate": 0.875, "entries": []},
        "q_existing": {"count": 10, "kousatsu_rate": 0.90, "entries": []},
    }
    candidates = generate_candidates(analysis, existing)
    new_cand = next(c for c in candidates if c["query"] == "q_new")
    assert new_cand["id"] == "rc_006"
