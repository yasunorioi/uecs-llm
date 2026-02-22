#!/usr/bin/env python3
"""
RS485 気象センサーサービス

Misol WH65LP互換センサーからデータを読み取り、MQTTでpublish

使用方法:
    python3 weather_sensor_service.py --port /dev/ttyUSB0 --mqtt-broker localhost

設定:
    環境変数または引数で設定可能
    - SERIAL_PORT: シリアルポート（デフォルト: /dev/ttyUSB0）
    - MQTT_BROKER: MQTTブローカーアドレス（デフォルト: localhost）
    - MQTT_PORT: MQTTポート（デフォルト: 1883）
    - SENSOR_INTERVAL: 読み取り間隔秒（デフォルト: 60）
"""

import argparse
import json
import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import paho.mqtt.client as mqtt
import serial

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('weather_sensor')


@dataclass
class WeatherData:
    """気象データ"""
    temperature: float      # 気温 (°C)
    humidity: float         # 湿度 (%)
    wind_speed: float       # 風速 (m/s)
    wind_direction: int     # 風向 (0-15, 16方位)
    rain_rate: float        # 降水強度 (mm/h)
    uv_index: float         # UVインデックス
    light: float            # 照度 (lux)
    timestamp: float        # タイムスタンプ


class MisolWH65Sensor:
    """Misol WH65LP互換センサードライバ"""

    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self.serial: Optional[serial.Serial] = None

    def connect(self) -> bool:
        """シリアルポートに接続"""
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=2,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            logger.info(f"Connected to {self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to {self.port}: {e}")
            return False

    def disconnect(self):
        """切断"""
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info("Disconnected")

    def read_data(self) -> Optional[WeatherData]:
        """センサーデータを読み取り"""
        if not self.serial or not self.serial.is_open:
            return None

        try:
            # バッファクリア
            self.serial.reset_input_buffer()

            # データ読み取り（センサーは定期的にデータを送信）
            # Misol WH65LPのプロトコルに合わせてパース
            data = self.serial.read(64)

            if len(data) < 20:
                logger.debug("Insufficient data received")
                return None

            # シンプルなパース例（実際のプロトコルに合わせて調整が必要）
            # Misolセンサーのデータフォーマットは製品によって異なる
            weather = self._parse_data(data)
            return weather

        except Exception as e:
            logger.error(f"Error reading data: {e}")
            return None

    def _parse_data(self, data: bytes) -> Optional[WeatherData]:
        """データパース（プロトコルに合わせて実装）"""
        try:
            # 例: 簡易パース（実際のプロトコルに合わせる）
            # ヘッダー検索
            header_idx = data.find(b'\xff\xff')
            if header_idx < 0:
                return None

            payload = data[header_idx + 2:]
            if len(payload) < 18:
                return None

            # データ抽出（仮のオフセット、実機で調整）
            temp_raw = struct.unpack('>h', payload[0:2])[0]
            humidity = payload[2]
            wind_speed_raw = struct.unpack('>H', payload[3:5])[0]
            wind_dir = payload[5]
            rain_raw = struct.unpack('>H', payload[6:8])[0]
            uv_raw = struct.unpack('>H', payload[8:10])[0]
            light_raw = struct.unpack('>I', b'\x00' + payload[10:13])[0]

            return WeatherData(
                temperature=temp_raw / 10.0,
                humidity=float(humidity),
                wind_speed=wind_speed_raw / 10.0,
                wind_direction=wind_dir,
                rain_rate=rain_raw / 10.0,
                uv_index=uv_raw / 10.0,
                light=float(light_raw),
                timestamp=time.time()
            )

        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None


class MQTTPublisher:
    """MQTTパブリッシャー"""

    TOPIC_BASE = "unipi-agri/weather"

    def __init__(self, broker: str, port: int = 1883):
        self.broker = broker
        self.port = port
        self.client = mqtt.Client(client_id="weather_sensor")
        self.connected = False

    def connect(self) -> bool:
        """MQTT接続"""
        try:
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()

            # 接続待ち
            timeout = 10
            while not self.connected and timeout > 0:
                time.sleep(0.5)
                timeout -= 0.5

            return self.connected
        except Exception as e:
            logger.error(f"MQTT connection failed: {e}")
            return False

    def disconnect(self):
        """切断"""
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info(f"Connected to MQTT broker {self.broker}")
            # Home Assistant MQTT Discovery
            self._publish_discovery()
        else:
            logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        logger.warning("Disconnected from MQTT broker")

    def _publish_discovery(self):
        """Home Assistant MQTT Discovery設定"""
        sensors = [
            ("temperature", "°C", "temperature"),
            ("humidity", "%", "humidity"),
            ("wind_speed", "m/s", None),
            ("wind_direction", "", None),
            ("rain_rate", "mm/h", None),
            ("uv_index", "", None),
            ("light", "lx", "illuminance"),
        ]

        for sensor_id, unit, device_class in sensors:
            config = {
                "name": f"Weather {sensor_id.replace('_', ' ').title()}",
                "state_topic": f"{self.TOPIC_BASE}/{sensor_id}",
                "unit_of_measurement": unit,
                "unique_id": f"unipi_weather_{sensor_id}",
                "device": {
                    "identifiers": ["unipi_weather_station"],
                    "name": "UniPi Weather Station",
                    "manufacturer": "Misol",
                    "model": "WH65LP"
                }
            }
            if device_class:
                config["device_class"] = device_class

            self.client.publish(
                f"homeassistant/sensor/unipi_weather_{sensor_id}/config",
                json.dumps(config),
                retain=True
            )

    def publish(self, data: WeatherData):
        """データをpublish"""
        if not self.connected:
            return

        # 個別トピックにpublish
        self.client.publish(f"{self.TOPIC_BASE}/temperature", data.temperature)
        self.client.publish(f"{self.TOPIC_BASE}/humidity", data.humidity)
        self.client.publish(f"{self.TOPIC_BASE}/wind_speed", data.wind_speed)
        self.client.publish(f"{self.TOPIC_BASE}/wind_direction", data.wind_direction)
        self.client.publish(f"{self.TOPIC_BASE}/rain_rate", data.rain_rate)
        self.client.publish(f"{self.TOPIC_BASE}/uv_index", data.uv_index)
        self.client.publish(f"{self.TOPIC_BASE}/light", data.light)

        # JSONで全データ
        self.client.publish(f"{self.TOPIC_BASE}/json", json.dumps(asdict(data)))

        logger.info(f"Published: temp={data.temperature}°C, humidity={data.humidity}%")


def main():
    parser = argparse.ArgumentParser(description='RS485 Weather Sensor Service')
    parser.add_argument('--port', default=os.getenv('SERIAL_PORT', '/dev/ttyUSB0'),
                        help='Serial port')
    parser.add_argument('--mqtt-broker', default=os.getenv('MQTT_BROKER', 'localhost'),
                        help='MQTT broker address')
    parser.add_argument('--mqtt-port', type=int, default=int(os.getenv('MQTT_PORT', '1883')),
                        help='MQTT port')
    parser.add_argument('--interval', type=int, default=int(os.getenv('SENSOR_INTERVAL', '60')),
                        help='Read interval in seconds')
    args = parser.parse_args()

    # シグナルハンドラ
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("Shutting down...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # センサー接続
    sensor = MisolWH65Sensor(args.port)
    if not sensor.connect():
        logger.error("Failed to connect to sensor")
        sys.exit(1)

    # MQTT接続
    mqtt_pub = MQTTPublisher(args.mqtt_broker, args.mqtt_port)
    if not mqtt_pub.connect():
        logger.error("Failed to connect to MQTT broker")
        sensor.disconnect()
        sys.exit(1)

    logger.info(f"Service started (interval: {args.interval}s)")

    # メインループ
    try:
        while running:
            data = sensor.read_data()
            if data:
                mqtt_pub.publish(data)
            time.sleep(args.interval)
    finally:
        sensor.disconnect()
        mqtt_pub.disconnect()
        logger.info("Service stopped")


if __name__ == '__main__':
    main()
