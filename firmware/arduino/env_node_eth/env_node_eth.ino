// env_node_eth.ino - Environment Sensor Node for W5500-EVB-Pico2
// I2C auto-detect: SCD41 + SHT40 + BMP280 (only detected sensors are used)
// Ethernet (PoE) + MQTT data transmission + HA Auto Discovery
// arduino-pico v5.5.0, PubSubClient 2.8, ArduinoJson 7.x, Sensirion drivers, Adafruit BMP280

#include <SPI.h>
#include <W5500lwIP.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <Wire.h>

// Shared headers
#include "sensor_registry.h"
#include "sw_watchdog.h"

// Sensirion official drivers
#include <SensirionI2cScd4x.h>
#include <SensirionI2cSht4x.h>
// Adafruit BMP280
#include <Adafruit_BMP280.h>

// ========== Firmware Version ==========
const char* FW_VERSION = "3.0.1";
const char* FW_NAME = "env_node_eth";

// ========== W5500 SPI0 Pins (W5500-EVB-Pico2 fixed) ==========
const int W5500_CS = 17;
const int W5500_INT = 21;
const int W5500_RST = 20;
// SPI0: MOSI=GP19, MISO=GP16, SCK=GP18

// ========== I2C0 (Grove connector) ==========
const int I2C_SDA = 8;
const int I2C_SCL = 9;

// ========== Compile-time Configuration ==========
const char* DEFAULT_MQTT_BROKER = "192.168.15.14";
const int DEFAULT_MQTT_PORT = 1883;
const char* DEFAULT_HOUSE_ID = "h1";
const char* DEFAULT_NODE_ID = "env_node_eth_01";

// ========== Sensor Calibration ==========
const float SCD41_TEMP_OFFSET = 3.29;
const float BMP280_TEMP_OFFSET = 0.60;

// ========== Timing ==========
const int SENSOR_INTERVAL = 60;  // seconds
const int ETH_CONNECT_TIMEOUT = 15;  // seconds
const int MQTT_RECONNECT_ATTEMPTS = 3;
const int MQTT_RECONNECT_DELAY = 5;  // seconds
const unsigned long REBOOT_INTERVAL = 600000;  // 10 minutes
const int MQTT_FAIL_THRESHOLD = 3;

// ========== Global Variables (Core 0) ==========
Wiznet5500lwIP eth(W5500_CS, SPI, W5500_INT);
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

String houseId;
String nodeId;
String mqttBroker;
int mqttPort;
String mqttClientId;
String mqttTopic;

int mqttFailCount = 0;
int loopCount = 0;

// ========== I2C Sensor Detection Flags ==========
bool scd41_detected = false;
bool sht40_detected = false;
bool bmp280_detected = false;

// ========== Shared Variables (volatile, Core 0 <-> Core 1) ==========
volatile float g_co2 = NAN;
volatile float g_scd41_temp = NAN;
volatile float g_scd41_hum = NAN;
volatile float g_sht40_temp = NAN;
volatile float g_sht40_hum = NAN;
volatile float g_pressure = NAN;
volatile float g_bmp280_temp = NAN;
volatile bool g_data_ready = false;
volatile bool g_sensors_ok = false;

// ========== Sensor Objects (Core 1) ==========
SensirionI2cScd4x scd4x;
SensirionI2cSht4x sht4x;
Adafruit_BMP280 bmp280(&Wire);

// ========== Function Declarations ==========
void loadConfig();
void initEthernet();
void connectMQTT();
void publishHADiscovery();
void rebootWithReason(const char* reason);

// ========== Core 0: Setup (Networking) ==========
void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("=== env_node_eth.ino: Environment Sensor Node (Ethernet) ===");
  Serial.println("Board: W5500-EVB-Pico2");
  Serial.println("Sensors: I2C auto-detect (SCD41/SHT40/BMP280)");

  // LittleFS initialization
  if (!LittleFS.begin()) {
    Serial.println("LittleFS mount failed, formatting...");
    LittleFS.format();
    if (!LittleFS.begin()) {
      Serial.println("WARNING: LittleFS init failed after format");
      Serial.println("Continuing with default configuration (LittleFS disabled)");
    } else {
      Serial.println("LittleFS formatted and mounted");
    }
  } else {
    Serial.println("LittleFS mounted");
  }

  // Load configuration
  loadConfig();

  Serial.printf("Node: %s, House: %s\n", nodeId.c_str(), houseId.c_str());
  Serial.printf("MQTT: %s:%d, Topic: %s\n", mqttBroker.c_str(), mqttPort, mqttTopic.c_str());

  // Ethernet connection
  initEthernet();

  // MQTT connection
  mqttClient.setServer(mqttBroker.c_str(), mqttPort);
  mqttClient.setKeepAlive(60);
  mqttClient.setBufferSize(256);
  connectMQTT();

  // HA MQTT Auto Discovery (deferred until sensors detected)
  // publishHADiscovery() is called after g_sensors_ok is set

  // Publish firmware version (retained)
  String versionTopic = String("agriha/") + nodeId + "/version";
  String versionPayload = String("{\"firmware\":\"") + FW_NAME + "\",\"version\":\"" + FW_VERSION + "\"}";
  mqttClient.publish(versionTopic.c_str(), versionPayload.c_str(), true);
  Serial.printf("Published firmware version: %s v%s\n", FW_NAME, FW_VERSION);

  // Start software watchdog
  swWdtStart();
  Serial.printf("Software watchdog: check %lums, threshold %d misses (%lus)\n",
                SWD_CHECK_MS, SWD_MISS_THRESHOLD, SWD_CHECK_MS * SWD_MISS_THRESHOLD / 1000);

  Serial.println("\n=== Core 0 Setup Complete ===\n");
}

// ========== Core 1: Setup (Sensors - I2C Auto Detect) ==========
void setup1() {
  Serial.println("=== Core 1 Setup: I2C Auto Detect ===");

  Wire.setSDA(I2C_SDA);
  Wire.setSCL(I2C_SCL);
  Wire.begin();
  delay(1000);

  // I2C scan -> registry lookup
  Serial.println("I2C scan:");
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  0x%02X found -> ", addr);
      bool matched = false;
      for (int i = 0; i < SENSOR_REGISTRY_SIZE; i++) {
        if (SENSOR_REGISTRY[i].addr == addr) {
          Serial.printf("%s\n", SENSOR_REGISTRY[i].name);
          switch (SENSOR_REGISTRY[i].type) {
            case SENSOR_SCD41:  scd41_detected = true;  break;
            case SENSOR_SHT40:  sht40_detected = true;  break;
            case SENSOR_BMP280: bmp280_detected = true;  break;
            default: break;
          }
          matched = true;
          break;
        }
      }
      if (!matched) Serial.println("unknown");
    }
  }

  Serial.printf("Detected: SCD41=%d SHT40=%d BMP280=%d\n",
                scd41_detected, sht40_detected, bmp280_detected);

  // Initialize only detected sensors
  if (scd41_detected) {
    scd4x.begin(Wire, 0x62);
    uint16_t error;
    char errorMessage[256];

    error = scd4x.stopPeriodicMeasurement();
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SCD41 stopPeriodicMeasurement error: %s\n", errorMessage);
    }
    delay(500);

    uint64_t serialNumber;
    error = scd4x.getSerialNumber(serialNumber);
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SCD41 getSerialNumber error: %s\n", errorMessage);
    } else {
      Serial.printf("SCD41 serial: 0x%012llX\n", serialNumber);
    }

    error = scd4x.startPeriodicMeasurement();
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SCD41 startPeriodicMeasurement error: %s\n", errorMessage);
    } else {
      Serial.println("SCD41 periodic measurement started (5s interval)");
    }
  }

  if (sht40_detected) {
    sht4x.begin(Wire, 0x44);
    Serial.println("SHT40 initialized at 0x44");
  }

  if (bmp280_detected) {
    if (!bmp280.begin(0x76)) {
      if (!bmp280.begin(0x77)) {
        Serial.println("BMP280 init failed (detected but begin() failed)");
        bmp280_detected = false;
      } else {
        Serial.println("BMP280 initialized at 0x77");
      }
    } else {
      Serial.println("BMP280 initialized at 0x76");
    }

    if (bmp280_detected) {
      bmp280.setSampling(Adafruit_BMP280::MODE_NORMAL,
                         Adafruit_BMP280::SAMPLING_X2,
                         Adafruit_BMP280::SAMPLING_X16,
                         Adafruit_BMP280::FILTER_X16,
                         Adafruit_BMP280::STANDBY_MS_500);
    }
  }

  g_sensors_ok = true;  // OK even with 0 sensors (no crash)
  Serial.println("=== Core 1 Setup Complete ===\n");
}

// ========== Core 0: Main Loop (Networking + MQTT) ==========
void loop() {
  loopCount++;
  swWdtFeed();

  // Tier 2: 10-minute periodic reboot
  if (millis() >= REBOOT_INTERVAL) {
    rebootWithReason("periodic_10min_reboot");
  }

  // Ethernet check
  if (!eth.connected()) {
    Serial.println("Ethernet disconnected");
    rebootWithReason("eth_disconnected");
  }

  // MQTT check
  if (!mqttClient.connected()) {
    Serial.println("MQTT disconnected, reconnecting...");
    connectMQTT();
  }
  mqttClient.loop();

  // Publish HA Discovery once sensors are ready (first loop after Core 1 setup)
  static bool ha_discovery_done = false;
  if (g_sensors_ok && !ha_discovery_done) {
    publishHADiscovery();
    ha_discovery_done = true;
  }

  // Wait for sensor data from Core 1
  if (g_data_ready) {
    // Core0: グローバル変数読み取り（Core1書き込みとの競合防止）
    noInterrupts();
    float co2 = g_co2;
    float scd41_temp = g_scd41_temp;
    float scd41_hum = g_scd41_hum;
    float sht40_temp = g_sht40_temp;
    float sht40_hum = g_sht40_hum;
    float pressure = g_pressure;
    float bmp280_temp = g_bmp280_temp;
    interrupts();

    // Build JSON payload - only include non-NAN fields
    JsonDocument doc;
    doc["house_id"] = houseId;
    doc["node_id"] = nodeId;
    if (!isnan(co2))          doc["co2"] = round(co2);
    if (!isnan(scd41_temp))   doc["temperature"] = round(scd41_temp * 100) / 100.0;
    if (!isnan(scd41_hum))    doc["humidity"] = round(scd41_hum * 10) / 10.0;
    if (!isnan(sht40_temp))   doc["sht40_temperature"] = round(sht40_temp * 100) / 100.0;
    if (!isnan(sht40_hum))    doc["sht40_humidity"] = round(sht40_hum * 10) / 10.0;
    if (!isnan(pressure))     doc["pressure"] = round(pressure * 10) / 10.0;
    if (!isnan(bmp280_temp))  doc["bmp280_temperature"] = round(bmp280_temp * 100) / 100.0;
    doc["uptime"] = millis() / 1000;

    char buffer[512];
    serializeJson(doc, buffer);

    Serial.printf("[%d] ", loopCount);
    if (!isnan(co2)) Serial.printf("CO2=%.0fppm ", co2);
    if (!isnan(scd41_temp)) Serial.printf("T=%.2fC ", scd41_temp);
    if (!isnan(scd41_hum)) Serial.printf("RH=%.1f%% ", scd41_hum);
    if (!isnan(pressure)) Serial.printf("P=%.1fhPa ", pressure);
    Serial.println();

    // MQTT publish
    if (mqttClient.publish(mqttTopic.c_str(), buffer)) {
      Serial.println("  Published to MQTT");
      mqttFailCount = 0;
    } else {
      Serial.println("  MQTT publish failed");
      mqttFailCount++;
      Serial.printf("  MQTT failures: %d/%d\n", mqttFailCount, MQTT_FAIL_THRESHOLD);

      if (mqttFailCount >= MQTT_FAIL_THRESHOLD) {
        rebootWithReason("mqtt_fail_count_exceeded");
      }

      connectMQTT();
    }

    g_data_ready = false;
  }

  delay(1000);
}

// ========== Core 1: Main Loop (Sensor Reading) ==========
void loop1() {
  if (!g_sensors_ok) {
    delay(5000);
    return;
  }

  // SCD41: read only if detected
  if (scd41_detected) {
    uint16_t error;
    char errorMessage[256];
    uint16_t co2_raw = 0;
    float temperature_raw = 0.0f;
    float humidity_raw = 0.0f;
    bool isDataReady = false;

    error = scd4x.getDataReadyStatus(isDataReady);
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SCD41 getDataReadyFlag error: %s\n", errorMessage);
    }

    if (isDataReady) {
      error = scd4x.readMeasurement(co2_raw, temperature_raw, humidity_raw);
      if (error) {
        errorToString(error, errorMessage, 256);
        Serial.printf("SCD41 readMeasurement error: %s\n", errorMessage);
      } else if (co2_raw != 0) {
        // Core1: グローバル変数書き込み（Core0読み取りとの競合防止）
        noInterrupts();
        g_co2 = co2_raw;
        g_scd41_temp = temperature_raw - SCD41_TEMP_OFFSET;
        g_scd41_hum = humidity_raw;
        interrupts();
      }
    }
  }

  // SHT40: read only if detected
  if (sht40_detected) {
    uint16_t error;
    char errorMessage[256];
    float sht40_temp_raw = 0.0f;
    float sht40_hum_raw = 0.0f;
    error = sht4x.measureHighPrecision(sht40_temp_raw, sht40_hum_raw);
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SHT40 measureHighPrecision error: %s\n", errorMessage);
    } else {
      // Core1: グローバル変数書き込み（Core0読み取りとの競合防止）
      noInterrupts();
      g_sht40_temp = sht40_temp_raw;
      g_sht40_hum = sht40_hum_raw;
      interrupts();
    }
  }

  // BMP280: read only if detected + sanity check
  if (bmp280_detected) {
    float p = bmp280.readPressure() / 100.0;  // Pa -> hPa
    float t = bmp280.readTemperature() - BMP280_TEMP_OFFSET;
    if (!isnan(p) && p > 300 && p < 1200) {
      // Core1: グローバル変数書き込み（Core0読み取りとの競合防止）
      noInterrupts();
      g_pressure = p;
      g_bmp280_temp = t;
      interrupts();
    }
  }

  g_data_ready = true;

  delay(SENSOR_INTERVAL * 1000);
}

// ========== Configuration Functions ==========
void loadConfig() {
  if (LittleFS.exists("/config.json")) {
    File file = LittleFS.open("/config.json", "r");
    if (file) {
      JsonDocument doc;
      DeserializationError error = deserializeJson(doc, file);
      file.close();

      if (!error) {
        Serial.println("Config loaded from /config.json");
        houseId = doc["house_id"] | DEFAULT_HOUSE_ID;
        nodeId = doc["node_id"] | DEFAULT_NODE_ID;
        mqttBroker = doc["mqtt_broker"] | DEFAULT_MQTT_BROKER;
        mqttPort = doc["mqtt_port"] | DEFAULT_MQTT_PORT;
        mqttClientId = String("w5500-") + nodeId;
        mqttTopic = String("agriha/") + houseId + "/sensor/env/state";
        return;
      } else {
        Serial.printf("Config parse error: %s\n", error.c_str());
      }
    }
  }

  // Use defaults
  Serial.println("Using default configuration");
  houseId = DEFAULT_HOUSE_ID;
  nodeId = DEFAULT_NODE_ID;
  mqttBroker = DEFAULT_MQTT_BROKER;
  mqttPort = DEFAULT_MQTT_PORT;
  mqttClientId = String("w5500-") + nodeId;
  mqttTopic = String("agriha/") + houseId + "/sensor/env/state";
}

// ========== Ethernet Functions ==========
void initEthernet() {
  // HW Reset
  pinMode(W5500_RST, OUTPUT);
  digitalWrite(W5500_RST, LOW);
  delay(100);
  digitalWrite(W5500_RST, HIGH);
  delay(1000);

  // SPI0 pin config
  SPI.setSCK(18);
  SPI.setTX(19);   // MOSI
  SPI.setRX(16);   // MISO
  SPI.begin();

  // lwIP Ethernet
  lwipPollingPeriod(5);
  eth.begin();

  // DHCP wait
  Serial.println("Waiting for Ethernet DHCP...");
  unsigned long start = millis();
  while (!eth.connected()) {
    if (millis() - start > ETH_CONNECT_TIMEOUT * 1000UL) {
      Serial.println("Ethernet DHCP timeout");
      rebootWithReason("eth_dhcp_timeout");
    }
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.printf("Ethernet connected: %s\n", eth.localIP().toString().c_str());
  Serial.printf("Subnet: %s\n", eth.subnetMask().toString().c_str());
  Serial.printf("Gateway: %s\n", eth.gatewayIP().toString().c_str());
}

// ========== MQTT Functions ==========
void connectMQTT() {
  for (int attempt = 0; attempt < MQTT_RECONNECT_ATTEMPTS; attempt++) {
    Serial.printf("MQTT attempt %d/%d: %s:%d\n",
                  attempt + 1, MQTT_RECONNECT_ATTEMPTS,
                  mqttBroker.c_str(), mqttPort);

    if (mqttClient.connect(mqttClientId.c_str())) {
      Serial.println("MQTT connected");
      // WA-4: OTA封印 — arduino-pico #3382 RP2350ブリックバグ未修正のため無効化
      // mqttClient.subscribe((String("agriha/") + nodeId + "/ota/trigger").c_str());
      return;
    }

    Serial.printf("MQTT connect failed, state=%d\n", mqttClient.state());
    if (attempt < MQTT_RECONNECT_ATTEMPTS - 1) {
      delay(MQTT_RECONNECT_DELAY * 1000);
    }
  }

  Serial.println("MQTT connection failed after retries");
  rebootWithReason("mqtt_connect_failed");
}

void publishHADiscovery() {
  const char* DISCOVERY_PREFIX = "homeassistant";
  String deviceId = String("w5500_env_node_") + nodeId;

  // Device info
  JsonDocument deviceDoc;
  JsonArray identifiers = deviceDoc["identifiers"].to<JsonArray>();
  identifiers.add(deviceId);
  deviceDoc["name"] = String("Env Node ") + nodeId;
  deviceDoc["model"] = "W5500-EVB-Pico2";
  deviceDoc["manufacturer"] = "DIY";
  deviceDoc["sw_version"] = FW_VERSION;

  int published = 0;
  for (int i = 0; i < HA_FIELDS_SIZE; i++) {
    // Only publish fields for detected sensors
    bool publish = false;
    switch (HA_FIELDS[i].source) {
      case SENSOR_SCD41:  publish = scd41_detected; break;
      case SENSOR_SHT40:  publish = sht40_detected; break;
      case SENSOR_BMP280: publish = bmp280_detected; break;
      default: break;
    }
    if (!publish) continue;

    String uid = deviceId + "_" + HA_FIELDS[i].key;
    String topic = String(DISCOVERY_PREFIX) + "/sensor/" + uid + "/config";

    JsonDocument doc;
    doc["name"] = HA_FIELDS[i].name;
    doc["stat_t"] = mqttTopic;
    doc["uniq_id"] = uid;
    doc["val_tpl"] = String("{{ value_json.") + HA_FIELDS[i].key + " }}";
    doc["unit_of_meas"] = HA_FIELDS[i].unit;
    doc["dev_cla"] = HA_FIELDS[i].dev_class;
    if (HA_FIELDS[i].icon) {
      doc["ic"] = HA_FIELDS[i].icon;
    }
    doc["dev"] = deviceDoc;

    char buffer[768];
    serializeJson(doc, buffer);
    mqttClient.publish(topic.c_str(), buffer, true);
    Serial.printf("HA Discovery: %s\n", topic.c_str());
    delay(500);
    published++;
  }

  Serial.printf("HA MQTT Auto Discovery published (%d sensors)\n", published);
}

// ========== Reboot Function ==========
void rebootWithReason(const char* reason) {
  Serial.printf("Rebooting: %s\n", reason);

  File file = LittleFS.open("/reboot_reason.txt", "w");
  if (file) {
    file.print(reason);
    file.close();
  }

  delay(2000);
  watchdog_reboot(0, 0, 0);
}
