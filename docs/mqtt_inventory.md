# MQTT 棚卸し (Inventory) — uecs-llm / agriha namespace

> **生成日**: 2026-03-16
> **対象ブランチ**: v4
> **調査対象**: `src/agriha/daemon/*.py` + `src/agriha/control/*.py` + `config/*.yaml`
> **ツール**: grep + コード精読

---

## 1. ブローカー設定 (`config/unipi_daemon.example.yaml`)

| 設定項目 | 値 | 備考 |
|---------|-----|------|
| `mqtt.broker` | `localhost` | 実環境: `/etc/agriha/unipi_daemon.yaml` で上書き |
| `mqtt.port` | `1883` | デフォルト Mosquitto ポート |
| `mqtt.client_id` | `unipi-daemon` | main.py では複数クライアントを役割別に生成 |
| `mqtt.keepalive` | `60` 秒 | |
| `daemon.house_id` | `h01` | トピックプレフィックスに使用 |

**main.py で生成される MQTT クライアント一覧**:

| client_id | 役割 |
|-----------|------|
| `unipi-daemon-emergency` | CommandGate (緊急オーバーライド pub) |
| `unipi-daemon-sensor` | SensorLoop pub + CcmReceiver pub |
| `unipi-daemon-relay` | MqttRelayBridge pub/sub |
| `unipi-daemon-rest-api` | RestApi sub (センサーキャッシュ) + relay pub |

---

## 2. MQTT トピック一覧

### 凡例
- **方向**: `pub` = publish / `sub` = subscribe
- **QoS**: メッセージ品質
- **retain**: `Y` = 保持 / `N` = 保持なし
- **{house_id}**: `daemon.house_id` の値（デフォルト `h01`）

---

### 2-1. センサーデータ（pub）

#### DS18B20 温度センサー
| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/{house_id}/sensor/DS18B20` |
| **方向** | pub |
| **QoS** | 1 |
| **retain** | Y |
| **ファイル:行** | `src/agriha/daemon/sensor_loop.py:55,114` |
| **用途** | 1-Wire DS18B20 温度センサーデータ配信（センサーごとに同一トピックへ上書き） |

**ペイロード**:
```json
{
  "device_id": "28-00000de13271",
  "temperature_c": 23.5,
  "timestamp": 1771680000.0
}
```
| フィールド | 型 | 単位 | 備考 |
|-----------|-----|------|------|
| `device_id` | string | — | 1-Wire デバイスID |
| `temperature_c` | float | ℃ | |
| `timestamp` | float | UNIX秒 | `time.time()` |

---

#### Misol WH65LP 外気象
| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/farm/weather/misol` |
| **方向** | pub |
| **QoS** | 1 |
| **retain** | Y |
| **ファイル:行** | `src/agriha/daemon/sensor_loop.py:56,130` |
| **用途** | Misol WH65LP UART RS485 外気象データ配信 |

**ペイロード**:
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
| フィールド | 型 | 単位 | null可 | 備考 |
|-----------|-----|------|--------|------|
| `wind_dir_deg` | int\|null | 度 (0-360) | ✓ | センチネル値時 null |
| `temperature_c` | float\|null | ℃ | ✓ | |
| `humidity_pct` | int | % | — | |
| `wind_speed_ms` | float\|null | m/s | ✓ | |
| `gust_speed_ms` | float\|null | m/s | ✓ | |
| `rainfall_mm` | float | mm (累積) | — | |
| `uv_wm2` | float\|null | W/m² | ✓ | |
| `light_lux` | float\|null | lux | ✓ | |
| `pressure_hpa` | float\|null | hPa | ✓ | 拡張フレームのみ |
| `battery_low` | bool | — | — | |
| `timestamp` | float | UNIX秒 | — | `time.time()` で付加 |

---

### 2-2. UECS-CCM データ（pub）

CCM UDP マルチキャスト（224.0.0.1:16520）受信を MQTT に変換する。

| 項目 | 値 |
|------|-----|
| **トピック（センサー）** | `agriha/{house_id}/ccm/sensor/{ccm_type}` |
| **トピック（アクチュエータ）** | `agriha/{house_id}/ccm/actuator/{ccm_type}` |
| **トピック（外気象）** | `agriha/{house_id}/ccm/weather/{ccm_type}` |
| **トピック（未分類）** | `agriha/{house_id}/ccm/other/{ccm_type}` |
| **方向** | pub |
| **QoS** | 0 |
| **retain** | Y |
| **ファイル:行** | `src/agriha/daemon/ccm_receiver.py:134-158` |
| **用途** | ArSprout CCM 内気象・アクチュエータ・外気象データの MQTT 変換 |

**ペイロード（全カテゴリ共通）**:
```json
{
  "ccm_type": "InAirTemp",
  "value": 23.5,
  "room": 1,
  "region": 11,
  "order": 1,
  "priority": 29,
  "level": "S",
  "source_ip": "192.168.1.70",
  "timestamp": "2026-03-16T01:32:00+00:00"
}
```
| フィールド | 型 | 備考 |
|-----------|-----|------|
| `ccm_type` | string | CCM DATA type 属性（サフィックス除去済み） |
| `value` | float\|string | 数値変換失敗時は文字列 |
| `room` | int | デフォルト 1 |
| `region` | int | デフォルト 1 |
| `order` | int | デフォルト 1 |
| `priority` | int | デフォルト 29 |
| `level` | string | `"S"` など |
| `source_ip` | string | 送信元 IP |
| `timestamp` | string | ISO 8601 UTC |

**ccm_type 分類**:

| カテゴリ | ccm_type 一覧 |
|---------|---------------|
| sensor | `InAirTemp`, `InAirHumid`, `InAirCO2`, `SoilTemp`, `InRadiation`, `SoilEC`, `SoilWC`, `Pulse`, `InAirHD`, `InAirAbsHumid`, `InAirDP`, `IntgRadiation` |
| actuator | `Irri`, `VenFan`, `CirHoriFan`, `AirHeatBurn`, `AirHeatHP`, `CO2Burn`, `VenRfWin`, `VenSdWin`, `ThCrtn`, `LsCrtn`, `AirCoolHP`, `AirHumFog` |
| weather | `WAirTemp`, `WAirHumid`, `WWindSpeed`, `WWindDir16`, `WRainfall`, `WRainfallAmt`, `WLUX` |

---

### 2-3. リレー制御（pub/sub）

#### リレー制御コマンド受信（sub）
| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/{house_id}/relay/+/set` |
| **方向** | sub |
| **QoS** | 1 |
| **retain** | N |
| **ファイル:行** | `src/agriha/daemon/mqtt_relay_bridge.py:110-111` |
| **用途** | REST API → MQTT → I2C リレー制御コマンド受信 |

**ペイロード**:
```json
{
  "value": 1,
  "duration_sec": 180.0,
  "reason": "LLM: 灌水開始"
}
```
| フィールド | 型 | 値域 | 備考 |
|-----------|-----|------|------|
| `value` | int | 0=OFF, 1=ON | |
| `duration_sec` | float | ≥0 | 0=タイマーなし |
| `reason` | string | — | ログ用 |

#### リレー状態配信（pub）
| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/{house_id}/relay/state` |
| **方向** | pub |
| **QoS** | 1 |
| **retain** | Y |
| **ファイル:行** | `src/agriha/daemon/mqtt_relay_bridge.py:100-101` |
| **用途** | 全8チャンネルのリレー ON/OFF 状態配信（コマンド受信・操作完了後に更新） |

**ペイロード**:
```json
{
  "ch1": 0, "ch2": 1, "ch3": 0, "ch4": 0,
  "ch5": 0, "ch6": 0, "ch7": 0, "ch8": 0,
  "ts": 1771680000
}
```
| フィールド | 型 | 備考 |
|-----------|-----|------|
| `ch1`〜`ch8` | int | 0=OFF, 1=ON |
| `ts` | int | UNIX秒 |

---

### 2-4. REST API → MQTT 変換（pub）

| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/{house_id}/relay/{ch}/set` |
| **方向** | pub |
| **QoS** | 1 |
| **retain** | N |
| **ファイル:行** | `src/agriha/daemon/rest_api.py:276,284` |
| **用途** | `POST /api/relay/{ch}` REST リクエストを MQTT に変換して MqttRelayBridge に転送 |

ペイロードは 2-3 リレー制御コマンドと同一。

**REST API が subscribe するトピック（センサーキャッシュ用）**:

| トピック | QoS | ファイル:行 |
|---------|-----|-----------|
| `agriha/{house_id}/sensor/#` | 1 | `rest_api.py:179` |
| `agriha/farm/weather/misol` | 1 | `rest_api.py:180` |
| `agriha/{house_id}/relay/state` | 1 | `rest_api.py:181` |
| `agriha/{house_id}/ccm/#` | 0 | `rest_api.py:182` |

---

### 2-5. 緊急オーバーライド（pub）

| 項目 | 値 |
|------|-----|
| **トピック** | `agriha/{house_id}/emergency/override` |
| **方向** | pub |
| **QoS** | 1 |
| **retain** | Y |
| **ファイル:行** | `src/agriha/daemon/emergency_override.py:192,201` |
| **用途** | 物理スイッチ(DI07-DI14)による緊急オーバーライド発生時の通知 |

**ペイロード**:
```json
{
  "di_pin": 7,
  "relay_ch": 1,
  "state": true,
  "timestamp": 1771680000.0,
  "lockout_sec": 300
}
```
| フィールド | 型 | 備考 |
|-----------|-----|------|
| `di_pin` | int | DI ピン番号 (7-14) |
| `relay_ch` | int | 対応リレーチャンネル |
| `state` | bool | true=ON, false=OFF |
| `timestamp` | float | UNIX秒 |
| `lockout_sec` | int | スイッチON時: lockout秒数, OFF時: 0 |

---

## 3. DI→リレーマッピング (`emergency_override.py`)

| DI ピン | リレー ch | 備考 |
|---------|----------|------|
| DI07 | ch1 | 緊急オーバーライド対象 |
| DI08 | ch2 | |
| DI09 | ch3 | |
| DI10 | ch4 | |
| DI11 | ch5 | |
| DI12 | ch6 | |
| DI13 | ch7 | |
| DI14 | ch8 | |

---

## 4. control/ の MQTT 利用状況

| ファイル | MQTT利用 | 備考 |
|---------|---------|------|
| `src/agriha/control/plan_executor.py` | なし | REST API (`/api/sensors`) 経由でセンサー取得。直接 MQTT 接続なし |
| `src/agriha/control/forecast_engine.py` | なし | 同上 |
| `src/agriha/control/rule_engine.py` | なし | `get_sensors()` ツール呼び出し経由 |

---

## 5. 既存仕様書との差異

`docs/mqtt_topic_spec.md` (v1.1.0, 2026-02-21) との差異:

| 差異 | 仕様書 | 実コード | 重要度 |
|------|--------|---------|--------|
| 緊急トピック名 | `agriha/{house_id}/emergency` | `agriha/{house_id}/emergency/override` | ⚠️ 要修正 |
| CCM sensor types | `IntgRadiation` 未記載 | `SENSOR_TYPES` に含まれる | 軽微 |

---

## 6. トピック全体サマリー

| トピック | 方向 | QoS | retain | 実装ファイル |
|---------|------|-----|--------|------------|
| `agriha/{id}/sensor/DS18B20` | pub | 1 | Y | sensor_loop.py |
| `agriha/farm/weather/misol` | pub | 1 | Y | sensor_loop.py |
| `agriha/{id}/ccm/sensor/{type}` | pub | 0 | Y | ccm_receiver.py |
| `agriha/{id}/ccm/actuator/{type}` | pub | 0 | Y | ccm_receiver.py |
| `agriha/{id}/ccm/weather/{type}` | pub | 0 | Y | ccm_receiver.py |
| `agriha/{id}/ccm/other/{type}` | pub | 0 | Y | ccm_receiver.py |
| `agriha/{id}/relay/{ch}/set` | sub | 1 | N | mqtt_relay_bridge.py |
| `agriha/{id}/relay/{ch}/set` | pub | 1 | N | rest_api.py |
| `agriha/{id}/relay/state` | pub | 1 | Y | mqtt_relay_bridge.py |
| `agriha/{id}/relay/state` | sub | 1 | Y | rest_api.py |
| `agriha/{id}/sensor/#` | sub | 1 | — | rest_api.py |
| `agriha/farm/weather/misol` | sub | 1 | — | rest_api.py |
| `agriha/{id}/ccm/#` | sub | 0 | — | rest_api.py |
| `agriha/{id}/emergency/override` | pub | 1 | Y | emergency_override.py |
