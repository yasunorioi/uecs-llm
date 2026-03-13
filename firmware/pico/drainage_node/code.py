# Drainage Sensor Node - MQTT Version
# Platform: CircuitPython
# Hardware: W5500-EVB-Pico-PoE + DFRobot SEN0575
# Author: Arsprout Analysis Team
# Date: 2026-02-05

import board
import busio
import time
import json
import os
import microcontroller
from digitalio import DigitalInOut
from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
import adafruit_wiznet5k.adafruit_wiznet5k_socket as socket
import adafruit_minimqtt.adafruit_minimqtt as MQTT

# Import sensor driver
import sys
sys.path.append('/lib')
from dfrobot_rainfall import DFRobot_RainfallSensor, calculate_rate, format_rainfall_json

# ========================================
# Configuration (from settings.toml)
# ========================================
HOUSE_ID = os.getenv("HOUSE_ID", "h1")
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.10")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

# Network configuration
STATIC_IP = tuple(map(int, os.getenv("STATIC_IP", "192.168.1.70").split(".")))
SUBNET = tuple(map(int, os.getenv("SUBNET", "255.255.255.0").split(".")))
GATEWAY = tuple(map(int, os.getenv("GATEWAY", "192.168.1.1").split(".")))
DNS = tuple(map(int, os.getenv("DNS", "8.8.8.8").split(".")))

# Sensor configuration
I2C_SDA_PIN = board.GP4
I2C_SCL_PIN = board.GP5
SENSOR_I2C_ADDR = int(os.getenv("SENSOR_I2C_ADDR", "0x1D"), 16)

# MQTT topics
TOPIC_BASE = f"greenhouse/{HOUSE_ID}/drainage"
TOPIC_AMOUNT = f"{TOPIC_BASE}/amount"
TOPIC_RATE = f"{TOPIC_BASE}/rate"
TOPIC_COUNT = f"{TOPIC_BASE}/count"
TOPIC_STATUS = f"{TOPIC_BASE}/status"
TOPIC_RESET = f"{TOPIC_BASE}/reset"
TOPIC_CONFIG = f"homeassistant/sensor/{HOUSE_ID}_drainage/config"

# Timing configuration
PUBLISH_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "600"))  # 10 minutes default
RESET_HOUR = 0  # Daily reset at midnight

# ========================================
# Hardware Initialization
# ========================================
print("=" * 50)
print("Drainage Sensor Node - MQTT Version")
print("=" * 50)

# W5500 Ethernet initialization
print("Initializing W5500 Ethernet...")
cs = DigitalInOut(board.GP17)
spi = busio.SPI(board.GP18, MOSI=board.GP19, MISO=board.GP16)
eth = WIZNET5K(spi, cs, is_dhcp=False)
eth.ifconfig = (STATIC_IP, SUBNET, GATEWAY, DNS)

print(f"IP Address: {eth.pretty_ip(eth.ip_address)}")
print(f"Subnet Mask: {eth.pretty_ip(eth.subnet_mask)}")
print(f"Gateway: {eth.pretty_ip(eth.gateway_address)}")

# Set socket interface
socket.set_interface(eth)

# I2C initialization for sensor
print("Initializing I2C...")
i2c = busio.I2C(I2C_SCL_PIN, I2C_SDA_PIN)

# Sensor initialization
print(f"Initializing DFRobot Rainfall Sensor (I2C: 0x{SENSOR_I2C_ADDR:02X})...")
sensor = DFRobot_RainfallSensor(i2c, address=SENSOR_I2C_ADDR)

if not sensor.begin():
    print("ERROR: Sensor initialization failed!")
    print("Check I2C connections and sensor power.")
    raise RuntimeError("Sensor init failed")

print("Sensor initialized successfully!")

# ========================================
# MQTT Setup
# ========================================
print(f"Setting up MQTT client (Broker: {MQTT_BROKER}:{MQTT_PORT})...")


def on_connect(client, userdata, flags, rc):
    """MQTT connection callback."""
    print(f"Connected to MQTT broker! (RC: {rc})")
    # Subscribe to reset command
    client.subscribe(TOPIC_RESET)
    # Publish status as online
    client.publish(TOPIC_STATUS, "online", retain=True)


def on_disconnect(client, userdata, rc):
    """MQTT disconnection callback."""
    print(f"Disconnected from MQTT broker! (RC: {rc})")


def on_message(client, topic, message):
    """MQTT message callback (for subscribed topics)."""
    print(f"Message on {topic}: {message}")
    if topic == TOPIC_RESET:
        if message.lower() in ["reset", "1", "true"]:
            print("Reset command received!")
            # Sensor reset requires power cycle
            microcontroller.reset()


# Create MQTT client
mqtt_client = MQTT.MQTT(
    broker=MQTT_BROKER,
    port=MQTT_PORT,
    username=MQTT_USER if MQTT_USER else None,
    password=MQTT_PASS if MQTT_PASS else None,
    socket_pool=socket,
    is_ssl=False,
    keep_alive=60
)

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message

# Set Last Will and Testament (LWT)
mqtt_client.will_set(TOPIC_STATUS, "offline", retain=True)

# Connect to MQTT broker
print("Connecting to MQTT broker...")
try:
    mqtt_client.connect()
except Exception as e:
    print(f"MQTT connection failed: {e}")
    print("Retrying in 5 seconds...")
    time.sleep(5)
    mqtt_client.connect()

# ========================================
# Home Assistant MQTT Discovery
# ========================================


def publish_ha_discovery():
    """Publish Home Assistant MQTT Discovery configuration."""
    print("Publishing Home Assistant MQTT Discovery...")

    # Drainage amount sensor
    config_amount = {
        "name": f"{HOUSE_ID.upper()} Drainage Amount",
        "state_topic": TOPIC_AMOUNT,
        "unit_of_measurement": "mm",
        "device_class": "precipitation",
        "value_template": "{{ value_json.amount_mm }}",
        "unique_id": f"{HOUSE_ID}_drainage_amount",
        "device": {
            "identifiers": [f"{HOUSE_ID}_drainage_sensor"],
            "name": f"{HOUSE_ID.upper()} Drainage Sensor",
            "model": "DFRobot SEN0575",
            "manufacturer": "DFRobot"
        }
    }

    # Drainage rate sensor
    config_rate = {
        "name": f"{HOUSE_ID.upper()} Drainage Rate",
        "state_topic": TOPIC_RATE,
        "unit_of_measurement": "mm/min",
        "value_template": "{{ value_json.rate_mm_min }}",
        "unique_id": f"{HOUSE_ID}_drainage_rate",
        "device": {
            "identifiers": [f"{HOUSE_ID}_drainage_sensor"],
            "name": f"{HOUSE_ID.upper()} Drainage Sensor",
            "model": "DFRobot SEN0575",
            "manufacturer": "DFRobot"
        }
    }

    # Publish discovery messages
    mqtt_client.publish(
        f"homeassistant/sensor/{HOUSE_ID}_drainage_amount/config",
        json.dumps(config_amount),
        retain=True
    )
    mqtt_client.publish(
        f"homeassistant/sensor/{HOUSE_ID}_drainage_rate/config",
        json.dumps(config_rate),
        retain=True
    )

    print("Home Assistant MQTT Discovery published!")


# Publish HA discovery on startup
publish_ha_discovery()

# ========================================
# Main Loop
# ========================================
print("=" * 50)
print("Starting main loop...")
print(f"Publish interval: {PUBLISH_INTERVAL} seconds")
print("=" * 50)

last_publish_time = time.monotonic()
last_reset_day = time.localtime().tm_mday
previous_rainfall = 0.0

while True:
    try:
        # MQTT loop (process incoming messages)
        mqtt_client.loop()

        # Check for daily reset
        current_time = time.localtime()
        if current_time.tm_mday != last_reset_day and current_time.tm_hour == RESET_HOUR:
            print(f"Daily reset triggered (Day: {current_time.tm_mday})")
            last_reset_day = current_time.tm_mday
            previous_rainfall = 0.0
            # Note: Sensor reset requires power cycle or specific command

        # Check if it's time to publish
        current_time_mono = time.monotonic()
        if current_time_mono - last_publish_time >= PUBLISH_INTERVAL:
            print(f"\n[{time.localtime()}] Reading sensor...")

            # Read sensor data
            total_rainfall = sensor.get_rainfall()
            tip_count = sensor.get_raw_data()
            working_time = sensor.get_working_time()

            # Calculate interval rainfall
            interval_rainfall = total_rainfall - previous_rainfall
            previous_rainfall = total_rainfall

            # Calculate rate
            rate = calculate_rate(interval_rainfall, PUBLISH_INTERVAL)

            print(f"  Total: {total_rainfall:.2f} mm")
            print(f"  Interval: {interval_rainfall:.2f} mm")
            print(f"  Rate: {rate:.2f} mm/min")
            print(f"  Tip count: {tip_count}")
            print(f"  Working time: {working_time:.1f} hours")

            # Format timestamp
            timestamp = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
                current_time.tm_year,
                current_time.tm_mon,
                current_time.tm_mday,
                current_time.tm_hour,
                current_time.tm_min,
                current_time.tm_sec
            )

            # Publish to MQTT
            payload_amount = json.dumps({
                "timestamp": timestamp,
                "amount_mm": round(interval_rainfall, 2),
                "total_mm": round(total_rainfall, 2),
                "interval_sec": PUBLISH_INTERVAL
            })

            payload_rate = json.dumps({
                "timestamp": timestamp,
                "rate_mm_min": round(rate, 2)
            })

            payload_count = json.dumps({
                "timestamp": timestamp,
                "count": tip_count
            })

            mqtt_client.publish(TOPIC_AMOUNT, payload_amount)
            mqtt_client.publish(TOPIC_RATE, payload_rate)
            mqtt_client.publish(TOPIC_COUNT, payload_count)

            print("  Published to MQTT successfully!")

            last_publish_time = current_time_mono

        # Short sleep to prevent CPU spinning
        time.sleep(1)

    except Exception as e:
        print(f"ERROR in main loop: {e}")
        time.sleep(5)
        # Attempt to reconnect MQTT if disconnected
        try:
            if not mqtt_client.is_connected():
                print("Reconnecting to MQTT...")
                mqtt_client.reconnect()
        except Exception:
            pass
