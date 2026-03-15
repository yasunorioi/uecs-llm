# uecs-llm 内部通信仕様書

> **Version**: 1.0.0-draft
> **策定日**: 2026-03-16
> **対象ブランチ**: v4 (main マージ済み)
> **策定根拠**: mqtt_inventory.md (subtask_906) + endpoint_inventory.md (subtask_907) コード精読
> **目的**: 現状の明文化。互換性確保より実動作の記録を優先する。

---

## 目次

1. [システムアーキテクチャ](#1-システムアーキテクチャ)
2. [MQTTトピック仕様](#2-mqttトピック仕様)
3. [データソース分類](#3-データソース分類)
4. [FastAPIエンドポイント仕様](#4-fastapiエンドポイント仕様)
5. [fetcher層インターフェース定義](#5-fetcher層インターフェース定義)
6. [既知問題・要対応事項](#6-既知問題要対応事項)

---

## 1. システムアーキテクチャ

### 1-1. コンポーネント構成

```
═══════════════════════════════════════════════════════════════
  外部                          RPi (unipi@10.10.0.10)
═══════════════════════════════════════════════════════════════

  LINE Bot ──HTTPS──► nginx:80 ──► agriha-chat:8502
                                         │
  Browser  ──HTTP──►  nginx:80 ──► agriha-chat:8502
                           │
                    /api/sensors,/api/relay
                           │
                           ▼
  VC Weather API ◄── forecast_engine (cron 毎時)
                           │
═══════════════════════════════════════════════════════════════
                     MQTT Broker (mosquitto:1883)
═══════════════════════════════════════════════════════════════
       ▲                ▲                        │
       │pub             │pub              sub▼   │pub▼
  sensor_loop     ccm_receiver       MqttRelayBridge
  (DS18B20,Misol)  (UECS-CCM UDP)         │
                                          ▼
                                     I2C MCP23008
                                     Relay ch1-8
                                          ▲
═══════════════════════════════════════════════════════════════
                    unipi-daemon REST API :8080
═══════════════════════════════════════════════════════════════
  agriha-chat ──httpx──► POST /api/relay/{ch}  ──MQTT pub──► relay bridge
  forecast_engine ────► GET /api/sensors       ◄──MQTT sub──  sensor cache
  (tool calling)  ────► GET /api/status
  rule_engine     ────► GET /api/sensors (via httpx in tools)
  plan_executor   ────► POST /api/relay/{ch}

═══════════════════════════════════════════════════════════════
  物理スイッチ (DI07-14) ──GPIO──► CommandGate ──MQTT pub──► emergency/override
═══════════════════════════════════════════════════════════════
```

### 1-2. 制御レイヤー概要

| レイヤー | コンポーネント | 起動方式 | 役割 |
|---------|--------------|---------|------|
| L1 緊急 | `emergency_guard.sh` + `CommandGate` | daemon常駐 | 物理スイッチ割り込み・ロックアウト |
| L2 ルール | `rule_engine.py` | cron（毎分） | 閾値ベース即時制御 |
| L3 LLM予報 | `forecast_engine.py` | cron（毎時） | LLM 1時間計画生成 |
| L3 実行 | `plan_executor.py` | cron（毎分） | L3計画を実際のAPI呼び出しに変換 |
| UI | `app.py` (agriha-chat) | systemd常駐 | ダッシュボード・設定・LINE Bot |
| Daemon | `main.py` (unipi-daemon) | systemd常駐 | MQTT/REST/GPIO管理 |

---

## 2. MQTTトピック仕様

### 2-1. 命名規則

```
agriha/{house_id}/{category}/{sub_category}[/{item}]
```

| 要素 | 値 | 備考 |
|------|-----|------|
| `agriha` | 固定プレフィックス | greenhouse/ 系列とは別系統 |
| `{house_id}` | `h01` (デフォルト) | `daemon.house_id` で設定 |
| `{category}` | `sensor` / `ccm` / `relay` / `emergency` | |
| 例外 | `agriha/farm/weather/misol` | house_id 非依存の農場共通トピック |

### 2-2. ブローカー設定

| 設定 | 値 | 設定箇所 |
|------|-----|---------|
| ブローカー | `localhost` | `config/unipi_daemon.example.yaml: mqtt.broker` |
| ポート | `1883` | `mqtt.port` |
| keepalive | 60秒 | `mqtt.keepalive` |

### 2-3. トピック一覧

#### センサー系（pub のみ）

| トピック | pub元 | QoS | retain | 説明 |
|---------|-------|-----|--------|------|
| `agriha/{house_id}/sensor/DS18B20` | sensor_loop.py | 1 | Y | DS18B20 1-Wire温度 |
| `agriha/farm/weather/misol` | sensor_loop.py | 1 | Y | Misol WH65LP 外気象 |
| `agriha/{house_id}/ccm/sensor/{type}` | ccm_receiver.py | 0 | Y | UECS-CCM 内気象センサー |
| `agriha/{house_id}/ccm/actuator/{type}` | ccm_receiver.py | 0 | Y | UECS-CCM アクチュエータ状態 |
| `agriha/{house_id}/ccm/weather/{type}` | ccm_receiver.py | 0 | Y | UECS-CCM 外気象 |
| `agriha/{house_id}/ccm/other/{type}` | ccm_receiver.py | 0 | Y | UECS-CCM 未分類 |

#### リレー制御系（pub/sub）

| トピック | 方向 | QoS | retain | 説明 |
|---------|------|-----|--------|------|
| `agriha/{house_id}/relay/{ch}/set` | sub (mqtt_relay_bridge) | 1 | N | リレー制御コマンド受信 |
| `agriha/{house_id}/relay/{ch}/set` | pub (rest_api) | 1 | N | REST→MQTT変換（agriha-chat経由） |
| `agriha/{house_id}/relay/state` | pub (mqtt_relay_bridge) | 1 | Y | 全chリレー状態 |
| `agriha/{house_id}/relay/state` | sub (rest_api キャッシュ) | 1 | — | 状態キャッシュ用 |

#### 緊急系

| トピック | 方向 | QoS | retain | 説明 |
|---------|------|-----|--------|------|
| `agriha/{house_id}/emergency/override` | pub (emergency_override) | 1 | Y | 緊急オーバーライド通知 |

#### REST API センサーキャッシュ用 sub（rest_api.py のみ）

| トピック | QoS |
|---------|-----|
| `agriha/{house_id}/sensor/#` | 1 |
| `agriha/farm/weather/misol` | 1 |
| `agriha/{house_id}/relay/state` | 1 |
| `agriha/{house_id}/ccm/#` | 0 |

### 2-4. ペイロードスキーマ

#### DS18B20 温度センサー

```json
{
  "device_id": "28-00000de13271",
  "temperature_c": 23.5,
  "timestamp": 1771680000.0
}
```

| フィールド | 型 | 単位 | 必須 |
|-----------|-----|------|------|
| `device_id` | string | — | ✓ |
| `temperature_c` | float | ℃ | ✓ |
| `timestamp` | float | UNIX秒 | ✓ |

#### Misol WH65LP 外気象

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

| フィールド | 型 | 単位 | null可 | 必須 |
|-----------|-----|------|--------|------|
| `wind_dir_deg` | int\|null | 度 | ✓ | ✓ |
| `temperature_c` | float\|null | ℃ | ✓ | ✓ |
| `humidity_pct` | int | % | — | ✓ |
| `wind_speed_ms` | float\|null | m/s | ✓ | ✓ |
| `gust_speed_ms` | float\|null | m/s | ✓ | ✓ |
| `rainfall_mm` | float | mm(累積) | — | ✓ |
| `uv_wm2` | float\|null | W/m² | ✓ | ✓ |
| `light_lux` | float\|null | lux | ✓ | ✓ |
| `pressure_hpa` | float\|null | hPa | ✓ | ✓ (拡張フレーム) |
| `battery_low` | bool | — | — | ✓ |
| `timestamp` | float | UNIX秒 | — | ✓ |

#### UECS-CCM 全カテゴリ共通

```json
{
  "ccm_type": "InAirTemp",
  "value": 23.5,
  "room": 1,
  "region": 1,
  "order": 1,
  "priority": 29,
  "level": "S",
  "source_ip": "192.168.1.70",
  "timestamp": "2026-03-16T01:00:00+00:00"
}
```

| フィールド | 型 | 必須 | 備考 |
|-----------|-----|------|------|
| `ccm_type` | string | ✓ | CCM DATA type属性（.mC/.cMC/.MC サフィックス除去済み） |
| `value` | float\|string | ✓ | 数値変換失敗時は文字列 |
| `room` | int | ✓ | デフォルト1 |
| `region` | int | ✓ | デフォルト1 |
| `order` | int | ✓ | デフォルト1 |
| `priority` | int | ✓ | デフォルト29 |
| `level` | string | ✓ | `"S"` 等 |
| `source_ip` | string | ✓ | 送信元IP |
| `timestamp` | string | ✓ | ISO 8601 UTC |

#### リレー制御コマンド（relay/{ch}/set）

```json
{
  "value": 1,
  "duration_sec": 180.0,
  "reason": "LLM: 灌水開始"
}
```

| フィールド | 型 | 値域 | 必須 |
|-----------|-----|------|------|
| `value` | int | 0=OFF, 1=ON | ✓ |
| `duration_sec` | float | ≥0 (0=タイマーなし) | — |
| `reason` | string | — | — |

#### リレー状態（relay/state）

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

#### 緊急オーバーライド（emergency/override）

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
| `relay_ch` | int | 対応リレーch |
| `state` | bool | true=ON, false=OFF |
| `timestamp` | float | UNIX秒 |
| `lockout_sec` | int | ON時: ロックアウト秒数 / OFF時: 0 |

### 2-5. タイムスタンプ形式

| データ源 | 形式 | 例 |
|---------|------|-----|
| sensor_loop, mqtt_relay_bridge, emergency_override | float (UNIX秒) `time.time()` | `1771680000.0` |
| ccm_receiver | string ISO 8601 UTC | `"2026-03-16T01:00:00+00:00"` |

---

## 3. データソース分類

### 3-1. ローカルセンサー

| センサー | MQTTトピック | 更新頻度 | 実装 |
|---------|------------|---------|------|
| DS18B20 (1-Wire温度) | `agriha/{id}/sensor/DS18B20` | `sensor_interval_sec`毎(デフォルト10秒) | sensor_loop.py |
| Misol WH65LP (外気象) | `agriha/farm/weather/misol` | UART受信毎 (約60秒) | sensor_loop.py |
| UECS-CCM (内気象・アクチュエータ) | `agriha/{id}/ccm/{category}/{type}` | CCMマルチキャスト受信毎 | ccm_receiver.py |

**UECS-CCM センサー種別**:

| 分類 | ccm_type一覧 |
|------|-------------|
| 内気象センサー | `InAirTemp`, `InAirHumid`, `InAirCO2`, `SoilTemp`, `InRadiation`, `SoilEC`, `SoilWC`, `Pulse`, `InAirHD`, `InAirAbsHumid`, `InAirDP`, `IntgRadiation` |
| アクチュエータ | `Irri`, `VenFan`, `CirHoriFan`, `AirHeatBurn`, `AirHeatHP`, `CO2Burn`, `VenRfWin`, `VenSdWin`, `ThCrtn`, `LsCrtn`, `AirCoolHP`, `AirHumFog` |
| 外気象 | `WAirTemp`, `WAirHumid`, `WWindSpeed`, `WWindDir16`, `WRainfall`, `WRainfallAmt`, `WLUX` |

### 3-2. アクチュエータ（リレー ch1-8）

設定ファイル: `config/channel_map.yaml` → デプロイ先: `/etc/agriha/channel_map.yaml`

| ch | ラベル | 種別 | 備考 |
|----|--------|------|------|
| 1 | 暖房 | 暖房 | |
| 2 | 循環扇 | ファン | |
| 3 | CO2発生器 | CO2 | |
| 4 | 灌水ポンプ | 灌水 | `irrigation.channel: 4` |
| 5 | 北側窓(開) | 側窓 | `side_window.groups[0].open_channel` |
| 6 | 北側窓(閉) | 側窓 | `side_window.groups[0].close_channel` |
| 7 | 南側窓(閉) | 側窓 | `side_window.groups[1].close_channel` |
| 8 | 南側窓(開) | 側窓 | `side_window.groups[1].open_channel` |

**DI→リレー緊急マッピング** (`emergency_override.py`):
DI07→ch1, DI08→ch2, ..., DI14→ch8（1:1対応）

**強風閉方角** (`channel_map.yaml` の `wind_close_directions`):
- 北側窓: `[1, 2, 16]`（北北東、北東、北）
- 南側窓: `[8, 9, 10]`（南南西、南西、西南西）

### 3-3. 外部API

| API | 用途 | 実装箇所 | 設定 |
|-----|------|---------|------|
| Visual Crossing Timeline API | 24時間天気予報（LLM予報エンジン向け） | `forecast_engine.py: fetch_weather_forecast()` | 環境変数 `VC_API_KEY`、緯度 `42.888` / 経度 `141.603` |

キャッシュ: `/var/lib/agriha/vc_cache.json`、TTL=1時間（API失敗時は古いキャッシュにフォールバック）

---

## 4. FastAPIエンドポイント仕様

### 4-1. ネットワーク構成

```
外部クライアント
      │
      ▼
 nginx :80
  ├── /                    → agriha-chat :8502 (⚠️ nginx.confは8501: §6参照)
  ├── /api/sensors         → unipi-daemon :8080
  └── /api/relay           → unipi-daemon :8080

unipi-daemon :8080 (ローカル直接アクセスも可)
agriha-chat  :8502 (ローカル直接アクセスも可)
```

### 4-2. unipi-daemon REST API（4件）

**ベースURL**: `http://localhost:8080`
**認証**: `X-API-Key` ヘッダー（`rest_api.api_key` が空文字の場合スキップ）
**実装**: `src/agriha/daemon/rest_api.py`

| # | メソッド | パス | リクエスト | レスポンス | ステータス |
|---|---------|------|-----------|-----------|----------|
| D1 | POST | `/api/relay/{ch}` | Path: `ch` (1-8)<br>Body: RelaySetRequest | `{"ch": N, "value": 0\|1, "queued": true}` | 202/423/503/403 |
| D2 | GET | `/api/sensors` | — | `{"sensors": {topic: data}, "updated_at": float, "age_sec": float\|null}` | 200/403 |
| D3 | GET | `/api/status` | — | `{"house_id", "uptime_sec", "locked_out", "lockout_remaining_sec", "relay_state", "ts"}` | 200/403 |
| D4 | POST | `/api/emergency/clear` | — | `{"cleared": true, "was_locked_out": bool}` | 200/403 |

**ロックアウト仕様**:
- 物理スイッチ ON 時に 300秒のロックアウト開始
- ロックアウト中に `POST /api/relay/{ch}` → 423 Locked
- `POST /api/emergency/clear` で手動解除可

**RelaySetRequest スキーマ**:

```json
{
  "value": 1,
  "duration_sec": 0.0,
  "reason": ""
}
```

### 4-3. agriha-chat（WebUI + LINE Bot）（15件）

**ベースURL**: `http://localhost:8502`（nginx経由は port 80）
**認証**: HTTP Basic（環境変数 `UI_AUTH_USER` / `UI_AUTH_PASS`、デフォルト `admin/agriha`）
**実装**: `src/agriha/chat/app.py`

#### HTML 画面（4件）

| # | メソッド | パス | 説明 |
|---|---------|------|------|
| U1 | GET | `/` | ダッシュボード（センサー・計画・リレー状態） |
| U2 | GET | `/api/dashboard-partial` | htmx ポーリング用部分テンプレート |
| U3 | GET | `/settings` | 設定画面（Query: `saved`, `error`） |
| U4 | GET | `/history` | 制御履歴（直近24時間の decisions） |

#### 設定保存（6件 / 全て POST → 303 リダイレクト）

| # | パス | 保存対象 | バリデーション |
|---|------|---------|--------------|
| S1 | `/settings/prompt` | `system_prompt.txt` | なし |
| S2 | `/settings/thresholds` | `thresholds.yaml` | `high_temp > low_temp`, `0 < co2 < 5000` |
| S3 | `/settings/rules` | `rules.yaml` | `yaml.safe_load` 構文チェック |
| S4 | `/settings/channel_map` | `channel_map.yaml` | `yaml.safe_load` 構文チェック |
| S5 | `/settings/forecast` | `forecast.yaml` | `yaml.safe_load` 構文チェック |
| S6 | `/settings/llm_provider` | `forecast.yaml` + `.env` | provider/model/api_key |

設定保存バックアップ方式: `.bak.{タイムスタンプ}` を同ディレクトリに生成

#### LINE Bot Webhook（1件）

| # | メソッド | パス | 説明 |
|---|---------|------|------|
| L1 | POST | `/callback` | LINE署名検証 → LLM tool calling → Reply |

**LINE Bot LLMツール**:

| ツール | 内部呼び出し | 備考 |
|--------|------------|------|
| `get_sensors` | GET `/api/sensors` (daemon:8080) | |
| `get_status` | GET `/api/status` (daemon:8080) | |
| `set_relay` | POST `/api/relay/{ch}` (daemon:8080) | NullClawクライアント時は無効化 |

#### JSON API（4件）

| # | メソッド | パス | 説明 |
|---|---------|------|------|
| J1 | GET | `/api/flags` | `{"lockout": bool, "rain": bool, "wind": bool}` |
| J2 | GET | `/api/plan` | `current_plan.json` の内容 or `{"plan": null}` |
| J3 | GET | `/api/dashboard` | センサー・計画・relay・フラグ・ログ集約JSON |
| J4 | GET | `/api/logs` | 4ログファイル末尾N行（`?lines=50`, max 200） |

---

## 5. fetcher層インターフェース定義

### 5-1. 現状のfetcher実装

現在は `forecast_engine.py` に直接実装されている。

| fetcher | 関数 | 外部 | 設定 |
|---------|------|------|------|
| Visual Crossing 天気予報 | `fetch_weather_forecast(lat, lon)` | Visual Crossing Timeline API | `VC_API_KEY` env |
| センサー取得 | `get_sensor_data(base_url, api_key)` | unipi-daemon REST | `UNIPI_API_URL` / `UNIPI_API_KEY` |
| 状態取得 | `get_status(base_url, api_key)` | unipi-daemon REST | 同上 |

### 5-2. 将来拡張ポイント

外部データソース追加時は `forecast_engine.py` 内の以下パターンに合わせることを推奨する:

```python
def fetch_{source}(
    **config_params
) -> dict[str, Any] | None:
    """
    Returns:
        データdict、またはNone（設定未完・エラー時）
    """
    api_key = os.environ.get("{SOURCE}_API_KEY", "")
    if not api_key:
        logger.warning("{SOURCE}_API_KEY未設定、スキップ")
        return None
    try:
        # HTTP呼び出し (urllib or httpx)
        ...
        return data
    except Exception as exc:
        logger.warning("{source} fetch failed: %s", exc)
        return None
```

**設計方針**:
- 失敗時は `None` を返す（例外を上位に伝播しない）
- `logger.warning` でスキップを記録する
- キャッシュが必要な場合は `/var/lib/agriha/{source}_cache.json`、TTL=1時間
- 環境変数でAPIキー管理（`.env` に保存、`python-dotenv` で読み込み）

### 5-3. 拡張例（将来参考）

| 拡張源 | 用途 | 追加env |
|--------|------|---------|
| Open-Meteo（無料） | 天気予報（VC代替） | 不要 |
| NARO農業気象サービス | 農業専用気象データ | `NARO_API_KEY` |
| 農家独自センサー（REST） | 追加センサー統合 | `CUSTOM_SENSOR_URL` |

---

## 6. 既知問題・要対応事項

### 6-1. emergencyトピック名の不一致 ⚠️

| 場所 | トピック名 | 状態 |
|------|-----------|------|
| `docs/mqtt_topic_spec.md` v1.1.0 | `agriha/{house_id}/emergency` | ❌ 誤り |
| `src/agriha/daemon/emergency_override.py:192` | `agriha/{house_id}/emergency/override` | ✓ 実コード |

**影響**: mqtt_topic_spec.md のみ誤記。実動作には影響なし。
**対応**: mqtt_topic_spec.md のトピック名と QoS/retain テーブルを修正する。

### 6-2. nginx ポート不一致 ⚠️

| 場所 | ポート | 状態 |
|------|--------|------|
| `config/nginx.conf:7` | `8501` | ❌ 要確認 |
| `src/agriha/chat/app.py:5` (docstring) | `8502` | ✓ 実コード |

**影響**: nginx経由のUI アクセスが失敗する可能性がある。
**対応**: 実際の起動コマンドを確認し、nginx.conf または起動ポートを統一する。
推定: `systemd/agriha-chat.service` に `--port` 指定があるはずなので確認要。

### 6-3. CORS未設定

| 項目 | 状態 |
|------|------|
| agriha-chat (app.py) | CORSMiddleware 未追加 |
| unipi-daemon (rest_api.py) | CORSMiddleware 未追加 |

**影響**: 外部ドメインからのブラウザJSリクエストは CORS エラー。
**現状**: 同一ホスト nginx 経由のみのためブラウザ直接アクセスなら問題なし。
**対応**: 外部からのAPI呼び出しが必要になったら `fastapi.middleware.cors` を追加する。

### 6-4. nginx 未公開エンドポイント

nginx経由では到達不可（直接 localhost:8080 or :8502 アクセスのみ）:
- `GET /api/status`
- `POST /api/emergency/clear`
- `GET /api/logs`
- `GET /history`
- `GET /api/plan`
- `GET /api/flags`

**影響**: LAN内からのみアクセス可。外部公開用途なら nginx設定追加が必要。

---

## 改版履歴

| バージョン | 日付 | 変更内容 |
|-----------|------|---------|
| 1.0.0-draft | 2026-03-16 | 初版（mqtt_inventory.md + endpoint_inventory.md 統合） |
