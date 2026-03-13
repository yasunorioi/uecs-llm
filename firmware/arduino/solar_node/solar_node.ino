// solar_node.ino - Solar Radiation Sensor Node for Pico 2 W
// PVSS-03 (0-1V = 0-1000 W/m2) on ADC0 (GP26)
// I2C auto-detect: optional SCD41/SHT40/BMP280/BH1750
// WiFi + MQTT data transmission + HA Auto Discovery
// arduino-pico v5.5.0, PubSubClient 2.8, ArduinoJson 7.x

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <Wire.h>

// Shared headers
#include "sensor_registry.h"
#include "sw_watchdog.h"

// I2C sensor drivers (conditionally used based on detection)
#include <SensirionI2cScd4x.h>
#include <SensirionI2cSht4x.h>
#include <Adafruit_BMP280.h>

// ========== Firmware Version ==========
const char* FW_VERSION = "3.0.1";
const char* FW_NAME = "solar_node";

// ========== Compile-time Configuration ==========
const char* DEFAULT_SSID = "aterm-03e34d-a";
const char* DEFAULT_PASSWORD = "38027f7faf81e";

const char* DEFAULT_MQTT_BROKER = "192.168.15.14";
const int DEFAULT_MQTT_PORT = 1883;
const char* DEFAULT_HOUSE_ID = "h1";
const char* DEFAULT_NODE_ID = "solar_node_01";

// ========== ADC Configuration ==========
const int ADC_PIN = 26;  // GP26 = ADC0
const int ADC_READ_COUNT = 5;
const float ADC_VREF = 3.3;
const float PVSS03_SCALE = 1000.0;  // 1V = 1000 W/m2

// ========== I2C0 Pins ==========
const int I2C_SDA = 8;
const int I2C_SCL = 9;

// ========== Sensor Calibration ==========
const float SCD41_TEMP_OFFSET = 3.29;
const float BMP280_TEMP_OFFSET = 0.60;

// ========== Timing ==========
const int SENSOR_INTERVAL = 60;  // seconds
const int WIFI_CONNECT_TIMEOUT = 15;  // seconds
const int WIFI_RECONNECT_ATTEMPTS = 3;
const int MQTT_RECONNECT_ATTEMPTS = 3;
const int MQTT_RECONNECT_DELAY = 5;  // seconds
const unsigned long REBOOT_INTERVAL = 600000;  // 10 minutes
const int MQTT_FAIL_THRESHOLD = 3;

// ========== Global Variables ==========
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

// ========== I2C Sensor Values (NAN = not available) ==========
float g_co2 = NAN;
float g_scd41_temp = NAN;
float g_scd41_hum = NAN;
float g_sht40_temp = NAN;
float g_sht40_hum = NAN;
float g_pressure = NAN;
float g_bmp280_temp = NAN;

// ========== Sensor Objects ==========
SensirionI2cScd4x scd4x;
SensirionI2cSht4x sht4x;
Adafruit_BMP280 bmp280(&Wire);

// ========== Function Declarations ==========
void loadConfig();
void saveConfig();
void connectWiFi();
bool wifiReconnect();
void connectMQTT();
void publishHADiscovery();
float readSolar(int* adcRaw, float* voltage);
void rebootWithReason(const char* reason);
void scanI2CSensors();
void readI2CSensors();

// ========== Setup ==========
void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("=== solar_node.ino: Solar Radiation Sensor Node ===");
  Serial.println("Board: Raspberry Pi Pico 2 W");
  Serial.println("Sensor: PVSS-03 (GP26/ADC0) + I2C auto-detect");

  // ADC initialization
  analogReadResolution(12);  // 12-bit = 0-4095
  pinMode(ADC_PIN, INPUT);
  Serial.printf("ADC initialized on GP%d (12-bit)\n", ADC_PIN);

  // I2C initialization + sensor scan
  Wire.setSDA(I2C_SDA);
  Wire.setSCL(I2C_SCL);
  Wire.begin();
  delay(1000);
  scanI2CSensors();

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

  // WiFi connection
  connectWiFi();

  // MQTT connection
  mqttClient.setServer(mqttBroker.c_str(), mqttPort);
  mqttClient.setKeepAlive(60);
  mqttClient.setBufferSize(256);
  connectMQTT();

  // HA MQTT Auto Discovery
  publishHADiscovery();

  // Publish firmware version (retained)
  String versionTopic = String("agriha/") + nodeId + "/version";
  String versionPayload = String("{\"firmware\":\"") + FW_NAME + "\",\"version\":\"" + FW_VERSION + "\"}";
  mqttClient.publish(versionTopic.c_str(), versionPayload.c_str(), true);
  Serial.printf("Published firmware version: %s v%s\n", FW_NAME, FW_VERSION);

  // Start software watchdog
  swWdtStart();
  Serial.printf("Software watchdog: check %lums, threshold %d misses (%lus)\n",
                SWD_CHECK_MS, SWD_MISS_THRESHOLD, SWD_CHECK_MS * SWD_MISS_THRESHOLD / 1000);

  Serial.printf("Reboot interval: %lu ms (%lu min)\n", REBOOT_INTERVAL, REBOOT_INTERVAL / 60000);
  Serial.println("\n=== Starting Main Loop ===\n");
}

// ========== Main Loop ==========
void loop() {
  loopCount++;
  unsigned long loopStart = millis();
  swWdtFeed();

  // Tier 2: 10-minute periodic reboot
  if (millis() >= REBOOT_INTERVAL) {
    rebootWithReason("periodic_10min_reboot");
  }

  // WiFi check
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected, reconnecting...");
    if (!wifiReconnect()) {
      rebootWithReason("wifi_reconnect_failed");
    }
  }

  // MQTT check
  if (!mqttClient.connected()) {
    Serial.println("MQTT disconnected, reconnecting...");
    connectMQTT();
  }
  mqttClient.loop();

  // Read solar radiation (always available)
  int adcRaw;
  float voltage;
  float solarWm2 = readSolar(&adcRaw, &voltage);

  // Read I2C sensors (only detected ones)
  readI2CSensors();

  Serial.printf("[%d] Solar: %.1f W/m2 (ADC=%d, V=%.4f)", loopCount, solarWm2, adcRaw, voltage);
  if (!isnan(g_co2)) Serial.printf(" CO2=%.0f", g_co2);
  if (!isnan(g_pressure)) Serial.printf(" P=%.1f", g_pressure);
  Serial.println();

  // Build JSON payload
  JsonDocument doc;
  doc["solar_radiation_w_m2"] = round(solarWm2 * 10) / 10.0;
  doc["adc_raw"] = adcRaw;
  doc["voltage"] = round(voltage * 10000) / 10000.0;
  doc["device"] = "pvss03";
  doc["house_id"] = houseId;
  doc["node_id"] = nodeId;
  // I2C sensor data (only if detected)
  if (!isnan(g_co2))          doc["co2"] = round(g_co2);
  if (!isnan(g_scd41_temp))   doc["temperature"] = round(g_scd41_temp * 100) / 100.0;
  if (!isnan(g_scd41_hum))    doc["humidity"] = round(g_scd41_hum * 10) / 10.0;
  if (!isnan(g_sht40_temp))   doc["sht40_temperature"] = round(g_sht40_temp * 100) / 100.0;
  if (!isnan(g_sht40_hum))    doc["sht40_humidity"] = round(g_sht40_hum * 10) / 10.0;
  if (!isnan(g_pressure))     doc["pressure"] = round(g_pressure * 10) / 10.0;
  if (!isnan(g_bmp280_temp))  doc["bmp280_temperature"] = round(g_bmp280_temp * 100) / 100.0;
  doc["uptime"] = millis() / 1000;

  char buffer[512];
  serializeJson(doc, buffer);

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

  // Sleep until next sensor reading
  unsigned long elapsed = millis() - loopStart;
  if (elapsed < SENSOR_INTERVAL * 1000UL) {
    delay(SENSOR_INTERVAL * 1000UL - elapsed);
  }
}

// ========== I2C Sensor Functions ==========
void scanI2CSensors() {
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

    error = scd4x.startPeriodicMeasurement();
    if (error) {
      errorToString(error, errorMessage, 256);
      Serial.printf("SCD41 startPeriodicMeasurement error: %s\n", errorMessage);
    } else {
      Serial.println("SCD41 periodic measurement started");
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
}

void readI2CSensors() {
  // SCD41
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
        g_co2 = co2_raw;
        g_scd41_temp = temperature_raw - SCD41_TEMP_OFFSET;
        g_scd41_hum = humidity_raw;
      }
    }
  }

  // SHT40
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
      g_sht40_temp = sht40_temp_raw;
      g_sht40_hum = sht40_hum_raw;
    }
  }

  // BMP280
  if (bmp280_detected) {
    float p = bmp280.readPressure() / 100.0;
    float t = bmp280.readTemperature() - BMP280_TEMP_OFFSET;
    if (!isnan(p) && p > 300 && p < 1200) {
      g_pressure = p;
      g_bmp280_temp = t;
    }
  }
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
        mqttClientId = String("pico2w-") + nodeId;
        mqttTopic = String("agriha/") + houseId + "/sensor/solar/state";
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
  mqttClientId = String("pico2w-") + nodeId;
  mqttTopic = String("agriha/") + houseId + "/sensor/solar/state";
}

void saveConfig() {
  File file = LittleFS.open("/config.json", "w");
  if (file) {
    JsonDocument doc;
    doc["house_id"] = houseId;
    doc["node_id"] = nodeId;
    doc["mqtt_broker"] = mqttBroker;
    doc["mqtt_port"] = mqttPort;
    doc["sensor_interval"] = SENSOR_INTERVAL;

    serializeJson(doc, file);
    file.close();
    Serial.println("Config saved to /config.json");
  } else {
    Serial.println("Failed to save config");
  }
}

// ========== WiFi Functions ==========
void connectWiFi() {
  Serial.println("=== WiFi Connection Debug ===");
  Serial.printf("SSID: %s\n", DEFAULT_SSID);
  Serial.printf("Password length: %d chars\n", strlen(DEFAULT_PASSWORD));

  Serial.println("Setting WiFi mode to STA...");
  WiFi.mode(WIFI_STA);
  delay(100);

  Serial.printf("Calling WiFi.begin('%s', '***')\n", DEFAULT_SSID);
  WiFi.begin(DEFAULT_SSID, DEFAULT_PASSWORD);

  unsigned long start = millis();
  unsigned long lastStatusPrint = 0;
  int lastStatus = -1;

  while (WiFi.status() != WL_CONNECTED) {
    unsigned long elapsed = millis() - start;
    int currentStatus = WiFi.status();

    if (elapsed - lastStatusPrint >= 2000 || currentStatus != lastStatus) {
      Serial.printf("[%lu ms] WiFi.status() = %d (", elapsed, currentStatus);
      switch (currentStatus) {
        case 0: Serial.print("IDLE"); break;
        case 1: Serial.print("NO_SSID_AVAIL"); break;
        case 2: Serial.print("SCAN_COMPLETED"); break;
        case 3: Serial.print("CONNECTED"); break;
        case 4: Serial.print("CONNECT_FAILED"); break;
        case 5: Serial.print("CONNECTION_LOST"); break;
        case 6: Serial.print("DISCONNECTED"); break;
        default: Serial.print("UNKNOWN"); break;
      }
      Serial.println(")");
      lastStatusPrint = elapsed;
      lastStatus = currentStatus;
    }

    if (elapsed > WIFI_CONNECT_TIMEOUT * 1000UL) {
      Serial.printf("WiFi connection timeout (%ds)\n", WIFI_CONNECT_TIMEOUT);
      Serial.printf("Final status: %d\n", currentStatus);
      rebootWithReason("wifi_connect_timeout");
    }
    delay(500);
    Serial.print(".");
  }

  Serial.println("\n=== WiFi Connected Successfully ===");
  Serial.printf("SSID: %s\n", WiFi.SSID().c_str());
  Serial.printf("IP Address: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("Subnet: %s\n", WiFi.subnetMask().toString().c_str());
  Serial.printf("Gateway: %s\n", WiFi.gatewayIP().toString().c_str());
  Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
  Serial.println("================================");
}

bool wifiReconnect() {
  for (int attempt = 0; attempt < WIFI_RECONNECT_ATTEMPTS; attempt++) {
    if (WiFi.status() == WL_CONNECTED) {
      return true;
    }

    Serial.printf("WiFi reconnect attempt %d/%d\n", attempt + 1, WIFI_RECONNECT_ATTEMPTS);
    WiFi.disconnect();
    delay(1000);
    WiFi.begin(DEFAULT_SSID, DEFAULT_PASSWORD);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
      if (millis() - start > WIFI_CONNECT_TIMEOUT * 1000UL) {
        break;
      }
      delay(500);
    }

    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("WiFi reconnected: %s\n", WiFi.localIP().toString().c_str());
      return true;
    }
  }

  Serial.println("WiFi reconnect failed");
  return false;
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
  String deviceId = String("pico2w_") + nodeId;

  // Device info
  JsonDocument deviceDoc;
  JsonArray identifiers = deviceDoc["identifiers"].to<JsonArray>();
  identifiers.add(deviceId);
  deviceDoc["name"] = String("Solar Node ") + nodeId;
  deviceDoc["model"] = "Pico 2 W + PVSS-03";
  deviceDoc["manufacturer"] = "DIY";
  deviceDoc["sw_version"] = FW_VERSION;

  int published = 0;

  // Solar Radiation (always published)
  {
    String uid = deviceId + "_solar_radiation_w_m2";
    String topic = String(DISCOVERY_PREFIX) + "/sensor/" + uid + "/config";

    JsonDocument doc;
    doc["name"] = "Solar Radiation";
    doc["stat_t"] = mqttTopic;
    doc["uniq_id"] = uid;
    doc["val_tpl"] = "{{ value_json.solar_radiation_w_m2 }}";
    doc["unit_of_meas"] = "W/m\xC2\xB2";
    doc["dev_cla"] = "irradiance";
    doc["ic"] = "mdi:white-balance-sunny";
    doc["dev"] = deviceDoc;

    char buffer[512];
    serializeJson(doc, buffer);
    mqttClient.publish(topic.c_str(), buffer, true);
    Serial.printf("HA Discovery: %s\n", topic.c_str());
    delay(500);
    published++;
  }

  // ADC Voltage (always published)
  {
    String uid = deviceId + "_voltage";
    String topic = String(DISCOVERY_PREFIX) + "/sensor/" + uid + "/config";

    JsonDocument doc;
    doc["name"] = "Solar ADC Voltage";
    doc["stat_t"] = mqttTopic;
    doc["uniq_id"] = uid;
    doc["val_tpl"] = "{{ value_json.voltage }}";
    doc["unit_of_meas"] = "V";
    doc["dev_cla"] = "voltage";
    doc["dev"] = deviceDoc;

    char buffer[512];
    serializeJson(doc, buffer);
    mqttClient.publish(topic.c_str(), buffer, true);
    Serial.printf("HA Discovery: %s\n", topic.c_str());
    delay(500);
    published++;
  }

  // I2C sensor fields (only detected sensors)
  for (int i = 0; i < HA_FIELDS_SIZE; i++) {
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

// ========== Sensor Functions ==========
float readSolar(int* adcRaw, float* voltage) {
  long sum = 0;
  for (int i = 0; i < ADC_READ_COUNT; i++) {
    sum += analogRead(ADC_PIN);
    delay(10);
  }

  int adcAvg = sum / ADC_READ_COUNT;
  float v = (adcAvg / 4095.0) * ADC_VREF;
  float solarWm2 = v * PVSS03_SCALE;  // 1V = 1000 W/m2

  *adcRaw = adcAvg;
  *voltage = v;
  return solarWm2;
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
