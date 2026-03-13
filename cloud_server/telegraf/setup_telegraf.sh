#!/bin/bash
# =============================================================================
# Telegraf セットアップスクリプト
# =============================================================================
# Agricultural Facility  → MQTT → InfluxDB データブリッジ構築
#
# 実行方法:
#   chmod +x setup_telegraf.sh
#   sudo ./setup_telegraf.sh
#
# 必要な情報:
#   - InfluxDB トークン（スクリプト実行時に入力）
#
# 前提条件:
#   - Mosquitto (localhost:1883) が稼働中
#   - InfluxDB v2 (localhost:8086) が稼働中
#   - Organization: agri-ha
#   - Bucket: sensor_data
# =============================================================================

set -e  # エラー時に終了

# 色付きログ出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# root 権限チェック
if [ "$EUID" -ne 0 ]; then
    log_error "このスクリプトは root 権限で実行してください (sudo ./setup_telegraf.sh)"
    exit 1
fi

# =============================================================================
# 前提条件チェック
# =============================================================================
log_info "前提条件をチェック中..."

# Mosquitto 確認
if ! systemctl is-active --quiet mosquitto; then
    log_error "Mosquitto が起動していません: sudo systemctl start mosquitto"
    exit 1
fi
log_info "✓ Mosquitto 起動中"

# InfluxDB 確認
if ! systemctl is-active --quiet influxdb; then
    log_error "InfluxDB が起動していません: sudo systemctl start influxdb"
    exit 1
fi
log_info "✓ InfluxDB 起動中"

# =============================================================================
# Telegraf インストール
# =============================================================================
log_info "Telegraf をインストール中..."

# 既にインストール済みか確認
if command -v telegraf &> /dev/null; then
    log_warn "Telegraf は既にインストール済みです ($(telegraf --version | head -1))"
else
    log_info "InfluxData リポジトリを追加中..."

    # GPG キーダウンロード
    wget -q https://repos.influxdata.com/influxdata-archive_compat.key -O /tmp/influxdata-archive_compat.key

    # GPG キー検証
    echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c /tmp/influxdata-archive_compat.key' | sha256sum -c

    # GPG キー追加
    cat /tmp/influxdata-archive_compat.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null

    # リポジトリ追加
    echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list

    # インストール
    log_info "apt update 実行中..."
    apt update

    log_info "Telegraf インストール中..."
    apt install -y telegraf

    log_info "✓ Telegraf インストール完了 ($(telegraf --version | head -1))"
fi

# =============================================================================
# 設定ファイル配置
# =============================================================================
log_info "設定ファイルを配置中..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_SOURCE="$SCRIPT_DIR/telegraf_agri-ha.conf"
CONFIG_DEST="/etc/telegraf/telegraf.d/agri-ha.conf"

if [ ! -f "$CONFIG_SOURCE" ]; then
    log_error "設定ファイルが見つかりません: $CONFIG_SOURCE"
    exit 1
fi

# 設定ディレクトリ作成
mkdir -p /etc/telegraf/telegraf.d

# 設定ファイルコピー
cp "$CONFIG_SOURCE" "$CONFIG_DEST"
chown root:root "$CONFIG_DEST"
chmod 640 "$CONFIG_DEST"

log_info "✓ 設定ファイル配置完了: $CONFIG_DEST"

# デフォルト設定の無効化（オプション）
if [ -f /etc/telegraf/telegraf.conf ]; then
    log_warn "デフォルト設定 (/etc/telegraf/telegraf.conf) が存在します"
    read -p "無効化しますか？ (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        mv /etc/telegraf/telegraf.conf /etc/telegraf/telegraf.conf.bak
        log_info "✓ デフォルト設定を無効化しました (.bak にリネーム)"
    fi
fi

# =============================================================================
# InfluxDB トークン設定
# =============================================================================
log_info "InfluxDB トークンを設定中..."

ENV_DIR="/etc/telegraf/env"
ENV_FILE="$ENV_DIR/influx.env"

mkdir -p "$ENV_DIR"

# トークン入力
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  InfluxDB トークンの取得方法:"
echo "  1. http://localhost:8086 にアクセス"
echo "  2. 左メニュー「API Tokens」をクリック"
echo "  3. トークンをコピー"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

read -p "InfluxDB トークンを入力してください: " INFLUX_TOKEN

if [ -z "$INFLUX_TOKEN" ]; then
    log_error "トークンが入力されていません"
    exit 1
fi

# 環境変数ファイル作成
echo "INFLUX_TOKEN=\"$INFLUX_TOKEN\"" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

log_info "✓ トークン設定完了: $ENV_FILE"

# systemd 環境変数読み込み設定
log_info "systemd 環境変数読み込み設定中..."

OVERRIDE_DIR="/etc/systemd/system/telegraf.service.d"
OVERRIDE_FILE="$OVERRIDE_DIR/influx-env.conf"

mkdir -p "$OVERRIDE_DIR"

cat > "$OVERRIDE_FILE" <<EOF
[Service]
EnvironmentFile=$ENV_FILE
EOF

systemctl daemon-reload

log_info "✓ systemd 環境変数設定完了"

# =============================================================================
# 設定テスト
# =============================================================================
log_info "設定ファイルをテスト中..."

# 環境変数読み込んでテスト
export $(cat "$ENV_FILE" | xargs)

if telegraf --config "$CONFIG_DEST" --test --input-filter mqtt_consumer 2>&1 | head -20; then
    log_info "✓ 設定ファイルテスト成功"
else
    log_error "設定ファイルにエラーがあります"
    exit 1
fi

# =============================================================================
# Telegraf 起動
# =============================================================================
log_info "Telegraf サービスを起動中..."

systemctl enable telegraf
systemctl restart telegraf

# 起動確認
sleep 3

if systemctl is-active --quiet telegraf; then
    log_info "✓ Telegraf 起動成功"
else
    log_error "Telegraf の起動に失敗しました"
    journalctl -u telegraf -n 20 --no-pager
    exit 1
fi

# =============================================================================
# 動作テスト
# =============================================================================
log_info "動作テストを実行中..."

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  テストデータを MQTT に送信します..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# mosquitto_pub が存在するか確認
if ! command -v mosquitto_pub &> /dev/null; then
    log_warn "mosquitto_pub が見つかりません。手動でテストしてください。"
else
    # テストデータ送信
    mosquitto_pub -h localhost -t "greenhouse/h1/sensor/temperature" -m '{
      "value": -9.2,
      "sensor_type": "temperature",
      "source": "192.168.1.71",
      "room": "1",
      "region": "1",
      "order": "1",
      "unit": "℃",
      "timestamp": "2026-02-07T12:00:00Z"
    }'

    log_info "✓ テストデータ送信完了"

    # ログ確認
    log_info "Telegraf ログを確認中（5秒待機）..."
    sleep 5

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    journalctl -u telegraf -n 10 --no-pager
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi

# =============================================================================
# セットアップ完了
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_info "✅ Telegraf セットアップ完了！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📋 次のステップ:"
echo "  1. InfluxDB でデータを確認:"
echo "     → http://localhost:8086"
echo "     → Data Explorer → Bucket: sensor_data → Measurement: greenhouse_data"
echo ""
echo "  2. リアルタイムログ監視:"
echo "     → sudo journalctl -u telegraf -f"
echo ""
echo "  3. 詳細な手順は SETUP.md を参照してください"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
