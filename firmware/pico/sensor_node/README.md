# センサーノード ファームウェア

W5500-EVB-Pico2 + Grove Shield を使用したセンサーノード

## 動作確認済み (2026-02-05)

- E2E通信成功: センサー → MQTT → Mosquitto

## ハードウェア構成

| コンポーネント | 型番 | I2Cアドレス |
|---------------|------|-------------|
| メインボード | W5500-EVB-Pico2 | - |
| 温湿度センサー | SHT40 | 0x44 |
| 気圧センサー | BMP280 | 0x76 |
| CO2センサー | SCD41 | 0x62 |

## ピン配置

### I2C (Grove Shield)
- SDA: GP8
- SCL: GP9

### W5500 SPI
- SCK: GP18
- MOSI: GP19
- MISO: GP16
- CS: GP17
- RST: GP20

## 必要ライブラリ

```bash
circup install adafruit_sht4x adafruit_bmp280 adafruit_scd4x adafruit_minimqtt adafruit_requests
```

## MQTT トピック

- `greenhouse/sensors` - センサーデータ (JSON)

### ペイロード例

```json
{"temp": 25.3, "hum": 56.4, "press": 1004.3}
```

## 使い方

1. `code.py` を CIRCUITPY ドライブにコピー
2. `settings.toml` でMQTTブローカーのIPを設定
3. 電源投入で自動起動
