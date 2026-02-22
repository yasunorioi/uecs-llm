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

### 3. LINE Bot（VPS、オプション）

```bash
cd uecs-llm/linebot
cp .env.example .env
# .env に LINE の認証情報を記入
docker compose up -d
```

### 4. SD カードイメージ（ゼロから構築）

```bash
cd uecs-llm/image
sudo ./build_image.sh raspios-bookworm-arm64-lite.img
# SD カードに書き込んで起動
```

## コンポーネント

| コンポーネント | 場所 | 対象環境 | 説明 |
|---------------|------|---------|------|
| `uecs_llm` | `src/uecs_llm/` | x86 / Pi5 | LLM 制御ループ (agriha_control.py) |
| `unipi_daemon` | `src/unipi_daemon/` | RPi | ハードウェアデーモン (I2C, GPIO, MQTT, REST) |
| `linebot` | `linebot/` | VPS | LINE Bot（Ollama 連携） |
| `image` | `image/` | ビルドホスト | Raspbian カスタムイメージビルダー |
| `config` | `config/` | — | 設定テンプレート |
| `systemd` | `systemd/` | — | サービスファイル・cron |

## テスト

```bash
make test
# または
pip install -e ".[dev]"
pytest tests/ -v
```

## ライセンス

MIT
