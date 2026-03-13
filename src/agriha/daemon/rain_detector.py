#!/usr/bin/env python3
"""
Rain Detector Service for AgriHA

UniPi DI経由の感雨センサー読み取りとMQTT publish。
Home Assistant MQTT Discovery対応。

Usage:
    python3 rain_detector.py [--config CONFIG] [--debug]
"""

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip3 install requests")
    sys.exit(1)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed. Run: pip3 install paho-mqtt")
    sys.exit(1)


# ===== Default Configuration =====
DEFAULT_CONFIG = {
    "evok": {
        "host": "localhost",
        "port": 8080,
        "version": "v2",  # v2 or v3
        "di_circuit": "1_01",  # Digital Input circuit (I01)
        "poll_interval": 5  # seconds
    },
    "mqtt": {
        "broker": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
        "client_id": "rain_detector",
        "base_topic": "agriha/sensor/rain",
        "discovery_prefix": "homeassistant"
    },
    "sensor": {
        "name": "Rain Detector",
        "device_id": "agriha_rain_01",
        "invert": False  # True if sensor is active-low
    }
}


# ===== Logging =====
LOG_DIR = Path("/var/log/agriha")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "rain_detector.log")
    ]
)
logger = logging.getLogger(__name__)


# ===== Data Classes =====
@dataclass
class SensorState:
    """Sensor state"""
    is_raining: bool
    raw_value: int
    timestamp: float


# ===== EVOK Client =====
class EvokClient:
    """EVOK REST API Client"""

    def __init__(self, host: str, port: int, version: str = "v2"):
        self.base_url = f"http://{host}:{port}"
        self.version = version
        # EVOK v2: input, v3: di
        self.di_endpoint = "input" if version == "v2" else "di"

    def get_digital_input(self, circuit: str) -> Optional[int]:
        """
        Get digital input value.

        Args:
            circuit: Circuit ID (e.g., "1_01")

        Returns:
            0 or 1, or None on error
        """
        url = f"{self.base_url}/rest/{self.di_endpoint}/{circuit}"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            return int(data.get("value", 0))
        except requests.RequestException as e:
            logger.error(f"EVOK API error: {e}")
            return None
        except (ValueError, KeyError) as e:
            logger.error(f"EVOK response parse error: {e}")
            return None

    def health_check(self) -> bool:
        """Check EVOK availability"""
        try:
            response = requests.get(f"{self.base_url}/rest/all", timeout=5)
            return response.status_code == 200
        except requests.RequestException:
            return False


# ===== MQTT Client =====
class MqttPublisher:
    """MQTT Publisher with HA Discovery support"""

    def __init__(self, config: dict):
        self.config = config
        self.client = mqtt.Client(
            client_id=config["client_id"],
            protocol=mqtt.MQTTv311
        )

        if config.get("username"):
            self.client.username_pw_set(
                config["username"],
                config.get("password")
            )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected")
            self.connected = True
        else:
            logger.error(f"MQTT connection failed: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        logger.warning(f"MQTT disconnected: {rc}")
        self.connected = False

    def connect(self) -> bool:
        """Connect to MQTT broker"""
        try:
            self.client.connect(
                self.config["broker"],
                self.config["port"],
                keepalive=60
            )
            self.client.loop_start()
            # Wait for connection
            for _ in range(10):
                if self.connected:
                    return True
                time.sleep(0.5)
            return False
        except Exception as e:
            logger.error(f"MQTT connect error: {e}")
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        """Publish message"""
        if not self.connected:
            logger.warning("MQTT not connected, skipping publish")
            return False
        try:
            result = self.client.publish(topic, payload, qos=1, retain=retain)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"MQTT publish error: {e}")
            return False

    def publish_discovery(self, sensor_config: dict, device_id: str):
        """
        Publish Home Assistant MQTT Discovery config.

        This creates a binary_sensor entity in Home Assistant.
        """
        discovery_topic = (
            f"{self.config['discovery_prefix']}/binary_sensor/"
            f"{device_id}/config"
        )

        discovery_payload = {
            "name": sensor_config["name"],
            "unique_id": device_id,
            "state_topic": f"{self.config['base_topic']}/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "moisture",  # rain = moisture
            "icon": "mdi:weather-rainy",
            "device": {
                "identifiers": [device_id],
                "name": "AgriHA Rain Sensor",
                "manufacturer": "AgriHA",
                "model": "Rain Detector v1"
            },
            "availability": {
                "topic": f"{self.config['base_topic']}/availability",
                "payload_available": "online",
                "payload_not_available": "offline"
            }
        }

        self.publish(
            discovery_topic,
            json.dumps(discovery_payload),
            retain=True
        )
        logger.info(f"HA Discovery published: {discovery_topic}")

    def publish_state(self, is_raining: bool):
        """Publish sensor state"""
        state = "ON" if is_raining else "OFF"
        self.publish(
            f"{self.config['base_topic']}/state",
            state,
            retain=True
        )

    def publish_availability(self, available: bool):
        """Publish availability status"""
        status = "online" if available else "offline"
        self.publish(
            f"{self.config['base_topic']}/availability",
            status,
            retain=True
        )


# ===== Rain Detector Service =====
class RainDetectorService:
    """Main service class"""

    def __init__(self, config: dict):
        self.config = config
        self.evok = EvokClient(
            host=config["evok"]["host"],
            port=config["evok"]["port"],
            version=config["evok"]["version"]
        )
        self.mqtt = MqttPublisher(config["mqtt"])
        self.running = False
        self.last_state: Optional[bool] = None

    def start(self):
        """Start the service"""
        logger.info("Starting Rain Detector Service")

        # Check EVOK
        if not self.evok.health_check():
            logger.error("EVOK not available, exiting")
            return False

        # Connect MQTT
        if not self.mqtt.connect():
            logger.error("MQTT connection failed, exiting")
            return False

        # Publish HA Discovery
        self.mqtt.publish_discovery(
            self.config["sensor"],
            self.config["sensor"]["device_id"]
        )

        # Publish availability
        self.mqtt.publish_availability(True)

        self.running = True
        return True

    def stop(self):
        """Stop the service"""
        logger.info("Stopping Rain Detector Service")
        self.running = False
        self.mqtt.publish_availability(False)
        self.mqtt.disconnect()

    def read_sensor(self) -> Optional[SensorState]:
        """Read sensor state from EVOK"""
        raw_value = self.evok.get_digital_input(
            self.config["evok"]["di_circuit"]
        )

        if raw_value is None:
            return None

        # Apply inversion if configured
        is_raining = bool(raw_value)
        if self.config["sensor"]["invert"]:
            is_raining = not is_raining

        return SensorState(
            is_raining=is_raining,
            raw_value=raw_value,
            timestamp=time.time()
        )

    def run(self):
        """Main polling loop"""
        poll_interval = self.config["evok"]["poll_interval"]
        error_count = 0
        max_errors = 10

        while self.running:
            state = self.read_sensor()

            if state is None:
                error_count += 1
                logger.warning(f"Sensor read failed ({error_count}/{max_errors})")
                if error_count >= max_errors:
                    logger.error("Too many errors, marking unavailable")
                    self.mqtt.publish_availability(False)
                    error_count = 0
            else:
                # Reset error count on success
                if error_count > 0:
                    error_count = 0
                    self.mqtt.publish_availability(True)

                # Publish on state change
                if self.last_state != state.is_raining:
                    status = "raining" if state.is_raining else "dry"
                    logger.info(f"State changed: {status} (raw={state.raw_value})")
                    self.mqtt.publish_state(state.is_raining)
                    self.last_state = state.is_raining

            time.sleep(poll_interval)


# ===== Signal Handlers =====
service: Optional[RainDetectorService] = None


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    if service:
        service.stop()
    sys.exit(0)


# ===== Main =====
def load_config(config_path: Optional[str]) -> dict:
    """Load configuration from file or use defaults"""
    config = DEFAULT_CONFIG.copy()

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                user_config = json.load(f)
            # Deep merge
            for key in user_config:
                if key in config and isinstance(config[key], dict):
                    config[key].update(user_config[key])
                else:
                    config[key] = user_config[key]
            logger.info(f"Config loaded: {config_path}")
        else:
            logger.warning(f"Config not found: {config_path}, using defaults")

    return config


def main():
    global service

    parser = argparse.ArgumentParser(description="Rain Detector Service")
    parser.add_argument("--config", help="Config file path (JSON)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Load config
    config = load_config(args.config)

    # Start service
    service = RainDetectorService(config)
    if service.start():
        service.run()
    else:
        logger.error("Service start failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
