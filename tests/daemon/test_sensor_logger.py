"""Tests for sensor_logger.py — MQTT message → SQLite insert.

paho-mqtt が未インストール環境でも実行できるよう unittest.mock でモックする。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# paho.mqtt.client を mock として登録してからインポート
sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())

from agriha.daemon.sensor_logger import SensorLogger  # noqa: E402


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _make_msg(topic: str, payload: dict) -> MagicMock:
    """Create a mock MQTTMessage."""
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload).encode()
    return msg


def _get_rows(db_path: Path) -> list[dict]:
    """Fetch all rows from sensor_log ordered by timestamp."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, source, metric, value FROM sensor_log ORDER BY rowid"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def sl(tmp_path):
    """SensorLogger with a temporary DB."""
    instance = SensorLogger(db_path=tmp_path / "sensor_log.db")
    instance._init_db()
    return instance


@pytest.fixture
def db_path(sl):
    return sl._db_path


# ------------------------------------------------------------------ #
# DS18B20
# ------------------------------------------------------------------ #


class TestDS18B20:
    def test_inserts_temp_inside(self, sl, db_path):
        """DS18B20 メッセージ → source=ds18b20, metric=temp_inside で挿入される。"""
        msg = _make_msg(
            "agriha/h01/sensor/DS18B20",
            {"device_id": "28-abc123", "temperature_c": 23.5, "timestamp": 1700000000.0},
        )
        sl._on_message(None, None, msg)

        rows = _get_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["source"] == "ds18b20"
        assert rows[0]["metric"] == "temp_inside"
        assert rows[0]["value"] == pytest.approx(23.5)

    def test_wildcard_house_id_accepted(self, sl, db_path):
        """任意の house_id を含むトピックでも挿入される。"""
        msg = _make_msg(
            "agriha/h02/sensor/DS18B20",
            {"device_id": "28-xyz", "temperature_c": 18.0, "timestamp": 1700000000.0},
        )
        sl._on_message(None, None, msg)

        rows = _get_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["metric"] == "temp_inside"

    def test_skips_none_temperature(self, sl, db_path):
        """temperature_c が None の場合は挿入しない。"""
        msg = _make_msg(
            "agriha/h01/sensor/DS18B20",
            {"device_id": "28-abc", "temperature_c": None, "timestamp": 1700000000.0},
        )
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []

    def test_skips_missing_temperature(self, sl, db_path):
        """temperature_c キーがない場合は挿入しない。"""
        msg = _make_msg(
            "agriha/h01/sensor/DS18B20",
            {"device_id": "28-abc", "timestamp": 1700000000.0},
        )
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []


# ------------------------------------------------------------------ #
# Misol
# ------------------------------------------------------------------ #


class TestMisol:
    def test_inserts_multiple_metrics(self, sl, db_path):
        """Misol メッセージ → source=misol, 複数 metric が挿入される。"""
        payload = {
            "temperature_c": 12.3,
            "humidity_pct": 60,
            "wind_speed_ms": 1.5,
            "rainfall_mm": 0.3,
            "light_lux": 5000.0,
            "timestamp": 1700000000.0,
        }
        sl._on_message(None, None, _make_msg("agriha/farm/weather/misol", payload))

        rows = _get_rows(db_path)
        metrics = {r["metric"]: r["value"] for r in rows}
        assert metrics["temp_outside"] == pytest.approx(12.3)
        assert metrics["humidity"] == pytest.approx(60)
        assert metrics["wind_speed"] == pytest.approx(1.5)
        assert metrics["rainfall"] == pytest.approx(0.3)
        assert metrics["solar_radiation"] == pytest.approx(5000.0)
        assert all(r["source"] == "misol" for r in rows)

    def test_skips_none_values(self, sl, db_path):
        """Misol のうち None のフィールドは挿入しない。"""
        payload = {
            "temperature_c": 10.0,
            "humidity_pct": None,   # 無効値
            "wind_speed_ms": None,
            "timestamp": 1700000000.0,
        }
        sl._on_message(None, None, _make_msg("agriha/farm/weather/misol", payload))

        rows = _get_rows(db_path)
        metrics = {r["metric"] for r in rows}
        assert "temp_outside" in metrics
        assert "humidity" not in metrics
        assert "wind_speed" not in metrics

    def test_all_none_inserts_nothing(self, sl, db_path):
        """全フィールドが None の場合は一行も挿入しない。"""
        payload = {k: None for k in ["temperature_c", "humidity_pct", "wind_speed_ms"]}
        sl._on_message(None, None, _make_msg("agriha/farm/weather/misol", payload))
        assert _get_rows(db_path) == []


# ------------------------------------------------------------------ #
# CCM
# ------------------------------------------------------------------ #


class TestCCM:
    def test_inairtemp_inserts_temp_inside(self, sl, db_path):
        """CCM InAirTemp → source=ccm, metric=temp_inside。"""
        msg = _make_msg(
            "agriha/h01/ccm/sensor/InAirTemp",
            {"ccm_type": "InAirTemp", "value": 25.1, "room": 1},
        )
        sl._on_message(None, None, msg)

        rows = _get_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["source"] == "ccm"
        assert rows[0]["metric"] == "temp_inside"
        assert rows[0]["value"] == pytest.approx(25.1)

    def test_inairhumid_inserts_humidity(self, sl, db_path):
        """CCM InAirHumid → source=ccm, metric=humidity。"""
        msg = _make_msg(
            "agriha/h01/ccm/sensor/InAirHumid",
            {"ccm_type": "InAirHumid", "value": 72.5, "room": 1},
        )
        sl._on_message(None, None, msg)

        rows = _get_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["metric"] == "humidity"
        assert rows[0]["value"] == pytest.approx(72.5)

    def test_inaircco2_inserts_co2(self, sl, db_path):
        """CCM InAirCO2 → source=ccm, metric=co2。"""
        msg = _make_msg(
            "agriha/h01/ccm/sensor/InAirCO2",
            {"ccm_type": "InAirCO2", "value": 415.0, "room": 1},
        )
        sl._on_message(None, None, msg)

        rows = _get_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["metric"] == "co2"
        assert rows[0]["value"] == pytest.approx(415.0)

    def test_unknown_ccm_type_ignored(self, sl, db_path):
        """未知の CCM タイプ（SoilTemp 等）は無視される。"""
        msg = _make_msg(
            "agriha/h01/ccm/sensor/SoilTemp",
            {"ccm_type": "SoilTemp", "value": 18.0, "room": 1},
        )
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []

    def test_ccm_actuator_topic_ignored(self, sl, db_path):
        """actuator トピック（ccm/actuator/...）は無視される。"""
        msg = _make_msg(
            "agriha/h01/ccm/actuator/Irri",
            {"ccm_type": "Irri", "value": 1, "room": 1},
        )
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []


# ------------------------------------------------------------------ #
# Edge cases
# ------------------------------------------------------------------ #


class TestEdgeCases:
    def test_invalid_json_payload_does_not_crash(self, sl, db_path):
        """不正な JSON ペイロードは無視され、DB に挿入しない。"""
        msg = MagicMock()
        msg.topic = "agriha/h01/sensor/DS18B20"
        msg.payload = b"not-json{"
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []

    def test_unrelated_topic_ignored(self, sl, db_path):
        """無関係のトピック（relay/state 等）は無視される。"""
        msg = _make_msg(
            "agriha/h01/relay/state",
            {"ch1": 0, "ts": 1700000000},
        )
        sl._on_message(None, None, msg)
        assert _get_rows(db_path) == []

    def test_multiple_messages_accumulate(self, sl, db_path):
        """複数メッセージが蓄積される。"""
        sl._on_message(None, None, _make_msg(
            "agriha/h01/sensor/DS18B20",
            {"temperature_c": 20.0, "timestamp": 1700000000.0},
        ))
        sl._on_message(None, None, _make_msg(
            "agriha/farm/weather/misol",
            {"temperature_c": 10.0, "timestamp": 1700000001.0},
        ))
        sl._on_message(None, None, _make_msg(
            "agriha/h01/ccm/sensor/InAirCO2",
            {"ccm_type": "InAirCO2", "value": 400.0},
        ))

        rows = _get_rows(db_path)
        sources = [r["source"] for r in rows]
        assert "ds18b20" in sources
        assert "misol" in sources
        assert "ccm" in sources

    def test_db_schema_has_required_columns(self, sl, db_path):
        """sensor_log テーブルに必須カラム (timestamp, source, metric, value) が存在する。"""
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sensor_log)").fetchall()}
        conn.close()
        assert {"timestamp", "source", "metric", "value"} <= cols
