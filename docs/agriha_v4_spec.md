# AgriHA v4 仕様書

> **Version**: 4.0-draft
> **Generated**: 2026-03-04
> **Source**: v3-rebuild branch + cmd_304 research + Memory MCP
> **正データ: v3-rebuildブランチの実コードのみ。v3.x設計書は参考程度。**

---

## §1 機能一覧

### 1.1 機能カタログ

| # | カテゴリ | 機能 | ステータス | 優先度 | 実装場所 |
|---|---------|------|----------|--------|---------|
| F01 | リモート制御 | LINE Bot「開けろ/閉めろ」 | RPi移植設計済み | P0 | chat/app.py + linebot_handler.py (RPi) |
| F02 | 状況確認 | LINE Bot「今どうなってる？」 | RPi移植設計済み | P0 | chat/app.py + linebot_handler.py (RPi) |
| F03 | 異常通知 | emergency_guard.sh LINE通知 | v3-rebuild実装済み | P1 | services/agriha-control/emergency_guard.sh |
| F04 | 自動制御 | Layer1: 緊急停止 | v3-rebuild実装済み | P1 | services/agriha-control/emergency_guard.sh |
| F05 | 自動制御 | Layer2: ルールベースPID制御 | v3-rebuild実装済み | P2 | services/agriha-control/rule_engine.py |
| F06 | 自動制御 | Layer3: LLM 1時間予報 | v3-rebuild実装済み | P2 | services/agriha-control/forecast_engine.py |
| F07 | 自動制御 | 計画実行（plan_executor） | v3-rebuild実装済み | P2 | services/agriha-control/plan_executor.py |
| F08 | 自動制御 | 日射比例灌水 | v3-rebuild実装済み | P2 | services/agriha-control/rule_engine.py |
| F09 | UI | ダッシュボード | v3-rebuild実装済み | P2 | services/agriha-chat/ (templates/static) |
| F10 | UI | Chat窓（Claude対話） | 稼働中(main) | P2 | services/agriha-chat/agriha_chat.py |
| F11 | HW抽象化 | unipi-daemon（センサー+リレー） | v3-rebuild実装済み | P1 | services/unipi-daemon/ |
| F12 | HW抽象化 | 雨検知（rain_detector） | v3-rebuild実装済み | P2 | services/rain_detector.py |
| F13 | HW抽象化 | CO2読取（uart_co2_reader） | v3-rebuild実装済み | P3 | services/uart_co2_reader.py |
| F14 | インフラ | カメラ定点撮影 | 稼働中(main) | P3 | agriha-capture.sh (mainブランチ) |
| F15 | インフラ | Nginx統合 | 未実装(設計済み) | P3 | — |
| F16 | 蒸留 | 判断パターン蒸留パイプライン | 未実装(構想) | P4 | — |
| F17 | UI | 反省会モード（週次LINE Bot） | 未実装(構想) | P4 | — |
| F18 | 設定 | channel_map.yaml外部設定 | v3-rebuild実装済み | P1 | config/channel_map.yaml + channel_config.py |
| F19 | ネットワーク | USB SIM / APN設定UI | 設計済み | P1 | chat/app.py (settings画面) + ModemManager |
| F20 | ネットワーク | HTTPS証明書管理 | 設計済み | P1 | certbot + Nginx |

### 1.2 優先度定義

| 優先度 | 意味 | 基準 |
|--------|------|------|
| P0 | 最重要 | 農家が毎日使う。停止＝業務不能 |
| P1 | 必須 | 安全装置。停止＝作物リスク |
| P2 | 重要 | 自動制御の中核。手動で代替可 |
| P3 | 便利 | あると便利。なくても運用可 |
| P4 | 将来 | 構想段階。1年分データ蓄積後に着手 |

### 1.3 設計思想（Memory MCP: tono-preferences）

- **マクガイバー精神**: シンプル・ローコスト・手元の道具で解決
- **三層構造**: 爆発（緊急停止）→ガムテ（ルールベース）→知恵（LLM）。下ほど確実、どの層が欠けても下が支える
- **怒り駆動開発**: LINEへのクレーム→system_prompt.txtに追記→制御ロジックになる
- **LLM自然減衰**: 初期はLLM毎時→パターン蓄積→ルールに蒸留→API代が自然減衰
- **機能優先順位**: 1位「開けろ/閉めろ」> 2位「今どうなってる？」> 3位 異常値通知 > 4位 自動制御

---

## §2 アーキテクチャ

### 2.1 システム構成図（2ノード + VPSオプション）

```
LINE Platform                    VPS (オプション)
  │ Webhook POST                 ┌──────────────────────────┐
  │ (HTTPS/443)                  │ Grafana + InfluxDB       │
  │                              │ + Telegraf               │
  │                              │ :3000 (監視用、必須ではない) │
  │                              └──────────────────────────┘
  │
  │  Let's Encrypt + 固定IP (USB SIM)
  │
┌─┴─────────────────────────────────────────────────────────┐
│                RPi (Raspberry Pi 4B)                        │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐       │
│  │ unipi-daemon│  │ agriha-ui   │  │ Mosquitto   │       │
│  │ (systemd)   │  │ (systemd)   │  │ MQTT Broker │       │
│  │ :8080       │  │ :8501       │  │ :1883       │       │
│  │ REST API    │  │ FastAPI     │  │             │       │
│  │ +MQTT pub   │  │ Chat+Dash   │  │             │       │
│  │             │  │ +LINE Bot   │  │             │       │
│  │             │  │ +Settings   │  │             │       │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘       │
│         │                │                │               │
│         │   ┌────────────┴────────┐       │               │
│         │   │ cron scripts        │       │               │
│         │   │ emergency_guard.sh  │◄──────┘               │
│         │   │ rule_engine.py      │  MQTT                 │
│         │   │ forecast_engine.py  │  subscribe            │
│         │   │ plan_executor.py    │                        │
│         │   └─────────────────────┘                        │
│         │                                                  │
│  ┌──────┴──────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ NullClaw    │  │ Nginx        │  │ USB SIM      │     │
│  │ Proxy       │  │ (HTTPS/443)  │  │ ドングル     │     │
│  │ :3001       │  │ + HTTP/80    │  │ (セルラー)   │     │
│  └─────────────┘  └──────────────┘  └──────────────┘     │
│                                                             │
│  ┌──────────────────────────────────────────────────┐     │
│  │ Hardware Layer                                     │     │
│  │ I2C: MCP23008 → 8ch Relay (ch1-8)                │     │
│  │ 1-Wire: DS18B20 → 内温                            │     │
│  │ UART RS485: Misol WH65LP → 気象(温湿度/風/雨)     │     │
│  │ UART: CDM7160/K30/SCD30 → CO2                    │     │
│  │ GPIO: DI07-14 → 緊急スイッチ                       │     │
│  └──────────────────────────────────────────────────┘     │
│                                                             │
│  ┌──────────────────┐  ┌──────────────────┐               │
│  │ rain_detector    │  │ uart_co2_reader  │               │
│  │ (systemd)        │  │ (systemd)        │               │
│  └──────────────────┘  └──────────────────┘               │
└─────────────────────────────────────────────────────────────┘
           │
    ┌──────┴──────┐
    │  ハウス内    │
    ├─────────────┤
    │ Pico W      │ Arduino, W5500 Ethernet
    │ env_node    │ → MQTT → unipi-daemon
    │ solar_node  │
    └─────────────┘
```

### 2.2 ディレクトリ構成（v4案）

```
unipi-agri-ha/
├── config/                    # 全設定ファイル統一
│   ├── agriha.cron            # cron設定（4ジョブ）
│   ├── channel_map.yaml       # リレーch↔機器マッピング
│   ├── crop_irrigation.yaml   # 灌水パラメータ
│   ├── emergency.conf.template# 緊急閾値テンプレート
│   ├── rules.yaml             # ルールエンジン設定（PID/灌水/風/温度）
│   ├── system_prompt.txt      # LLMシステムプロンプト
│   └── unipi_daemon.yaml      # unipi-daemon設定
├── src/agriha/                # 全Pythonソース統一パッケージ
│   ├── control/               # 三層制御 + executor
│   │   ├── emergency_guard.sh
│   │   ├── rule_engine.py
│   │   ├── forecast_engine.py
│   │   ├── plan_executor.py
│   │   └── channel_config.py
│   ├── chat/                  # WebUI + チャットAPI
│   │   ├── app.py
│   │   ├── templates/
│   │   └── static/
│   ├── daemon/                # HW抽象化デーモン
│   │   ├── main.py, rest_api.py, sensor_loop.py
│   │   ├── i2c_relay.py, gpio_watch.py, ds18b20.py
│   │   ├── wh65lp_reader.py, mqtt_relay_bridge.py
│   │   ├── ccm_receiver.py, emergency_override.py
│   │   └── mosquitto.conf
│   ├── services/              # 補助サービス
│   │   ├── rain_detector.py
│   │   └── uart_co2_reader.py
│   └── lib/                   # 共通ライブラリ
│       ├── datetime_helper.py
│       └── sensor_drivers/
├── tests/                     # 全テスト統一（pytest tests/）
│   ├── control/ chat/ daemon/ lib/
├── arduino/                   # Picoファームウェア
├── firmware/                  # CircuitPython
├── linebot/                   # LINE Bot（VPS）
├── systemd/                   # systemdサービスファイル統一
├── scripts/                   # デプロイ・ユーティリティ
├── docs/                      # 設計書
├── data/                      # 分析データ
├── docker/ cloud_server/      # インフラ構成
└── pyproject.toml             # プロジェクト定義
```

### 2.3 データフロー図

```
                    ┌──────────────┐
                    │ Visual       │
                    │ Crossing API │
                    └──────┬───────┘
                           │ HTTPS (毎時, TTL 1h cache)
                           ▼
┌───────────────┐   ┌──────────────┐   ┌──────────────┐
│ Misol WH65LP  │   │ forecast     │   │ Claude       │
│ (気象センサー) │   │ _engine.py   │──▶│ Haiku API    │
└───────┬───────┘   │ (cron 毎時)  │   └──────────────┘
        │UART       └──────┬───────┘
        ▼                  │ current_plan.json
┌───────────────┐   ┌──────▼───────┐   ┌──────────────┐
│ unipi-daemon  │◄──│ plan         │   │ 高札API      │
│ :8080         │   │ _executor.py │   │ (類似検索)   │
│ REST API      │   │ (cron 10分)  │   └──────────────┘
│ +MQTT publish │   └──────────────┘
└───────┬───────┘          ▲
        │                  │ flag files
┌───────▼───────┐   ┌──────┴───────┐
│ MCP23008      │   │ rule         │
│ 8ch Relay     │   │ _engine.py   │
│ (I2C)         │   │ (cron 10分)  │
└───────────────┘   └──────┬───────┘
                           ▲
┌───────────────┐   ┌──────┴───────┐
│ DS18B20       │   │ emergency    │
│ (内温)        │   │ _guard.sh    │
└───────────────┘   │ (cron 毎分)  │
                    └──────────────┘

センサー → MQTT → unipi-daemon キャッシュ → REST API → 制御スクリプト群
制御スクリプト → POST /api/relay/{ch} → MQTT publish → MCP23008 Relay → 物理動作
```

### 2.4 通信プロトコル

| 経路 | プロトコル | 詳細 |
|------|-----------|------|
| センサー→RPi | MQTT (mosquitto :1883) | Pico W → MQTT publish → unipi-daemon subscribe |
| RPi内部 | REST API (localhost) | unipi-daemon :8080 ← 制御スクリプト群 |
| RPi内部 | MQTT | unipi-daemon → relay/{ch}/set → MCP23008 |
| RPi→Claude | HTTPS | forecast_engine.py → Claude Haiku API (オプション) |
| RPi→天気 | HTTPS | forecast_engine.py → Visual Crossing API |
| RPi→LINE | HTTPS | emergency_guard.sh → LINE Notify API, linebot_handler → LINE Reply/Push API |
| LINE→RPi | HTTPS | LINE Platform → Webhook POST → RPi (Let's Encrypt + 固定IP) |
| RPi内部 | HTTP | agriha-ui → NullClaw Proxy (localhost:3001) |

### 2.5 MQTTトピック体系

```
agriha/
  {house_id}/                     # 例: h01
    sensor/
      DS18B20                     # 内温 (retain=True)
    relay/
      {ch}/set                    # リレー制御コマンド
      state                       # リレー状態一括 (retain=True)
    ccm/
      #                           # UECS-CCM変換データ
  farm/
    weather/
      misol                       # 気象データ (retain=True)
```

---

## §3 各機能の仕様

### §3.1 emergency_guard.sh（Layer1: 緊急停止）

**概要**: 最下層の安全装置。bash + curl のみで動作し、Python不要。毎分cronで実行。

| 項目 | 内容 |
|------|------|
| **入力** | GET localhost:8080/api/sensors → `indoor_temp` |
| **設定** | /etc/agriha/emergency.conf (temp_high=40, temp_low=5) |
| **処理** | 温度閾値チェック → 全窓操作 + lockout + LINE通知 |
| **出力** | POST /api/relay/{ch} (全窓ch), /var/lib/agriha/lockout 作成, LINE Notify |
| **依存** | unipi-daemon REST API, LINE Notify API |
| **コード行数** | 240行 |

**閾値と動作**:

| 条件 | 動作 | LINE通知 |
|------|------|---------|
| 内温 ≥ 40℃ | 全窓全開(value=1) + lockout発動 | 🚨高温緊急: {temp}℃ 全窓全開 |
| 内温 ≤ 5℃ | 全窓全閉(value=0) + lockout発動 | 🚨低温緊急: {temp}℃ 全窓全閉 |
| lockout中 + 15℃≤temp≤35℃ + 10分経過 | lockout解除 | ✅緊急解除 |

**lockout機構**:
- `/var/lib/agriha/lockout` にUNIXタイムスタンプを書き込み
- lockout中はLayer2/Layer3の制御をスキップ（下層が上層を黙らせる原則）
- 解除条件: 10分経過 AND 温度が15-35℃の安全範囲内

**環境変数オーバーライド**: `LOCKOUT_FILE`, `LOG_FILE`, `CONF_FILE`, `UNIPI_API_URL`, `LINE_NOTIFY_TOKEN`, `CONF_DIR`

### §3.2 rule_engine.py（Layer2: ルールベース制御）

**概要**: PID制御 + ルールベース判断。10分毎にcronで実行。channel_map.yaml経由でch番号を取得。

| 項目 | 内容 |
|------|------|
| **入力** | GET localhost:8080/api/sensors → indoor_temp, rainfall, wind_speed, wind_dir, solar_wm2 |
| **設定** | config/rules.yaml, config/channel_map.yaml, /var/lib/agriha/pid_override.json |
| **処理** | 優先度付きルール評価 → リレー操作 + フラグファイル管理 |
| **出力** | POST /api/relay/{ch}, rain_flag, wind_flag, solar_accum.json, temp_history.json |
| **依存** | unipi-daemon REST API, channel_config.py, astral (日の出/日没計算) |
| **コード行数** | 506行 |

**ルール優先度チェーン**:

| 優先度 | ルール | 条件 | 動作 |
|--------|--------|------|------|
| 1 | 降雨時全閉 | rainfall > 0 | 全窓OFF + rain_flag作成 |
| 2 | 強風方向制御 | wind_speed ≥ 5.0 m/s | 風向別片側閉鎖 + wind_flag |
| 3 | 気温急上昇 | 20分で+3℃以上 | 全窓全開 |
| 4 | 時間帯制御 | 日没後〜日の出前 | 全窓閉鎖 |
| 5 | PID温度制御 | 昼間 + 上記非該当 | PID出力>0→全窓開, ≤0→全窓閉 |
| 6 | 日射比例灌水 | solar積算 ≥ 0.9 MJ/m² | ch4灌水60秒 |

**PIDパラメータ** (config/rules.yaml):
- Kp=8.0, Ki=0.5, Kd=2.0, dt=600秒
- 昼間目標温度: 26.5℃（pid_override.jsonで上書き可）
- 出力範囲: 0.0-100.0%

**風向制御** (channel_map.yaml連動):
- 北風(NNW,N,NNE: dir={15,16,1,2}) → 北側窓(ch7,8)閉鎖
- 南風(SSW,S,SSE: dir={8,9,10}) → 南側窓(ch5,6)閉鎖

**灌水制御**:
- 日射積算: solar_wm2 × 600秒 / 1,000,000 = MJ/m² (10分毎加算)
- 閾値: 0.9 MJ/m²到達で ch4 灌水60秒、積算リセット
- 降雨時スキップ: rainfall ≥ 0.5 mm/h、または降雨停止後30分以内

**位置情報**: 北海道恵庭 (42.888°N, 141.603°E) — astralで日の出/日没を計算

### §3.3 forecast_engine.py（Layer3: LLM 1時間予報）

**概要**: デフォルトはNullClaw（ローカル・ゼロコスト）で向こう1時間の制御計画を生成。APIキー設定時はClaudeなどのクラウドAPIを優先使用し、API失敗・回線断時は自動でNullClawにフォールバック。毎時0分にcronで実行。高札APIの類似検索で既知パターンが3件以上あればLLMをスキップ。

| 項目 | 内容 |
|------|------|
| **入力** | sensors API, Visual Crossing天気予報, 高札API類似検索, system_prompt.txt |
| **設定** | config/system_prompt.txt, config/forecast.yaml (provider, api_key_env), .env (APIキー: オプション) |
| **処理** | 天気予報取得→類似検索→LLM判断(or skip)→計画JSON生成→PIDパラメータ変換 |
| **出力** | /var/lib/agriha/current_plan.json, pid_override.json, search_log.jsonl |
| **依存** | NullClaw (デフォルト, localhost:3001), Visual Crossing API, 高札API, unipi-daemon REST API |
| **依存(オプション)** | Claude Haiku API / OpenAI / Gemini (APIキー設定時のみ、失敗時はNullClaw) |
| **コード行数** | 456行 |

**実行フロー**:

```
1. lockoutチェック → lockout中はスキップ
2. Starlink接続チェック → ping失敗時はスキップ
3. GET /api/sensors → 現在のセンサーデータ取得
4. Visual Crossing API → 24時間天気予報 (TTL 1hキャッシュ: vc_cache.json)
5. build_search_query() → "{季節}_{時間帯}_{温度バンド}_{天気}" 生成
6. 高札API /search?q={query} → 類似過去判断を検索
7. 類似結果 ≥ 3件? → Yes: 高札結果からplan構築(source="kousatsu")
                     → No: Claude Haiku API呼び出し(source="llm")
8. convert_llm_to_pid_override() → co2_mode/humidity_max→PIDパラメータ変換
9. save_plan() → current_plan.json書き出し
10. log_search() → search_log.jsonl追記
```

**search_query形式**: `夏_午前_25-30℃_Clear`
- 季節: 月から判定（冬12-2, 春3-5, 夏6-8, 秋9-11）
- 時間帯: 早朝(<6), 午前(6-12), 午後(12-18), 夜間(≥18)
- 温度バンド: indoor_temp を5℃刻み (例: 25-30℃)
- 天気: Visual Crossing conditions フィールド

**LLM→PIDパラメータ変換**:
- co2_mode "ventilate" → co2_setpoint: 400
- co2_mode "accumulate" → co2_setpoint: 700
- humidity_max → VPD = (1 - humidity_max/100) × SVP(indoor_temp)
- SVP: Tetens式 `6.1078 × 10^(7.5T/(237.3+T))` hPa

**current_plan.json スキーマ**:
```json
{
  "generated_at": "ISO 8601",
  "valid_until": "ISO 8601 (now + 1h)",
  "source": "llm" | "kousatsu",
  "actions": [
    {"time": "ISO 8601", "relay": {"ch": 5, "value": 1, "duration_sec": 60}, "reason": "..."}
  ],
  "summary": "自然言語の計画要約"
}
```

### §3.4 plan_executor.py（計画実行）

**概要**: forecast_engine.pyが生成したcurrent_plan.jsonを読み、時刻マッチしたアクションを実行。10分毎にcronで実行。

| 項目 | 内容 |
|------|------|
| **入力** | /var/lib/agriha/current_plan.json, sensors API, flag files |
| **設定** | channel_map.yaml (WINDOW_CHANNELS, VALID_CH_MIN/MAX) |
| **処理** | 計画読込→有効期限確認→緊急温度チェック→時刻マッチ→天気フラグスキップ→リレー操作 |
| **出力** | POST /api/relay/{ch}, current_plan.json(実行結果書き戻し) |
| **依存** | unipi-daemon REST API, channel_config.py |
| **コード行数** | 284行 |

**時刻マッチング**: アクション時刻 ± 5分 (TIME_WINDOW_SEC=300) の範囲内で実行

**天気フラグスキップ**: 側窓チャンネル(ch5-8)のアクションは rain_flag/wind_flag 存在時にスキップ → `executed: "skipped_weather"`

**緊急温度制御**:
- indoor_temp > 27℃ → 全窓開放
- indoor_temp < 16℃ → 全窓閉鎖
- 緊急発動時はplan実行をスキップ

**duration_sec制限**: 最大3600秒（1時間）にクランプ

### §3.5 agriha_chat.py（Chat窓 + ダッシュボード + API）

**概要**: FastAPIアプリケーション。Chat UI、ダッシュボード、センサー/リレーAPIプロキシを統合。port 8501で稼働。

| 項目 | 内容 |
|------|------|
| **入力** | ユーザーメッセージ, unipi-daemon API, control_log.db, current_plan.json, ログファイル, フラグファイル |
| **設定** | channel_map.yaml, system_prompt.txt, api.env (ANTHROPIC_API_KEY) |
| **処理** | Chat: Claude API呼び出し。Dashboard: 各種データ集約。API: プロキシ+加工 |
| **出力** | HTML (Chat UI / Dashboard), JSON (各種API) |
| **依存** | Claude Haiku API, unipi-daemon REST API, astral, Jinja2, Chart.js (CDN) |
| **コード行数** | 599行 (+ frontend 527行) |

**APIエンドポイント一覧**:

| メソッド | パス | 機能 | 認証 |
|---------|------|------|------|
| GET | `/` | Chat UI (inline HTML) | なし |
| POST | `/chat` | Claude APIプロキシ (センサー+履歴注入) | なし |
| GET | `/api/history` | 判断履歴 (control_log.db, limit 1-100) | なし |
| GET | `/api/sensors` | センサープロキシ → :8080 | なし |
| GET | `/health` | ヘルスチェック | なし |
| GET | `/dashboard` | ダッシュボードHTML (Jinja2) | なし |
| GET | `/api/plan` | 制御計画JSON (current_plan.json) | なし |
| GET | `/api/logs` | ログ末尾 (4ファイル合算, default 50行, max 200) | なし |
| GET | `/api/relay` | リレー状態プロキシ + channel_map.yamlラベル付与 | なし |
| GET | `/api/flags` | フラグ状態 (lockout/rain/wind) | なし |
| GET | `/api/channel_map` | channel_map.yaml内容 | なし |
| GET | `/api/dashboard` | 一括取得 (sensors+plan+relay+flags+logs+timestamp) | なし |

**Chat API (POST /chat)**:
1. GET /api/sensors でセンサーデータ取得
2. control_log.db から直近5件の判断履歴取得
3. system_prompt.txt 読み込み（毎リクエスト）
4. 日時+日の出/日没+時間帯をユーザーメッセージに注入
5. Claude Haiku API (claude-haiku-4-5-20251001) に送信
6. テキスト応答を返却

**ダッシュボードフロントエンド**:
- dashboard.html (69行): センサー/リレー/フラグ/タイムライン(Chart.js)/ログ
- dashboard.js (349行): 30秒ポーリング、Chart.js scatter chart
- dashboard.css (109行): レスポンシブ2カラム、センサー色分け(ok/warn/danger)

### §3.6 unipi-daemon（センサー取得 + リレー制御）

**概要**: HW抽象化デーモン。5つのasyncioタスクを並行実行。port 8080でREST API提供。

| 項目 | 内容 |
|------|------|
| **入力** | I2C (MCP23008), 1-Wire (DS18B20), UART RS485 (Misol WH65LP), GPIO (DI07-14), MQTT, UECS-CCM (UDP multicast) |
| **設定** | /etc/agriha/unipi_daemon.yaml |
| **処理** | センサー周期読取→MQTT publish、REST API→MQTT relay制御、GPIO→緊急オーバーライド |
| **出力** | REST API (JSON), MQTT topics (agriha/{house_id}/*) |
| **依存** | mosquitto MQTT broker, smbus2 (I2C), serial (UART), gpiod (GPIO) |
| **コード行数** | 2,311行 (10ファイル) |

**REST APIエンドポイント** (port 8080):

| メソッド | パス | 機能 | 認証 |
|---------|------|------|------|
| POST | `/api/relay/{ch}` | リレー制御 (MQTT経由) | X-API-Key |
| GET | `/api/sensors` | センサーキャッシュ | X-API-Key |
| GET | `/api/status` | デーモン状態+lockout | X-API-Key |
| POST | `/api/emergency/clear` | lockout手動解除 | X-API-Key |

**POST /api/relay/{ch}** リクエスト:
```json
{"value": 0|1, "duration_sec": 180, "reason": "manual control"}
```
- duration_sec > 0: 自動OFFタイマー設定
- lockout中: 423 Locked Out レスポンス

**5つの並行タスク**:

| タスク | 機能 | 周期 |
|--------|------|------|
| sensor_loop | DS18B20 + Misol WH65LP → MQTT publish | 10秒 |
| mqtt_loop | MQTT subscribe → relay/{ch}/set → MCP23008 制御 | 常時 |
| gpio_watch | GPIO DI07-14 エッジ検出 → 緊急オーバーライド | 常時 |
| rest_api | FastAPI REST-MQTT変換 | 常時 |
| ccm_loop | UECS-CCM UDP multicast → MQTT publish | 常時 |

**ハードウェアインターフェース**:

| IF | デバイス | 用途 |
|----|---------|------|
| I2C (bus 1, addr 0x20) | MCP23008 | 8chリレー制御 (ch1=GP7〜ch8=GP0、逆配線) |
| 1-Wire (/sys/bus/w1/devices/) | DS18B20 | ハウス内温度 |
| UART RS485 (/dev/ttyUSB0, 9600bps) | Misol WH65LP | 気象 (温湿度/風速/風向/降雨/日射) |
| GPIO (/dev/gpiochip0, DI07-14) | 物理スイッチ | 緊急オーバーライド (300秒lockout) |
| UDP multicast (224.0.0.1:16520) | UECS-CCM | レガシーセンサー受信 |

### §3.7 LINE Bot（RPi直接稼働）

**概要**: RPi上のagriha-ui（FastAPI, port 8501）に統合。VPS不要。LLMはNullClaw（デフォルト）またはClaude API（APIキー設定時）。HTTPS到達はLet's Encrypt + 固定IP。

| 項目 | 内容 |
|------|------|
| **入力** | LINE Messaging API Webhook (POST /callback), unipi-daemon REST API (localhost) |
| **設定** | .env (LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID), forecast.yaml |
| **処理** | 署名検証 → 全テキスト→LLM tool calling→REST API実行→Reply |
| **出力** | LINE応答メッセージ, POST /api/relay/{ch} (localhost) |
| **LLM** | NullClawFallbackClient（APIキーなし→NullClaw直行、あり→API優先→失敗時フォールバック） |

**主要ファイル**:
- `src/agriha/chat/app.py` L733-810: /callback エンドポイント（agriha-uiに統合）
- `src/agriha/chat/linebot_handler.py` (293行): 署名検証 + LLM tool calling + Reply/Push
- `src/agriha/control/nullclaw_proxy.py` (155行): NullClaw OpenAI互換プロキシ (port 3001)

**ツール定義** (OpenAI tools形式, linebot_handler.py):

| ツール | 機能 | パラメータ |
|--------|------|-----------|
| get_sensors | 全センサーデータ取得（CCM+DS18B20+Misol+リレー状態） | なし |
| get_status | デーモン状態取得（リレー状態+ロックアウト+稼働時間） | なし |
| set_relay | リレーch ON/OFF制御 | channel(1-8), value(0/1), duration_sec(0=永続) |

**LLMフォールバック設計** (NullClawFallbackClient, forecast_engine.py):
- APIキー未設定 → NullClaw直行（tools除外、プロンプト埋め込み方式）
- APIキーあり → check_connectivity() → オンライン → Claude API（tool calling有効）
- API失敗 or オフライン → NullClawフォールバック（tools除外）
- 毎回ステートレス判定、回線復帰→次メッセージから自動復帰
- NullClawフォールバック時の制限: set_relay不可（読み取りのみ）

**Webhook到達方式**:

| 方式 | 条件 | URL |
|------|------|-----|
| Let's Encrypt + 固定IP | USB SIM（固定IP付きSIMカード）使用時 | `https://{固定IP}/callback` |
| 自己署名証明書 | LINE Messaging APIは自己署名をサポート（証明書登録が必要） | `https://{固定IP}/callback` |

- settings画面でWebhook URLを自動生成・表示（§3.9参照）
- LINE Developers Console に Webhook URL を設定

**旧VPS版との差分**:

| 項目 | 旧（VPS版） | 新（RPi直接版） |
|------|------------|----------------|
| 実行場所 | VPS (Docker) | RPi (agriha-ui内) |
| LLM | Anthropic SDK直接 | NullClawFallbackClient（デフォルトNullClaw） |
| センサー取得 | WireGuard VPN経由 | localhost REST API (直接) |
| Webhook到達 | VPS固定IP | Let's Encrypt + USB SIM固定IP |
| 設定管理 | VPS上.env | RPi /opt/agriha/.env + settings UI |
| system_prompt | linebot/system_prompt.py | /etc/agriha/system_prompt.txt (共用) |
| 依存サービス | Docker, Nginx, Certbot | agriha-ui内蔵 + certbot |

### §3.8 カメラ定点撮影

### §3.9 USB SIM / APN設定UI

**概要**: settings画面に「ネットワーク設定」セクションを追加。USB SIMドングルのAPN設定、接続状態表示、LINE Webhook URL自動生成を提供。

| 項目 | 内容 |
|------|------|
| **入力** | ユーザーのAPN設定入力 |
| **設定** | NetworkManager connection profile |
| **処理** | ModemManager/NetworkManager経由でUSBドングル制御 |
| **出力** | セルラー接続確立、固定IP取得、Webhook URL表示 |
| **依存** | ModemManager, NetworkManager, usb-modeswitch |

**settings画面「ネットワーク設定」セクション**:

```
┌─────────────────────────────────────────┐
│ ネットワーク設定                          │
│                                          │
│ 接続状態: ● セルラー (SIM)  ○ WiFi  ○ 有線 │
│ 固定IP:   xxx.xxx.xxx.xxx                │
│ Webhook URL: https://xxx.xxx.xxx.xxx/callback │
│ [URLをコピー]                            │
│                                          │
│ ── APN設定 ──                            │
│ プリセット: [SORACOM ▼] [IIJmio] [手動入力] │
│                                          │
│ APN名:     [soracom.io                ] │
│ ユーザー:   [sora                      ] │
│ パスワード: [sora                       ] │
│ 認証方式:   [CHAP ▼]                     │
│                                          │
│ [保存して接続]                            │
│                                          │
│ ── LINE Bot設定 ──                       │
│ Channel Secret:  [********cret]          │
│ Access Token:    [********oken]          │
│ User ID:         [********efgh]          │
│ [保存]                                   │
│ 状態: 設定済み / 未設定                   │
│                                          │
│ ── HTTPS証明書 ──                        │
│ 証明書状態: 有効 (有効期限: 2026-06-05)   │
│ [証明書更新] [自己署名証明書で代替]        │
└─────────────────────────────────────────┘
```

**既知キャリアプリセット**:

| キャリア | APN | ユーザー | パスワード | 認証方式 |
|---------|-----|---------|-----------|---------|
| SORACOM Air（デフォルト） | soracom.io | sora | sora | CHAP |
| IIJmio | iijmio.jp | mio@iij | iij | CHAP |
| 手動入力 | (ユーザー入力) | — | — | — |

> **注意**: 上記以外のキャリアをご利用の場合は「手動入力」で農家ご自身がAPN情報を入力してください。

**ModemManager/NetworkManager連携**:

```bash
# USB SIMドングル検出
mmcli -L                              # モデム一覧
mmcli -m 0                            # モデム詳細（SIM状態、信号強度）

# APN設定 → NetworkManager connection作成
nmcli connection add type gsm \
  con-name "agriha-sim" \
  ifname "*" \
  gsm.apn "soracom.io" \
  gsm.username "sora" \
  gsm.password "sora"

# 接続
nmcli connection up agriha-sim

# IP確認
nmcli device show | grep IP4.ADDRESS
```

**固定IP取得**: SIMカード契約に依存。IIJmioフルMVNO等は固定IPオプションあり。SORACOM Beamでも可能。settings画面で取得したIPをWebhook URLとして表示。

**app.pyエンドポイント追加**:

| メソッド | パス | 機能 |
|---------|------|------|
| GET | `/api/network/status` | 接続状態(SIM/WiFi/有線)、固定IP、モデム情報 |
| POST | `/settings/network/apn` | APN設定保存+接続 |
| POST | `/settings/network/cert` | HTTPS証明書取得/更新 |

### §3.8 カメラ定点撮影

**概要**: agriha-capture.sh (mainブランチ)。cron定期実行でカメラ画像を撮影し、Nginx経由で /picture/ パスで配信。

| 項目 | 内容 |
|------|------|
| **入力** | USBカメラ (RPi接続) |
| **処理** | 定期撮影 → /var/www/agriha-photos/last.jpg 保存 |
| **出力** | JPEG画像ファイル |
| **備考** | v3-rebuildブランチには未移行。Nginx統合設計(report#683)でdashboardにimg表示予定 |

---

## §4 データモデル

### 4.1 データベース

#### control_log.db (判断履歴)

| パス | /var/lib/agriha/control_log.db |
|------|------|
| エンジン | SQLite3 |
| 参照元 | agriha_chat.py (GET /api/history, POST /chat) |
| 書込元 | agriha_control.py (旧LLM制御ループ、v4で整理予定) |

**テーブル: decisions**

| カラム | 型 | 内容 |
|--------|-----|------|
| id | INTEGER PRIMARY KEY | 自動採番 |
| timestamp | TEXT | ISO 8601 |
| summary | TEXT | 判断要約 |
| actions_taken | TEXT | 実行アクション (JSON) |
| sensor_snapshot | TEXT | センサースナップショット (JSON) |

### 4.2 設定ファイル

| ファイル | デプロイ先 | 内容 | 参照元 |
|---------|----------|------|--------|
| channel_map.yaml | /etc/agriha/ | ch↔機器マッピング (v2 spec) | rule_engine, plan_executor, agriha_chat, emergency_guard |
| rules.yaml | config/ (リポジトリ内) | PID/灌水/風/温度閾値 | rule_engine |
| crop_irrigation.yaml | config/ | 作物別灌水パラメータ (長ナス6ステージ) | (将来参照) |
| system_prompt.txt | /etc/agriha/ | LLMシステムプロンプト | forecast_engine, agriha_chat |
| emergency.conf | /etc/agriha/ | 緊急閾値 (temp_high=40, temp_low=5) | emergency_guard |
| unipi_daemon.yaml | /etc/agriha/ | daemon設定 (house_id, MQTT, I2C, GPIO, UART) | unipi-daemon |
| api.env | /etc/agriha/ | APIキー (ANTHROPIC_API_KEY等) | agriha_chat (EnvironmentFile) |

### 4.3 ランタイムファイル (/var/lib/agriha/)

| ファイル | 形式 | 作成元 | 参照元 | 用途 |
|---------|------|--------|--------|------|
| lockout | Plain text (UNIX timestamp) | emergency_guard.sh | rule_engine, plan_executor, forecast_engine | 緊急ロックアウトフラグ |
| rain_flag | Empty file | rule_engine.py | plan_executor | 降雨フラグ |
| wind_flag | Empty file | rule_engine.py | plan_executor | 強風フラグ |
| current_plan.json | JSON | forecast_engine.py | plan_executor, agriha_chat | LLM制御計画 (1時間) |
| pid_override.json | JSON | forecast_engine.py | rule_engine | LLM→PIDパラメータ変換結果 |
| vc_cache.json | JSON | forecast_engine.py | forecast_engine | Visual Crossing APIキャッシュ (TTL 1h) |
| search_log.jsonl | JSONL | forecast_engine.py | — | 高札検索ログ |
| control_log.db | SQLite | agriha_control.py | agriha_chat | 判断履歴DB |
| solar_accum.json | JSON | rule_engine.py | rule_engine | 日射積算値 (灌水用) |
| temp_history.json | JSON | rule_engine.py | rule_engine | 温度履歴 (急上昇検出用、30分ローリング) |
| rain_stopped_at.json | JSON | rule_engine.py | rule_engine | 降雨停止タイムスタンプ |

### 4.4 ログファイル (/var/log/agriha/)

| ファイル | ソース | ローテーション |
|---------|--------|-------------|
| emergency.log | emergency_guard.sh | — |
| rule_engine.log | rule_engine.py | — |
| forecast_engine.log | forecast_engine.py | — |
| executor.log | plan_executor.py | — |

### 4.5 MQTTメッセージスキーマ

**センサーデータ (agriha/{house_id}/sensor/DS18B20)**:
```json
{"device_id": "28-00000de13271", "temperature_c": 22.5, "timestamp": 1740000000.0}
```

**気象データ (agriha/farm/weather/misol)**:
```json
{"temperature_c": 15.2, "humidity_pct": 65, "wind_speed_ms": 2.1, "timestamp": 1740000000.0}
```

**リレー状態 (agriha/{house_id}/relay/state)**:
```json
{"ch1": 0, "ch2": 0, "ch3": 0, "ch4": 0, "ch5": 1, "ch6": 1, "ch7": 0, "ch8": 0, "ts": 1740000000}
```

**リレー制御コマンド (agriha/{house_id}/relay/{ch}/set)**:
```json
{"value": 1, "duration_sec": 180, "reason": "manual control"}
```

---

## §5 デプロイ構成

### 5.1 systemdサービス

| サービス | ポート | デプロイ先 | 実行ユーザー | 概要 |
|---------|--------|----------|-------------|------|
| unipi-daemon | 8080 | /opt/agriha/ | agriha | HW抽象化 + REST API + MQTT |
| agriha-nullclaw-proxy | 3001 | /opt/agriha/ | agriha | NullClaw OpenAI互換プロキシ (デフォルトLLM) |
| agriha-chat | 8501 | /opt/agriha-chat/ | root | WebUI + Chat + Dashboard API |
| rain_detector | — | /opt/agriha/services/ | pi | 雨検知サービス |
| uart_co2_reader | — | (リポジトリ直参照) | root | CO2センサー読取 |
| ModemManager | — | (OS標準) | root | USB SIMドングル制御 |
| certbot.timer | — | (OS標準) | root | Let's Encrypt HTTPS証明書自動更新 |

### 5.2 cronスケジュール (config/agriha.cron)

| 頻度 | スクリプト | ログ出力先 |
|------|----------|-----------|
| `* * * * *` | emergency_guard.sh | /var/log/agriha/emergency.log |
| `*/10 * * * *` | rule_engine.py | /var/log/agriha/rule_engine.log |
| `0 * * * *` | forecast_engine.py | /var/log/agriha/forecast_engine.log |
| `*/10 * * * *` | plan_executor.py | /var/log/agriha/executor.log |

### 5.3 Nginx統合設計

**port 80 → 統合フロントエンド** (report#683 設計案):

```
http://rpi/              → agriha-chat /dashboard  (ダッシュボード)
http://rpi/chat          → agriha-chat /           (Chat窓)
http://rpi/picture/      → /var/www/agriha-photos/ (カメラ画像)
http://rpi/api/*         → agriha-chat /api/*      (ダッシュボードAPI)
http://rpi/api/unipi/*   → unipi-daemon /api/*     (直接API, API Key認証)
http://rpi/static/*      → agriha-chat /static/*   (JS/CSS)
http://rpi/health        → agriha-chat /health
```

- **LAN内HTTP**: port 80 → ダッシュボード・Chat窓（RPi Chromium kiosk表示）
- **LINE Webhook用HTTPS**: port 443 → /callback のみ。Let's Encrypt + 固定IP（USB SIM）
- **WebSocket不要**: REST + 30秒ポーリング
- **CORS不要**: Nginx統合後は同一オリジン

**Nginx HTTPS設定（LINE Webhook用）**:
```
server {
    listen 443 ssl;
    server_name _;    # 固定IPでアクセス

    ssl_certificate     /etc/letsencrypt/live/agriha/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agriha/privkey.pem;

    location /callback {
        proxy_pass http://localhost:8501/callback;
    }
    location / {
        proxy_pass http://localhost:8501;
    }
}
```

### 5.4 RPiディレクトリ構成

| パス | 用途 |
|------|------|
| ~/uecs-llm/ | git clone先（デプロイ元） |
| /etc/agriha/ | 設定ファイル (channel_map.yaml, system_prompt.txt, emergency.conf, api.env, unipi_daemon.yaml) |
| /var/lib/agriha/ | ランタイムデータ (DB, plan, flags, cache) |
| /var/log/agriha/ | ログ (4ファイル) |
| /opt/agriha/ | unipi-daemon + rain_detector デプロイ先 |
| /opt/agriha-chat/ | agriha-chat デプロイ先 |
| /var/www/agriha-photos/ | カメラ画像 (Nginx配信) |

### 5.5 セットアップ (setup.sh)

`setup.sh` (128行) が冪等デプロイを提供:

1. Python venv作成 + pip install（daemon extras）
2. /etc/agriha/ ディレクトリ作成 + 設定ファイルコピー（既存は上書きしない）
3. agrihaシステムユーザー作成 + /var/lib/agriha/, /var/log/agriha/ ディレクトリ作成
4. systemdサービスインストール + enable (unipi-daemon, agriha-ui)
5. cron設定（三層制御用）
6. .env.example → .env コピー
7. Nginx設定デプロイ

**v4追加ステップ（実装予定）**:

8. USB SIM関連パッケージインストール
   ```bash
   apt install -y modem-manager network-manager usb-modeswitch
   systemctl enable ModemManager NetworkManager
   ```
9. HTTPS証明書セットアップ
   ```bash
   apt install -y certbot python3-certbot-nginx
   # 固定IP取得後に実行（setup.sh内では案内表示のみ）:
   # certbot --nginx -d {固定IP}.nip.io --non-interactive --agree-tos -m user@example.com
   # または自己署名証明書:
   # openssl req -x509 -newkey rsa:2048 -keyout /etc/agriha/ssl/key.pem -out /etc/agriha/ssl/cert.pem -days 365 -nodes
   ```
10. NullClawプロキシサービスインストール
    ```bash
    # systemd/agriha-nullclaw-proxy.service インストール + enable
    ```

**初回セットアップウィザード（構想）**:

setup.sh実行後、agriha-uiの初回アクセス時にウィザードを表示:
1. APN設定（USB SIMドングル検出→プリセット選択→接続テスト）
2. LINE Bot設定（Channel Secret/Access Token/User ID入力）
3. LINE Webhook URL表示（固定IP自動検出→URLコピー→LINE Developers Consoleへ案内）
4. テスト送信（LINE Botから「設定完了」メッセージ送信で動作確認）
5. LLMプロバイダー選択（NullClaw=デフォルト、APIキー入力でClaude等も可）

ウィザードは `/setup` エンドポイントで提供。完了後は `/dashboard` にリダイレクト。
`/var/lib/agriha/.setup_complete` フラグファイルで初回判定。

### 5.6 外部サービス依存

| サービス | 用途 | APIキー | 費用見込み |
|---------|------|---------|-----------|
| NullClaw (ローカル) | forecast_engine デフォルト | 不要 | **ゼロコスト** (箱出し即動作) |
| Claude Haiku API | forecast_engine + agriha_chat (オプション) | ANTHROPIC_API_KEY | 月数百円→自然減衰 |
| OpenAI / Gemini / Ollama | forecast_engine (オプション) | 各APIキー | 使用量に応じた費用 |
| Visual Crossing | 天気予報 (24h先) | VISUAL_CROSSING_API_KEY | 無料枠 (1000 req/day) |
| LINE Notify | 緊急通知 | LINE_NOTIFY_TOKEN | 無料 |
| LINE Messaging API | Bot対話 | LINE_CHANNEL_* | 無料枠 (200 msg/day) |

**設計思想**: デフォルト=NullClaw（ゼロコスト・オフライン・箱出し即動作）。APIキー設定時のみクラウドAPI優先。API失敗・回線断時は自動フォールバック。LLM自然減衰モデルの最終形=デフォルト状態に回帰。

### 5.7 ArSprout既存USB SIMとの互換性

**概要**: ArSprout（農研機構系統の温室環境制御装置）のオプションUSB SIMドングルを引き継いで使用できるかの整理。

**ArSproutのUSB SIM構成**:
- 一般的なUSB SIMドングル（Huawei E8372, AnyDATA W600 等）を使用
- ArSprout本体のLinux（Debian系）でNetworkManager経由で接続
- 農研機構推奨SIM: IIJmio、SORACOM Air 等

**AgriHA v4での互換性**:

| 項目 | 互換性 | 備考 |
|------|--------|------|
| USBドングルハードウェア | ○ 互換 | RPi4のUSBポートにそのまま差し替え可能 |
| SIMカード | ○ 互換 | APN設定が同じなら流用可能 |
| APN設定 | ○ 移行可能 | ArSproutのNetworkManager設定を参照して手動入力、またはプリセット選択 |
| 固定IPアドレス | △ 要確認 | SIMカードに固定IPが紐づいていれば引き継がれる。キャリア契約依存 |
| ドライバー | ○ 互換 | Raspberry Pi OS（Debian系）はModemManager/usb-modeswitchを標準サポート |

**ArSproutからの移行手順**:
1. ArSproutからUSB SIMドングルを抜く
2. RPi4のUSBポートに差す
3. `mmcli -L` でモデム検出を確認
4. settings画面「ネットワーク設定」でAPN設定（ArSproutと同じ値を入力、またはプリセット選択）
5. 接続テスト → 固定IP確認
6. LINE Webhook URLをLINE Developers Consoleに設定

**注意事項**:
- ArSproutの制御PCとRPiを同時に同じSIMで使うことはできない（物理的に1枚のSIM）
- ArSproutを廃止してAgriHAに完全移行する場合にのみSIMを流用可能
- ArSproutと並行運用する場合は別途SIMカードが必要

---

<!-- §6-9: agriha_v4_spec_part2.md を参照 -->
