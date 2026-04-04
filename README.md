# AgriHA (uecs-llm)

LLM による温室環境制御システム — 三層自律制御アーキテクチャ (v5移行中)

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
│  ┌── Layer 3: LLMルールコンパイラ (v5) ─────────────┐  │
│  │ rule_compiler.py (週次レビュー 月曜07:00)          │  │
│  │ NullClaw Proxy (port 3001): オフラインLLM推論      │  │
│  │ → ルール候補レビュー・rules.yaml自動更新           │  │
│  │ ※ forecast_engine / plan_executor は v4 legacy    │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  NullClaw Proxy (port 3001): オフラインLLM推論          │
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

> **v5移行状況**: Layer 3はリアルタイム予報制御(forecast_engine, v4)から週次ルールコンパイル(rule_compiler, v5)に移行中。
> forecast_engine.py と plan_executor.py は v4 legacy として残存しているが、cronでの定期実行は停止済み。

## 設計原則

- **下層が上層を黙らせる**: Layer 1ロックアウト中はLayer 2/3は動作しない
- **各層独立動作**: 上層が死んでも下層の安全機構は生きる
- **RPi単一構成**: 全てRaspberry Pi上で完結（VPS不要・クラウドAPI不要）
- **オフラインファースト**: NullClaw Proxy でローカル完結。通信費ゼロ
- **HW不在でも起動**: I2Cデバイス未接続時はNullRelay（no-op）で全サービス起動継続
- **マルチLLM対応**: OpenAI SDK互換でClaude/GPT/Gemini/Ollama/NullClawを切替可能

## NullClaw（デフォルトLLMプロバイダー）

v5ではNullClaw Proxy (port 3001) をデフォルトLLMとして採用。
NullClaw CLIをOpenAI SDK互換エンドポイントとして公開するプロキシサーバー。

- **API**: OpenAI互換 `/v1/chat/completions` エンドポイント (port 3001)
- **tool calling**: 非対応（センサーデータをプロンプトに事前埋め込みで代替）
- **実装**: `src/agriha/control/nullclaw_proxy.py`
- **systemdサービス**: `agriha-nullclaw-proxy.service`

```
APIキーなし (デフォルト)     APIキーあり
      │                          │
      ▼                          ▼
  NullClaw直行              クラウドAPI試行
  (localhost:3001)               │
                           成功? ─┬─ Yes → クラウド応答
                                 └─ No  → NullClawフォールバック
```

## LINE Bot

RPi上のagriha-ui内で直接稼働（VPS不要）。Cloudflare TunnelまたはNginx+Let's Encryptで外部公開。

- LLMプロバイダーにはNullClaw Proxyを使用（tool calling非対応のためプロンプト埋め込み方式）

## ディレクトリ構成

| パッケージ | 場所 | 説明 |
|-----------|------|------|
| `agriha.control` | `src/agriha/control/` | 三層制御（emergency_guard, rule_engine, rule_compiler, forecast_engine[v4], plan_executor[v4]） |
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
# 1. Mosquitto (MQTT broker)
sudo apt install -y mosquitto mosquitto-clients

# 2. AgriHA セットアップ
git clone https://github.com/yasunorioi/uecs-llm.git ~/uecs-llm
cd ~/uecs-llm && git checkout v5
sudo bash setup.sh

# 3. I2C / 1-Wire 有効化
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_onewire 0

# 4. WireGuard VPN設定（setup.shでテンプレート配置済み）
sudo nano /etc/wireguard/wg0.conf   # キー・IP設定を編集
sudo systemctl enable --now wg-quick@wg0

# 5. 起動
sudo systemctl start agriha-nullclaw-proxy unipi-daemon agriha-ui
```

`sudo bash setup.sh` で以下が自動完了:
- Python venv作成 + パッケージインストール
- `/etc/agriha/` に設定ファイル配置
- `/var/lib/agriha/`, `/var/log/agriha/` ディレクトリ作成
- agriha システムユーザー作成 + 所有権設定
- systemd サービス有効化（unipi-daemon, agriha-ui, agriha-nullclaw-proxy）
- 三層制御 cron 設定
- WireGuard VPNインストール + テンプレート配置
- Nginx設定配置（LINE Bot外部公開用）

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
for svc in agriha-nullclaw-proxy unipi-daemon agriha-ui mosquitto; do
  echo "$svc: $(systemctl is-active $svc)"
done

# NullClaw API確認
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
| `agriha-nullclaw-proxy.service` | 3001 | NullClaw OpenAI互換プロキシ |
| `unipi-daemon.service` | 8080 | センサー・リレー・MQTT・REST APIデーモン |
| `agriha-ui.service` | 8501 | Web UI + LINE Bot (FastAPI + htmx) |
| `mosquitto.service` | 1883 | MQTT broker |

```bash
# ログ確認
journalctl -u agriha-nullclaw-proxy -f
journalctl -u unipi-daemon -f
```

## cron (三層制御)

| ジョブ | 間隔 | 説明 |
|--------|------|------|
| emergency_guard.sh | 毎分 | Layer 1: 緊急制御 |
| rule_engine.py | 10分毎 | Layer 2: ルールベース制御 |
| tide_forecaster.py | 10分毎 | TiDE予測更新 (InAirTemp/InAirHumid/InAirCO2 6h先行予測) |
| rule_compiler.py review | 週次 月曜07:00 | Layer 3 (v5): ルール候補レビュー・自動更新 |
| distiller.py | 週次 月曜03:00 | ルール候補蒸留 |
| reflection.py | 週次 月曜07:00 | LINE反省会（⚠ ファイル削除済み・要修復） |
| forecast_engine.py | — | v4 legacy、コメントアウト（無効） |
| plan_executor.py | — | v4 legacy、コメントアウト（無効） |

## Web UI 設定画面

`http://<RPi-IP>:8501/settings` から以下を編集可能:

- **LLMプロバイダー**: NullClaw / Anthropic(Claude) / OpenAI / Gemini / Ollama 選択
- **APIキー設定**: NullClaw以外を選択した場合のみ必要
- **システムプロンプト**: Layer 3 LLMへの指示文
- **緊急閾値**: Layer 1 の高温/低温閾値
- **制御ルール**: Layer 2 の rules.yaml（YAML直接編集+構文チェック）
- **チャンネルマップ**: リレーチャンネル割当（channel_map.yaml）

## LLMプロバイダー切替

forecast_engine.py は OpenAI SDK 互換クライアントを使用。
設定画面のプルダウンから選択するか、`/etc/agriha/forecast.yaml` を直接編集。

| プロバイダー | base_url | APIキー | 備考 |
|-------------|----------|---------|------|
| NullClaw (デフォルト) | `http://localhost:3001/v1/` | 不要 | オフライン・無料・tool calling不可 |
| Anthropic (Claude) | `https://api.anthropic.com/v1/` | 必要 | 高精度・tool calling可 |
| OpenAI (GPT) | `https://api.openai.com/v1/` | 必要 | tool calling可 |
| Google (Gemini) | `https://generativelanguage.googleapis.com/v1beta/openai/` | 必要 | tool calling可 |
| Ollama (ローカル) | `http://localhost:11434/v1/` | 不要 | 要Ollama別途インストール |

## 設定ファイル

`/etc/agriha/` に配置される設定ファイル一覧:

| ファイル | 説明 |
|---------|------|
| `rules.yaml` | Layer 2 制御ルール |
| `channel_map.yaml` | リレーチャンネル割当 |
| `forecast.yaml` | LLMプロバイダー設定 |
| `thresholds.yaml` | 緊急閾値 |
| `emergency.conf` | Layer 1 緊急制御設定 |
| `agri_knowledge.yaml` | 農学知識ベース（rule_compiler用） |
| `crop_irrigation.yaml` | 作物別灌水設定 |
| `rule_candidates.yaml` | 蒸留ルール候補 |
| `system_prompt.txt` | LLMシステムプロンプト |
| `unipi_daemon.example.yaml` | デーモン設定テンプレート |
| `nginx.conf` | Nginx設定テンプレート |
| `wg0.conf.template` | WireGuard VPN設定テンプレート |

## スクリプト

| スクリプト | 説明 |
|-----------|------|
| `scripts/agriha-capture.sh` | 定時カメラキャプチャ（cron 5分） |
| `scripts/agriha-growth-archive.sh` | 生育ログアーカイブ |
| `scripts/setup-camera.sh` | カメラHWセットアップ |
| `scripts/start-tmux.sh` | 開発用tmuxセッション起動 |

## USB SIM / APN設定

固定回線のない圃場向けに、USB SIMモデムによるモバイル回線接続をサポート。

- setup.sh実行時にUSBモデムを自動検出し、ModemManager + NetworkManagerを設定
- APN設定はプリセットから選択: **SORACOM** (デフォルト) / IIJmio / 手動入力

## テスト

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## 依存インストール

```bash
# 通常（daemon + TiDE予測）
pip install -e ".[daemon,tide]"

# RPi4/5 (aarch64): tflite-runtime が自動選択される
# x86_64 開発環境: tensorflow にフォールバック

# TiDE関連（numpy + tflite-runtime/tensorflow）
pip install -e ".[tide]"
```

> **RPi4補足**: `tflite-runtime` は `tensorflow` 全体より軽量（~50MB vs ~500MB）。
> setup.sh は自動で `[daemon,tide]` をインストールする。

## ライセンス

MIT
