#!/bin/bash
#===============================================================================
# EVOK インストールスクリプト（UniPi 1.1対応）
#
# EVOK: UniPi向けREST/WebSocket API
# https://github.com/UniPiTechnology/evok
#===============================================================================

set -e

echo "Installing EVOK for UniPi 1.1..."

EVOK_VERSION="2.4.15"
EVOK_DIR="/opt/evok"

# 依存パッケージ
apt-get update
apt-get install -y \
    python3-pip \
    python3-dev \
    python3-setuptools \
    python3-tornado \
    python3-toro \
    python3-jsonrpclib-pelix \
    python3-yaml \
    nginx \
    pigpio

# pigpioデーモン有効化
systemctl enable pigpiod
systemctl start pigpiod

# EVOKダウンロード
cd /tmp
if [ ! -d evok ]; then
    git clone https://github.com/UniPiTechnology/evok.git
fi
cd evok

# Python依存関係インストール
pip3 install --break-system-packages \
    tornado \
    toro \
    jsonrpclib-pelix \
    pymodbus \
    pyyaml \
    smbus2 \
    RPi.GPIO

# EVOKインストール
mkdir -p "$EVOK_DIR"
cp -r * "$EVOK_DIR/"

# UniPi 1.1用設定
cat > /etc/evok.conf << 'EVOKCONF'
[MAIN]
# UniPi 1.1 Configuration
log_level = WARNING
log_file = /var/log/evok.log
port = 80
webhook_enabled = False
webhook_address = http://localhost:1880/evok
webhook_device_mask = ["input","relay"]
webhook_complex_events = False
wifi_control_enabled = False

[I2C]
# I2C bus (UniPi 1.1 uses bus 1)
bus = 1

[EPROM]
i2cbus = 1
address = 0x50

[MCP23008]
# GPIO Expander (Relays)
i2cbus = 1
address = 0x20

[MCP9808]
# Temperature Sensor
i2cbus = 1
address = 0x18

[1WDEVICES]
# 1-Wire Devices (optional)
bus = 1
interval = 15

# Relay definitions (UniPi 1.1 has 8 relays)
[RELAY_1]
chip = mcp
pin = 7

[RELAY_2]
chip = mcp
pin = 6

[RELAY_3]
chip = mcp
pin = 5

[RELAY_4]
chip = mcp
pin = 4

[RELAY_5]
chip = mcp
pin = 3

[RELAY_6]
chip = mcp
pin = 2

[RELAY_7]
chip = mcp
pin = 1

[RELAY_8]
chip = mcp
pin = 0

# Digital Inputs (I1-I14)
[DI_1]
pin = 4
[DI_2]
pin = 17
[DI_3]
pin = 27
[DI_4]
pin = 22
[DI_5]
pin = 23
[DI_6]
pin = 24
[DI_7]
pin = 25
[DI_8]
pin = 5
[DI_9]
pin = 6
[DI_10]
pin = 12
[DI_11]
pin = 13
[DI_12]
pin = 16
[DI_13]
pin = 19
[DI_14]
pin = 26

# Analog Input (UniPi 1.1: 2ch 12-bit ADC)
[AI_1]
chip = mcp3422
channel = 0
[AI_2]
chip = mcp3422
channel = 1

# Analog Output (1ch 0-10V)
[AO_1]
chip = mcp4802
channel = 0
EVOKCONF

# systemdサービス作成
cat > /etc/systemd/system/evok.service << 'EVOKSVC'
[Unit]
Description=EVOK - UniPi API
After=network.target pigpiod.service
Requires=pigpiod.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/evok/evok.py
WorkingDirectory=/opt/evok
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EVOKSVC

# nginx設定（EVOKプロキシ）
cat > /etc/nginx/sites-available/evok << 'NGINXCONF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket support
    location /ws {
        proxy_pass http://127.0.0.1:8080/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
NGINXCONF

# nginx有効化
ln -sf /etc/nginx/sites-available/evok /etc/nginx/sites-enabled/evok
rm -f /etc/nginx/sites-enabled/default

# サービス有効化
systemctl daemon-reload
systemctl enable evok
systemctl start evok
systemctl restart nginx

echo "EVOK installed successfully"
echo "API available at: http://localhost/rest/"
echo ""
echo "Test commands:"
echo "  curl http://localhost/rest/all"
echo "  curl -X POST http://localhost/rest/relay/1 -d 'value=1'"
