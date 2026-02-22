# uecs-llm

LLM-based greenhouse environment control with UECS-CCM integration.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ x86 Mini PC (nipogi.local)                              │
│  llama-server (LFM2.5 1.2B)                            │
│  agriha_control.py  ← cron 5min                        │
│    └→ REST API → unipi-daemon                          │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP / WireGuard VPN
┌───────────────────────▼─────────────────────────────────┐
│ Raspberry Pi (AgriHA OS)                                │
│  unipi-daemon                                          │
│    ├ sensor_loop   : DS18B20 + Misol WH65LP → MQTT     │
│    ├ ccm_receiver  : UECS-CCM multicast → MQTT         │
│    ├ mqtt_bridge   : MQTT ↔ MCP23008 I2C relay         │
│    ├ gpio_watch    : DI07-14 emergency override        │
│    └ rest_api      : FastAPI REST-MQTT converter       │
│  Mosquitto (MQTT broker)                               │
└───────────────────────┬─────────────────────────────────┘
                        │ I2C / GPIO / 1-Wire / RS485
┌───────────────────────▼─────────────────────────────────┐
│ UniPi 1.1 Hardware                                      │
│  MCP23008 relay (8ch) + DS18B20 + GPIO DI + Misol RS485│
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ VPS (optional)                                          │
│  LINE Bot → Ollama (via VPN)                            │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. LLM Control Server (x86 Mini PC)

```bash
git clone https://github.com/yasunorioi/uecs-llm.git
cd uecs-llm
make install-llm-server

# Download LFM2.5 model
# https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF

# Start llama-server
sudo cp systemd/agriha-llm.service /etc/systemd/system/
sudo systemctl enable --now agriha-llm

# Setup 5-min control loop
sudo cp systemd/agriha-control.cron /etc/cron.d/agriha-control
```

### 2. UniPi Daemon (Raspberry Pi)

```bash
cd uecs-llm
make install-pi-daemon

# Copy and edit config
sudo mkdir -p /etc/agriha
sudo cp config/unipi_daemon.example.yaml /etc/agriha/unipi_daemon.yaml

# Start MQTT broker
cd docker && docker compose up -d && cd ..

# Start daemon
sudo cp systemd/unipi-daemon.service /etc/systemd/system/
sudo systemctl enable --now unipi-daemon
```

### 3. LINE Bot (VPS, optional)

```bash
cd uecs-llm/linebot
cp .env.example .env
# Edit .env with your LINE credentials
docker compose up -d
```

### 4. SD Card Image (from scratch)

```bash
cd uecs-llm/image
sudo ./build_image.sh raspios-bookworm-arm64-lite.img
# Flash to SD card and boot
```

## Components

| Component | Location | Target | Description |
|-----------|----------|--------|-------------|
| `uecs_llm` | `src/uecs_llm/` | x86 / Pi5 | LLM control loop (agriha_control.py) |
| `unipi_daemon` | `src/unipi_daemon/` | RPi | Hardware daemon (I2C, GPIO, MQTT, REST) |
| `linebot` | `linebot/` | VPS | LINE Bot with Ollama integration |
| `image` | `image/` | Build host | Raspbian custom image builder |
| `config` | `config/` | — | Configuration templates |
| `systemd` | `systemd/` | — | Service files and cron |

## Tests

```bash
make test
# or
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
