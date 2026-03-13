#!/usr/bin/env python3
"""
UART CO2 Reader Service

CDM7160/K30 UART CO2センサーから定期的にCO2濃度を読み取り、
MQTTブローカーにpublishするサービス。

Features:
- CDM7160/K30センサー対応
- MQTT自動再接続
- systemdサービスとして動作
- 設定ファイル（YAML）からパラメータ読み込み
- ロギング機能

Configuration:
    設定ファイル: /etc/agriha/uart_co2_reader.yaml

    sensor:
      type: "cdm7160"  # or "k30"
      port: "/dev/ttyAMA0"
      interval: 10  # 読み取り間隔（秒）

    mqtt:
      broker: "192.168.1.100"
      port: 1883
      username: null
      password: null
      topic: "agriha/sensors/co2"
      qos: 1
      client_id: "uart_co2_reader"

Usage:
    # 直接実行
    python uart_co2_reader.py --config /etc/agriha/uart_co2_reader.yaml

    # systemdサービスとして実行
    sudo systemctl start uart_co2_reader

Author: Ashigaru-4 (multi-agent-shogun)
Date: 2026-02-05
"""

import sys
import os
import time
import json
import yaml
import argparse
import logging
from pathlib import Path
from typing import Optional

# センサードライバをインポート（lib/パスを追加）
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.cdm7160 import CDM7160, CDM7160Error
from lib.k30 import K30, K30Error

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("Warning: paho-mqtt not installed. Install with: pip install paho-mqtt", file=sys.stderr)


class CO2ReaderService:
    """UART CO2読み取り→MQTT publishサービス"""

    def __init__(self, config_path: str):
        """
        サービス初期化

        Args:
            config_path: 設定ファイルパス（YAML）
        """
        self.config = self._load_config(config_path)
        self.logger = self._setup_logging()
        self.sensor = None
        self.mqtt_client = None
        self.running = False

    def _load_config(self, config_path: str) -> dict:
        """
        設定ファイル読み込み

        Args:
            config_path: YAMLファイルパス

        Returns:
            設定辞書
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            return config
        except Exception as e:
            print(f"Failed to load config from {config_path}: {e}", file=sys.stderr)
            sys.exit(1)

    def _setup_logging(self) -> logging.Logger:
        """
        ロギング設定

        Returns:
            ロガーオブジェクト
        """
        logger = logging.getLogger('uart_co2_reader')
        logger.setLevel(logging.INFO)

        # コンソールハンドラ
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def _init_sensor(self):
        """センサー初期化"""
        sensor_config = self.config.get('sensor', {})
        sensor_type = sensor_config.get('type', '').lower()
        port = sensor_config.get('port', '/dev/ttyAMA0')

        try:
            if sensor_type == 'cdm7160':
                self.sensor = CDM7160(port)
                self.logger.info(f"CDM7160 sensor initialized on {port}")
            elif sensor_type == 'k30':
                self.sensor = K30(port)
                self.logger.info(f"K30 sensor initialized on {port}")
            else:
                raise ValueError(f"Unknown sensor type: {sensor_type}")

        except Exception as e:
            self.logger.error(f"Failed to initialize sensor: {e}")
            raise

    def _init_mqtt(self):
        """MQTT接続初期化"""
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt is not installed")

        mqtt_config = self.config.get('mqtt', {})
        broker = mqtt_config.get('broker', 'localhost')
        port = mqtt_config.get('port', 1883)
        username = mqtt_config.get('username')
        password = mqtt_config.get('password')
        client_id = mqtt_config.get('client_id', 'uart_co2_reader')

        self.mqtt_client = mqtt.Client(client_id=client_id)

        # 認証設定
        if username and password:
            self.mqtt_client.username_pw_set(username, password)

        # コールバック設定
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        # 接続
        try:
            self.mqtt_client.connect(broker, port, keepalive=60)
            self.mqtt_client.loop_start()
            self.logger.info(f"MQTT client connected to {broker}:{port}")
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT broker: {e}")
            raise

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT接続時コールバック"""
        if rc == 0:
            self.logger.info("MQTT connected successfully")
        else:
            self.logger.error(f"MQTT connection failed with code {rc}")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT切断時コールバック"""
        if rc != 0:
            self.logger.warning(f"MQTT disconnected unexpectedly (code {rc}), reconnecting...")

    def _read_co2(self) -> Optional[int]:
        """
        CO2濃度を読み取る

        Returns:
            CO2濃度 (ppm)、エラー時はNone
        """
        try:
            co2_ppm = self.sensor.read_co2()
            return co2_ppm
        except (CDM7160Error, K30Error) as e:
            self.logger.error(f"Sensor read error: {e}")
            return None

    def _publish_co2(self, co2_ppm: int):
        """
        CO2濃度をMQTT publish

        Args:
            co2_ppm: CO2濃度 (ppm)
        """
        mqtt_config = self.config.get('mqtt', {})
        topic = mqtt_config.get('topic', 'agriha/sensors/co2')
        qos = mqtt_config.get('qos', 1)

        payload = json.dumps({
            'co2_ppm': co2_ppm,
            'timestamp': time.time(),
            'sensor': self.config['sensor']['type']
        })

        try:
            result = self.mqtt_client.publish(topic, payload, qos=qos)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.logger.info(f"Published: {topic} = {co2_ppm} ppm")
            else:
                self.logger.error(f"Publish failed with code {result.rc}")
        except Exception as e:
            self.logger.error(f"Publish error: {e}")

    def run(self):
        """サービスメインループ"""
        self.logger.info("Starting UART CO2 Reader Service")

        # 初期化
        self._init_sensor()
        self._init_mqtt()

        sensor_config = self.config.get('sensor', {})
        interval = sensor_config.get('interval', 10)

        self.running = True

        try:
            while self.running:
                # CO2読み取り
                co2_ppm = self._read_co2()

                if co2_ppm is not None:
                    # MQTT publish
                    self._publish_co2(co2_ppm)
                else:
                    self.logger.warning("Failed to read CO2, skipping publish")

                # 次の読み取りまで待機
                time.sleep(interval)

        except KeyboardInterrupt:
            self.logger.info("Interrupted by user")
        finally:
            self.stop()

    def stop(self):
        """サービス停止"""
        self.logger.info("Stopping UART CO2 Reader Service")
        self.running = False

        # センサークローズ
        if self.sensor:
            self.sensor.close()
            self.logger.info("Sensor closed")

        # MQTT切断
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.logger.info("MQTT disconnected")


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(description='UART CO2 Reader Service')
    parser.add_argument(
        '--config',
        type=str,
        default='/etc/agriha/uart_co2_reader.yaml',
        help='Path to configuration file (default: /etc/agriha/uart_co2_reader.yaml)'
    )
    args = parser.parse_args()

    # 設定ファイル存在チェック
    if not os.path.exists(args.config):
        print(f"Error: Configuration file not found: {args.config}", file=sys.stderr)
        print("\nExample configuration:", file=sys.stderr)
        print("""
sensor:
  type: "cdm7160"  # or "k30"
  port: "/dev/ttyAMA0"
  interval: 10

mqtt:
  broker: "192.168.1.100"
  port: 1883
  username: null
  password: null
  topic: "agriha/sensors/co2"
  qos: 1
  client_id: "uart_co2_reader"
        """, file=sys.stderr)
        sys.exit(1)

    # サービス起動
    service = CO2ReaderService(args.config)
    service.run()


if __name__ == '__main__':
    main()
