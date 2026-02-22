#!/bin/bash
#===============================================================================
# 設定バックアップスクリプト
#
# 毎日cronで実行され、設定ファイルをクラウドへ同期
#===============================================================================

set -e

BACKUP_DIR="/opt/uecs-llm/backup"
DOCKER_DIR="/opt/uecs-llm/docker"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$TIMESTAMP.tar.gz"
REMOTE_BACKUP_DIR="/backup/$(hostname)"

# バックアップディレクトリ作成
mkdir -p "$BACKUP_DIR"

echo "$(date) - Starting backup..."

# バックアップ対象
BACKUP_TARGETS=(
    "/opt/uecs-llm/docker/ha-config"       # Home Assistant設定
    "/opt/uecs-llm/docker/nodered-data"    # Node-REDフロー
    "/opt/uecs-llm/docker/mosquitto/config" # Mosquitto設定
    "/etc/wireguard"                             # WireGuard設定
    "/etc/evok.conf"                             # EVOK設定
    "/opt/uecs-llm/config"                  # カスタム設定
)

# 存在するディレクトリのみ収集
EXISTING_TARGETS=()
for target in "${BACKUP_TARGETS[@]}"; do
    if [ -e "$target" ]; then
        EXISTING_TARGETS+=("$target")
    fi
done

# tarで圧縮
if [ ${#EXISTING_TARGETS[@]} -gt 0 ]; then
    tar -czf "$BACKUP_FILE" "${EXISTING_TARGETS[@]}" 2>/dev/null
    echo "Local backup created: $BACKUP_FILE"
else
    echo "No backup targets found"
    exit 0
fi

# 古いバックアップを削除（7日以上前）
find "$BACKUP_DIR" -name "backup_*.tar.gz" -mtime +7 -delete

# クラウドへ同期（WireGuard VPN経由）
if systemctl is-active --quiet wg-quick@wg0; then
    VPN_SERVER="10.10.0.1"

    # rsyncでクラウドへ転送（SSH鍵認証が必要）
    if command -v rsync &> /dev/null; then
        if ssh -o BatchMode=yes -o ConnectTimeout=5 "$VPN_SERVER" exit 2>/dev/null; then
            ssh "$VPN_SERVER" "mkdir -p $REMOTE_BACKUP_DIR"
            rsync -avz --delete "$BACKUP_DIR/" "$VPN_SERVER:$REMOTE_BACKUP_DIR/"
            echo "Synced to cloud: $VPN_SERVER:$REMOTE_BACKUP_DIR"
        else
            echo "Cannot connect to VPN server for backup sync"
        fi
    fi
else
    echo "WireGuard VPN not active, skipping cloud sync"
fi

echo "$(date) - Backup complete"
