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
│  │ LLM API→天気予報+1時間制御計画生成                │  │
│  │ plan_executor.py (cron 10分) → 計画実行            │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  unipi-daemon: センサー・リレー・MQTT・REST API         │
│  agriha-ui: ローカルWebUI (FastAPI+htmx, ポート8501)   │
├─────────────────────────────────────────────────────────┤
│ UniPi 1.1 ハードウェア                                  │
│  MCP23008 リレー(8ch) + DS18B20 + GPIO DI + Misol RS485│
└─────────────────────────────────────────────────────────┘
```

## 設計原則

- **下層が上層を黙らせる**: Layer 1ロックアウト中はLayer 2/3は動作しない
- **各層独立動作**: 上層が死んでも下層の安全機構は生きる
- **RPi単一構成**: 全てRaspberry Pi上で完結
- **マルチLLM対応**: OpenAI SDK互換でClaude/GPT/Gemini/ローカルLLMを切替可能

## ディレクトリ構成

| パッケージ | 場所 | 説明 |
|-----------|------|------|
| `agriha.control` | `src/agriha/control/` | 三層制御（emergency_guard, rule_engine, forecast_engine, plan_executor） |
| `agriha.daemon` | `src/agriha/daemon/` | ハードウェアデーモン（I2C, GPIO, MQTT, REST API） |
| `agriha.chat` | `src/agriha/chat/` | ローカルWebUI（ダッシュボード+設定画面） |
| `config` | `config/` | 設定テンプレート（rules.yaml, channel_map.yaml, forecast.yaml等） |

## セットアップ

```bash
# 1. clone
sudo git clone https://github.com/yasunorioi/uecs-llm.git /opt/agriha
# 2. セットアップ（ユーザー作成+venv+systemd全部入り）
cd /opt/agriha && sudo bash setup.sh
# 3. API KEY設定
sudo nano /opt/agriha/.env
# 4. 起動
sudo systemctl start unipi-daemon agriha-ui
```

`sudo bash setup.sh` で以下が完了する:
- Python venv作成 + パッケージインストール
- `/etc/agriha/` に設定ファイル配置
- `/var/lib/agriha/`, `/var/log/agriha/` ディレクトリ作成
- agriha システムユーザー作成 + 所有権設定
- systemd サービス有効化（unipi-daemon, agriha-ui）
- 三層制御 cron 設定
- Nginx 設定（config/nginx.conf がある場合）

## Web UI 設定画面

`http://<RPi-IP>:8501/settings` から以下を編集可能:

- **システムプロンプト**: Layer 3 LLMへの指示文
- **緊急閾値**: Layer 1 の高温/低温閾値
- **制御ルール**: Layer 2 の rules.yaml（YAML直接編集+構文チェック）
- **チャンネルマップ**: リレーチャンネル割当（channel_map.yaml）
- **LLMプロバイダー**: プルダウンでClaude/OpenAI/Gemini/ローカルLLM選択+APIキー設定
- **LLM詳細設定**: forecast.yaml 直接編集（上級者向け）

## LLMプロバイダー切替

forecast_engine.py は OpenAI SDK 互換クライアントを使用。
`base_url` を変えるだけでプロバイダーを切替可能:

| プロバイダー | base_url |
|-------------|----------|
| Anthropic (Claude) | `https://api.anthropic.com/v1/` |
| OpenAI (GPT) | `https://api.openai.com/v1/` |
| Google (Gemini) | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| ローカルLLM (Ollama) | `http://localhost:11434/v1/` |

設定画面のプルダウンから選択するか、forecast.yaml を直接編集。

## テスト

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
