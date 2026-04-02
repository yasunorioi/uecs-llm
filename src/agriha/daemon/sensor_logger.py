"""sensor_logger.py — MQTT subscriber → SQLite time-series logger.

Subscribes to sensor topics published by sensor_loop.py and ccm_receiver.py,
and persists each reading to sensor_log.db.

DB: /var/lib/agriha/sensor_log.db
Table: sensor_log (timestamp TEXT, source TEXT, metric TEXT, value REAL)

MQTT topics:
  agriha/+/sensor/DS18B20        → source=ds18b20, metric=temp_inside
  agriha/farm/weather/misol      → source=misol, multiple metrics
  agriha/+/ccm/sensor/InAirTemp  → source=ccm, metric=temp_inside
  agriha/+/ccm/sensor/InAirHumid → source=ccm, metric=humidity
  agriha/+/ccm/sensor/InAirCO2   → source=ccm, metric=co2
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

DB_PATH_DEFAULT = Path("/var/lib/agriha/sensor_log.db")

# Misol WH65LP payload keys → metric names (None values are skipped)
_MISOL_METRICS: dict[str, str] = {
    "temperature_c": "temp_outside",
    "humidity_pct": "humidity",
    "wind_speed_ms": "wind_speed",
    "gust_speed_ms": "gust_speed",
    "rainfall_mm": "rainfall",
    "uv_wm2": "uv_radiation",
    "light_lux": "solar_radiation",
    "wind_dir_deg": "wind_dir",
    "pressure_hpa": "pressure",
}

# UECS CCM sensor types → metric names
_CCM_METRICS: dict[str, str] = {
    "InAirTemp": "temp_inside",
    "InAirHumid": "humidity",
    "InAirCO2": "co2",
}


class SensorLogger:
    """MQTT subscriber that persists sensor readings to SQLite.

    Args:
        broker:    MQTT broker hostname (default: localhost)
        port:      MQTT broker port (default: 1883)
        house_id:  AgriHA house ID (default: h01)
        db_path:   Path to SQLite database
        client_id: MQTT client ID
        keepalive: MQTT keepalive seconds
    """

    def __init__(
        self,
        broker: str = "localhost",
        port: int = 1883,
        house_id: str = "h01",
        db_path: Path = DB_PATH_DEFAULT,
        client_id: str = "agriha-sensor-logger",
        keepalive: int = 60,
    ) -> None:
        self._broker = broker
        self._port = port
        self._house_id = house_id
        self._db_path = Path(db_path)
        self._keepalive = keepalive

        self._client = mqtt.Client(client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        self._db_conn: sqlite3.Connection | None = None
        self._running = False

    # ------------------------------------------------------------------ #
    # DB
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        """Create sensor_log table and index if not exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_log (
                timestamp TEXT NOT NULL,
                source    TEXT NOT NULL,
                metric    TEXT NOT NULL,
                value     REAL NOT NULL
            )
        """)
        self._db_conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sensor_log_ts
            ON sensor_log (timestamp)
        """)
        self._db_conn.commit()
        logger.info("DB initialized: %s", self._db_path)

    def _insert(self, timestamp: str, source: str, metric: str, value: float) -> None:
        """Insert a single sensor reading into sensor_log."""
        if self._db_conn is None:
            return
        try:
            self._db_conn.execute(
                "INSERT INTO sensor_log (timestamp, source, metric, value) VALUES (?, ?, ?, ?)",
                (timestamp, source, metric, float(value)),
            )
            self._db_conn.commit()
            logger.debug("insert: %s %s/%s = %s", timestamp, source, metric, value)
        except sqlite3.Error as e:
            logger.error("DB insert error: %s", e)

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Connect to MQTT broker and start background loop."""
        self._init_db()
        logger.info("Connecting to MQTT broker %s:%d", self._broker, self._port)
        self._client.connect(self._broker, self._port, keepalive=self._keepalive)
        self._client.loop_start()
        self._running = True

    def disconnect(self) -> None:
        """Stop MQTT loop and close DB connection."""
        self._running = False
        self._client.loop_stop()
        self._client.disconnect()
        if self._db_conn:
            self._db_conn.close()
            self._db_conn = None
        logger.info("SensorLogger disconnected")

    # ------------------------------------------------------------------ #
    # paho callbacks
    # ------------------------------------------------------------------ #

    def _on_connect(self, client: mqtt.Client, userdata: object, flags: dict, rc: int) -> None:
        if rc != 0:
            logger.error("MQTT connection failed: rc=%d", rc)
            return

        topics = [
            ("agriha/+/sensor/DS18B20", 0),
            ("agriha/farm/weather/misol", 0),
            ("agriha/+/ccm/sensor/InAirTemp", 0),
            ("agriha/+/ccm/sensor/InAirHumid", 0),
            ("agriha/+/ccm/sensor/InAirCO2", 0),
        ]
        client.subscribe(topics)
        logger.info(
            "MQTT connected. Subscribed: %s",
            ", ".join(t for t, _ in topics),
        )

    def _on_disconnect(self, client: mqtt.Client, userdata: object, rc: int) -> None:
        if rc != 0:
            logger.warning("MQTT unexpected disconnect: rc=%d", rc)

    def _on_message(self, client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        """Route incoming MQTT message to the appropriate parser."""
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Failed to parse payload [%s]: %s", msg.topic, e)
            return

        topic = msg.topic
        parts = topic.split("/")

        # agriha/{house_id}/sensor/DS18B20
        if len(parts) >= 4 and parts[2] == "sensor" and parts[3] == "DS18B20":
            self._handle_ds18b20(payload)

        # agriha/farm/weather/misol
        elif topic == "agriha/farm/weather/misol":
            self._handle_misol(payload)

        # agriha/{house_id}/ccm/sensor/{ccm_type}
        elif len(parts) >= 5 and parts[2] == "ccm" and parts[3] == "sensor":
            self._handle_ccm_sensor(parts[4], payload)

    # ------------------------------------------------------------------ #
    # Parsers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _handle_ds18b20(self, payload: dict) -> None:
        """DS18B20: temperature_c → source=ds18b20, metric=temp_inside."""
        temp = payload.get("temperature_c")
        if temp is None:
            return
        self._insert(self._now_iso(), "ds18b20", "temp_inside", float(temp))

    def _handle_misol(self, payload: dict) -> None:
        """Misol WH65LP: multiple metrics → source=misol."""
        ts = self._now_iso()
        for key, metric in _MISOL_METRICS.items():
            value = payload.get(key)
            if value is None:
                continue
            try:
                self._insert(ts, "misol", metric, float(value))
            except (TypeError, ValueError):
                logger.debug("Misol: skip non-numeric %s = %r", key, value)

    def _handle_ccm_sensor(self, ccm_type: str, payload: dict) -> None:
        """CCM sensor: InAirTemp/InAirHumid/InAirCO2 → source=ccm."""
        metric = _CCM_METRICS.get(ccm_type)
        if metric is None:
            return
        value = payload.get("value")
        if value is None:
            return
        try:
            self._insert(self._now_iso(), "ccm", metric, float(value))
        except (TypeError, ValueError):
            logger.debug("CCM: skip non-numeric %s = %r", ccm_type, value)


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> None:
    _setup_logging()
    broker = os.environ.get("MQTT_BROKER", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    house_id = os.environ.get("HOUSE_ID", "h01")
    db_path = Path(os.environ.get("SENSOR_LOG_DB", str(DB_PATH_DEFAULT)))

    logger.info(
        "SensorLogger starting: broker=%s:%d house=%s db=%s",
        broker, port, house_id, db_path,
    )

    sl = SensorLogger(
        broker=broker,
        port=port,
        house_id=house_id,
        db_path=db_path,
    )

    def _shutdown(signum: int, frame: object) -> None:
        logger.info("Signal %d received, shutting down...", signum)
        sl.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    sl.connect()

    try:
        while sl._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sl.disconnect()


if __name__ == "__main__":
    main()
