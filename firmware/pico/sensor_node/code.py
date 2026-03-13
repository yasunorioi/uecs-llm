"""
W5500-EVB-Pico2 センサーノード ファームウェア
E2E動作確認済み: 2026-02-05

センサー:
- SHT40 (0x44): 温湿度
- BMP280 (0x76): 気圧/温度
- SCD41 (0x62): CO2/温湿度

通信:
- W5500 Ethernet (PoE給電)
- MQTT (Mosquitto)
"""

import board
import busio
import digitalio
import json
import time

# Sensor libraries
import adafruit_sht4x
import adafruit_bmp280

# Network libraries
from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
from adafruit_wiznet5k.adafruit_wiznet5k_socketpool import SocketPool
import adafruit_minimqtt.adafruit_minimqtt as MQTT

# Configuration
MQTT_BROKER = "192.168.15.14"
MQTT_PORT = 1883
MQTT_TOPIC = "greenhouse/sensors"
PUBLISH_INTERVAL = 60  # seconds

# I2C pins (Grove Shield)
I2C_SDA = board.GP8
I2C_SCL = board.GP9

# W5500 SPI pins
SPI_SCK = board.GP18
SPI_MOSI = board.GP19
SPI_MISO = board.GP16
W5500_CS = board.GP17
W5500_RST = board.GP20


def read_sensors(i2c):
    """Read all sensors and return dict"""
    data = {}

    # SHT40 - Temperature & Humidity
    try:
        sht = adafruit_sht4x.SHT4x(i2c)
        temp, hum = sht.measurements
        data["temp"] = round(temp, 1)
        data["hum"] = round(hum, 1)
        print(f"SHT40: {temp:.1f}C, {hum:.1f}%")
    except Exception as e:
        print(f"SHT40 error: {e}")

    # BMP280 - Pressure & Temperature
    try:
        bmp = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=0x76)
        data["press"] = round(bmp.pressure, 1)
        data["temp_bmp"] = round(bmp.temperature, 1)
        print(f"BMP280: {bmp.temperature:.1f}C, {bmp.pressure:.1f}hPa")
    except Exception as e:
        print(f"BMP280 error: {e}")

    return data


def setup_ethernet():
    """Initialize W5500 Ethernet"""
    spi = busio.SPI(SPI_SCK, MOSI=SPI_MOSI, MISO=SPI_MISO)
    cs = digitalio.DigitalInOut(W5500_CS)
    rst = digitalio.DigitalInOut(W5500_RST)

    eth = WIZNET5K(spi, cs, reset=rst, is_dhcp=True)
    print(f"IP: {eth.pretty_ip(eth.ip_address)}")

    return eth


def setup_mqtt(eth):
    """Initialize MQTT client"""
    pool = SocketPool(eth)
    mqtt = MQTT.MQTT(
        broker=MQTT_BROKER,
        port=MQTT_PORT,
        socket_pool=pool
    )
    return mqtt


def main():
    print("=" * 40)
    print("Sensor Node Starting...")
    print("=" * 40)

    # Setup Ethernet first
    eth = setup_ethernet()
    mqtt = setup_mqtt(eth)

    # Connect to MQTT broker
    print(f"Connecting to MQTT broker {MQTT_BROKER}...")
    mqtt.connect()
    print("MQTT connected!")

    while True:
        try:
            # Setup I2C (need to init each time due to SPI conflict)
            i2c = busio.I2C(I2C_SCL, I2C_SDA)

            # Read sensors
            data = read_sensors(i2c)

            # Release I2C
            i2c.deinit()

            # Publish to MQTT
            if data:
                payload = json.dumps(data)
                mqtt.publish(MQTT_TOPIC, payload)
                print(f"Published: {payload}")

            # Wait for next cycle
            time.sleep(PUBLISH_INTERVAL)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
