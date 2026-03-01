# v2 三層制御 詳細設計書

> **Version**: 1.2 (殿裁定: MAJOR-2/3 案B採用 — 下層が上層を黙らせる原則)
> **Date**: 2026-03-01
> **Status**: Approved (監査合格 audit_060, 殿裁定完了)
> **Parent**: llm_control_loop_design.md v3.0
> **Branch**: v2-three-layer

---

## 概要

本設計書は llm_control_loop_design.md v3.0 で定義された三層制御アーキテクチャの**実装詳細**を定める。
v3.0 が「何を作るか」を定義するのに対し、本書は「どう作るか」を定義する。

### 設計原則

1. **下位層は上位層の障害に影響されない** — 各層は独立プロセス/スクリプトとして動作
2. **既存稼働機能を壊さない** — LINE Bot リモート制御、CommandGate は現行のまま維持
3. **unipi-daemon は触らない** — REST API の消費者として設計
4. **マクガイバー精神** — シンプル・ローコスト・手元の道具で解決

### 対象スコープ

```
新規作成:
  /opt/agriha-control/emergency_guard.sh   ... Layer 1（緊急停止）
  /opt/agriha-control/rule_engine.py       ... Layer 2（ルールベース）
  /opt/agriha-control/forecast_engine.py   ... Layer 3（LLM 1時間予報）
  /opt/agriha-control/plan_executor.py     ... Layer 3 補助（計画実行）
  /etc/agriha/layer1.env                    ... Layer 1 設定（シェル変数）
  /etc/agriha/layer2_config.yaml           ... Layer 2 設定
  /etc/agriha/layer3_config.yaml           ... Layer 3 設定

既存変更なし:
  unipi-daemon (REST API, MQTT, CommandGate, MqttRelayBridge)
  LINE Bot (linebot/app.py, rpi_client.py)

既存リファクタリング:
  agriha_control.py → forecast_engine.py に発展的移行
```

---

## 目次

1. [各スクリプトの I/O 定義](#1-各スクリプトの-io-定義)
2. [層間インターフェース](#2-層間インターフェース)
3. [エラーハンドリング](#3-エラーハンドリング)
4. [cron 設定の全容](#4-cron-設定の全容)
5. [既存コードとの接続点](#5-既存コードとの接続点)
6. [設定ファイル一覧](#6-設定ファイル一覧)
7. [テスト戦略](#7-テスト戦略)
8. [付録: MCP23008 ビットマッピング](#付録-mcp23008-ビットマッピング)
9. [未決事項（殿裁定待ち）](#未決事項殿裁定待ち)

---

## 1. 各スクリプトの I/O 定義

### 1.1 emergency_guard.sh (Layer 1: 爆発)

**言語**: bash (POSIX sh 互換)
**起動**: cron 毎分 (`* * * * *`)
**実行時間**: < 3 秒（curl 2 回 + 条件分岐のみ）

```
┌──────────────────────────────────────────────────────────────┐
│ emergency_guard.sh                                           │
│                                                              │
│ 入力:                                                        │
│   [API] GET /api/sensors → InAirTemp (内気温, CCM経由)       │
│   [API] GET /api/status  → locked_out (ロックアウト状態)     │
│   [FILE] /etc/agriha/layer1.env（シェル変数ファイル）         │
│         → source で読み込み。bashのYAMLパース不要            │
│         → HIGH_TEMP_THRESHOLD (default: 27)                  │
│         → LOW_TEMP_THRESHOLD  (default: 16)                  │
│         → WINDOW_CHANNELS     (default: "5 6 7 8")           │
│         → LINE_CHANNEL_ACCESS_TOKEN, LINE_GROUP_ID           │
│   [FILE] /var/lib/agriha/lockout_state.json                  │
│         → Layer 1 ロックアウト状態（§2.1 参照）              │
│                                                              │
│ 出力:                                                        │
│   [API] POST /api/relay/{ch} → リレー ON/OFF                │
│   [API] POST LINE Messaging API → 緊急通知                  │
│   [FILE] /var/lib/agriha/lockout_state.json                  │
│         → 緊急発動時にロックアウト状態を書き込み             │
│   [LOG] >> /var/log/agriha/emergency.log                     │
│                                                              │
│ 使用外部コマンド:                                            │
│   curl, python3 -c (JSON パース), bc (数値比較), date        │
│                                                              │
│ 依存パッケージ:                                              │
│   なし（OS 標準コマンドのみ）                                │
└──────────────────────────────────────────────────────────────┘
```

**処理フロー**:

```
cron (* * * * *) → emergency_guard.sh 起動
  │
  ├─ Step 1: ロックアウト状態確認
  │   └─ /var/lib/agriha/lockout_state.json 読み込み
  │       └─ CommandGate ロックアウト中 → スキップ（unipi-daemon が 423 を返すため）
  │       └─ Layer 1 自身のロックアウト中（発動後5分） → スキップ（連打防止）
  │
  ├─ Step 2: センサーデータ取得
  │   └─ curl -s http://localhost:8080/api/sensors
  │       └─ 失敗 → ログ出力して終了（安全側: 操作しない）
  │
  ├─ Step 3: 内気温抽出
  │   └─ python3 -c でJSONから InAirTemp を取得
  │       └─ CCM データなし → Misol 外気温で代替判断
  │       └─ 両方なし → ログ出力して終了
  │
  ├─ Step 4: 閾値判定
  │   ├─ TEMP > high_threshold (27℃)
  │   │   └─ 全窓全開: ch5-8 を value=1 で POST
  │   │   └─ LINE 通知: 「🚨 {TEMP}℃ 緊急全開」
  │   │   └─ lockout_state.json 更新（5分ロックアウト）
  │   │
  │   ├─ TEMP < low_threshold (16℃)
  │   │   └─ 全窓全閉: ch5-8 を value=0 で POST
  │   │   └─ LINE 通知: 「🚨 {TEMP}℃ 緊急全閉」
  │   │   └─ lockout_state.json 更新（5分ロックアウト）
  │   │
  │   └─ 範囲内 → 何もしない（正常終了）
  │
  └─ 終了（プロセス終了、次のcron起動まで待機）
```

**Layer 1 ロックアウト（連打防止）**:

Layer 1 が緊急発動した後、5 分間は再発動しない。
これは unipi-daemon の CommandGate ロックアウト (300秒) とは別系統。
CommandGate はDI物理スイッチ用、Layer 1 ロックアウトは自身の連打防止用。

```json
// /var/lib/agriha/lockout_state.json
{
  "layer1_lockout_until": "2026-03-01T14:05:00+09:00",
  "last_action": "emergency_open",
  "last_temp": 28.3,
  "last_triggered_at": "2026-03-01T14:00:00+09:00"
}
```

### 1.2 rule_engine.py (Layer 2: ガムテ)

**言語**: Python 3.11+
**起動**: cron 5 分毎 (`*/5 * * * *`)
**実行時間**: < 5 秒（REST API 呼び出し + ルール評価）

```
┌──────────────────────────────────────────────────────────────┐
│ rule_engine.py                                               │
│                                                              │
│ 入力:                                                        │
│   [API] GET /api/sensors                                     │
│         → CCM: InAirTemp, InAirHumi, CO2, InSolar           │
│         → DS18B20: temperature_c                             │
│         → Misol: temperature_c, humidity_pct, wind_speed_ms, │
│                  wind_direction, rainfall, uv_index,         │
│                  solar_radiation_wm2                         │
│   [API] GET /api/status                                      │
│         → relay_state (ch1-8), locked_out                    │
│   [FILE] /etc/agriha/layer2_config.yaml                      │
│         → ルール定義（温度閾値、日射比例パラメータ等）       │
│   [FILE] /etc/agriha/crop_irrigation.yaml                    │
│         → 作物ステージ別灌水パラメータ                      │
│   [FILE] /var/lib/agriha/lockout_state.json                  │
│         → Layer 1 ロックアウト状態                           │
│   [FILE] /var/lib/agriha/current_plan.json                   │
│         → Layer 3 アクション計画（存在すれば参照）           │
│   [FILE] /var/lib/agriha/solar_accumulator.json              │
│         → 日射積算値（日射比例灌水用）                      │
│   [CALC] astral: 日の出/日没計算                             │
│                                                              │
│ 出力:                                                        │
│   [API] POST /api/relay/{ch} → リレー ON/OFF                │
│   [FILE] /var/lib/agriha/solar_accumulator.json              │
│         → 日射積算値更新                                    │
│   [FILE] /var/lib/agriha/rule_engine_state.json              │
│         → 最終実行状態（次回参照用）                        │
│   [LOG] >> /var/log/agriha/rule_engine.log                   │
│                                                              │
│ 依存パッケージ:                                              │
│   httpx, pyyaml, astral                                      │
└──────────────────────────────────────────────────────────────┘
```

**処理フロー**:

```
cron (*/5 * * * *) → rule_engine.py 起動
  │
  ├─ Step 1: ロックアウト確認
  │   ├─ lockout_state.json → Layer 1 ロックアウト中 → スキップ
  │   └─ GET /api/status → CommandGate ロックアウト中 → スキップ
  │
  ├─ Step 2: 設定読み込み
  │   ├─ layer2_config.yaml（ルール定義）
  │   └─ crop_irrigation.yaml（灌水パラメータ）
  │
  ├─ Step 3: センサーデータ取得
  │   └─ GET /api/sensors + GET /api/status
  │
  ├─ Step 4: 日の出/日没計算
  │   └─ astral → 時間帯判定（日の出前/日中/日没前1h/日没後）
  │
  ├─ Step 5: Layer 3 計画確認（オプション）
  │   └─ current_plan.json が存在し valid_until 内
  │       → Layer 3 の計画を優先（Layer 2 は補助に回る）
  │       → current_plan.json なし or 期限切れ → Layer 2 が全権制御
  │
  ├─ Step 6: ルール評価（優先順位順）
  │   ├─ 6a: 降雨チェック（rainfall > 0.5mm/h → 全窓閉）
  │   ├─ 6b: 強風チェック（wind_speed > 5m/s → 風上側閉）
  │   ├─ 6c: 時間帯制御（日没後 → 全窓閉、日の出前 → 全窓閉）
  │   ├─ 6d: 温度制御（目標温度 ± マージン → 側窓開閉）
  │   └─ 6e: 日射比例灌水（積算日射 > 閾値 → 灌水実行）
  │
  ├─ Step 7: アクション実行
  │   └─ POST /api/relay/{ch} （変更がある場合のみ）
  │
  ├─ Step 8: 状態保存
  │   ├─ solar_accumulator.json 更新
  │   └─ rule_engine_state.json 更新
  │
  └─ 終了
```

**日射比例灌水の仕組み**:

```
solar_accumulator.json:
{
  "date": "2026-03-01",
  "accumulated_mj": 2.45,
  "irrigations_today": 3,
  "last_irrigation_at": "2026-03-01T11:30:00+09:00",
  "last_updated_at": "2026-03-01T12:00:00+09:00"
}

計算:
  5分間の日射量 = InSolar (W/m²) × 300秒 / 1,000,000 (MJ変換)
  accumulated_mj += 5分間の日射量
  IF accumulated_mj >= solar_threshold_mj (0.9 MJ):
      灌水実行（ch4 ON, duration_sec from crop_irrigation.yaml）
      accumulated_mj = 0（リセット）
      irrigations_today += 1
  日付変更 → accumulated_mj = 0, irrigations_today = 0
```

**温度制御ロジック**:

```python
# 側窓制御（温度閾値ベース）
target_temp = 26.0  # layer2_config.yaml から
margin_open = 2.0   # 目標+2℃で開
margin_close = 1.0  # 目標-1℃で閉

if temp > target_temp + margin_open:
    # 窓開（ch5-8 の該当チャンネルを ON）
    # duration_sec で開度を秒数制御（全開60秒の30%なら18秒）
elif temp < target_temp - margin_close:
    # 窓閉（ch5-8 の該当チャンネルを OFF）
```

### 1.3 forecast_engine.py (Layer 3: 知恵)

**言語**: Python 3.11+
**起動**: cron 毎時 (`0 * * * *`)
**実行時間**: < 30 秒（Claude Haiku API 応答 + ツール呼び出し）

```
┌──────────────────────────────────────────────────────────────┐
│ forecast_engine.py                                           │
│                                                              │
│ 入力:                                                        │
│   [API] Claude Haiku API (Anthropic SDK)                     │
│         → tools: get_sensors, get_status（set_relay除外）    │
│   [API] GET /api/sensors (via tool calling)                   │
│   [API] GET /api/status  (via tool calling)                   │
│   [FILE] /etc/agriha/system_prompt.txt                       │
│         → [A]-[G] セクション                                │
│   [FILE] /etc/agriha/layer3_config.yaml                      │
│         → claude_model, max_tokens, max_tool_rounds等        │
│   [FILE] /var/lib/agriha/control_log.db (SQLite)             │
│         → 直近3件の判断履歴                                  │
│   [FILE] /var/lib/agriha/lockout_state.json                  │
│         → Layer 1 ロックアウト状態                           │
│   [CALC] astral: 日の出/日没計算                             │
│                                                              │
│ 出力:                                                        │
│   [FILE] /var/lib/agriha/current_plan.json                   │
│         → 1時間アクション計画 JSON                          │
│   [FILE] /var/lib/agriha/control_log.db                      │
│         → 判断ログ（summary, actions, sensor_snapshot）      │
│   [FILE] /var/lib/agriha/last_decision.json                  │
│         → 最終判断状態                                      │
│   [LOG] >> /var/log/agriha/control.log                       │
│                                                              │
│ 依存パッケージ:                                              │
│   anthropic, httpx, pyyaml, astral                           │
└──────────────────────────────────────────────────────────────┘
```

**処理フロー**:

```
cron (0 * * * *) → forecast_engine.py 起動
  │
  ├─ Step 1: ロックアウト確認
  │   ├─ lockout_state.json → Layer 1 ロックアウト中 → **計画生成スキップ、即終了**
  │   │   （殿裁定: 下層が上層を黙らせる原則。lockout中の計画生成は
  │   │    閉窓ループの原因となるため、5分間のCO2/露点判断停止は許容）
  │   └─ GET /api/status → CommandGate ロックアウト中 → **計画生成スキップ、即終了**
  │
  ├─ Step 2: 設定読み込み
  │   ├─ layer3_config.yaml（API設定）
  │   └─ system_prompt.txt（[A]-[G]）
  │
  ├─ Step 3: 直近判断履歴読み込み
  │   └─ control_log.db → 直近3件のsummary+actions
  │
  ├─ Step 4: 日の出/日没計算 + 時間帯注入
  │   └─ astral → 時間帯4区分
  │
  ├─ Step 5: Claude Haiku API 呼び出し
  │   ├─ system: system_prompt.txt
  │   ├─ user: 履歴 + 日時情報 + 1時間予報指示
  │   ├─ tools: [get_sensors, get_status]（set_relay除外）
  │   └─ tool calling ループ（max 5 rounds）
  │       ├─ Round 0: LLM が get_sensors 呼び出し → センサーデータ取得
  │       ├─ Round 1: LLM が get_status 呼び出し → リレー状態確認
  │       ├─ Round 2: LLM が判断 → 計画 JSON 生成（リレー操作はしない）
  │       └─ end_turn → 最終応答（計画 JSON + 判断理由）
  │
  │   **重要**: forecast_engine は計画を生成するだけ。
  │   リレー操作は plan_executor.py が唯一の実行者。
  │   これにより、LLM即時実行 + plan_executor再実行の二重実行リスクを排除する。
  │
  ├─ Step 6: current_plan.json 保存（スキーマ検証付き）
  │   └─ LLM 応答から JSON ブロック抽出
  │   └─ actions[] の各アクションをバリデーション:
  │       ✓ relay_ch ∈ [1, 8]（範囲外 → そのアクションをスキップ）
  │       ✓ value ∈ [0, 1]（範囲外 → スキップ）
  │       ✓ duration_sec ≤ 3600（超過 → 3600に切り詰め、ログ警告）
  │       ✓ execute_at が ISO8601 でパース可能（不正 → スキップ）
  │       ※ スキップされたアクションはログに記録（LLMハレーション検知用）
  │   └─ 検証済み JSON をファイル書き込み
  │
  ├─ Step 7: 判断ログ保存
  │   └─ control_log.db に INSERT
  │
  ├─ Step 8: last_decision.json 更新
  │
  └─ 終了
```

**agriha_control.py (RPi版) からの発展的移行**:

forecast_engine.py は既存の `agriha_control.py` (RPi 版, 756 行) をベースに作成する。

| agriha_control.py の機能 | forecast_engine.py での扱い |
|--------------------------|---------------------------|
| `init_db()` / `save_decision()` / `load_recent_history()` | **そのまま再利用** |
| `call_tool()` | **再利用（ただし set_relay は除外）** |
| `_to_anthropic_tools()` / `TOOLS` 定義 | **再利用（set_relay をツール定義から除外）** |
| `llm_chat_claude()` | **そのまま再利用** |
| `get_sun_times()` / `get_time_period()` | **そのまま再利用** |
| `load_last_decision()` / `save_last_decision()` | **そのまま再利用** |
| `check_failsafe_rules()` / `apply_failsafe()` | **Layer 1/2 に移管、削除** |
| `run_control_loop()` 本体 | **リファクタリング**: フェイルセーフ部分を除去し、1時間予報に特化 |
| `DEFAULT_CONFIG` | **layer3_config.yaml に移行** |
| cron 間隔 `*/10` | **`0 * * * *` (毎時) に変更** |

### 1.4 plan_executor.py (Layer 3 補助)

**言語**: Python 3.11+
**起動**: cron 毎分 (`* * * * *`)、ただし Layer 1 の後に実行
**実行時間**: < 3 秒

```
┌──────────────────────────────────────────────────────────────┐
│ plan_executor.py                                             │
│                                                              │
│ 入力:                                                        │
│   [FILE] /var/lib/agriha/current_plan.json                   │
│         → actions[].execute_at, relay_ch, value, duration_sec│
│   [FILE] /var/lib/agriha/lockout_state.json                  │
│         → Layer 1 ロックアウト状態                           │
│   [API] GET /api/status → locked_out                         │
│                                                              │
│ 出力:                                                        │
│   [API] POST /api/relay/{ch} → 計画通りにリレー制御          │
│   [FILE] /var/lib/agriha/current_plan.json                   │
│         → 実行済みアクションに executed=true マーク          │
│   [LOG] >> /var/log/agriha/plan_executor.log                 │
│                                                              │
│ 依存パッケージ:                                              │
│   httpx, pyyaml                                              │
└──────────────────────────────────────────────────────────────┘
```

**処理フロー**:

```
cron (* * * * *) → plan_executor.py 起動
  │
  ├─ Step 1: current_plan.json 読み込み
  │   └─ ファイルなし or valid_until 期限切れ → 何もせず終了
  │
  ├─ Step 2: ロックアウト確認
  │   └─ Layer 1 or CommandGate ロックアウト中 → 何もせず終了
  │
  ├─ Step 3: 降雨/強風チェック（殿裁定: 下層が上層を黙らせる原則）
  │   └─ GET /api/sensors → rainfall, wind_speed 取得
  │       rainfall > 0.5mm/h or wind_speed > 5m/s → 側窓操作(relay_ch ∈ window_channels)をスキップ
  │       ※ 閾値は layer2_config.yaml と同一値を参照（重複定義回避）
  │       ※ 灌水・換気扇等の側窓以外のアクションは影響なし
  │
  ├─ Step 4: 実行可能アクション抽出
  │   └─ actions[] を走査:
  │       execute_at は ISO8601 絶対時刻（datetime.fromisoformat() でパース）
  │       現在時刻が execute_at を過ぎていて、executed != true → 実行対象
  │       Step 3 で側窓スキップ対象 → executed: "skipped_weather" として記録
  │
  ├─ Step 5: アクション実行
  │   └─ POST /api/relay/{ch}
  │       └─ 423 応答（ロックアウト）→ スキップ、次回リトライ
  │
  ├─ Step 6: current_plan.json 更新
  │   └─ 実行済みアクションに "executed": true を追加
  │   └─ 天候スキップアクションに "executed": "skipped_weather" を追加
  │
  └─ 終了
```

---

## 2. 層間インターフェース

### 2.1 Layer 1 → Layer 2/3: ロックアウト状態共有

Layer 1 が緊急発動すると、Layer 2/3 はリレー操作を一時停止する必要がある。

**共有メカニズム: ファイルベース + REST API**

```
┌─────────────────────────────────────────────────────────┐
│ ロックアウト状態の伝達経路（2系統）                      │
│                                                         │
│ [系統A] Layer 1 独自ロックアウト (ファイルベース)        │
│   emergency_guard.sh                                    │
│     → WRITE: /var/lib/agriha/lockout_state.json         │
│   rule_engine.py / plan_executor.py                     │
│     → READ: /var/lib/agriha/lockout_state.json          │
│   forecast_engine.py                                    │
│     → READ: /var/lib/agriha/lockout_state.json          │
│                                                         │
│ [系統B] CommandGate ロックアウト (REST API)              │
│   unipi-daemon CommandGate                              │
│     → 物理スイッチ(DI) → 300秒ロックアウト             │
│     → GET /api/status の locked_out=true で確認         │
│   全 Layer                                              │
│     → POST /api/relay/{ch} が 423 を返す                │
│     → 423 応答でリトライ不要と判断                      │
└─────────────────────────────────────────────────────────┘
```

**lockout_state.json スキーマ**:

```json
{
  "layer1_lockout_until": "ISO8601 datetime (JST)",
  "last_action": "emergency_open | emergency_close | none",
  "last_temp": 28.3,
  "last_triggered_at": "ISO8601 datetime (JST)"
}
```

**判定ロジック (Layer 2/3 共通)**:

```python
import json
from datetime import datetime
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")

def is_layer1_locked(path: str = "/var/lib/agriha/lockout_state.json") -> bool:
    """Layer 1 ロックアウト中かどうか判定する。"""
    try:
        with open(path) as f:
            data = json.load(f)
        until = datetime.fromisoformat(data.get("layer1_lockout_until", ""))
        return datetime.now(_JST) < until
    except (FileNotFoundError, ValueError, KeyError):
        return False  # ファイルなし or パースエラー → ロックアウトなし


def is_commandgate_locked(base_url: str = "http://localhost:8080") -> bool:
    """CommandGate ロックアウト中かどうか REST API で確認する。"""
    try:
        import httpx
        r = httpx.get(f"{base_url}/api/status", timeout=5)
        return r.json().get("locked_out", False)
    except Exception:
        return False  # API 断 → ロックアウトなしと見なす（安全側）
```

### 2.2 Layer 3 → Layer 2: current_plan.json

Layer 3 (forecast_engine.py) が生成した 1 時間アクション計画を Layer 2 (rule_engine.py) が参照する。

**共有メカニズム: ファイルベース**

```
┌─────────────────────────────────────────────────────────┐
│ current_plan.json のライフサイクル                       │
│                                                         │
│ 生成:                                                   │
│   forecast_engine.py (cron 毎時)                        │
│     → Claude Haiku API → 1時間計画 JSON 生成            │
│     → /var/lib/agriha/current_plan.json に WRITE        │
│                                                         │
│ 参照:                                                   │
│   rule_engine.py (cron 5分毎)                           │
│     → current_plan.json を READ                         │
│     → valid_until 内 → Layer 3 計画を優先               │
│     → 期限切れ or なし → Layer 2 が全権制御             │
│                                                         │
│   plan_executor.py (cron 毎分)                          │
│     → current_plan.json を READ                         │
│     → execute_at に達した未実行アクションを実行          │
│     → executed=true マークを WRITE                      │
│                                                         │
│ 上書き:                                                 │
│   毎時の forecast_engine.py 実行で新しい計画に上書き     │
│   古い計画は control_log.db に判断理由とともに保存済み   │
└─────────────────────────────────────────────────────────┘
```

**current_plan.json スキーマ**:

```json
{
  "generated_at": "2026-03-01T14:00:00+09:00",
  "valid_until": "2026-03-01T15:00:00+09:00",
  "summary": "日射強く気温上昇傾向。CO2は換気で自然値。露点リスクなし。",
  "actions": [
    {
      "execute_at": "2026-03-01T14:00:00+09:00",
      "relay_ch": 5,
      "value": 1,
      "duration_sec": 30,
      "reason": "北側窓50%開（気温上昇対応）",
      "executed": false
    },
    {
      "execute_at": "2026-03-01T14:30:00+09:00",
      "relay_ch": 4,
      "value": 1,
      "duration_sec": 300,
      "reason": "灌水5分（日射比例閾値到達見込み）",
      "executed": false
    }
  ],
  "co2_advisory": "換気中のためCO2自然値で推移。密閉判断不要",
  "dewpoint_risk": "low",
  "next_check_note": "15時に日射減衰見込み。側窓調整の可能性あり"
}

注意: execute_at は ISO8601 絶対時刻で記載する（相対表記 "+30min" は使用しない）。
forecast_engine が generated_at からの相対時間を計算し、ISO8601 に変換して書き込む。
plan_executor は `datetime.fromisoformat()` で単純に現在時刻と比較するだけでよい。
```

**Layer 2 と Layer 3 の協調ルール**:

```
┌─────────────────────────────────────────────────────────┐
│ Layer 2 の判断フロー（Layer 3 計画あり/なし）           │
│                                                         │
│ current_plan.json が有効:                               │
│   Layer 2 は以下のみ実行:                               │
│     ✓ 降雨/強風の安全チェック（Layer 3 計画を上書き）   │
│     ✓ 日射比例灌水（Layer 3 が灌水計画を出していない場合）│
│     ✗ 温度閾値による側窓制御 → Layer 3 に委譲          │
│                                                         │
│ current_plan.json が無効 or 存在しない:                  │
│   Layer 2 が全権制御:                                   │
│     ✓ 降雨/強風の安全チェック                           │
│     ✓ 温度閾値による側窓制御                            │
│     ✓ 日射比例灌水                                     │
│     ✓ 時間帯制御（日没後全閉等）                       │
│                                                         │
│ 注意: Layer 1 緊急発動時は Layer 2/3 とも一時停止       │
└─────────────────────────────────────────────────────────┘
```

### 2.3 層間データフロー全体図

```
                    /var/lib/agriha/
                  ┌──────────────────────────────────────┐
                  │ lockout_state.json                    │
                  │   ← Layer 1 WRITE (緊急発動時)       │
                  │   → Layer 2 READ  (起動時チェック)   │
                  │   → Layer 3 READ  (起動時チェック)   │
                  ├──────────────────────────────────────┤
                  │ current_plan.json                     │
                  │   ← Layer 3 WRITE (毎時生成)         │
                  │   → Layer 2 READ  (5分毎参照)        │
                  │   ← plan_executor WRITE (executed)    │
                  │   → plan_executor READ (毎分実行)     │
                  ├──────────────────────────────────────┤
                  │ solar_accumulator.json                │
                  │   ←→ Layer 2 READ/WRITE (5分毎)      │
                  ├──────────────────────────────────────┤
                  │ rule_engine_state.json                │
                  │   ← Layer 2 WRITE (最終実行状態)      │
                  ├──────────────────────────────────────┤
                  │ control_log.db                        │
                  │   ← Layer 3 WRITE (判断ログ INSERT)   │
                  │   → Layer 3 READ  (直近3件履歴)       │
                  ├──────────────────────────────────────┤
                  │ last_decision.json                    │
                  │   ← Layer 3 WRITE (最終判断状態)      │
                  └──────────────────────────────────────┘

             unipi-daemon REST API (localhost:8080)
          ┌──────────────────────────────────────────┐
          │ GET  /api/sensors    ← Layer 1,2,3 READ  │
          │ GET  /api/status     ← Layer 1,2,3 READ  │
          │ POST /api/relay/{ch} ← Layer 1,2,3 WRITE │
          │      423 = ロックアウト (CommandGate)     │
          │ POST /api/emergency/clear ← 手動解除     │
          └──────────────────────────────────────────┘
```

---

## 3. エラーハンドリング

### 3.1 Layer 1 障害時

Layer 1 (emergency_guard.sh) の障害パターンと影響:

| 障害パターン | 原因 | Layer 2/3 への影響 | 対処 |
|-------------|------|------------------|------|
| cron 未実行 | crond 停止 | **なし** — Layer 2/3 は独立動作 | crond の systemd 自動再起動に依存 |
| REST API 接続失敗 | unipi-daemon 停止 | Layer 2/3 も同じ REST API 障害 | Layer 1 はログ出力して終了（操作しない=安全側） |
| curl タイムアウト | ネットワーク遅延 | **なし** — Layer 1 はログ出力して終了 | curl に `-m 5` (5秒タイムアウト) を設定 |
| LINE 通知失敗 | 回線断/API 障害 | **なし** — 通知と制御は独立 | 制御アクション自体は実行済み。通知だけ不達 |
| JSON パースエラー | センサーデータ異常 | **なし** — Layer 1 はログ出力して終了 | `python3 -c` の try/except で安全処理 |

**設計原則**: Layer 1 が失敗しても「何もしない」状態に倒す。
緊急停止が動かないリスクはあるが、Layer 2/3 が正常動作していれば温度閾値制御は継続される。

### 3.2 Layer 2 障害時

| 障害パターン | 原因 | Layer 1/3 への影響 | 対処 |
|-------------|------|------------------|------|
| Python 実行エラー | 依存パッケージ不足 | **なし** — Layer 1/3 は独立 | 初回デプロイ時に pip install 確認 |
| REST API 接続失敗 | unipi-daemon 停止 | Layer 1/3 も同じ障害 | ログ出力して終了 |
| crop_irrigation.yaml 読み込み失敗 | ファイル破損 | **なし** — Layer 1 は独立 | デフォルト値でフォールバック |
| solar_accumulator.json 破損 | 不正な JSON | **灌水制御のみ影響** | 初期値 (0) にリセット |
| current_plan.json 読み込み失敗 | Layer 3 未生成 | **なし** — Layer 2 が全権制御 | Layer 3 なしモードで動作（設計通り） |

**設計原則**: Layer 2 が失敗しても Layer 1 の緊急停止は動作する。
Layer 2 障害時は日射比例灌水と温度閾値制御が停止するが、
Layer 1 の 27℃/16℃ 閾値で最低限の安全は確保される。

### 3.3 Layer 3 障害時 (API 断)

| 障害パターン | 原因 | Layer 1/2 への影響 | 対処 |
|-------------|------|------------------|------|
| Anthropic API タイムアウト | API 遅延 | **なし** — Layer 1/2 は独立 | `anthropic.APITimeoutError` をキャッチ、ログ出力 |
| Anthropic API 障害 | サービス停止 | **なし** — Layer 1/2 は独立 | `anthropic.APIError` をキャッチ、ログ出力 |
| Starlink 回線断 | 衛星通信障害 | **Layer 1/2 はローカルで動作継続** | Layer 3 のみ停止、Layer 1+2 で 95% カバー |
| ANTHROPIC_API_KEY 無効 | キー期限切れ | **なし** — Layer 1/2 は独立 | `anthropic.AuthenticationError` をキャッチ |
| current_plan.json 書き込み失敗 | ディスク障害 | Layer 2 が全権制御に切り替わる | plan_executor はファイルなしで何もしない |
| LLM ハレーション | 不適切な判断 | **Layer 1 が最終防壁** | 27℃/16℃ 閾値は if 文で確実に動作 |

**設計原則**: Layer 3 は「あってもなくても動く」存在。
API 断時は current_plan.json が生成されず、Layer 2 が全権制御を行う。
これが三層構造の核心思想である。

**API 断時のフォールバック遷移**:

```
正常時: Layer 3 が毎時計画生成 → Layer 2 が計画を参照 → plan_executor が実行
    │
    │ Anthropic API 障害
    ▼
API 断時: Layer 3 がログ出力して終了 → current_plan.json 未更新
    → valid_until 期限切れ → Layer 2 が全権制御に自動移行
    → Layer 2 の温度閾値 + 日射比例灌水で 95% カバー
    → Layer 1 の緊急停止は常時稼働
    │
    │ API 復旧
    ▼
復旧時: 次の毎時 cron で Layer 3 が新しい計画を生成
    → current_plan.json が更新される → Layer 2 が計画を参照再開
    → 自動復旧（手動介入不要）
```

### 3.4 unipi-daemon 全停止時

全 Layer に影響する最悪ケース:

```
unipi-daemon 停止
  │
  ├─ REST API (localhost:8080) 応答なし
  │   ├─ Layer 1: curl 失敗 → ログ出力して終了（操作不能）
  │   ├─ Layer 2: httpx 例外 → ログ出力して終了（操作不能）
  │   └─ Layer 3: tool_call 失敗 → ログ出力して終了
  │
  ├─ リレー状態:
  │   └─ MCP23008 はラッチ型 → 最後の状態を保持
  │   └─ duration_sec タイマーが MqttRelayBridge で動作中なら
  │       → unipi-daemon 停止でタイマー消失
  │       → 灌水 ON 放置のリスク → systemd restart で復旧
  │
  └─ 対策:
      ├─ unipi-daemon を systemd で Restart=always に設定（既存設定）
      ├─ LINE 通知は Layer 3 の API 呼び出しと独立
      │   → Layer 1 の LINE curl は unipi-daemon なしでも動作
      └─ RPi 再起動時は MCP23008 が POR (Power-On Reset) で全 OFF
```

---

## 4. cron 設定の全容

### 4.1 crontab 設定

```bash
# /etc/cron.d/agriha-control
# AgriHA 三層制御 cron スケジュール
#
# 実行順序:
#   1. emergency_guard.sh  (毎分, 最優先)
#   2. forecast_engine.py  (毎時:00, LLM 1時間予報)
#   3. rule_engine.py      (5分毎, :00を回避して:05,:10,...,:55)
#   4. plan_executor.py    (毎分:20秒後, forecast_engine完了後に計画実行)

# === Layer 1: 緊急停止監視（毎分, 最優先） ===
* * * * * root /opt/agriha-control/emergency_guard.sh \
  >> /var/log/agriha/emergency.log 2>&1

# === Layer 3: LLM 1時間予報（毎時:00） ===
0 * * * * root flock -n /tmp/forecast_engine.lock \
  /usr/bin/python3 /opt/agriha-control/forecast_engine.py \
  >> /var/log/agriha/control.log 2>&1

# === Layer 2: ルールベース制御（5分毎, :00を回避） ===
5-59/5 * * * * root flock -n /tmp/rule_engine.lock \
  /usr/bin/python3 /opt/agriha-control/rule_engine.py \
  >> /var/log/agriha/rule_engine.log 2>&1

# === Layer 3 補助: 計画実行（毎分, 20秒後に起動） ===
* * * * * root sleep 20 && flock -n /tmp/plan_executor.lock \
  /usr/bin/python3 /opt/agriha-control/plan_executor.py \
  >> /var/log/agriha/plan_executor.log 2>&1
```

### 4.2 実行順序とタイミング

```
毎分のタイムライン（:00 ～ :59）:

:00   emergency_guard.sh 実行（< 3秒で完了）
:20   plan_executor.py 実行（sleep 20 後、< 3秒で完了）
:??   （残り時間は何も動かない）

5分毎の追加（:05, :10, :15, ..., :55）:
:00   emergency_guard.sh → Layer 1
:05   rule_engine.py 実行（< 5秒で完了）
:20   plan_executor.py
:??

毎時のタイムライン（:00分）:
:00   emergency_guard.sh → Layer 1 最優先
:00   forecast_engine.py → Layer 3（flock排他、< 30秒で計画生成）
:20   plan_executor.py → 新しい計画の即時アクション実行
:??

注意: :00分ではrule_engine.pyは起動しない（5-59/5 で:00を回避）。
      forecast_engine.pyが生成した計画をplan_executorが:20秒後に実行する。
```

### 4.3 同時実行防止

```
┌─────────────────────────────────────────────────────────┐
│ flock による排他制御                                     │
│                                                         │
│ emergency_guard.sh: flock 不要（< 3秒、bash、冪等）     │
│ plan_executor.py:   flock -n /tmp/plan_executor.lock    │
│ rule_engine.py:     flock -n /tmp/rule_engine.lock      │
│ forecast_engine.py: flock -n /tmp/forecast_engine.lock  │
│                                                         │
│ -n (non-blocking): 既に実行中なら即座にスキップ         │
│ → API 遅延で前回が終わっていない場合の二重実行防止      │
│                                                         │
│ 注意: Layer 間の排他は不要。各 Layer は独立動作。       │
│ REST API 側の CommandGate が唯一の排他ポイント。        │
└─────────────────────────────────────────────────────────┘
```

### 4.4 緊急割り込みの仕組み

Layer 1 の「緊急割り込み」は cron ベースであり、真のリアルタイム割り込みではない。

```
割り込み応答時間:
  最悪ケース: 59秒（cron 1分間隔のスキマ）
  平均ケース: 30秒
  最良ケース: < 1秒（cron 直後に閾値超過）

これで十分な理由:
  - ハウス内温度は急変しない（熱容量が大きい）
  - 27℃ → 40℃ まで数十分かかる
  - 59秒の遅延は実運用上問題なし
  - CommandGate (物理スイッチ) は真のリアルタイム（即時、cron不要）
```

---

## 5. 既存コードとの接続点

### 5.1 unipi-daemon REST API

全 Layer が消費者として利用する REST API。unipi-daemon のコードは変更しない。

```
┌─────────────────────────────────────────────────────────────┐
│ unipi-daemon REST API (localhost:8080)                       │
│ 実装: ~/unipi-agri-ha/services/unipi-daemon/rest_api.py     │
│                                                              │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ POST /api/relay/{ch}                                     │ │
│ │   Request:  {"value": 0|1, "duration_sec": N, "reason":} │ │
│ │   Response: 202 {"ch":N, "value":N, "queued":true}       │ │
│ │   Error:    423 {"error":"locked_out", "remaining_sec":N} │ │
│ │   Error:    503 {"error":"mqtt_unavailable"}              │ │
│ │   Auth:     X-API-Key header                              │ │
│ │   動作: MQTT publish → MqttRelayBridge → I2C → リレー   │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ GET /api/sensors                                         │ │
│ │   Response: {"sensors": {...}, "updated_at":N, "age_sec":N}│
│ │   sensors keys:                                           │ │
│ │     agriha/h01/ccm/InAirTemp    → CCM内気温              │ │
│ │     agriha/h01/ccm/InAirHumi    → CCM内湿度              │ │
│ │     agriha/h01/ccm/CO2          → CCM CO2                │ │
│ │     agriha/h01/ccm/InSolar      → CCM日射量              │ │
│ │     agriha/h01/sensor/DS18B20   → DS18B20温度            │ │
│ │     agriha/farm/weather/misol   → Misol外気象            │ │
│ │     agriha/h01/relay/state      → リレー状態(ch1-8)      │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ GET /api/status                                          │ │
│ │   Response: {"house_id":"h01", "uptime_sec":N,           │ │
│ │     "locked_out":bool, "lockout_remaining_sec":N,        │ │
│ │     "relay_state":{"ch1":bool,...,"ch8":bool}, "ts":N}   │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ POST /api/emergency/clear                                │ │
│ │   Response: {"cleared":true, "was_locked_out":bool}      │ │
│ │   動作: CommandGate.clear_lockout()                      │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**センサーデータの構造（GET /api/sensors 詳細）**:

```json
{
  "sensors": {
    "agriha/h01/ccm/InAirTemp": {"value": 25.3, "unit": "celsius"},
    "agriha/h01/ccm/InAirHumi": {"value": 65.2, "unit": "percent"},
    "agriha/h01/ccm/CO2": {"value": 420, "unit": "ppm"},
    "agriha/h01/ccm/InSolar": {"value": 350.0, "unit": "W/m2"},
    "agriha/h01/sensor/DS18B20": {
      "device_id": "28-xxxx",
      "temperature_c": 24.8,
      "timestamp": 1709280000.0
    },
    "agriha/farm/weather/misol": {
      "temperature_c": 18.5,
      "humidity_pct": 72,
      "wind_speed_ms": 2.3,
      "wind_direction": 5,
      "rainfall": 0.0,
      "uv_index": 4,
      "solar_radiation_wm2": 280.0,
      "timestamp": 1709280000.0
    },
    "agriha/h01/relay/state": {
      "ch1": false, "ch2": false, "ch3": false, "ch4": false,
      "ch5": true, "ch6": false, "ch7": false, "ch8": false
    }
  },
  "updated_at": 1709280010.5,
  "age_sec": 3.2
}
```

**API 認証 (X-API-Key) の空文字動作**:

unipi-daemon v1.0 の REST API 認証は `config.yaml` の `rest_api.api_key` で設定する。
`api_key` が空文字列（デフォルト）の場合、`_check` 関数は `if api_key and ...` の条件により
認証チェックをスキップする（rest_api.py L238）。
つまり、**api_key 未設定時は全リクエストが認証パスする**。

デプロイ時に `api_key` を設定する場合は、以下の全ファイルに同じキーを反映する必要がある:
- unipi-daemon `config.yaml` の `rest_api.api_key`
- `/etc/agriha/layer1.env` の `UNIPI_API_KEY`
- `/etc/agriha/layer2_config.yaml` の `unipi_api.api_key`
- `/etc/agriha/layer3_config.yaml` の `unipi_api.api_key`

### 5.2 LINE Bot との関係

LINE Bot は三層制御とは独立したシステム。変更不要。

```
┌─────────────────────────────────────────────────────────────┐
│ LINE Bot (linebot/app.py)                                    │
│   ├─ 「開けろ」「閉めろ」→ rpi_client.py → REST API         │
│   │   → POST /api/relay/{ch} (VPN経由 10.10.0.10:8080)      │
│   ├─ 「今どうなってる？」→ rpi_client.py → REST API         │
│   │   → GET /api/sensors (VPN経由)                           │
│   └─ LLM: Ollama qwen3:8b (Claude未移行)                    │
│                                                              │
│ 三層制御との接点:                                            │
│   - REST API を共有（LINE Bot と三層制御は同じ REST API を叩く）│
│   - CommandGate ロックアウトは LINE Bot にも影響              │
│   - 競合: LINE Bot と Layer 2/3 が同時に relay 操作する可能性 │
│     → REST API 側で MQTT publish → MqttRelayBridge が最終状態決定│
│     → 最後に publish された値が勝つ（last-writer-wins）      │
│     → 実害なし: 農家の「開けろ」は即時性が重要、その後のcronで│
│       Layer 2/3 が状態を再評価する                           │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 CommandGate / emergency_override との関係

```
┌─────────────────────────────────────────────────────────────┐
│ CommandGate (emergency_override.py)                           │
│ → 物理スイッチ (DI07-DI14) による緊急オーバーライド         │
│                                                              │
│ 動作:                                                        │
│   DI ON → I2C 直接リレー制御（REST API/MQTT を経由しない）   │
│        → MQTT publish (agriha/h01/emergency/override)        │
│        → 300秒ロックアウト開始                               │
│                                                              │
│ 三層制御との関係:                                            │
│   Layer 1 (emergency_guard.sh):                              │
│     → POST /api/relay/{ch} が 423 → ロックアウト中と判断    │
│     → 何もしない（CommandGate が直接制御済み）               │
│   Layer 2 (rule_engine.py):                                  │
│     → GET /api/status の locked_out=true → 制御スキップ      │
│   Layer 3 (forecast_engine.py):                              │
│     → ロックアウト中でも計画生成は実行（リレー操作なし）     │
│     → plan_executor がロックアウト解除後に計画を実行         │
│                                                              │
│ 重要:                                                        │
│   CommandGate は I2C 直接制御 → REST API を経由しない        │
│   → Layer 1 の REST API 経由の制御とは完全に別系統           │
│   → 物理スイッチの方が常に優先される                        │
└─────────────────────────────────────────────────────────────┘
```

### 5.4 agriha_chat.py (History API) との関係

```
┌─────────────────────────────────────────────────────────────┐
│ 判断ログ DB の分離状況                                       │
│                                                              │
│ [制御側] control_log.db (/var/lib/agriha/)                   │
│   ← forecast_engine.py が書き込み                            │
│   ← 制御判断のログ（summary, actions, sensor_snapshot）      │
│                                                              │
│ [LINE Bot側] conversations.db (/app/data/)                   │
│   ← linebot/app.py が書き込み                                │
│   ← ユーザーとの会話ログ                                    │
│                                                              │
│ 現状: 2つのDBは完全に分離                                    │
│ 将来: History API で統合の可能性（設計書§6記載、未実装）     │
│ 本設計書のスコープ外                                         │
└─────────────────────────────────────────────────────────────┘
```

### 5.5 MQTT トピック一覧（三層制御で参照するもの）

三層制御スクリプトは REST API 経由でアクセスするため、MQTT を直接 subscribe しない。
以下は REST API のセンサーキャッシュが subscribe しているトピック一覧。

| トピック | QoS | 発行元 | 内容 |
|---------|-----|--------|------|
| `agriha/h01/sensor/DS18B20` | 1 | sensor_loop.py | DS18B20 温度 |
| `agriha/farm/weather/misol` | 1 | sensor_loop.py | Misol WH65LP 外気象 (10項目) |
| `agriha/h01/ccm/InAirTemp` | 0 | ccm_receiver.py | CCM 内気温 |
| `agriha/h01/ccm/InAirHumi` | 0 | ccm_receiver.py | CCM 内湿度 |
| `agriha/h01/ccm/CO2` | 0 | ccm_receiver.py | CCM CO2 |
| `agriha/h01/ccm/InSolar` | 0 | ccm_receiver.py | CCM 日射量 (W/m2) |
| `agriha/h01/relay/state` | 1 | MqttRelayBridge | リレー状態 (ch1-8) |
| `agriha/h01/relay/{ch}/set` | 1 | REST API | リレー制御コマンド |
| `agriha/h01/emergency/override` | 1 | CommandGate | 緊急オーバーライド通知 |

---

## 6. 設定ファイル一覧

### 6.1 /etc/agriha/layer1.env（シェル変数ファイル）

bashスクリプトからYAMLをパースする必要をなくすため、Layer 1 の設定はシェル変数形式とする。
`emergency_guard.sh` 内で `source /etc/agriha/layer1.env` で読み込む。

```bash
# /etc/agriha/layer1.env
# Layer 1: 緊急停止設定（シェル変数形式）

# 閾値
HIGH_TEMP_THRESHOLD=27          # ℃ — 超過で全窓全開
LOW_TEMP_THRESHOLD=16           # ℃ — 以下で全窓全閉
WINDOW_CHANNELS="5 6 7 8"       # 側窓リレーチャンネル（スペース区切り）
LOCKOUT_DURATION_SEC=300        # 緊急発動後のロックアウト秒数
SENSOR_FALLBACK=true            # CCMデータなし時にMisol外気温で代替

# unipi-daemon REST API
UNIPI_API_BASE_URL="http://localhost:8080"
UNIPI_API_KEY=""                # 空文字で認証スキップ（§5.1参照）
CURL_TIMEOUT=5                  # curl -m タイムアウト秒数

# LINE 通知
LINE_CHANNEL_ACCESS_TOKEN=""    # LINE Messaging API トークン
LINE_GROUP_ID=""                # 通知先グループID
LINE_ENABLED=true               # false で通知無効化（テスト用）

# 状態ファイル
LOCKOUT_FILE="/var/lib/agriha/lockout_state.json"
```

### 6.2 /etc/agriha/layer2_config.yaml

```yaml
# Layer 2: ルールベース制御設定
temperature:
  target_day: 26.0             # 日中目標温度 (℃)
  target_night: 17.0           # 夜間目標温度 (℃) ※LOW_TEMP_THRESHOLD(16℃)+1℃以上を推奨
  margin_open: 2.0             # 目標+Nで窓開 (℃)
  margin_close: 1.0            # 目標-Nで窓閉 (℃)
  window_channels: [5, 6, 7, 8]

wind:
  strong_wind_threshold_ms: 5.0  # 強風閾値 (m/s)
  # 北風(NNW～NNE): wind_direction 1,2,16
  north_directions: [1, 2, 16]
  north_channels: [5, 6]        # 北側窓チャンネル（要実測確認）
  # 南風(SSE～SSW): wind_direction 8,9,10
  south_directions: [8, 9, 10]
  south_channels: [7, 8]        # 南側窓チャンネル（要実測確認）

rain:
  threshold_mm_h: 0.5           # 降雨閾値 (mm/h)
  resume_delay_min: 30          # 降雨停止後の再開猶予 (分)

irrigation:
  channel: 4                    # 灌水電磁弁チャンネル
  crop_config_path: "/etc/agriha/crop_irrigation.yaml"

unipi_api:
  base_url: "http://localhost:8080"
  api_key: ""
  timeout_sec: 10

location:
  latitude: 42.888
  longitude: 141.603
  elevation: 21
```

**注意: Layer 1/2 閾値の干渉リスク**

Layer 1 の `LOW_TEMP_THRESHOLD`(16℃) と Layer 2 の `target_night`(17℃) が近接している。
夜間に 16℃付近で気温が変動すると、Layer 1 が緊急全閉→ロックアウト→Layer 2 がブロック
→ロックアウト解除→Layer 2 が窓開→再び16℃以下→Layer 1 再発動、のループが起こりうる。

**デプロイ時の必須確認**: `target_night` は `LOW_TEMP_THRESHOLD + 1℃` 以上に設定すること。
デフォルト値は target_night=17℃, LOW_TEMP_THRESHOLD=16℃ で 1℃のマージンを確保済み。

### 6.3 /etc/agriha/layer3_config.yaml

```yaml
# Layer 3: LLM 1時間予報設定
claude:
  model: "claude-haiku-4-5-20251001"
  max_tokens: 1024
  max_tool_rounds: 5
  api_timeout_sec: 30.0
  # ANTHROPIC_API_KEY は環境変数から読み込み

system_prompt_path: "/etc/agriha/system_prompt.txt"

db:
  path: "/var/lib/agriha/control_log.db"
  history_count: 3              # プロンプトに含める直近判断件数

state:
  plan_path: "/var/lib/agriha/current_plan.json"
  last_decision_path: "/var/lib/agriha/last_decision.json"

unipi_api:
  base_url: "http://localhost:8080"
  api_key: ""
  timeout_sec: 10

location:
  latitude: 42.888
  longitude: 141.603
  elevation: 21
```

### 6.4 /etc/agriha/crop_irrigation.yaml (既存)

既存ファイル。変更なし。Layer 2 (rule_engine.py) が参照する。

パス: `~/unipi-agri-ha/config/crop_irrigation.yaml`
デプロイ先: `/etc/agriha/crop_irrigation.yaml`

主要パラメータ（収穫盛期）:
- `solar_threshold_mj`: 0.9 MJ/m²
- `irrigation_ml_per_plant`: 270-300 ml
- `ec_ms_cm`: 1.8-2.0 (制御対象外、ドサトロン手動)

### 6.5 /etc/agriha/system_prompt.txt (既存)

既存ファイル。[G] セクション追加済み (v3.0)。Layer 3 (forecast_engine.py) が参照する。

セクション構成:
- [A] 役割定義（1時間予報 AI として更新済み）
- [B] ハウス固有情報
- [C] 作物パラメータ
- [D] 制御ルール
- [E] 暗黙知（怒り駆動で蓄積）
- [F] 安全制約
- [G] 出力形式（1時間アクション計画 JSON）

### 6.6 ファイルパス一覧

```
/etc/agriha/                          # 設定ファイル（read-only at runtime）
  ├─ layer1.env                        # 新規: Layer 1 設定（シェル変数形式）
  ├─ layer2_config.yaml               # 新規: Layer 2 設定
  ├─ layer3_config.yaml               # 新規: Layer 3 設定
  ├─ crop_irrigation.yaml             # 既存: 灌水パラメータ
  └─ system_prompt.txt                # 既存: LLM プロンプト

/opt/agriha-control/                  # 実行スクリプト
  ├─ emergency_guard.sh               # 新規: Layer 1
  ├─ rule_engine.py                   # 新規: Layer 2
  ├─ forecast_engine.py               # 新規: Layer 3（agriha_control.py から発展）
  └─ plan_executor.py                 # 新規: Layer 3 補助

/var/lib/agriha/                      # ランタイムデータ（read-write）
  ├─ control_log.db                   # 既存: 判断ログ SQLite
  ├─ current_plan.json                # 新規: 1時間アクション計画
  ├─ last_decision.json               # 既存: 最終判断状態
  ├─ lockout_state.json               # 新規: Layer 1 ロックアウト状態
  ├─ solar_accumulator.json           # 新規: 日射積算値
  └─ rule_engine_state.json           # 新規: Layer 2 最終実行状態

/var/log/agriha/                      # ログファイル
  ├─ emergency.log                    # Layer 1 ログ
  ├─ rule_engine.log                  # Layer 2 ログ
  ├─ control.log                      # Layer 3 ログ
  └─ plan_executor.log                # plan_executor ログ

/tmp/                                 # flock ファイル
  ├─ plan_executor.lock
  ├─ rule_engine.lock
  └─ forecast_engine.lock
```

---

## 7. テスト戦略

### 7.1 Layer 1 テスト: bats (Bash Automated Testing System)

```
テストフレームワーク: bats-core (apt install bats)
テストファイル: tests/test_emergency_guard.bats

テスト方針:
  - curl をモック関数で置換（実際の REST API を叩かない）
  - LINE API 呼び出しもモック（実際の通知を送らない）
  - bc, python3 -c は実際のコマンドを使用
  - bats自体がbash依存のため、テスト内の export -f (関数モック) は正当
  - テスト用shebangは #!/usr/bin/env bash に明示
    （テスト対象のemergency_guard.shはPOSIX互換だが、テストランナーはbash）

テストケース:
  1. 正常範囲（20℃）→ 何もしない
  2. 高温超過（28℃）→ ch5-8 全開 + LINE 通知
  3. 低温超過（15℃）→ ch5-8 全閉 + LINE 通知
  4. センサーデータ取得失敗 → ログ出力して終了
  5. ロックアウト中 → スキップ
  6. Layer 1 ロックアウト連打防止 → 5分以内は再発動しない
  7. CommandGate ロックアウト（423応答）→ スキップ
  8. LINE 通知失敗 → 制御アクション自体は成功
  9. CCM データなし → Misol 外気温で代替判断
```

**bats テスト例**:

```bash
#!/usr/bin/env bats
# tests/test_emergency_guard.bats

setup() {
  export AGRIHA_CONFIG="/tmp/test_layer1.env"
  export AGRIHA_LOCKOUT="/tmp/test_lockout_state.json"
  # curl モック: 正常センサーデータを返す
  curl() {
    case "$2" in
      *sensors*) echo '{"sensors":{"agriha/h01/ccm/InAirTemp":{"value":'"$TEST_TEMP"'}}}' ;;
      *status*)  echo '{"locked_out":false}' ;;
      *relay*)   echo '{"ch":5,"value":1,"queued":true}' ;;
      *line.me*) echo '{}' ;;
    esac
  }
  export -f curl
}

@test "正常温度範囲では何もしない" {
  TEST_TEMP=22.0
  run /opt/agriha-control/emergency_guard.sh
  [ "$status" -eq 0 ]
  [[ ! "$output" =~ "EMERGENCY" ]]
}

@test "27℃超過で全窓全開" {
  TEST_TEMP=28.5
  run /opt/agriha-control/emergency_guard.sh
  [ "$status" -eq 0 ]
  [[ "$output" =~ "emergency_open" ]]
}

@test "16℃以下で全窓全閉" {
  TEST_TEMP=14.0
  run /opt/agriha-control/emergency_guard.sh
  [ "$status" -eq 0 ]
  [[ "$output" =~ "emergency_close" ]]
}
```

### 7.2 Layer 2 テスト: pytest

```
テストフレームワーク: pytest + pytest-mock
テストファイル: tests/test_rule_engine.py

テスト方針:
  - httpx.Client をモック（REST API レスポンスを固定）
  - ファイル I/O は tmp_path フィクスチャで一時ディレクトリ使用
  - astral は実際の計算を使用（決定的なため）

テストケース:
  1. 降雨検知 → 全窓閉
  2. 強風（北風5m/s）→ 北側窓閉、南側開維持
  3. 高温（目標+3℃）→ 側窓開
  4. 低温（目標-2℃）→ 側窓閉
  5. 日射比例灌水 → 積算閾値到達で灌水実行
  6. 日射比例灌水 → 閾値未到達で何もしない
  7. 日付変更 → 積算値リセット
  8. Layer 1 ロックアウト中 → 全スキップ
  9. CommandGate ロックアウト中 → 全スキップ
  10. current_plan.json 有効 → 温度制御を Layer 3 に委譲
  11. current_plan.json 期限切れ → Layer 2 全権制御
  12. REST API 接続失敗 → ログ出力して終了
  13. 日没後 → 全窓閉
  14. 日の出前 → 全窓閉
```

**pytest テスト例**:

```python
# tests/test_rule_engine.py
import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def mock_sensors_rain():
    """降雨時のセンサーデータ"""
    return {
        "sensors": {
            "agriha/h01/ccm/InAirTemp": {"value": 25.0},
            "agriha/farm/weather/misol": {
                "temperature_c": 20.0, "rainfall": 1.5,
                "wind_speed_ms": 2.0, "wind_direction": 5,
            },
        }
    }


def test_rain_closes_all_windows(tmp_path, mock_sensors_rain):
    """降雨検知で全窓閉."""
    # ... httpx mock setup ...
    # rule_engine.evaluate_rules() を呼び出し
    # POST /api/relay/{5,6,7,8} value=0 が呼ばれることを確認
    pass


def test_solar_irrigation_threshold(tmp_path):
    """日射積算が閾値到達で灌水実行."""
    accumulator = {"date": "2026-03-01", "accumulated_mj": 0.85}
    acc_path = tmp_path / "solar_accumulator.json"
    acc_path.write_text(json.dumps(accumulator))
    # InSolar=400 W/m2 × 300秒 = 0.12 MJ → 0.85+0.12=0.97 > 0.9 → 灌水
    pass
```

### 7.3 Layer 3 テスト: pytest + Claude API モック

```
テストフレームワーク: pytest + pytest-mock
テストファイル: tests/test_forecast_engine.py

テスト方針:
  - anthropic.Anthropic() をモック（API 呼び出しなし）
  - tool_use レスポンスを固定データで返す
  - httpx.Client をモック
  - SQLite は :memory: で動作

テストケース:
  1. 正常フロー → get_sensors → get_status → 計画 JSON 生成
  2. API タイムアウト → ログ出力、current_plan.json 未生成
  3. API 認証エラー → ログ出力
  4. ツール呼び出しエラー → エラーレスポンスを LLM に返す
  5. current_plan.json 書き込み → valid_until が1時間後
  6. control_log.db に判断ログが保存される
  7. last_decision.json が更新される
  8. ロックアウト中 → 計画生成は実行（リレー操作はplan_executor担当）
  9. max_tool_rounds 到達 → ループ終了
```

**pytest テスト例**:

```python
# tests/test_forecast_engine.py
import json
import pytest
from unittest.mock import MagicMock, patch

def test_normal_flow_generates_plan(tmp_path):
    """正常フローで current_plan.json が生成される."""
    plan_path = tmp_path / "current_plan.json"

    # Anthropic モック: 3ラウンド (get_sensors → get_status → text応答)
    mock_client = MagicMock()
    # Round 0: tool_use (get_sensors)
    # Round 1: tool_use (get_status)
    # Round 2: text response (計画 JSON)
    # ... setup mock responses ...

    # forecast_engine.run_forecast(config={...}, anthropic_client=mock_client)
    # assert plan_path.exists()
    # plan = json.loads(plan_path.read_text())
    # assert "actions" in plan
    pass


def test_api_timeout_no_plan(tmp_path):
    """API タイムアウト時に current_plan.json が生成されない."""
    import anthropic
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())
    # forecast_engine.run_forecast(config={...}, anthropic_client=mock_client)
    # assert not (tmp_path / "current_plan.json").exists()
    pass
```

### 7.4 統合テスト: 層間連携テスト

```
テストフレームワーク: pytest
テストファイル: tests/test_integration.py

テスト方針:
  - 全 Layer のスクリプトを一時ディレクトリで順次実行
  - REST API は httpx モック
  - ファイルベースの層間通信を検証

統合テストケース:
  1. Layer 3 が計画生成 → Layer 2 が計画を参照 → plan_executor が実行
     → current_plan.json の actions が executed=true になる

  2. Layer 1 緊急発動 → lockout_state.json 書き込み
     → Layer 2 がロックアウト検知 → スキップ
     → Layer 3 が計画生成（リレー操作なし、plan_executorがロックアウト解除後に実行）

  3. Layer 3 API 断 → current_plan.json 未生成
     → Layer 2 が全権制御（温度閾値 + 日射比例）

  4. 日射比例灌水フロー:
     → Layer 2 が solar_accumulator.json を5分毎に更新
     → 閾値到達 → 灌水実行 → リセット

  5. cron タイミングシミュレーション:
     → :00 Layer 1 → :05 Layer 2 → :10 plan_executor → :15 Layer 3
     → 各スクリプトが正しい順序で動作
```

**統合テスト例**:

```python
# tests/test_integration.py
import json
import pytest
from pathlib import Path

def test_layer3_plan_to_layer2_reference(tmp_path):
    """Layer 3 計画が Layer 2 に正しく参照される."""
    plan = {
        "generated_at": "2026-03-01T14:00:00+09:00",
        "valid_until": "2026-03-01T15:00:00+09:00",
        "summary": "テスト計画",
        "actions": [
            {"execute_at": "2026-03-01T14:00:00+09:00", "relay_ch": 5, "value": 1,
             "duration_sec": 30, "reason": "テスト", "executed": False}
        ],
        "co2_advisory": "テスト",
        "dewpoint_risk": "low",
        "next_check_note": "テスト"
    }
    plan_path = tmp_path / "current_plan.json"
    plan_path.write_text(json.dumps(plan))

    # rule_engine が current_plan.json を読み込み、
    # 温度制御を Layer 3 に委譲することを確認
    # ...


def test_layer1_lockout_blocks_layer2(tmp_path):
    """Layer 1 ロックアウトが Layer 2 をブロックする."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    _JST = ZoneInfo("Asia/Tokyo")
    lockout = {
        "layer1_lockout_until": (datetime.now(_JST) + timedelta(minutes=5)).isoformat(),
        "last_action": "emergency_open",
        "last_temp": 28.3,
    }
    lockout_path = tmp_path / "lockout_state.json"
    lockout_path.write_text(json.dumps(lockout))

    # rule_engine がロックアウトを検知してスキップすることを確認
    # ...
```

### 7.5 テスト実行コマンド

```bash
# === Layer 1 テスト (bats) ===
bats tests/test_emergency_guard.bats

# === Layer 2/3/統合テスト (pytest) ===
cd /opt/agriha-control
pip install pytest pytest-mock
pytest tests/ -v

# === 全テスト一括 ===
bats tests/test_emergency_guard.bats && pytest tests/ -v
```

---

## 付録: MCP23008 ビットマッピング

unipi-daemon の I2C リレー制御は逆ビット順配線。

```
MCP23008 GPIO レジスタ (OLAT 0x0A):
  bit7  bit6  bit5  bit4  bit3  bit2  bit1  bit0
  ch1   ch2   ch3   ch4   ch5   ch6   ch7   ch8

ch_to_bit マッピング (i2c_relay.py):
  ch1 = 0x80 (bit7)
  ch2 = 0x40 (bit6)
  ch3 = 0x20 (bit5)
  ch4 = 0x10 (bit4)  ← 灌水
  ch5 = 0x08 (bit3)  ← 側窓
  ch6 = 0x04 (bit2)  ← 側窓
  ch7 = 0x02 (bit1)  ← 側窓
  ch8 = 0x01 (bit0)  ← 側窓

GET /api/status の relay_state:
  {"ch1": bool(raw & 0x80), "ch2": bool(raw & 0x40), ...}
  → rest_api.py L337: bool(raw & (1 << (8 - ch)))

DI ピン → リレーチャンネル (emergency_override.py):
  DI07 → ch1, DI08 → ch2, ..., DI14 → ch8
```

**三層制御スクリプトはこのマッピングを意識する必要がない。**
REST API (`POST /api/relay/{ch}`) がチャンネル番号 (1-8) を受け取り、
内部で MCP23008 のビットマッピングに変換する。

---

## 殿裁定事項（2026-03-01 確定）

> **原則**: 下層が上層を黙らせる。例外を作ると階層化の意味がなくなる。

### MAJOR-2: Layer 1 緊急開窓後のフィードバックループ → **案B採用**

**裁定**: lockout中はforecast_engine計画生成スキップ。
- forecast_engine Step 1 でlockout_state.json確認 → lockout中なら即終了
- 5分間のCO2/露点判断停止は許容（下層の緊急判断が優先）
- 設計書 §1.3 Step 1 に反映済み

### MAJOR-3: 降雨/強風オーバーライドとplan_executor競合 → **案B採用**

**裁定**: plan_executorが降雨/強風フラグ確認、フラグ中は側窓操作スキップ。
- plan_executor Step 3 でGET /api/sensors → 降雨/強風時は側窓操作をskipped_weatherとして記録
- 閾値はlayer2_config.yamlから参照（重複定義回避）
- 責務分離を維持しつつ、下層の安全判断を上層が尊重する設計
- 設計書 §1.4 Step 3-4 に反映済み
