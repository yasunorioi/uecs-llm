# AgriHA (uecs-llm)

LLM による温室環境制御システム — 三層自律制御アーキテクチャ (v4)

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│ Raspberry Pi 5 (AgriHA) — 全制御をオンボード実行         │
│                                                         │
│  ┌── Layer 1: 緊急制御 ──────────────────────────────┐  │
│  │ emergency_guard.sh (cron 1分)                     │  │
│  │ 高温/低温→即時開窓・ロックアウト                   │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌── Layer 2: ルールベース制御 ──────────────────────┐  │
│  │ rule_engine.py (cron 10分)                        │  │
│  │ YAML定義ルール→灌水・換気・強風・降雨制御         │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌── Layer 3: LLM予報制御 ──────────────────────────┐  │
│  │ forecast_engine.py (cron 1時間)                   │  │
│  │ llama-server + Qwen3.5-2B (デフォルト)            │  │
│  │ → 天気予報+1時間制御計画生成                       │  │
│  │ plan_executor.py (cron 10分) → 計画実行            │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  llama-server (port 3001): Qwen3.5-2B Q8_0 ローカル推論│
│  unipi-daemon: センサー・リレー・MQTT・REST API         │
│  agriha-ui: ローカルWebUI (FastAPI+htmx, ポート8501)   │
│  Mosquitto: MQTT broker (ポート1883)                    │
├─────────────────────────────────────────────────────────┤
│ UniPi 1.1 ハードウェア                                  │
│  MCP23008 リレー(8ch) + DS18B20 + GPIO DI + Misol RS485│
│                                                         │
│ USB SIMモデム (オプション): 固定回線不要な圃場向け       │
└─────────────────────────────────────────────────────────┘
```

## 設計原則

- **下層が上層を黙らせる**: Layer 1ロックアウト中はLayer 2/3は動作しない
- **各層独立動作**: 上層が死んでも下層の安全機構は生きる
- **RPi単一構成**: 全てRaspberry Pi上で完結（VPS不要・クラウドAPI不要）
- **オフラインファースト**: llama-server + Qwen3.5-2B でローカル完結。通信費ゼロ
- **HW不在でも起動**: I2Cデバイス未接続時はNullRelay（no-op）で全サービス起動継続
- **マルチLLM対応**: OpenAI SDK互換でClaude/GPT/Gemini/Ollama/llama-serverを切替可能

## llama-server（デフォルトLLMプロバイダー）

v4ではllama.cpp (llama-server) + Qwen3.5-2B をデフォルトLLMとして採用。
RPi5のCPU（Cortex-A76, ARM NEON）のみで推論。GPU不要。

- **モデル**: Qwen3.5-2B Q8_0 (1.9GB) — ツールコール精度83%（RPi5実測）
- **API**: OpenAI互換 `/v1/chat/completions` エンドポイント (port 3001)
- **tool calling対応**: get_sensors / get_status / set_relay の3ツール
- **レイテンシ**: 約18秒/リクエスト（RPi5 CPU推論）
- **メモリ**: 約2GB（OS含め全体で2.7GB / 8GB中）

```
APIキーなし (デフォルト)     APIキーあり
      │                          │
      ▼                          ▼
  llama-server直行         クラウドAPI試行
  (localhost:3001)              │
                          成功? ─┬─ Yes → クラウド応答
                                └─ No  → llama-serverフォールバック
```

### モデル選定ベンチマーク（2026-03-18実測）

| 環境 | モデル | 量子化 | ツールコール精度 | 平均レイテンシ |
|------|--------|--------|:---------------:|:-------------:|
| MBP M4 Pro | Qwen3.5-2B | FP16 | 100% (12/12) | 1.5s |
| RPi5 8GB | Qwen3.5-2B | Q8_0 | 83% (10/12) | 18.0s |
| RPi5 8GB | Qwen3.5-2B | Q4_K_M | 67% (8/12) | 12.7s |

## LINE Bot

RPi上のagriha-ui内で直接稼働（VPS不要）。Cloudflare TunnelまたはNginx+Let's Encryptで外部公開。

- LLMプロバイダーにはllama-serverを使用（tool calling対応）
- get_sensors / get_status / set_relay のtool callingが利用可能

## ディレクトリ構成

| パッケージ | 場所 | 説明 |
|-----------|------|------|
| `agriha.control` | `src/agriha/control/` | 三層制御（emergency_guard, rule_engine, forecast_engine, plan_executor） |
| `agriha.daemon` | `src/agriha/daemon/` | ハードウェアデーモン（I2C, GPIO, MQTT, REST API） |
| `agriha.chat` | `src/agriha/chat/` | ローカルWebUI（ダッシュボード+設定画面+LINE Bot） |
| `config` | `config/` | 設定テンプレート（rules.yaml, channel_map.yaml, forecast.yaml等） |

## セットアップ

### 前提条件

- Raspberry Pi 5 (8GB推奨)
- Raspberry Pi OS Bookworm (64-bit)
- Python 3.11+
- cmake, git

### 手順

```bash
# 1. llama.cpp ビルド + モデルダウンロード
git clone https://github.com/ggml-org/llama.cpp /tmp/llama.cpp
cd /tmp/llama.cpp && cmake -B build && cmake --build build -j4
sudo mkdir -p /opt/agriha/bin /opt/agriha/lib /opt/agriha/models
sudo cp build/bin/llama-server /opt/agriha/bin/
sudo cp build/bin/lib*.so* /opt/agriha/lib/
echo '/opt/agriha/lib' | sudo tee /etc/ld.so.conf.d/agriha.conf && sudo ldconfig

# モデルダウンロード (Q8_0推奨: 精度優先, Q4_K_Mはメモリ節約)
wget -O /opt/agriha/models/qwen3.5-2b-q8_0.gguf \
  "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q8_0.gguf"

# 2. llama-server systemdサービス作成
sudo tee /etc/systemd/system/llama-server.service << 'EOF'
[Unit]
Description=llama.cpp Inference Server for AgriHA
After=network.target

[Service]
Type=simple
User=yasu
ExecStart=/opt/agriha/bin/llama-server \
    -m /opt/agriha/models/qwen3.5-2b-q8_0.gguf \
    --host 127.0.0.1 --port 3001 \
    --jinja --flash-attn auto -ngl 0 -t 4 -c 4096
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now llama-server

# 3. Mosquitto (MQTT broker)
sudo apt install -y mosquitto mosquitto-clients

# 4. AgriHA セットアップ
git clone https://github.com/yasunorioi/uecs-llm.git ~/uecs-llm
cd ~/uecs-llm && git checkout v4
sudo bash setup.sh

# 5. I2C / 1-Wire 有効化
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_onewire 0

# 6. 起動
sudo systemctl start llama-server unipi-daemon agriha-ui
```

`sudo bash setup.sh` で以下が自動完了:
- Python venv作成 + パッケージインストール
- `/etc/agriha/` に設定ファイル配置
- `/var/lib/agriha/`, `/var/log/agriha/` ディレクトリ作成
- agriha システムユーザー作成 + 所有権設定
- systemd サービス有効化（unipi-daemon, agriha-ui）
- 三層制御 cron 設定
- USB SIMモデム検出時: ModemManager + NetworkManager APN自動設定

### DS18B20 温度センサー設定（HW接続後）

```bash
# デバイスID確認
ls /sys/bus/w1/devices/
# → 28-xxxxxxxxxxxx 形式のIDが表示される

# 設定ファイルに追記
sudo nano /etc/agriha/unipi_daemon.yaml
# onewire:
#   devices:
#     - "28-xxxxxxxxxxxx"

sudo systemctl restart unipi-daemon
```

### 動作確認

```bash
# 全サービスの状態確認
for svc in llama-server unipi-daemon agriha-ui mosquitto; do
  echo "$svc: $(systemctl is-active $svc)"
done

# LLM API確認
curl -s http://localhost:3001/v1/models | python3 -m json.tool

# Web UI確認 (デフォルト認証: admin/agriha)
curl -s -u admin:agriha http://localhost:8501/

# メモリ確認
free -h
```

## Web UI ログイン

| 項目 | デフォルト値 | 環境変数 |
|------|:----------:|----------|
| ユーザー名 | `admin` | `UI_AUTH_USER` |
| パスワード | `agriha` | `UI_AUTH_PASS` |

HTTP Basic認証。変更は `/opt/agriha/.env` に記載するか、systemd の `Environment=` で指定。

```bash
# .env で変更する例
echo 'UI_AUTH_USER=myuser' >> /opt/agriha/.env
echo 'UI_AUTH_PASS=mypassword' >> /opt/agriha/.env
sudo systemctl restart agriha-ui
```

## systemd サービス一覧

| サービス | ポート | 説明 |
|---------|:------:|------|
| `llama-server.service` | 3001 | Qwen3.5-2B ローカルLLM推論サーバー |
| `unipi-daemon.service` | 8080 | センサー・リレー・MQTT・REST APIデーモン |
| `agriha-ui.service` | 8501 | Web UI + LINE Bot (FastAPI + htmx) |
| `mosquitto.service` | 1883 | MQTT broker |

```bash
# ログ確認
journalctl -u llama-server -f
journalctl -u unipi-daemon -f
```

## cron (三層制御)

| ジョブ | 間隔 | 説明 |
|--------|------|------|
| emergency_guard.sh | 毎分 | Layer 1: 緊急制御 |
| rule_engine.py | 10分毎 | Layer 2: ルールベース制御 |
| forecast_engine.py | 毎時 | Layer 3: LLM予報制御 |
| plan_executor.py | 10分毎 | Layer 3: 計画実行 |
| distiller.py | 週次(月曜03:00) | ルール候補蒸留 |
| reflection.py | 週次(月曜07:00) | LINE反省会 |

## Web UI 設定画面

`http://<RPi-IP>:8501/settings` から以下を編集可能:

- **LLMプロバイダー**: llama-server / Anthropic(Claude) / OpenAI / Gemini / Ollama 選択
- **APIキー設定**: llama-server以外を選択した場合のみ必要
- **システムプロンプト**: Layer 3 LLMへの指示文
- **緊急閾値**: Layer 1 の高温/低温閾値
- **制御ルール**: Layer 2 の rules.yaml（YAML直接編集+構文チェック）
- **チャンネルマップ**: リレーチャンネル割当（channel_map.yaml）

## LLMプロバイダー切替

forecast_engine.py は OpenAI SDK 互換クライアントを使用。
設定画面のプルダウンから選択するか、`/etc/agriha/forecast.yaml` を直接編集。

| プロバイダー | base_url | APIキー | 備考 |
|-------------|----------|---------|------|
| llama-server (デフォルト) | `http://localhost:3001/v1/` | 不要 | オフライン・無料・tool calling可 |
| Anthropic (Claude) | `https://api.anthropic.com/v1/` | 必要 | 高精度・tool calling可 |
| OpenAI (GPT) | `https://api.openai.com/v1/` | 必要 | tool calling可 |
| Google (Gemini) | `https://generativelanguage.googleapis.com/v1beta/openai/` | 必要 | tool calling可 |
| Ollama (ローカル) | `http://localhost:11434/v1/` | 不要 | 要Ollama別途インストール |

## メモリ使用量 (RPi5 8GB)

| コンポーネント | 使用量 |
|---------------|--------|
| OS + systemd | ~0.5 GB |
| llama-server + Qwen3.5-2B Q8_0 | ~2.0 GB |
| unipi-daemon + Mosquitto | ~0.2 GB |
| agriha-ui (FastAPI) | ~0.1 GB |
| **合計** | **~2.7 GB** |
| **空き** | **~5.1 GB** |

## USB SIM / APN設定

固定回線のない圃場向けに、USB SIMモデムによるモバイル回線接続をサポート。

- setup.sh実行時にUSBモデムを自動検出し、ModemManager + NetworkManagerを設定
- APN設定はプリセットから選択: **SORACOM** (デフォルト) / IIJmio / 手動入力

## テスト

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
