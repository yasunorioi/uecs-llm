"""tests/control/test_tide_forecaster.py — tide_forecaster.py のユニットテスト。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from agriha.control.tide_forecaster import (
    _build_alerts,
    _build_future_cov,
    fetch_openmeteo_forecast,
    load_sensor_window,
    run_forecast,
)

_JST = timezone(timedelta(hours=9))
_UTC = timezone.utc


# ────────────────────────────────────────────────────────────────
# fixtures
# ────────────────────────────────────────────────────────────────

COLUMNS_16 = [
    "InAirTemp", "InAirHumid", "InAirCO2",
    "WTemp", "WAirHumid", "WWindSpeed", "WRainfall",
    "om_temp2m", "om_humidity2m", "om_radiation", "om_precip", "om_wind10m",
    "hour_sin", "hour_cos", "doy_sin", "doy_cos",
]


def _make_db(db_path: str, rows: list[tuple]) -> None:
    """(timestamp_utc_str, source, metric, value) のリストをDBに挿入。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sensor_log (timestamp TEXT, source TEXT, metric TEXT, value REAL)"
    )
    conn.executemany(
        "INSERT INTO sensor_log VALUES (?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


# ────────────────────────────────────────────────────────────────
# load_sensor_window テスト
# ────────────────────────────────────────────────────────────────

class TestLoadSensorWindow:
    def test_returns_correct_shape(self):
        """lookback=48 のとき (48, 16) を返す。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        now_utc = datetime.now(_UTC).replace(minute=0, second=0, microsecond=0)
        rows = []
        for i in range(48):
            ts = (now_utc - timedelta(hours=48 - i)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            rows.append((ts, "ccm", "temp_inside", 25.0 + i * 0.1))
            rows.append((ts, "ccm", "humidity", 70.0))
            rows.append((ts, "ccm", "co2", 400.0))

        _make_db(db_path, rows)
        result = load_sensor_window(db_path, lookback_hours=48, columns=COLUMNS_16)
        assert result.shape == (48, 16)

    def test_ccm_values_in_correct_columns(self):
        """CCMデータが InAirTemp/InAirHumid/InAirCO2 列に入る。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        now_utc = datetime.now(_UTC).replace(minute=0, second=0, microsecond=0)
        slot = now_utc - timedelta(hours=1)
        ts = slot.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        _make_db(db_path, [
            (ts, "ccm", "temp_inside", 24.5),
            (ts, "ccm", "humidity",    72.0),
            (ts, "ccm", "co2",         420.0),
        ])
        result = load_sensor_window(db_path, lookback_hours=48, columns=COLUMNS_16)
        # 最後から2番目のスロット（slot = now-1h）がインデックス46（lookback-2）
        # ただし実際には now_utc - 48h がindex 0、 now_utc - 1h がindex 47
        assert result.shape == (48, 16)
        # 少なくとも 0 以外の値が InAirTemp 列にある
        temp_col = result[:, 0]
        assert np.any(temp_col != 0.0)

    def test_empty_db_returns_zeros(self):
        """DBが空の場合は全ゼロを返す（NaN なし）。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sensor_log (timestamp TEXT, source TEXT, metric TEXT, value REAL)"
        )
        conn.commit()
        conn.close()

        result = load_sensor_window(db_path, lookback_hours=48, columns=COLUMNS_16)
        assert result.shape == (48, 16)
        assert not np.any(np.isnan(result)), "NaN が残っていてはいけない"
        assert np.all(result == 0.0)

    def test_missing_hours_are_filled(self):
        """一部時間帯のデータがなくてもffill/bfillで埋まる（NaN なし）。"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        now_utc = datetime.now(_UTC).replace(minute=0, second=0, microsecond=0)
        # 先頭3スロットのみデータあり
        rows = []
        for i in range(3):
            slot = now_utc - timedelta(hours=48 - i)
            ts = slot.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            rows.append((ts, "ccm", "temp_inside", 20.0))
        _make_db(db_path, rows)

        result = load_sensor_window(db_path, lookback_hours=48, columns=COLUMNS_16)
        assert not np.any(np.isnan(result[:, 0])), "InAirTemp 列に NaN が残っている"


# ────────────────────────────────────────────────────────────────
# fetch_openmeteo_forecast テスト
# ────────────────────────────────────────────────────────────────

class TestFetchOpenmeteoForecast:
    def test_returns_none_on_network_error(self):
        """ネットワークエラー時は None を返す（フォールバック）。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("network error")):
            result = fetch_openmeteo_forecast(hours=6)
        assert result is None

    def test_returns_dict_with_6_values_on_success(self):
        """正常レスポンス時は 6 要素の dict を返す。"""
        now_utc = datetime.now(_UTC).replace(minute=0, second=0, microsecond=0)
        times = [(now_utc + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(12)]
        fake_response = json.dumps({
            "hourly": {
                "time": times,
                "temperature_2m": [10.0] * 12,
                "relative_humidity_2m": [60.0] * 12,
                "shortwave_radiation": [100.0] * 12,
                "precipitation": [0.0] * 12,
                "wind_speed_10m": [5.0] * 12,
            }
        }).encode()

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = fetch_openmeteo_forecast(hours=6)

        assert result is not None
        assert len(result["om_temp2m"]) == 6
        assert len(result["om_humidity2m"]) == 6


# ────────────────────────────────────────────────────────────────
# _build_future_cov テスト
# ────────────────────────────────────────────────────────────────

class TestBuildFutureCov:
    def test_shape(self):
        now = datetime.now(_UTC)
        cov = _build_future_cov(pred_len=6, now_utc=now)
        assert cov.shape == (6, 4)

    def test_values_in_range(self):
        """hour_sin/cos は [-1,1]、doy_sin/cos も [-1,1]。"""
        now = datetime.now(_UTC)
        cov = _build_future_cov(pred_len=6, now_utc=now)
        assert np.all(np.abs(cov) <= 1.0 + 1e-6)


# ────────────────────────────────────────────────────────────────
# _build_alerts テスト
# ────────────────────────────────────────────────────────────────

class TestBuildAlerts:
    def test_no_alerts_when_below_threshold(self):
        preds = {"InAirHumid": [70.0, 75.0, 78.0, 79.9, 70.0, 65.0],
                 "InAirTemp": [24.0, 25.0, 26.0, 26.9, 25.0, 24.0]}
        assert _build_alerts(preds) == []

    def test_humidity_high_alert(self):
        preds = {"InAirHumid": [75.0, 82.0, 85.0, 70.0, 70.0, 70.0],
                 "InAirTemp": [24.0] * 6}
        alerts = _build_alerts(preds)
        types = [a["type"] for a in alerts]
        assert "humidity_high" in types
        # hour 2 と 3 が超過
        hours = [a["hour"] for a in alerts if a["type"] == "humidity_high"]
        assert 2 in hours and 3 in hours

    def test_temp_high_alert(self):
        preds = {"InAirHumid": [70.0] * 6,
                 "InAirTemp": [24.0, 24.0, 27.5, 28.0, 24.0, 24.0]}
        alerts = _build_alerts(preds)
        types = [a["type"] for a in alerts]
        assert "temp_high" in types


# ────────────────────────────────────────────────────────────────
# run_forecast 統合テスト（DBモック + TFLiteモック）
# ────────────────────────────────────────────────────────────────

class TestRunForecast:
    def _make_norm_params(self, tmp_dir: Path) -> Path:
        norm = {
            "mean": [25.0, 70.0, 400.0] + [15.0, 65.0, 3.0, 0.0] + [15.0, 65.0, 100.0, 0.0, 3.0] + [0.0, 0.0, 0.0, 0.0],
            "std":  [3.0,  10.0, 100.0] + [5.0,  15.0, 2.0, 1.0] + [5.0,  15.0, 200.0, 1.0, 2.0] + [1.0, 1.0, 1.0, 1.0],
            "columns": [
                "InAirTemp", "InAirHumid", "InAirCO2",
                "WTemp", "WAirHumid", "WWindSpeed", "WRainfall",
                "om_temp2m", "om_humidity2m", "om_radiation", "om_precip", "om_wind10m",
                "hour_sin", "hour_cos", "doy_sin", "doy_cos",
            ],
            "targets": ["InAirTemp", "InAirHumid", "InAirCO2"],
            "lookback": 48,
            "pred_len": 6,
            "n_targets": 3,
            "n_future_cov": 4,
        }
        p = tmp_dir / "norm_params.json"
        p.write_text(json.dumps(norm))
        return p

    def test_output_json_structure(self, tmp_path):
        """run_forecast が正しい構造の JSON を生成する。"""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        self._make_norm_params(model_dir)
        # ダミー tflite ファイル
        (model_dir / "agriha_tide.tflite").write_bytes(b"dummy")

        db_path = tmp_path / "sensor_log.db"
        _make_db(str(db_path), [])
        output_path = tmp_path / "tide_forecast.json"

        # TFLite Interpreter をモック化
        fake_pred = np.zeros((1, 6, 3), dtype=np.float32)
        fake_pred[0, :, 0] = 24.0  # InAirTemp (norm)
        fake_pred[0, :, 1] = 0.0   # InAirHumid (norm)
        fake_pred[0, :, 2] = 0.0   # InAirCO2 (norm)

        mock_interp = mock.MagicMock()
        mock_interp.get_input_details.return_value = [
            {"index": 0}, {"index": 1}
        ]
        mock_interp.get_output_details.return_value = [{"index": 2}]
        mock_interp.get_tensor.return_value = fake_pred

        with mock.patch(
            "agriha.control.tide_forecaster._get_interpreter",
            return_value=mock_interp,
        ), mock.patch(
            "agriha.control.tide_forecaster.fetch_openmeteo_forecast",
            return_value=None,
        ):
            result = run_forecast(
                db_path=str(db_path),
                model_dir=str(model_dir),
                output_path=str(output_path),
            )

        assert output_path.exists()
        assert "generated_at" in result
        assert "predictions" in result
        assert "InAirTemp" in result["predictions"]
        assert "InAirHumid" in result["predictions"]
        assert "InAirCO2" in result["predictions"]
        assert len(result["predictions"]["InAirTemp"]) == 6
        assert "alerts" in result
        assert "horizon_hours" in result
        assert result["horizon_hours"] == 6

    def test_open_meteo_failure_does_not_abort(self, tmp_path):
        """Open-Meteo 取得失敗でも予測が完了する（フォールバック）。"""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        self._make_norm_params(model_dir)
        (model_dir / "agriha_tide.tflite").write_bytes(b"dummy")

        db_path = tmp_path / "sensor_log.db"
        _make_db(str(db_path), [])
        output_path = tmp_path / "tide_forecast.json"

        fake_pred = np.zeros((1, 6, 3), dtype=np.float32)
        mock_interp = mock.MagicMock()
        mock_interp.get_input_details.return_value = [{"index": 0}, {"index": 1}]
        mock_interp.get_output_details.return_value = [{"index": 2}]
        mock_interp.get_tensor.return_value = fake_pred

        with mock.patch(
            "agriha.control.tide_forecaster._get_interpreter",
            return_value=mock_interp,
        ), mock.patch(
            "agriha.control.tide_forecaster.fetch_openmeteo_forecast",
            return_value=None,  # 失敗
        ):
            result = run_forecast(
                db_path=str(db_path),
                model_dir=str(model_dir),
                output_path=str(output_path),
            )

        assert "predictions" in result

    def test_denormalization_correct(self, tmp_path):
        """逆正規化: norm_pred * std + mean が正しく適用される。"""
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        self._make_norm_params(model_dir)
        (model_dir / "agriha_tide.tflite").write_bytes(b"dummy")

        db_path = tmp_path / "sensor_log.db"
        _make_db(str(db_path), [])
        output_path = tmp_path / "tide_forecast.json"

        # InAirTemp の norm 出力 = 1.0 → 25.0 + 3.0*1.0 = 28.0
        fake_pred = np.zeros((1, 6, 3), dtype=np.float32)
        fake_pred[0, :, 0] = 1.0  # InAirTemp norm=1.0 → real=25+3*1=28

        mock_interp = mock.MagicMock()
        mock_interp.get_input_details.return_value = [{"index": 0}, {"index": 1}]
        mock_interp.get_output_details.return_value = [{"index": 2}]
        mock_interp.get_tensor.return_value = fake_pred

        with mock.patch(
            "agriha.control.tide_forecaster._get_interpreter",
            return_value=mock_interp,
        ), mock.patch(
            "agriha.control.tide_forecaster.fetch_openmeteo_forecast",
            return_value=None,
        ):
            result = run_forecast(
                db_path=str(db_path),
                model_dir=str(model_dir),
                output_path=str(output_path),
            )

        for val in result["predictions"]["InAirTemp"]:
            assert abs(val - 28.0) < 0.01, f"期待値28.0、実際={val}"
