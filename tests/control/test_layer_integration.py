"""test_layer_integration.py — 層間連携テスト (5件)

設計書 §3.4「下層が上層を黙らせる」原則の検証。
Layer 1 (lockout) → Layer 2 (rule_engine flag) → Layer 3 (plan_executor) の
抑制連鎖が正しく動作することを確認する。

テストケース:
    1. lockout中 → plan_executorが全アクションをスキップ
    2. rain_flag存在 → 側窓ch5-8スキップ、灌水ch4は実行
    3. wind_flag存在 → 側窓ch5スキップ、灌水ch4は実行
    4. rain_flag鮮度チェック: 30分前のflag → 無視されて正常実行
    5. lockout + rain_flag同時 → 全スキップ（lockoutが優先）
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from agriha.control.plan_executor import run_executor

# ---------------------------------------------------------------------------
# テスト用定数
# ---------------------------------------------------------------------------

_JST = ZoneInfo("Asia/Tokyo")

_NOW = datetime(2026, 3, 5, 10, 0, 0, tzinfo=_JST)
_PAST = datetime(2026, 3, 5, 9, 50, 0, tzinfo=_JST)   # 10分前 → 実行対象
_FUTURE = datetime(2026, 3, 5, 10, 30, 0, tzinfo=_JST)
_VALID_UNTIL = datetime(2026, 3, 5, 11, 0, 0, tzinfo=_JST)

# 側窓チャンネル (channel_map.yaml のデフォルト値)
_WINDOW_CHANNELS = [5, 6, 7, 8]
# 灌水チャンネル (window_channels に含まれない)
_IRRIGATION_CH = 4


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> dict[str, Any]:
    """テスト用 config 辞書を返す。"""
    return {
        "plan_path": str(tmp_path / "current_plan.json"),
        "lockout_path": str(tmp_path / "lockout_state.json"),
        "rules_config_path": str(tmp_path / "rules.yaml"),
        "unipi_api": "http://localhost:8080",
        "api_key": "",
        "timeout_sec": 5,
        "flag_dir": str(tmp_path / "flags"),
    }


def _write_plan(cfg: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    """current_plan.json を書き込む。"""
    plan = {
        "generated_at": _NOW.isoformat(),
        "valid_until": _VALID_UNTIL.isoformat(),
        "summary": "層間連携テスト計画",
        "actions": actions,
    }
    Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")


def _make_action(
    relay_ch: int,
    execute_at: datetime = _PAST,
) -> dict[str, Any]:
    """action 辞書を生成する。"""
    return {
        "relay_ch": relay_ch,
        "value": 1,
        "duration_sec": 60,
        "execute_at": execute_at.isoformat(),
        "reason": "integration_test",
    }


def _make_http_client(relay_status_code: int = 200) -> MagicMock:
    """httpx.Client モック。/api/status でロックアウトなし、/api/relay は指定コード。"""
    client = MagicMock()

    status_resp = MagicMock()
    status_resp.json.return_value = {"locked_out": False}

    def _get(url: str, **kwargs: Any) -> MagicMock:
        if "/api/status" in url:
            return status_resp
        raise ValueError(f"unexpected GET: {url}")

    client.get.side_effect = _get

    relay_resp = MagicMock()
    relay_resp.status_code = relay_status_code
    relay_resp.raise_for_status = MagicMock()
    client.post.return_value = relay_resp

    return client


def _write_fresh_flag(flag_dir: Path, name: str, content: str = "active") -> None:
    """flagファイルを新規作成する（現在時刻のmtime）。"""
    flag_dir.mkdir(parents=True, exist_ok=True)
    (flag_dir / name).write_text(content)


def _write_stale_flag(
    flag_dir: Path, name: str, age_sec: int = 30 * 60
) -> None:
    """古いflagファイルを作成する（mtime を age_sec 秒前に設定）。"""
    flag_dir.mkdir(parents=True, exist_ok=True)
    flag_path = flag_dir / name
    flag_path.write_text("stale")
    stale_mtime = time.time() - age_sec
    os.utime(flag_path, (stale_mtime, stale_mtime))


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestLayerIntegration:
    """§3.4「下層が上層を黙らせる」層間連携テスト。"""

    def test_01_lockout_skips_all_actions(self, tmp_path: Path) -> None:
        """1. lockout_state.json 存在 → plan_executor が全アクションをスキップ。

        Layer 1 (lockout) が Layer 3 (plan_executor) を黙らせる。
        current_plan.json に複数アクションがあっても一切実行されない。
        """
        cfg = _make_config(tmp_path)
        _write_plan(cfg, [
            _make_action(relay_ch=4),   # 灌水
            _make_action(relay_ch=5),   # 側窓
        ])

        # lockout: 現在時刻より先まで有効
        lockout_data = {
            "layer1_lockout_until": datetime(2026, 3, 5, 11, 0, 0, tzinfo=_JST).isoformat()
        }
        Path(cfg["lockout_path"]).write_text(json.dumps(lockout_data))

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert "layer1" in result["skipped_lockout"], "lockoutスキップが記録されること"
        assert result["executed"] == [], "全アクションが実行されないこと"
        client.post.assert_not_called()

    def test_02_rain_flag_skips_windows_but_not_irrigation(self, tmp_path: Path) -> None:
        """2. rain_flag 存在 → 側窓ch5-8スキップ、灌水ch4は実行。

        Layer 2 flag (rain) が側窓操作のみを抑制し、灌水は影響なし。
        """
        cfg = _make_config(tmp_path)
        _write_plan(cfg, [
            _make_action(relay_ch=_IRRIGATION_CH),  # ch4: 灌水（スキップされない）
            _make_action(relay_ch=5),                 # ch5: 側窓（スキップ）
            _make_action(relay_ch=7),                 # ch7: 側窓（スキップ）
        ])
        _write_fresh_flag(Path(cfg["flag_dir"]), "rain_flag")

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert _IRRIGATION_CH in result["executed"], "灌水ch4は実行されること"
        assert 5 in result["skipped_weather"], "側窓ch5はスキップされること"
        assert 7 in result["skipped_weather"], "側窓ch7はスキップされること"
        assert client.post.call_count == 1, "POST呼び出しは灌水の1回のみ"

        # skipped_rain がcurrent_plan.jsonに記録されること
        saved = json.loads(Path(cfg["plan_path"]).read_text())
        window_actions = [a for a in saved["actions"] if a["relay_ch"] in (5, 7)]
        for a in window_actions:
            assert a["executed"] == "skipped_rain", f"ch{a['relay_ch']}はskipped_rainであること"

    def test_03_wind_flag_skips_windows_but_not_irrigation(self, tmp_path: Path) -> None:
        """3. wind_flag 存在 → 側窓ch5スキップ、灌水ch4は実行。

        Layer 2 flag (wind) が側窓操作のみを抑制する。
        """
        cfg = _make_config(tmp_path)
        _write_plan(cfg, [
            _make_action(relay_ch=_IRRIGATION_CH),  # ch4: 灌水（スキップされない）
            _make_action(relay_ch=5),                 # ch5: 側窓（スキップ）
        ])
        _write_fresh_flag(
            Path(cfg["flag_dir"]), "wind_flag",
            content='{"timestamp": "2026-03-05T10:00:00+09:00", "wind_speed_ms": 9.5}'
        )

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert _IRRIGATION_CH in result["executed"], "灌水ch4は実行されること"
        assert 5 in result["skipped_weather"], "側窓ch5はスキップされること"
        assert client.post.call_count == 1, "POST呼び出しは灌水の1回のみ"

        saved = json.loads(Path(cfg["plan_path"]).read_text())
        window_action = next(a for a in saved["actions"] if a["relay_ch"] == 5)
        assert window_action["executed"] == "skipped_wind", "ch5はskipped_windであること"

    def test_04_stale_flag_ignored_window_executes(self, tmp_path: Path) -> None:
        """4. 30分前のrain_flag → FLAG_MAX_AGE_SEC(20分)超過で無視 → 側窓も正常実行。

        鮮度チェック: 古いflagは無効。FLAG_MAX_AGE_SEC = 20分。
        """
        cfg = _make_config(tmp_path)
        _write_plan(cfg, [_make_action(relay_ch=5)])  # ch5: 側窓
        # 30分前のmtime → 20分制限を超えている
        _write_stale_flag(Path(cfg["flag_dir"]), "rain_flag", age_sec=30 * 60)

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 5 in result["executed"], "古いflagは無視されch5が実行されること"
        assert result["skipped_weather"] == [], "天候スキップが発生しないこと"
        client.post.assert_called_once()

    def test_05_lockout_and_rain_flag_lockout_wins(self, tmp_path: Path) -> None:
        """5. lockout + rain_flag 同時存在 → lockoutが優先、全操作スキップ。

        Layer 1 > Layer 2: lockoutはflagより上位の抑制。
        """
        cfg = _make_config(tmp_path)
        _write_plan(cfg, [
            _make_action(relay_ch=_IRRIGATION_CH),  # ch4: 灌水
            _make_action(relay_ch=5),                 # ch5: 側窓
        ])

        # lockout有効
        lockout_data = {
            "layer1_lockout_until": datetime(2026, 3, 5, 11, 0, 0, tzinfo=_JST).isoformat()
        }
        Path(cfg["lockout_path"]).write_text(json.dumps(lockout_data))

        # rain_flag も存在
        _write_fresh_flag(Path(cfg["flag_dir"]), "rain_flag")

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert "layer1" in result["skipped_lockout"], "lockoutスキップが記録されること"
        assert result["executed"] == [], "全アクションが実行されないこと"
        # rain_flag由来のskipped_weatherは発生しない（lockoutの時点でreturn）
        assert result["skipped_weather"] == [], "lockout優先のためweatherスキップは発生しない"
        client.post.assert_not_called()
