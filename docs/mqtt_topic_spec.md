# MQTT Topic Specification for Greenhouse IoT System

> **Version**: 1.1.0
> **Last Updated**: 2026-02-21
> **System**: unipi-agri-ha (Greenhouse Monitoring & Control)
> **Protocol**: MQTT v3.1.1
> **Broker**: Mosquitto (port 1883)

---

## Table of Contents

1. [Overview](#overview)
2. [Topic Naming Convention](#topic-naming-convention)
3. [House ID Naming Convention](#house-id-naming-convention)
4. [JSON Payload Specification](#json-payload-specification)
5. [Sensor Types](#sensor-types)
6. [Device Types](#device-types)
7. [Actuator Types](#actuator-types)
8. [QoS Recommendations](#qos-recommendations)
9. [Telegraf Configuration Notes](#telegraf-configuration-notes)
10. [Examples](#examples)

---

## Overview

This document defines the MQTT topic structure and payload format for the greenhouse IoT system. The system uses a unified MQTT-based architecture for sensor data collection, actuator control, and alert notifications.

**Key Principles**:
- **JSON-only payloads**: All messages use JSON format (no plain values)
- **Hierarchical topics**: Topics follow a greenhouse/house/entity structure
- **ISO 8601 timestamps**: All timestamps use ISO 8601 format with timezone
- **UTF-8 encoding**: All string data uses UTF-8 encoding

---

## Topic Naming Convention

### 1. Sensor Data Topics

#### Single-Value Sensors
For sensors that report a single measurement value:

```
greenhouse/{house_id}/sensor/{type}
```

**Examples**:
- `greenhouse/h1/sensor/temperature`
- `greenhouse/h2/sensor/humidity`
- `greenhouse/h3/sensor/co2`

#### Multi-Value Sensors (Device-Based)
For sensors that report multiple values (e.g., SHT31 reports both temperature and humidity):

```
greenhouse/{house_id}/sensor/{device}/state
```

**Examples**:
- `greenhouse/h1/sensor/sht/state` (temperature + humidity)
- `greenhouse/h1/sensor/scd/state` (CO2 + temperature + humidity)
- `greenhouse/h1/sensor/bmp280/state` (pressure + temperature)

### 2. Actuator Control Topics

#### Command Topic (Control)
For sending commands to actuators:

```
greenhouse/{house_id}/actuator/{type}/command
```

**Examples**:
- `greenhouse/h1/actuator/irrigation/command`
- `greenhouse/h1/actuator/ventilation/command`
- `greenhouse/h1/actuator/heater/command`

#### State Topic (Feedback)
For actuator state reporting:

```
greenhouse/{house_id}/actuator/{type}/state
```

**Examples**:
- `greenhouse/h1/actuator/irrigation/state`
- `greenhouse/h1/actuator/ventilation/state`
- `greenhouse/h1/actuator/heater/state`

### 3. Node Status Topics

For node health monitoring and Last Will Testament (LWT):

```
greenhouse/{house_id}/status
```

**Example**:
- `greenhouse/h1/status` (node alive/dead)

### 4. Alert Topics

For alert notifications (sensor anomalies, system errors):

```
greenhouse/{house_id}/alert
```

**Example**:
- `greenhouse/h1/alert` (high temperature, sensor failure)

### 5. System-Wide Topics

For system-level messages that affect all houses:

```
greenhouse/system/{type}
```

**Examples**:
- `greenhouse/system/alert` (global alerts)
- `greenhouse/system/config` (configuration updates)
- `greenhouse/system/ota` (OTA update notifications)

---

## House ID Naming Convention

House IDs follow a simple numeric pattern:

| House ID | Description |
|----------|-------------|
| `h1` | House 1 |
| `h2` | House 2 |
| `h3` | House 3 |
| ... | ... |
| `h99` | House 99 |

**Format**: `h{N}` where N is an integer (1-99)

**Special Cases**:
- `test` - Test environment nodes
- `lab` - Laboratory/development nodes

---

## JSON Payload Specification

### Sensor Data Payload

#### Single-Value Sensor

```json
{
  "value": 25.5,
  "unit": "℃",
  "sensor_type": "temperature",
  "house_id": "h1",
  "source": "pico_node_01",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- `value` (number): Sensor reading value
- `sensor_type` (string): Type of sensor (see [Sensor Types](#sensor-types))
- `house_id` (string): House identifier
- `source` (string): Node/device identifier
- `timestamp` (string): ISO 8601 timestamp with timezone

**Optional Fields**:
- `unit` (string): Unit of measurement (e.g., "℃", "%", "ppm")
- `location` (string): Physical location within house (e.g., "indoor", "outdoor")
- `room` (string): Room number (e.g., "1", "2")
- `region` (string): Region within room (e.g., "north", "south")
- `order` (string): Sensor order/position

#### Multi-Value Sensor (Device State)

```json
{
  "temperature": 25.5,
  "humidity": 65.0,
  "unit_temperature": "℃",
  "unit_humidity": "%",
  "device": "sht31",
  "house_id": "h1",
  "source": "pico_node_01",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- One or more measurement fields (e.g., `temperature`, `humidity`, `co2`)
- `device` (string): Device model/type
- `house_id` (string): House identifier
- `source` (string): Node/device identifier
- `timestamp` (string): ISO 8601 timestamp

**Optional Fields**:
- `unit_{field_name}` (string): Unit for each measurement field

### Actuator Command Payload

```json
{
  "command": 1,
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- `command` (integer): Command value
  - `0` = OFF
  - `1` = ON
- `timestamp` (string): ISO 8601 timestamp

**Optional Fields**:
- `duration` (integer): Duration in seconds (for timed operations)
- `source` (string): Command originator (e.g., "node-red", "ha", "manual")

### Actuator State Payload

```json
{
  "state": "ON",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- `state` (string): Current state ("ON" or "OFF")
- `timestamp` (string): ISO 8601 timestamp

### Node Status Payload

```json
{
  "status": "online",
  "uptime": 3600,
  "ip": "192.168.1.100",
  "rssi": -65,
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- `status` (string): Node status ("online" or "offline")
- `timestamp` (string): ISO 8601 timestamp

**Optional Fields**:
- `uptime` (integer): Uptime in seconds
- `ip` (string): Node IP address
- `rssi` (integer): WiFi signal strength (dBm)
- `firmware_version` (string): Firmware version

### Alert Payload

```json
{
  "alert_level": "warning",
  "message": "High temperature: 38.5℃ (threshold: 35℃)",
  "sensor_type": "temperature",
  "value": 38.5,
  "threshold": 35.0,
  "house_id": "h1",
  "source": "node-red",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Required Fields**:
- `alert_level` (string): Alert severity
  - `"info"` - Informational
  - `"warning"` - Warning condition
  - `"critical"` - Critical condition
- `message` (string): Human-readable alert message
- `timestamp` (string): ISO 8601 timestamp

**Optional Fields**:
- `sensor_type` (string): Related sensor type
- `value` (number): Current value that triggered alert
- `threshold` (number): Threshold value
- `house_id` (string): Affected house
- `source` (string): Alert originator

---

## Sensor Types

Standard sensor types for single-value sensor topics (`greenhouse/{house_id}/sensor/{type}`):

| Sensor Type | Description | Unit | Typical Range |
|-------------|-------------|------|---------------|
| `temperature` | Air temperature | ℃ | -10 to 50 |
| `humidity` | Relative humidity | % | 0 to 100 |
| `co2` | CO2 concentration | ppm | 400 to 3000 |
| `illuminance` | Light intensity | lx | 0 to 100000 |
| `wind_speed` | Wind speed | m/s | 0 to 20 |
| `soil_moisture` | Soil moisture | % | 0 to 100 |
| `pressure` | Atmospheric pressure | hPa | 950 to 1050 |
| `solar_radiation` | Solar radiation | W/m² | 0 to 1200 |
| `water_pressure` | Water pressure | kPa | 0 to 500 |
| `rainfall` | Rainfall detection | binary | 0 or 1 |

---

## Device Types

Standard device types for multi-value sensor topics (`greenhouse/{house_id}/sensor/{device}/state`):

| Device Type | Description | Measurements | Interface | Models |
|-------------|-------------|--------------|-----------|--------|
| `sht` | Temperature & Humidity | temperature, humidity | I2C | SHT30, SHT31, SHT40, SHT41 |
| `scd` | CO2, Temperature & Humidity | co2, temperature, humidity | I2C | SCD30, SCD40, SCD41 |
| `bmp280` | Pressure & Temperature | pressure, temperature | I2C | BMP280 |
| `solar` | Solar Radiation | value | ADC | Custom |
| `water_pressure` | Water Pressure | value | ADC | Custom |
| `cdm7160` | CO2 Sensor | co2 | UART | CDM7160 |
| `k30` | CO2 Sensor | co2 | UART | K30 |
| `drainage` | Drainage Monitor | volume, ec | GPIO + ADC | Custom |
| `rain` | Rain Detector | detected | GPIO | DFRobot SEN0575 |

---

## Actuator Types

Standard actuator types for control topics:

| Actuator Type | Description | Command Values | State Values |
|---------------|-------------|----------------|--------------|
| `irrigation` | Irrigation valve/pump | 0 (OFF), 1 (ON) | "ON", "OFF" |
| `ventilation` | Ventilation fan | 0 (OFF), 1 (ON) | "ON", "OFF" |
| `heater` | Heating system | 0 (OFF), 1 (ON) | "ON", "OFF" |
| `cooling` | Cooling system | 0 (OFF), 1 (ON) | "ON", "OFF" |
| `lighting` | Supplemental lighting | 0 (OFF), 1 (ON) | "ON", "OFF" |
| `curtain` | Shade curtain | 0 (OPEN), 1 (CLOSE) | "OPEN", "CLOSE" |
| `relay` | Generic relay | 0 (OFF), 1 (ON) | "ON", "OFF" |

---

## QoS Recommendations

Quality of Service (QoS) levels for different message types:

| Message Type | Topic Pattern | QoS | Retain | Rationale |
|--------------|---------------|-----|--------|-----------|
| Sensor Data | `greenhouse/{house_id}/sensor/**` | **0** | No | High frequency, loss acceptable |
| Actuator Command | `greenhouse/{house_id}/actuator/*/command` | **1** | No | Must be delivered, no duplicates needed |
| Actuator State | `greenhouse/{house_id}/actuator/*/state` | **1** | Yes | Must be delivered, retain for status queries |
| Node Status | `greenhouse/{house_id}/status` | **1** | Yes | Must be delivered, retain for monitoring |
| Alert | `greenhouse/{house_id}/alert` | **1** | Yes | Must be delivered, retain for review |
| System Message | `greenhouse/system/**` | **1** | No | Must be delivered to all subscribers |

**QoS Level Definitions**:
- **QoS 0** (At most once): Fire-and-forget, no acknowledgment
- **QoS 1** (At least once): Acknowledged delivery, may duplicate
- **QoS 2** (Exactly once): Guaranteed delivery, no duplicates (not used in this system)

---

## Telegraf Configuration Notes

### Critical Requirements

1. **JSON Format Required**
   - Telegraf's MQTT consumer plugin requires `data_format = "json"`
   - Plain text values (e.g., `"25.5"`) will be rejected
   - All payloads must be valid JSON objects

   ```toml
   [[inputs.mqtt_consumer]]
     data_format = "json"
     json_string_fields = []  # Parse all fields as their natural types
   ```

2. **Tag Extraction**
   - Use `tag_keys` to extract fields for InfluxDB tags
   - Common tags: `house_id`, `sensor_type`, `source`, `device`

   ```toml
   [[inputs.mqtt_consumer]]
     tag_keys = ["house_id", "sensor_type", "source", "device"]
   ```

3. **Field Filtering**
   - Use `json_query` to extract nested values if needed
   - Use `fieldpass` to include only specific fields in InfluxDB

4. **Topic Wildcards**
   - Single-level wildcard: `+` (e.g., `greenhouse/+/sensor/temperature`)
   - Multi-level wildcard: `#` (e.g., `greenhouse/#` for all topics)

   ```toml
   [[inputs.mqtt_consumer]]
     topics = [
       "greenhouse/+/sensor/#",
       "greenhouse/+/actuator/+/state",
       "greenhouse/+/alert"
     ]
   ```

5. **Timestamp Parsing**
   - Telegraf can parse ISO 8601 timestamps from JSON `timestamp` field
   - Configure timezone if needed

   ```toml
   [[inputs.mqtt_consumer]]
     json_time_key = "timestamp"
     json_time_format = "2006-01-02T15:04:05Z07:00"
   ```

### Example Telegraf Configuration

```toml
[[inputs.mqtt_consumer]]
  servers = ["tcp://localhost:1883"]
  topics = ["greenhouse/#"]
  qos = 0
  data_format = "json"

  # Tag extraction
  tag_keys = ["house_id", "sensor_type", "source", "device"]

  # Timestamp
  json_time_key = "timestamp"
  json_time_format = "2006-01-02T15:04:05Z07:00"

  # Measurement name
  name_override = "greenhouse_data"

[[outputs.influxdb_v2]]
  urls = ["http://localhost:8086"]
  token = "$INFLUX_TOKEN"
  organization = "agri-ha"
  bucket = "sensor_data"
```

---

## Examples

### Example 1: Temperature Sensor Data

**Topic**: `greenhouse/h1/sensor/temperature`

**Payload**:
```json
{
  "value": 25.5,
  "unit": "℃",
  "sensor_type": "temperature",
  "house_id": "h1",
  "source": "pico_node_01",
  "location": "indoor",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Publishing (mosquitto_pub)**:
```bash
mosquitto_pub -h localhost -t "greenhouse/h1/sensor/temperature" -m '{
  "value": 25.5,
  "unit": "℃",
  "sensor_type": "temperature",
  "house_id": "h1",
  "source": "pico_node_01",
  "timestamp": "2026-02-07T12:00:00+09:00"
}'
```

### Example 2: SHT31 Sensor (Multi-Value)

**Topic**: `greenhouse/h1/sensor/sht/state`

**Payload**:
```json
{
  "temperature": 25.5,
  "humidity": 65.0,
  "unit_temperature": "℃",
  "unit_humidity": "%",
  "device": "sht31",
  "house_id": "h1",
  "source": "pico_node_01",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

### Example 3: Irrigation Control

**Command Topic**: `greenhouse/h1/actuator/irrigation/command`

**Command Payload (Turn ON)**:
```json
{
  "command": 1,
  "duration": 600,
  "source": "node-red",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**State Topic**: `greenhouse/h1/actuator/irrigation/state`

**State Payload (Feedback)**:
```json
{
  "state": "ON",
  "timestamp": "2026-02-07T12:00:05+09:00"
}
```

### Example 4: High Temperature Alert

**Topic**: `greenhouse/h1/alert`

**Payload**:
```json
{
  "alert_level": "warning",
  "message": "High temperature: 38.5℃ (threshold: 35℃)",
  "sensor_type": "temperature",
  "value": 38.5,
  "threshold": 35.0,
  "house_id": "h1",
  "source": "node-red",
  "timestamp": "2026-02-07T14:30:00+09:00"
}
```

### Example 5: Node Status (LWT)

**Topic**: `greenhouse/h1/status`

**Online Payload**:
```json
{
  "status": "online",
  "uptime": 3600,
  "ip": "192.168.1.100",
  "rssi": -65,
  "firmware_version": "1.0.0",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

**Offline Payload (Last Will Testament)**:
```json
{
  "status": "offline",
  "timestamp": "2026-02-07T12:00:00+09:00"
}
```

### Example 6: MicroPython Publishing (Pico)

```python
from mqtt_mgr import create_greenhouse_mqtt
import time

# Initialize MQTT manager
mqtt = create_greenhouse_mqtt(node_id="pico_node_01", broker="192.168.15.14")
mqtt.connect()

# Publish temperature data
payload = {
    "value": 25.5,
    "unit": "℃",
    "sensor_type": "temperature",
    "house_id": "h1",
    "source": "pico_node_01",
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+09:00")
}

mqtt.publish("greenhouse/h1/sensor/temperature", payload, qos=0)
```

### Example 7: Home Assistant MQTT Sensor

```yaml
mqtt:
  sensor:
    - name: "Greenhouse H1 Temperature"
      state_topic: "greenhouse/h1/sensor/temperature"
      unit_of_measurement: "℃"
      device_class: temperature
      value_template: "{{ value_json.value | float }}"
      unique_id: "greenhouse_h1_temperature"
```

---

## unipi-daemon Topics (agriha/ namespace)

unipi-daemon (Pi Lite) が publish する独自トピック。`greenhouse/` ネームスペースとは別系統。

### Topic Structure

```
agriha/{house_id}/sensor/DS18B20           ... 1-Wire温度センサー
agriha/farm/weather/misol                  ... Misol WH65LP 外気象
agriha/{house_id}/ccm/sensor/{ccm_type}    ... CCM 内気象センサー
agriha/{house_id}/ccm/actuator/{ccm_type}  ... CCM アクチュエータ状態
agriha/{house_id}/ccm/weather/{ccm_type}   ... CCM 外気象（ArSprout経由）
agriha/{house_id}/ccm/other/{ccm_type}     ... CCM 未分類
agriha/{house_id}/relay/{ch}/set           ... リレー制御コマンド (REST API → MQTT)
agriha/{house_id}/relay/state              ... リレー全チャンネル状態
agriha/{house_id}/emergency                ... 緊急オーバーライド通知
```

### CCM Sensor Types

| ccm_type | Description | Unit |
|----------|-------------|------|
| `InAirTemp` | Indoor air temperature | ℃ |
| `InAirHumid` | Indoor air humidity | % |
| `InAirCO2` | Indoor CO2 | ppm |
| `SoilTemp` | Soil temperature | ℃ |
| `SoilEC` | Soil electrical conductivity | mS/cm |
| `SoilWC` | Soil water content | % |
| `InRadiation` | Indoor solar radiation | W/m² |
| `Pulse` | Pulse counter | count |
| `InAirHD` | Indoor humidity deficit | hPa |
| `InAirAbsHumid` | Indoor absolute humidity | g/m³ |
| `InAirDP` | Indoor dew point | ℃ |

### CCM Payload Example

```json
{
  "ccm_type": "InAirTemp",
  "value": 23.5,
  "room": 1,
  "region": 11,
  "order": 1,
  "priority": 1,
  "level": "S",
  "source_ip": "192.168.1.70",
  "timestamp": "2026-02-21T14:05:54.748325+00:00"
}
```

### Misol Weather Payload Example

```json
{
  "wind_dir_deg": 270,
  "temperature_c": 3.1,
  "humidity_pct": 88,
  "wind_speed_ms": 1.12,
  "gust_speed_ms": 2.24,
  "rainfall_mm": 0.0,
  "uv_wm2": 0.0,
  "light_lux": 0.0,
  "pressure_hpa": 1014.3,
  "battery_low": false,
  "timestamp": 1771680000.0
}
```

### QoS for agriha/ Topics

| Topic Pattern | QoS | Retain |
|--------------|-----|--------|
| `agriha/{id}/sensor/DS18B20` | 1 | Yes |
| `agriha/farm/weather/misol` | 1 | Yes |
| `agriha/{id}/ccm/#` | 0 | Yes |
| `agriha/{id}/relay/{ch}/set` | 1 | No |
| `agriha/{id}/relay/state` | 1 | Yes |
| `agriha/{id}/emergency` | 1 | Yes |

### REST API → MQTT Mapping

| REST Endpoint | MQTT Topic | Direction |
|--------------|------------|-----------|
| `GET /api/sensors` | subscribes `agriha/{id}/sensor/#`, `weather/misol`, `ccm/#` | MQTT→REST |
| `POST /api/relay/{ch}` | publishes `agriha/{id}/relay/{ch}/set` | REST→MQTT |
| `GET /api/status` | reads relay state + lockout | internal |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.1.0 | 2026-02-21 | Add unipi-daemon topics (agriha/ namespace): CCM, Misol, relay, emergency |
| 1.0.0 | 2026-02-07 | Initial specification |

---

## References

- [MQTT Version 3.1.1 Specification](http://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)
- [Telegraf MQTT Consumer Plugin](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/mqtt_consumer)
- [Home Assistant MQTT Integration](https://www.home-assistant.io/integrations/mqtt/)
- [ISO 8601 Date and Time Format](https://en.wikipedia.org/wiki/ISO_8601)

---

**Document Owner**: unipi-agri-ha Project Team
**Contact**: See repository documentation
