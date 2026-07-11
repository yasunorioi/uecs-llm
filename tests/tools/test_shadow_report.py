"""tools/shadow_report.py の pytest.

CLI/HTML/sample の 3 コマンドを in-process で叩き、集計値と副産物ファイルを
最小限だけ検証する。実 shadow_log.jsonl と shape が同じであることは
`gen_sample_entries()` が担保する前提。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "shadow_report.py"
_spec = importlib.util.spec_from_file_location("shadow_report", _MODULE_PATH)
assert _spec and _spec.loader
sr = importlib.util.module_from_spec(_spec)
sys.modules["shadow_report"] = sr
_spec.loader.exec_module(sr)


# ── summarize() ─────────────────────────────────────

def test_summarize_counts_match_and_divergence():
    entries = [
        {"ts": "2026-07-01T00:00:00+09:00", "match": True,
         "interp": {"triggered": ["a"], "target_opening_pct": 0,
                    "close_all_windows": False, "close_by_wind_direction": False,
                    "relay_pulses": [], "skipped": [], "active_after": []},
         "live": {"triggered": ["a"], "target_opening_pct": 0,
                  "schedule_period": "night"},
         "delta": {}},
        {"ts": "2026-07-01T00:10:00+09:00", "match": False,
         "interp": {"triggered": ["b"], "target_opening_pct": 30,
                    "close_all_windows": False, "close_by_wind_direction": False,
                    "relay_pulses": [], "skipped": [["c", "err"]],
                    "active_after": []},
         "live": {"triggered": ["b"], "target_opening_pct": 20,
                  "schedule_period": "morning"},
         "delta": {"target_opening_pct": {"interp": 30, "live": 20}}},
    ]
    s = sr.summarize(entries)
    assert s.total == 2
    assert s.matched == 1
    assert s.diverged == 1
    assert s.match_rate == 0.5
    assert s.first_ts == "2026-07-01T00:00:00+09:00"
    assert s.last_ts == "2026-07-01T00:10:00+09:00"
    assert s.delta_key_counts == Counter({"target_opening_pct": 1})
    assert s.delta_pattern_counts["target_opening_pct"] == Counter({"interp=30 live=20": 1})
    assert s.interp_trigger_counts == Counter({"a": 1, "b": 1})
    assert s.skipped_counts == Counter({"c": 1})


def test_summarize_empty_input():
    s = sr.summarize([])
    assert s.total == 0
    assert s.match_rate == 0.0
    assert s.first_ts is None


# ── take_recent_divergences() ───────────────────────

def test_take_recent_divergences_returns_tail_only_diverged():
    entries = [
        {"match": True, "ts": "a", "interp": {}, "live": {}, "delta": {}},
        {"match": False, "ts": "b", "interp": {}, "live": {}, "delta": {"k": 1}},
        {"match": False, "ts": "c", "interp": {}, "live": {}, "delta": {"k": 2}},
        {"match": False, "ts": "d", "interp": {}, "live": {}, "delta": {"k": 3}},
    ]
    got = sr.take_recent_divergences(entries, limit=2)
    assert [e["ts"] for e in got] == ["c", "d"]


# ── sample generator ────────────────────────────────

def test_gen_sample_entries_shape_and_diverge_rate():
    entries = sr.gen_sample_entries(count=500, diverge_rate=0.2, seed=1)
    assert len(entries) == 500
    for e in entries:
        assert set(["ts", "match", "interp", "live", "delta"]).issubset(e)
        assert isinstance(e["interp"]["triggered"], list)
        # match と delta の整合
        if e["match"]:
            assert e["delta"] == {}
        else:
            assert e["delta"] != {}
    diverged = sum(1 for e in entries if not e["match"])
    # 概ね 20% ± 一定幅 (seed 固定なので緩めに)
    assert 60 <= diverged <= 160


def test_gen_sample_no_zero_zero_delta():
    """delta の interp==live は生成されないこと (0 case bug 再発防止)。"""
    entries = sr.gen_sample_entries(count=500, diverge_rate=0.3, seed=7)
    for e in entries:
        pair = e["delta"].get("target_opening_pct")
        if pair is not None:
            assert pair["interp"] != pair["live"]


# ── load_jsonl / round-trip via CLI ─────────────────

def test_cli_sample_then_summary_roundtrip(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    rc = sr.main(["sample", "--out", str(log), "--count", "40", "--seed", "3"])
    assert rc == 0
    capsys.readouterr()  # drain

    rc = sr.main(["summary", "--log", str(log), "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shadow-run summary" in out
    assert "total=40" in out


def test_cli_html_writes_selfcontained_file(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    sr.main(["sample", "--out", str(log), "--count", "20", "--seed", "3"])
    capsys.readouterr()

    out = tmp_path / "report.html"
    rc = sr.main(["html", "--log", str(log), "--out", str(out), "--limit", "20"])
    assert rc == 0
    doc = out.read_text(encoding="utf-8")
    assert "<title>shadow-run report</title>" in doc
    # embedded JSON blob 存在確認
    assert "const ENTRIES = [" in doc
    # 完全に self-contained (外部 <link> / <script src=> なし)
    assert "<link" not in doc
    assert "<script src" not in doc


def test_load_jsonl_skips_malformed_line(tmp_path, capsys):
    p = tmp_path / "log.jsonl"
    p.write_text(
        '{"ts":"t1","match":true,"interp":{},"live":{},"delta":{}}\n'
        'not-json\n'
        '{"ts":"t2","match":false,"interp":{},"live":{},"delta":{"k":1}}\n',
        encoding="utf-8",
    )
    entries = sr.load_jsonl(p)
    err = capsys.readouterr().err
    assert len(entries) == 2
    assert "malformed json" in err
