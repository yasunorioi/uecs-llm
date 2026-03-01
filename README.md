# uecs-llm

LLM による温室環境制御システム — 三層自律制御アーキテクチャ（v2）

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│ Raspberry Pi (AgriHA OS) — 全制御をオンボード実行       │
│                                                         │
│  ┌── Layer 1: 緊急制御 ──────────────────────────────┐  │
│  │ emergency_guard.sh (POSIX sh, cron 1分)           │  │
│  │ 高温/低温→即時開窓・ロックアウト                   │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌── Layer 2: ルールベース制御 ──────────────────────┐  │
│  │ rule_engine.py (cron 5分)                         │  │
│  │ YAML定義ルール→灌水・換気・CO2制御                │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌── Layer 3: LLM予報制御 ──────────────────────────┐  │
│  │ forecast_engine.py (cron 1時間)                   │  │
│  │ Claude Haiku API→天気予報+制御計画生成             │  │
│  │ plan_executor.py (cron 1分) → 計画実行             │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  unipi-daemon: センサー・リレー・MQTT・REST API         │
│  agriha_ui: ローカルWebUI (FastAPI+htmx, ポート8502)   │
│  Mosquitto: MQTTブローカー                              │
├─────────────────────────────────────────────────────────┤
│ UniPi 1.1 ハードウェア                                  │
│  MCP23008 リレー(8ch) + DS18B20 + GPIO DI + Misol RS485│
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ VPS（オプション）                                       │
│  Telegraf→InfluxDB→Grafana（VPN経由MQTT転送）          │
│  LINE Bot (Claude Haiku API)                            │
└─────────────────────────────────────────────────────────┘
```

## 設計原則

- **下層が上層を黙らせる**: Layer 1ロックアウト中はLayer 2/3は動作しない
- **各層独立動作**: 上層が死んでも下層の安全機構は生きる
- **RPi単一構成**: x86 Mini PC不要。全てRaspberry Pi上で完結
- **LLMはクラウドAPI**: Claude Haiku API使用。ローカルLLM不要

## コンポーネント

| コンポーネント | 場所 | 説明 |
|---------------|------|------|
| `v2_control` | `src/v2_control/` | 三層制御（emergency_guard.sh, rule_engine.py, forecast_engine.py, plan_executor.py） |
| `unipi_daemon` | `src/unipi_daemon/` | ハードウェアデーモン（I2C, GPIO, MQTT, REST API） |
| `agriha_ui` | `src/agriha_ui/` | ローカルWebUI（FastAPI + Jinja2 + htmx, ポート8502） |
| `linebot` | `linebot/` | LINE Bot（VPS, Claude Haiku API） |
| `cloud` | `cloud/` | VPS構成（InfluxDB + Telegraf + Grafana + LINE Bot） |
| `image` | `image/` | Raspbian カスタムイメージビルダー |
| `config` | `config/` | 設定テンプレート（thresholds.yaml, unipi_daemon.yaml等） |

## セットアップ

### RPi（本番環境）

```bash
git clone https://github.com/yasunorioi/uecs-llm.git ~/uecs-llm
cd ~/uecs-llm
cp .env.example .env
nano .env  # ANTHROPIC_API_KEY を記入
./setup.sh
```

これだけで以下が完了する:
- Python venv作成 + パッケージインストール
- /etc/agriha/ に設定ファイル配置
- /var/lib/agriha/ データディレクトリ作成
- systemd サービス有効化（unipi-daemon, agriha-ui）
- 三層制御 cron 設定

### VPS（クラウドサーバー）

```bash
cd ~/uecs-llm/cloud
cp .env.example .env
nano .env  # LINE Bot 認証情報を記入
docker compose up -d
```

## LINE API 認証情報の取得

LINE Bot を利用するには LINE Developers Console でチャネルを作成し、認証情報を取得する必要があります。

### 手順

1. [LINE Developers Console](https://developers.line.biz/console/) にログイン
2. **プロバイダーを作成**（初回のみ）
3. **Messaging API チャネルを作成**
   - チャネル名: 任意（例: `AgriHA Bot`）
   - チャネル説明: 任意
4. **チャネルシークレットを取得**
   - チャネル基本設定 → チャネルシークレット → `.env` の `LINE_CHANNEL_SECRET` に記入
5. **チャネルアクセストークン（長期）を発行**
   - Messaging API設定 → チャネルアクセストークン → 「発行」 → `.env` の `LINE_CHANNEL_ACCESS_TOKEN` に記入
6. **あなたのユーザーIDを取得**
   - チャネル基本設定 → あなたのユーザーID → `.env` の `LINE_USER_ID` に記入
   - ※ Grafana アラートの LINE 通知先としても使用
7. **Webhook URLを設定**
   - Messaging API設定 → Webhook URL → `https://your-vps-domain:8443/callback`
   - Webhookの利用 → ON
   - 応答メッセージ → OFF（Bot が直接返答するため）

### .env 設定例

```bash
LINE_CHANNEL_SECRET=abc123def456...
LINE_CHANNEL_ACCESS_TOKEN=XXXXXXXXXXX...
LINE_USER_ID=U1234567890abcdef...
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5-20251001
```

## テスト

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
