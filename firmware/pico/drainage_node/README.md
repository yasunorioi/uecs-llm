# Drainage Sensor Node - MQTT Version

CircuitPython firmware for greenhouse drainage monitoring using DFRobot SEN0575 tipping bucket sensor with MQTT communication.

---

## Hardware Requirements

### Components
- **W5500-EVB-Pico-PoE** - Raspberry Pi Pico compatible board with Ethernet and PoE support
- **DFRobot SEN0575** - Gravity Tipping Bucket Rainfall Sensor
- **Ethernet Cable** - Cat5e or better
- **PoE Injector** - Optional, for power over Ethernet

### Wiring

#### W5500 SPI (Pre-wired on W5500-EVB-Pico-PoE)
- GP16: MISO
- GP17: CS
- GP18: SCK
- GP19: MOSI

#### DFRobot SEN0575 I2C Connection
| Sensor Pin | Pico Pin | Description |
|-----------|----------|-------------|
| VCC       | 3.3V     | Power supply |
| GND       | GND      | Ground |
| SDA       | GP4      | I2C Data |
| SCL       | GP5      | I2C Clock |

---

## Software Requirements

### CircuitPython
- **Version**: 9.0.0 or later
- **Download**: https://circuitpython.org/board/wiznet_w5500_evb_pico/

### Required Libraries
Install these libraries to the `/lib` folder on CIRCUITPY drive:

1. **adafruit_wiznet5k** - W5500 Ethernet driver
2. **adafruit_minimqtt** - MQTT client
3. **adafruit_bus_device** - I2C device helper

Download from: https://circuitpython.org/libraries

---

## Installation

### Step 1: Flash CircuitPython
1. Download CircuitPython UF2 file for W5500-EVB-Pico
2. Hold BOOTSEL button while connecting USB
3. Drag and drop UF2 file to RPI-RP2 drive
4. Board will restart with CIRCUITPY drive

### Step 2: Install Libraries
1. Download Adafruit CircuitPython Library Bundle
2. Copy required libraries from bundle to `/lib` on CIRCUITPY:
   ```
   /lib/adafruit_wiznet5k/
   /lib/adafruit_minimqtt/
   /lib/adafruit_bus_device/
   /lib/dfrobot_rainfall.py  (included in this project)
   ```

### Step 3: Deploy Firmware
1. Copy `code.py` to root of CIRCUITPY drive
2. Copy `settings.toml` to root of CIRCUITPY drive
3. Edit `settings.toml` with your network and MQTT settings
4. Board will auto-restart and run firmware

---

## Configuration

Edit `settings.toml` before deployment:

```toml
# House identification
HOUSE_ID = "h1"

# Network (Static IP)
STATIC_IP = "192.168.1.70"
SUBNET = "255.255.255.0"
GATEWAY = "192.168.1.1"
DNS = "8.8.8.8"

# MQTT Broker
MQTT_BROKER = "192.168.1.10"
MQTT_PORT = "1883"
MQTT_USER = ""  # Optional
MQTT_PASS = ""  # Optional

# Sensor I2C Address
SENSOR_I2C_ADDR = "0x1D"

# Publish interval (seconds)
PUBLISH_INTERVAL = "600"  # 10 minutes
```

---

## MQTT Topics

### Published Topics
- `greenhouse/{HOUSE_ID}/drainage/amount` - Drainage amount (JSON)
- `greenhouse/{HOUSE_ID}/drainage/rate` - Drainage rate (JSON)
- `greenhouse/{HOUSE_ID}/drainage/count` - Tip count (JSON)
- `greenhouse/{HOUSE_ID}/drainage/status` - Online/Offline status

### Subscribed Topics
- `greenhouse/{HOUSE_ID}/drainage/reset` - Reset command (payload: "reset" or "1")

### Payload Format

**Amount:**
```json
{
  "timestamp": "2026-02-05T10:30:00",
  "amount_mm": 2.5,
  "total_mm": 15.8,
  "interval_sec": 600
}
```

**Rate:**
```json
{
  "timestamp": "2026-02-05T10:30:00",
  "rate_mm_min": 0.25
}
```

**Count:**
```json
{
  "timestamp": "2026-02-05T10:30:00",
  "count": 45
}
```

---

## Home Assistant Integration

### MQTT Discovery

Firmware automatically publishes Home Assistant MQTT Discovery configuration on startup. Sensors will appear as:

- `sensor.{house_id}_drainage_amount` - Drainage amount (mm)
- `sensor.{house_id}_drainage_rate` - Drainage rate (mm/min)

### Manual Configuration

If auto-discovery is disabled, add to `configuration.yaml`:

```yaml
mqtt:
  sensor:
    - name: "H1 Drainage Amount"
      state_topic: "greenhouse/h1/drainage/amount"
      unit_of_measurement: "mm"
      device_class: precipitation
      value_template: "{{ value_json.amount_mm }}"

    - name: "H1 Drainage Rate"
      state_topic: "greenhouse/h1/drainage/rate"
      unit_of_measurement: "mm/min"
      value_template: "{{ value_json.rate_mm_min }}"
```

---

## Operation

### Normal Operation
1. Board connects to Ethernet on startup
2. Connects to MQTT broker
3. Publishes sensor readings every 10 minutes (configurable)
4. Performs daily reset at midnight (00:00)

### Status Monitoring

**Serial Console (USB):**
```bash
# Linux/Mac
screen /dev/ttyACM0 115200

# Windows
# Use PuTTY or Tera Term
```

**Expected Output:**
```
==================================================
Drainage Sensor Node - MQTT Version
==================================================
Initializing W5500 Ethernet...
IP Address: 192.168.1.70
Initializing I2C...
Initializing DFRobot Rainfall Sensor (I2C: 0x1D)...
Sensor initialized successfully!
Setting up MQTT client (Broker: 192.168.1.10:1883)...
Connected to MQTT broker! (RC: 0)
Publishing Home Assistant MQTT Discovery...
==================================================
Starting main loop...
Publish interval: 600 seconds
==================================================
```

### LED Indicators
- **Power LED**: Solid = Board powered
- **W5500 Link LED**: Solid = Ethernet connected
- **W5500 Activity LED**: Blinking = Network traffic

---

## Troubleshooting

### Sensor Not Detected
**Symptom:** `ERROR: Sensor initialization failed!`

**Solutions:**
1. Check I2C wiring (SDA, SCL, VCC, GND)
2. Verify sensor power (3.3V)
3. Test I2C address:
   ```python
   # Add to code.py temporarily
   import board
   import busio
   i2c = busio.I2C(board.GP5, board.GP4)
   while not i2c.try_lock():
       pass
   devices = i2c.scan()
   print("I2C devices found:", [hex(d) for d in devices])
   i2c.unlock()
   ```
4. Expected address: `0x1d`

### MQTT Connection Failed
**Symptom:** `MQTT connection failed: [Errno 113] EHOSTUNREACH`

**Solutions:**
1. Verify MQTT broker IP in `settings.toml`
2. Check broker is running: `mosquitto_sub -h 192.168.1.10 -t '#' -v`
3. Check network connectivity: Ping broker from another device
4. Verify firewall allows port 1883

### No Data Published
**Symptom:** MQTT connected but no sensor data

**Solutions:**
1. Check serial console for errors
2. Verify sensor is receiving rainfall events
3. Check publish interval (default: 10 minutes)
4. Monitor MQTT topic: `mosquitto_sub -h 192.168.1.10 -t 'greenhouse/+/drainage/#' -v`

### Ethernet Not Connecting
**Symptom:** `Ethernet connection timeout`

**Solutions:**
1. Check Ethernet cable connection
2. Verify static IP settings in `settings.toml`
3. Check router/switch port is active
4. Try DHCP temporarily (modify code.py: `is_dhcp=True`)

---

## Maintenance

### Sensor Calibration
Default: 0.2794 mm per tip (factory calibration)

To recalibrate:
1. Collect known volume of water (e.g., 100ml)
2. Pour into sensor funnel
3. Count number of tips
4. Calculate: `mm_per_tip = (volume_ml / funnel_area_cm2) / tip_count * 10`
5. Update in sensor driver (requires code modification)

### Daily Reset
Sensor cumulative values reset automatically at midnight (00:00).

Manual reset:
```bash
mosquitto_pub -h 192.168.1.10 -t 'greenhouse/h1/drainage/reset' -m 'reset'
```

---

## File Structure

```
drainage_node/
├── code.py              # Main firmware
├── settings.toml        # Configuration
└── README.md            # This file

/lib/
└── dfrobot_rainfall.py  # Sensor driver
```

---

## References

- **DFRobot SEN0575 Datasheet**: https://wiki.dfrobot.com/SKU_SEN0575
- **CircuitPython Documentation**: https://docs.circuitpython.org/
- **W5500-EVB-Pico Schematic**: https://docs.wiznet.io/Product/iEthernet/W5500/w5500-evb-pico
- **MQTT Protocol**: https://mqtt.org/

---

## License

This firmware is part of the Arsprout greenhouse automation project.

**Author**: Arsprout Analysis Team
**Date**: 2026-02-05
**Version**: 1.0.0
