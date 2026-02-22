# uecs-llm

LLM による温室環境制御システム（UECS-CCM 連携）

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│ x86 Mini PC (nuc.local)                              │
│  llama-server (LFM2.5 1.2B)                            │
│  agriha_control.py  ← cron 5分間隔                     │
│    └→ REST API → unipi-daemon                          │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP / WireGuard VPN
┌───────────────────────▼─────────────────────────────────┐
│ Raspberry Pi (AgriHA OS)                                │
│  unipi-daemon                                          │
│    ├ sensor_loop   : DS18B20 + Misol WH65LP → MQTT     │
│    ├ ccm_receiver  : UECS-CCM マルチキャスト → MQTT     │
│    ├ mqtt_bridge   : MQTT ↔ MCP23008 I2C リレー        │
│    ├ gpio_watch    : DI07-14 緊急オーバーライド         │
│    └ rest_api      : FastAPI REST-MQTT 変換             │
│  Mosquitto (MQTT ブローカー)                            │
└───────────────────────┬─────────────────────────────────┘
                        │ I2C / GPIO / 1-Wire / RS485
┌───────────────────────▼─────────────────────────────────┐
│ UniPi 1.1 ハードウェア                                  │
│  MCP23008 リレー(8ch) + DS18B20 + GPIO DI + Misol RS485│
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ VPS（オプション）                                       │
│  Telegraf : MQTT→InfluxDB ブリッジ (VPN経由)            │
│  InfluxDB : 時系列データベース                           │
│  Grafana  : ダッシュボード＋アラート (LINE通知)          │
│  LINE Bot → Ollama (VPN経由)                            │
└─────────────────────────────────────────────────────────┘
```

## クイックスタート

### 1. LLM 制御サーバー（x86 Mini PC）

```bash
git clone https://github.com/yasunorioi/uecs-llm.git
cd uecs-llm
make install-llm-server

# LFM2.5 モデルのダウンロード
# https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF

# llama-server の起動
sudo cp systemd/agriha-llm.service /etc/systemd/system/
sudo systemctl enable --now agriha-llm

# 5分間隔の制御ループを設定
sudo cp systemd/agriha-control.cron /etc/cron.d/agriha-control
```

### 2. UniPi デーモン（Raspberry Pi）

```bash
cd uecs-llm
make install-pi-daemon

# 設定ファイルをコピーして編集
sudo mkdir -p /etc/agriha
sudo cp config/unipi_daemon.example.yaml /etc/agriha/unipi_daemon.yaml

# MQTT ブローカーの起動
cd docker && docker compose up -d && cd ..

# デーモンの起動
sudo cp systemd/unipi-daemon.service /etc/systemd/system/
sudo systemctl enable --now unipi-daemon
```

### 3. クラウドサーバー（VPS）

VPS に InfluxDB + Telegraf + Grafana + LINE Bot をまとめてデプロイします。
Telegraf は WireGuard VPN 経由で RPi の Mosquitto (MQTT) に接続し、センサーデータを InfluxDB に蓄積します。

```bash
cd uecs-llm/cloud
cp .env.example .env
# .env に認証情報を記入（下記「LINE API 認証情報の取得」参照）
docker compose up -d
```

| サービス | ポート | 説明 |
|---------|--------|------|
| InfluxDB | 8086 | 時系列データベース |
| Telegraf | — | MQTT→InfluxDB ブリッジ（VPN経由でRPi接続） |
| Grafana | 3000 | ダッシュボード・アラート（LINE通知） |
| LINE Bot | 8443 | Webhook受信 + Ollama連携 |

#### LINE Bot 単体（Ollama同梱）

Ollama を VPS 上で動かす場合は `linebot/` の docker-compose を使用：

```bash
cd uecs-llm/linebot
cp .env.example .env
docker compose up -d
```

### 4. SD カードイメージ（ゼロから構築）

```bash
cd uecs-llm/image
sudo ./build_image.sh raspios-bookworm-arm64-lite.img
# SD カードに書き込んで起動
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
```

## コンポーネント

| コンポーネント | 場所 | 対象環境 | 説明 |
|---------------|------|---------|------|
| `uecs_llm` | `src/uecs_llm/` | x86 / Pi5 | LLM 制御ループ (agriha_control.py) |
| `unipi_daemon` | `src/unipi_daemon/` | RPi | ハードウェアデーモン (I2C, GPIO, MQTT, REST) |
| `linebot` | `linebot/` | VPS | LINE Bot（Ollama 連携） |
| `cloud` | `cloud/` | VPS | InfluxDB + Telegraf + Grafana + LINE Bot（Docker Compose） |
| `image` | `image/` | ビルドホスト | Raspbian カスタムイメージビルダー |
| `config` | `config/` | — | 設定テンプレート |
| `systemd` | `systemd/` | — | サービスファイル・cron |

## tmux ダッシュボード

LLM 制御サーバー（nuc.local）で tmux ベースの監視・対話ダッシュボードを起動できます。

```bash
./scripts/start-tmux.sh
```

```
┌──────────────────┬──────────────────┐
│ 0: llama-server  │ 2: unipi-daemon  │
│    ログ          │    制御ログ       │
├──────────────────┼──────────────────┤
│ 1: MQTT monitor  │ 3: REST API      │
│   (RPi経由)      │    状態監視       │
├──────────────────┴──────────────────┤
│ 4: LLM Chat (対話窓)               │
└─────────────────────────────────────┘
```

| ペイン | 内容 |
|--------|------|
| 0 | llama-server のジャーナルログ |
| 1 | RPi の MQTT ブローカーをサブスクライブ（全トピック） |
| 2 | agriha-control（cron 制御ループ）のログ |
| 3 | RPi の REST API からセンサー値・ステータスを定期取得 |
| 4 | llama-server と対話できるチャット窓（system_prompt.txt 自動注入） |

### 操作方法

```bash
# セッションに接続
tmux attach -t agriha

# ペイン間の移動
Ctrl+b → 矢印キー

# セッションからデタッチ（バックグラウンド維持）
Ctrl+b → d

# セッション終了
tmux kill-session -t agriha
```

### 環境変数で設定を上書き

```bash
RPI_HOST=10.10.0.10 LLAMA_URL=http://localhost:8081 ./scripts/start-tmux.sh
```

## テスト

```bash
make test
# または
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
