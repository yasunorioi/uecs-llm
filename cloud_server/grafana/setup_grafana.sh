#!/bin/bash
#
# Grafana OSS 自動セットアップスクリプト
# Agricultural Facility 自前クラウド基盤
#
# Target: Ubuntu 22.04/24.04 LTS
# Author: Ashigaru-2
# Date: 2026-02-07
#

set -euo pipefail

# ログ出力関数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
    exit 1
}

# root権限確認
if [ "$EUID" -ne 0 ]; then
    error "このスクリプトは root 権限で実行してください: sudo $0"
fi

log "=== Grafana OSS セットアップ開始 ==="

# 1. Grafana公式リポジトリを追加
log "1. Grafana公式リポジトリを追加中..."

if [ ! -f /etc/apt/keyrings/grafana.gpg ]; then
    log "  - GPGキーを追加中..."
    mkdir -p /etc/apt/keyrings/
    wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | tee /etc/apt/keyrings/grafana.gpg > /dev/null
    log "  ✓ GPGキー追加完了"
else
    log "  ✓ GPGキーは既に存在します"
fi

if [ ! -f /etc/apt/sources.list.d/grafana.list ]; then
    log "  - aptリポジトリを追加中..."
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
    log "  ✓ aptリポジトリ追加完了"
else
    log "  ✓ aptリポジトリは既に存在します"
fi

# 2. Grafana をインストール
log "2. Grafana をインストール中..."
apt-get update -qq
if ! dpkg -l | grep -q "^ii  grafana "; then
    apt-get install -y grafana
    log "  ✓ Grafana インストール完了"
else
    log "  ✓ Grafana は既にインストールされています"
fi

# 3. systemd サービスを有効化・起動
log "3. Grafana サービスを有効化・起動中..."
systemctl daemon-reload

if ! systemctl is-enabled grafana-server > /dev/null 2>&1; then
    systemctl enable grafana-server
    log "  ✓ grafana-server を有効化しました"
else
    log "  ✓ grafana-server は既に有効です"
fi

if ! systemctl is-active grafana-server > /dev/null 2>&1; then
    systemctl start grafana-server
    log "  ✓ grafana-server を起動しました"
else
    log "  ✓ grafana-server は既に起動しています"
fi

# 起動確認（最大30秒待機）
log "  - 起動確認中..."
for i in {1..30}; do
    if systemctl is-active grafana-server > /dev/null 2>&1; then
        log "  ✓ grafana-server が起動しました"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "grafana-server の起動に失敗しました"
    fi
    sleep 1
done

# 4. プロビジョニング用ディレクトリ作成
log "4. プロビジョニング用ディレクトリを作成中..."
mkdir -p /etc/grafana/provisioning/datasources
mkdir -p /etc/grafana/provisioning/dashboards
mkdir -p /var/lib/grafana/dashboards/agri-ha
log "  ✓ ディレクトリ作成完了"

# 5. InfluxDB データソースプロビジョニングYAML を配置
log "5. InfluxDB データソースプロビジョニングYAML を配置中..."

DATASOURCE_YAML="/etc/grafana/provisioning/datasources/influxdb.yaml"

if [ ! -f "$DATASOURCE_YAML" ]; then
    cat > "$DATASOURCE_YAML" <<'EOF'
apiVersion: 1

datasources:
  - name: InfluxDB-Agricultural Facility
    type: influxdb
    access: proxy
    url: http://localhost:8086
    jsonData:
      version: Flux
      organization: agri-ha
      defaultBucket: sensor_data
      tlsSkipVerify: true
    secureJsonData:
      token: <INFLUXDB_TOKEN>
    editable: true
EOF

    chown root:grafana "$DATASOURCE_YAML"
    chmod 640 "$DATASOURCE_YAML"
    log "  ✓ InfluxDB データソースYAML配置完了"
    log "  ⚠  注意: <INFLUXDB_TOKEN> を実際のトークンに置き換えてください"
else
    log "  ✓ InfluxDB データソースYAMLは既に存在します"
fi

# 6. ダッシュボードプロビジョニングYAML を配置
log "6. ダッシュボードプロビジョニングYAML を配置中..."

DASHBOARD_YAML="/etc/grafana/provisioning/dashboards/agri-ha.yaml"

if [ ! -f "$DASHBOARD_YAML" ]; then
    cat > "$DASHBOARD_YAML" <<'EOF'
apiVersion: 1

providers:
  - name: 'Agricultural Facility'
    orgId: 1
    folder: 'Agricultural Facility'
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards/agri-ha
EOF

    chown root:grafana "$DASHBOARD_YAML"
    chmod 640 "$DASHBOARD_YAML"
    log "  ✓ ダッシュボードプロビジョニングYAML配置完了"
else
    log "  ✓ ダッシュボードプロビジョニングYAMLは既に存在します"
fi

chown -R grafana:grafana /var/lib/grafana/dashboards/agri-ha

# 7. 設定反映のため Grafana を再起動
log "7. Grafana を再起動してプロビジョニング設定を反映中..."
systemctl restart grafana-server

# 再起動確認（最大30秒待機）
for i in {1..30}; do
    if systemctl is-active grafana-server > /dev/null 2>&1; then
        log "  ✓ Grafana 再起動完了"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "Grafana の再起動に失敗しました"
    fi
    sleep 1
done

# 8. セットアップ完了
log "=== Grafana OSS セットアップ完了 ==="
log ""
log "次のステップ:"
log "  1. InfluxDB トークンを取得:"
log "     influx auth list --json | jq -r '.[] | select(.description == \"agri-ha-token\") | .token'"
log ""
log "  2. データソース設定のトークンを置き換え:"
log "     sudo nano /etc/grafana/provisioning/datasources/influxdb.yaml"
log "     （<INFLUXDB_TOKEN> を実際のトークンに置き換える）"
log ""
log "  3. Grafana を再起動:"
log "     sudo systemctl restart grafana-server"
log ""
log "  4. Webブラウザでアクセス:"
log "     http://192.168.15.14:3000"
log "     （初回ログイン: admin / admin）"
log ""
log "  5. ダッシュボードJSONファイルを配置:"
log "     sudo cp cloud_server/grafana/dashboards/*.json /var/lib/grafana/dashboards/agri-ha/"
log "     sudo chown -R grafana:grafana /var/lib/grafana/dashboards/agri-ha/"
log "     sudo systemctl restart grafana-server"
log ""
