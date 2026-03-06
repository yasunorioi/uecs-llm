# AgriHA (uecs-llm)

LLM による温室環境制御システム — 三層自律制御アーキテクチャ (v4)

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│ Raspberry Pi (AgriHA) — 全制御をオンボード実行           │
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
│  │ NullClaw(デフォルト) or クラウドLLM API            │  │
│  │ → 天気予報+1時間制御計画生成                       │  │
│  │ plan_executor.py (cron 10分) → 計画実行            │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  NullClaw Proxy (port 3001): オフラインLLM推論          │
│  unipi-daemon: センサー・リレー・MQTT・REST API         │
│  agriha-ui: ローカルWebUI (FastAPI+htmx, ポート8501)   │
│  LINE Bot: RPi上で直接稼働 (agriha-ui内)               │
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
- **RPi単一構成**: 全てRaspberry Pi上で完結（VPS不要）
- **オフラインファースト**: デフォルトはNullClaw（ローカルLLM）、API不要・無料
- **マルチLLM対応**: OpenAI SDK互換でClaude/GPT/Gemini/NullClaw/Ollamaを切替可能

## NullClaw（デフォルトLLMプロバイダー）

v4ではNullClawをデフォルトLLMとして採用。APIキー不要・オフライン動作・無料。

- **nullclaw_proxy.py** (port 3001): OpenAI API互換プロキシ。NullClaw CLIをラップし `/v1/chat/completions` エンドポイントを提供
- **NullClawFallbackClient**: forecast_engine.py内のクライアント。APIキーあり→クラウドAPI優先→失敗時NullClawフォールバック。APIキーなし→NullClaw直行
- **制約**: NullClawはtool calling非対応。センサーデータはプロンプト埋め込みで代替

```
APIキーなし (デフォルト)     APIキーあり
      │                          │
      ▼                          ▼
  NullClaw直行           クラウドAPI試行
  (localhost:3001)              │
                          成功? ─┬─ Yes → クラウド応答
                                └─ No  → NullClawフォールバック
```

## LINE Bot

RPi上のagriha-ui内で直接稼働（VPS不要）。Cloudflare TunnelまたはNginx+Let's Encryptで外部公開。

- LLMプロバイダーにはNullClawFallbackClientを使用（設定画面の選択に従う）
- NullClaw使用時はtool calling不可のため、リレー制御（set_relay）は利用不可
- クラウドLLM使用時はget_sensors / get_status / set_relay のtool callingが利用可能

## ディレクトリ構成

| パッケージ | 場所 | 説明 |
|-----------|------|------|
| `agriha.control` | `src/agriha/control/` | 三層制御（emergency_guard, rule_engine, forecast_engine, plan_executor） |
| `agriha.control` | `src/agriha/control/nullclaw_proxy.py` | NullClaw OpenAI互換プロキシ (port 3001) |
| `agriha.daemon` | `src/agriha/daemon/` | ハードウェアデーモン（I2C, GPIO, MQTT, REST API） |
| `agriha.chat` | `src/agriha/chat/` | ローカルWebUI（ダッシュボード+設定画面+LINE Bot） |
| `config` | `config/` | 設定テンプレート（rules.yaml, channel_map.yaml, forecast.yaml等） |

## セットアップ

```bash
# 1. clone
sudo git clone https://github.com/yasunorioi/uecs-llm.git /opt/agriha
cd /opt/agriha && git checkout v4

# 2. セットアップ（ユーザー作成+venv+systemd+NullClaw全部入り）
sudo bash setup.sh

# 3. (オプション) クラウドLLMを使う場合のみAPIキー設定
sudo nano /opt/agriha/.env

# 4. 起動
sudo systemctl start unipi-daemon agriha-ui agriha-nullclaw-proxy
```

`sudo bash setup.sh` で以下が完了する:
- Python venv作成 + パッケージインストール
- NullClaw CLIインストール + agriha-nullclaw-proxy.service 有効化
- `/etc/agriha/` に設定ファイル配置
- `/var/lib/agriha/`, `/var/log/agriha/` ディレクトリ作成
- agriha システムユーザー作成 + 所有権設定
- systemd サービス有効化（unipi-daemon, agriha-ui, agriha-nullclaw-proxy）
- 三層制御 cron 設定
- Nginx + Let's Encrypt HTTPS設定（LINE Bot外部公開用）
- USB SIMモデム検出時: ModemManager + NetworkManager APN自動設定

## systemd サービス一覧

| サービス | 説明 |
|---------|------|
| `unipi-daemon.service` | センサー・リレー・MQTT・REST APIデーモン |
| `agriha-ui.service` | Web UI + LINE Bot (FastAPI, ポート8501) |
| `agriha-nullclaw-proxy.service` | NullClaw OpenAI互換プロキシ (ポート3001) |
| `ModemManager.service` | USB SIMモデム管理（SIM使用時） |
| `certbot.timer` | Let's Encrypt証明書自動更新 |

## Web UI 設定画面

`http://<RPi-IP>:8501/settings` から以下を編集可能:

- **LLMプロバイダー**: プルダウンで NullClaw / Anthropic(Claude) / OpenAI / Gemini / Ollama 選択
- **APIキー設定**: NullClaw以外を選択した場合のみ必要
- **システムプロンプト**: Layer 3 LLMへの指示文
- **緊急閾値**: Layer 1 の高温/低温閾値
- **制御ルール**: Layer 2 の rules.yaml（YAML直接編集+構文チェック）
- **チャンネルマップ**: リレーチャンネル割当（channel_map.yaml）
- **LLM詳細設定**: forecast.yaml 直接編集（上級者向け）

## LLMプロバイダー切替

forecast_engine.py は OpenAI SDK 互換クライアント（NullClawFallbackClient）を使用。
設定画面のプルダウンから選択するか、forecast.yaml を直接編集。

| プロバイダー | base_url | APIキー | 備考 |
|-------------|----------|---------|------|
| NullClaw (デフォルト) | `http://localhost:3001/v1/` | 不要 | オフライン・無料・tool calling不可 |
| Anthropic (Claude) | `https://api.anthropic.com/v1/` | 必要 | 高精度・tool calling可 |
| OpenAI (GPT) | `https://api.openai.com/v1/` | 必要 | tool calling可 |
| Google (Gemini) | `https://generativelanguage.googleapis.com/v1beta/openai/` | 必要 | tool calling可 |
| Ollama (ローカル) | `http://localhost:11434/v1/` | 不要 | 要Ollama別途インストール |

## USB SIM / APN設定

固定回線のない圃場向けに、USB SIMモデムによるモバイル回線接続をサポート。

- setup.sh実行時にUSBモデムを自動検出し、ModemManager + NetworkManagerを設定
- APN設定はプリセットから選択: **SORACOM** (デフォルト) / IIJmio / 手動入力
- ArSprout既存USB SIM環境との互換性あり（既存設定を検出した場合は上書きしない）

## テスト

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
