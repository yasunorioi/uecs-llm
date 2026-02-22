#!/bin/bash
#===============================================================================
# UniPi Agri HA 初回起動スクリプト
#
# このスクリプトは初回起動時に1度だけ実行される
#===============================================================================

set -e

LOG_FILE="/var/log/first-boot.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo " UniPi Agri HA - First Boot Setup"
echo " $(date)"
echo "=========================================="

SCRIPT_DIR="/opt/uecs-llm/scripts"
CONFIG_DIR="/opt/uecs-llm/config"
DOCKER_DIR="/opt/uecs-llm/docker"

#-------------------------------------------------------------------------------
# 1. システム更新
#-------------------------------------------------------------------------------
echo ""
echo "[1/6] Updating system..."
apt-get update
apt-get upgrade -y

#-------------------------------------------------------------------------------
# 2. Docker インストール
#-------------------------------------------------------------------------------
echo ""
echo "[2/6] Installing Docker..."
if ! command -v docker &> /dev/null; then
    bash "$SCRIPT_DIR/install_docker.sh"
else
    echo "Docker already installed"
fi

# operatorユーザーをdockerグループに追加
usermod -aG docker operator 2>/dev/null || true

#-------------------------------------------------------------------------------
# 3. EVOK インストール（UniPi 1.1対応）
#-------------------------------------------------------------------------------
echo ""
echo "[3/6] Installing EVOK..."
if ! systemctl is-active --quiet evok; then
    bash "$SCRIPT_DIR/install_evok.sh"
else
    echo "EVOK already installed"
fi

#-------------------------------------------------------------------------------
# 4. Docker Compose サービス起動
#-------------------------------------------------------------------------------
echo ""
echo "[4/6] Starting Docker services..."

cd "$DOCKER_DIR"

# 必要なディレクトリ作成
mkdir -p ha-config
mkdir -p nodered-data
mkdir -p mosquitto/config mosquitto/data mosquitto/log

# Node-RED設定ファイルをコピー
NODERED_TEMPLATE="/opt/uecs-llm/nodered"
if [ -f "$NODERED_TEMPLATE/settings.js" ]; then
    cp "$NODERED_TEMPLATE/settings.js" nodered-data/
fi
if [ -f "$NODERED_TEMPLATE/setup_flow.json" ]; then
    cp "$NODERED_TEMPLATE/setup_flow.json" nodered-data/flows.json
fi

# Mosquitto設定
cat > mosquitto/config/mosquitto.conf << 'MQTTCONF'
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
MQTTCONF

# Home Assistant基本設定
if [ ! -f ha-config/configuration.yaml ]; then
    cat > ha-config/configuration.yaml << 'HACONF'
# UniPi Agri HA Configuration
homeassistant:
  name: UniPi Agri
  unit_system: metric
  time_zone: Asia/Tokyo
  country: JP

# MQTT設定
mqtt:
  broker: mosquitto
  port: 1883

# ロガー
logger:
  default: info

# HTTP設定
http:
  server_port: 8123

# 履歴
recorder:
  db_url: sqlite:////config/home-assistant_v2.db
  purge_keep_days: 7

# RESTfulコマンド（EVOK連携用）
rest_command:
  evok_relay:
    url: "http://host.docker.internal/rest/relay/{{ relay_id }}"
    method: POST
    payload: '{"value": {{ value }}}'
    content_type: "application/json"
HACONF
fi

# Docker Compose起動
docker compose up -d

echo "Waiting for services to start..."
sleep 30

# サービス状態確認
docker compose ps

#-------------------------------------------------------------------------------
# 5. WireGuard設定（テンプレートがあれば）
#-------------------------------------------------------------------------------
echo ""
echo "[5/6] Configuring WireGuard..."

if [ -f "$CONFIG_DIR/wg0.conf" ]; then
    cp "$CONFIG_DIR/wg0.conf" /etc/wireguard/
    chmod 600 /etc/wireguard/wg0.conf
    systemctl enable wg-quick@wg0
    systemctl start wg-quick@wg0
    echo "WireGuard configured and started"
else
    echo "No WireGuard config found. Configure manually:"
    echo "  1. Copy wg0.conf to /etc/wireguard/"
    echo "  2. sudo systemctl enable --now wg-quick@wg0"
fi

#-------------------------------------------------------------------------------
# 6. バックアップcron設定
#-------------------------------------------------------------------------------
echo ""
echo "[6/6] Setting up backup cron..."

# 毎日3時にバックアップ
cat > /etc/cron.d/unipi-agri-backup << 'CRON'
# UniPi Agri HA - Daily backup
0 3 * * * root /opt/uecs-llm/scripts/backup_config.sh >> /var/log/backup.log 2>&1
CRON

chmod 644 /etc/cron.d/unipi-agri-backup

#-------------------------------------------------------------------------------
# 完了
#-------------------------------------------------------------------------------
echo ""
echo "=========================================="
echo " First Boot Setup Complete!"
echo " $(date)"
echo "=========================================="
echo ""
echo "Access URLs:"
echo "  Home Assistant: http://$(hostname -I | awk '{print $1}'):8123"
echo "  Node-RED:       http://$(hostname -I | awk '{print $1}'):1880"
echo "  EVOK API:       http://$(hostname -I | awk '{print $1}')/rest/"
echo ""
echo "Default credentials:"
echo "  SSH User: operator / changeme123"
echo ""
echo "Please change the password: passwd operator"
