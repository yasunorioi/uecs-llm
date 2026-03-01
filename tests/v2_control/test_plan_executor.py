"""plan_executor.py のユニットテスト。

設計書 §1.4, §7.3 に基づく 12 ケース。
http_client は MagicMock で差し替え、ファイル I/O は tmp_path を使用。

テストケース一覧:
    1.  current_plan.json なし → 何もしない
    2.  valid_until 期限切れ → 何もしない
    3.  Layer 1 ロックアウト中 → 何もしない
    4.  降雨中に側窓操作 → skipped_weather で記録
    5.  降雨中に灌水操作 → 正常実行（側窓のみスキップ）
    6.  強風中に側窓操作 → skipped_weather
    7.  execute_at 未到来 → スキップ
    8.  execute_at 到来 + 未実行 → POST 実行
    9.  executed: true → スキップ（既実行）
    10. 423 応答 → スキップ、次回リトライ
    11. バリデーション: relay_ch 範囲外 → スキップ
    12. バリデーション: duration_sec > 3600 → 3600 に切り詰め
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from v2_control.plan_executor import run_executor

# ---------------------------------------------------------------------------
# テスト用定数
# ---------------------------------------------------------------------------

_JST = ZoneInfo("Asia/Tokyo")

# テスト用「現在時刻」: 2026-03-01 14:30 JST
_NOW = datetime(2026, 3, 1, 14, 30, 0, tzinfo=_JST)
# 現在より前（実行対象）
_PAST = datetime(2026, 3, 1, 14, 20, 0, tzinfo=_JST)
# 現在より後（未到来）
_FUTURE = datetime(2026, 3, 1, 15, 0, 0, tzinfo=_JST)
# valid_until: 現在より後
_VALID_UNTIL = datetime(2026, 3, 1, 15, 30, 0, tzinfo=_JST)
# valid_until: 既に過ぎた
_EXPIRED = datetime(2026, 3, 1, 13, 0, 0, tzinfo=_JST)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_plan(
    actions: list[dict[str, Any]],
    valid_until: datetime = _VALID_UNTIL,
) -> dict[str, Any]:
    """current_plan.json の内容を生成するヘルパー。"""
    return {
        "generated_at": "2026-03-01T14:00:00+09:00",
        "valid_until": valid_until.isoformat(),
        "summary": "テスト計画",
        "actions": actions,
    }


def _make_action(
    relay_ch: int = 5,
    value: int = 1,
    duration_sec: int = 30,
    execute_at: datetime = _PAST,
    executed: Any = False,
    reason: str = "テスト",
) -> dict[str, Any]:
    """action 辞書を生成するヘルパー。"""
    a: dict[str, Any] = {
        "relay_ch": relay_ch,
        "value": value,
        "duration_sec": duration_sec,
        "execute_at": execute_at.isoformat(),
        "reason": reason,
    }
    if executed is not False:
        a["executed"] = executed
    return a


def _make_http_client(
    sensors: dict[str, Any] | None = None,
    locked_out: bool = False,
    relay_status_code: int = 200,
) -> MagicMock:
    """httpx.Client のモックを生成するヘルパー。

    Args:
        sensors: /api/sensors の "sensors" キーの値。None で空。
        locked_out: /api/status の locked_out フィールド値。
        relay_status_code: POST /api/relay/{ch} のステータスコード。
    """
    client = MagicMock()

    # GET /api/status
    status_resp = MagicMock()
    status_resp.json.return_value = {"locked_out": locked_out}
    status_resp.raise_for_status = MagicMock()

    # GET /api/sensors
    sensors_resp = MagicMock()
    sensors_resp.json.return_value = {"sensors": sensors or {}}
    sensors_resp.raise_for_status = MagicMock()

    def _get(url: str, **kwargs: Any) -> MagicMock:
        if "/api/status" in url:
            return status_resp
        if "/api/sensors" in url:
            return sensors_resp
        raise ValueError(f"unexpected GET: {url}")

    client.get.side_effect = _get

    # POST /api/relay/{ch}
    relay_resp = MagicMock()
    relay_resp.status_code = relay_status_code
    relay_resp.raise_for_status = MagicMock()
    if relay_status_code >= 400:
        relay_resp.raise_for_status.side_effect = Exception(f"HTTP {relay_status_code}")
    client.post.return_value = relay_resp

    return client


def _normal_sensors() -> dict[str, Any]:
    """通常天候（降雨なし、風速なし）のセンサーデータ。"""
    return {
        "agriha/farm/weather/misol": {
            "rainfall": 0.0,
            "wind_speed_ms": 0.5,
            "temperature_c": 18.0,
        }
    }


def _rain_sensors(rainfall: float = 1.0) -> dict[str, Any]:
    """降雨中のセンサーデータ。"""
    return {
        "agriha/farm/weather/misol": {
            "rainfall": rainfall,
            "wind_speed_ms": 1.0,
            "temperature_c": 15.0,
        }
    }


def _wind_sensors(wind_speed_ms: float = 8.0) -> dict[str, Any]:
    """強風中のセンサーデータ。"""
    return {
        "agriha/farm/weather/misol": {
            "rainfall": 0.0,
            "wind_speed_ms": wind_speed_ms,
            "temperature_c": 20.0,
        }
    }


def _make_config(tmp_path: Path) -> dict[str, Any]:
    """テスト用 config 辞書を返す（layer2_config.yaml はデフォルト閾値）。"""
    return {
        "plan_path": str(tmp_path / "current_plan.json"),
        "lockout_path": str(tmp_path / "lockout_state.json"),
        "layer2_config_path": str(tmp_path / "layer2_config.yaml"),
        "unipi_api": "http://localhost:8080",
        "api_key": "",
        "timeout_sec": 5,
    }


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestNoPlan:
    """テスト 1, 2: 計画なし / 期限切れ"""

    def test_01_no_plan_file_does_nothing(self, tmp_path: Path) -> None:
        """1. current_plan.json なし → 何もしない"""
        cfg = _make_config(tmp_path)
        client = _make_http_client()

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert result["no_plan"] is True
        assert result["executed"] == []
        client.post.assert_not_called()

    def test_02_expired_plan_does_nothing(self, tmp_path: Path) -> None:
        """2. valid_until 期限切れ → 何もしない"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action()], valid_until=_EXPIRED)
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client()

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert result["no_plan"] is True
        assert result["executed"] == []
        client.post.assert_not_called()


class TestLockout:
    """テスト 3: ロックアウト中"""

    def test_03_layer1_lockout_does_nothing(self, tmp_path: Path) -> None:
        """3. Layer 1 ロックアウト中 → 何もしない"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action()])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")

        # lockout_state.json: 現在より先にロックアウト期限を設定
        lockout_until = datetime(2026, 3, 1, 15, 0, 0, tzinfo=_JST)
        lockout_data = {"layer1_lockout_until": lockout_until.isoformat()}
        Path(cfg["lockout_path"]).write_text(json.dumps(lockout_data), encoding="utf-8")

        client = _make_http_client()
        result = run_executor(cfg, http_client=client, now=_NOW)

        assert "layer1" in result["skipped_lockout"]
        assert result["executed"] == []
        client.post.assert_not_called()


class TestWeatherSkip:
    """テスト 4, 5, 6: 天候スキップ"""

    def test_04_rain_skips_window_channel(self, tmp_path: Path) -> None:
        """4. 降雨中に側窓操作（ch5）→ skipped_weather で記録"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=5, execute_at=_PAST)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_rain_sensors(1.0))  # rainfall=1.0 > 0.5

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 5 in result["skipped_weather"]
        assert result["executed"] == []
        client.post.assert_not_called()

        # current_plan.json に skipped_weather が記録されていること
        saved = json.loads(Path(cfg["plan_path"]).read_text())
        assert saved["actions"][0]["executed"] == "skipped_weather"

    def test_05_rain_does_not_skip_irrigation(self, tmp_path: Path) -> None:
        """5. 降雨中に灌水操作（ch4）→ 正常実行（側窓のみスキップ）"""
        cfg = _make_config(tmp_path)
        # ch4=灌水（window_channels=[5,6,7,8] に含まれない）
        plan = _make_plan([_make_action(relay_ch=4, execute_at=_PAST)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_rain_sensors(1.0))

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 4 in result["executed"]
        assert result["skipped_weather"] == []
        client.post.assert_called_once()

    def test_06_strong_wind_skips_window_channel(self, tmp_path: Path) -> None:
        """6. 強風中に側窓操作（ch6）→ skipped_weather"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=6, execute_at=_PAST)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_wind_sensors(8.0))  # wind=8.0 > 5.0

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 6 in result["skipped_weather"]
        assert result["executed"] == []
        client.post.assert_not_called()


class TestExecutionTiming:
    """テスト 7, 8, 9: execute_at タイミング"""

    def test_07_not_due_yet_skipped(self, tmp_path: Path) -> None:
        """7. execute_at 未到来 → スキップ"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=5, execute_at=_FUTURE)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors())

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 5 in result["skipped_not_due"]
        assert result["executed"] == []
        client.post.assert_not_called()

    def test_08_due_action_executed(self, tmp_path: Path) -> None:
        """8. execute_at 到来 + 未実行 → POST 実行"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=5, execute_at=_PAST)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors())

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 5 in result["executed"]
        assert result["skipped_weather"] == []
        client.post.assert_called_once()

        # executed: true が記録されること
        saved = json.loads(Path(cfg["plan_path"]).read_text())
        assert saved["actions"][0]["executed"] is True

    def test_09_already_executed_skipped(self, tmp_path: Path) -> None:
        """9. executed: true → スキップ（既実行）"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=5, execute_at=_PAST, executed=True)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors())

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 5 in result["skipped_already_done"]
        assert result["executed"] == []
        client.post.assert_not_called()


class TestErrorHandling:
    """テスト 10, 11, 12: エラーハンドリング"""

    def test_10_relay_423_skipped_for_retry(self, tmp_path: Path) -> None:
        """10. 423 応答 → スキップ、次回リトライ（executed は更新しない）"""
        cfg = _make_config(tmp_path)
        plan = _make_plan([_make_action(relay_ch=5, execute_at=_PAST)])
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors(), relay_status_code=423)

        result = run_executor(cfg, http_client=client, now=_NOW)

        # 実行されず、ロックアウトスキップとして記録
        assert result["executed"] == []
        assert any("relay_ch5" in str(x) for x in result["skipped_lockout"])

        # executed は更新されない（次回リトライのため）
        saved = json.loads(Path(cfg["plan_path"]).read_text())
        assert saved["actions"][0].get("executed") is False or "executed" not in saved["actions"][0]

    def test_11_invalid_relay_ch_skipped(self, tmp_path: Path) -> None:
        """11. バリデーション: relay_ch 範囲外 → スキップ"""
        cfg = _make_config(tmp_path)
        # relay_ch=9 は範囲外 [1-8]
        plan = _make_plan([_make_action(relay_ch=9, execute_at=_PAST)])
        plan["actions"][0]["relay_ch"] = 9  # 範囲外
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors())

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 9 in result["skipped_invalid"]
        assert result["executed"] == []
        client.post.assert_not_called()

    def test_12_duration_sec_over_3600_clamped(self, tmp_path: Path) -> None:
        """12. バリデーション: duration_sec > 3600 → 3600 に切り詰め"""
        cfg = _make_config(tmp_path)
        plan = _make_plan(
            [_make_action(relay_ch=4, execute_at=_PAST, duration_sec=7200)]
        )
        Path(cfg["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
        client = _make_http_client(sensors=_normal_sensors())

        result = run_executor(cfg, http_client=client, now=_NOW)

        assert 4 in result["executed"]
        client.post.assert_called_once()

        # POST のペイロードで duration_sec が 3600 になっていること
        call_kwargs = client.post.call_args
        posted_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json") or call_kwargs[0][1]
        assert posted_json["duration_sec"] == 3600

        # current_plan.json でも 3600 に更新されること
        saved = json.loads(Path(cfg["plan_path"]).read_text())
        assert saved["actions"][0]["duration_sec"] == 3600
