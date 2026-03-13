#!/bin/bash
#===============================================================================
# wg_farmers_vps_setup.sh - VPS側 農家用WGサーバ初期設定スクリプト
#
# Usage: sudo bash wg_farmers_vps_setup.sh
#
# VPSを農家VPNサーバ (wg-farmers) として設定する。
# MBP版(wg_farmer_setup.sh)と異なり、wg-vps クライアント設定は不要。
#
# 生成ファイル:
#   /etc/wireguard/wg-farmers.conf  : 農家RPi用WGサーバ (10.20.0.1, port 51821)
#
# 設計: docs/multi_farmer_design.md §3 参照
#===============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

#-------------------------------------------------------------------------------
# 設定変数
#-------------------------------------------------------------------------------
FARMERS_WG_IF="wg-farmers"
FARMERS_WG_PORT="51821"
FARMERS_WG_IP="10.20.0.1/24"

#-------------------------------------------------------------------------------
# 前提チェック
#-------------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: root権限が必要です。sudo で実行してください。${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  VPS農家用WGサーバ初期設定${NC}"
echo -e "${GREEN}========================================${NC}"

#-------------------------------------------------------------------------------
# WireGuardインストール確認
#-------------------------------------------------------------------------------
if ! command -v wg &>/dev/null; then
    echo -e "\n${YELLOW}WireGuardが未インストールです。インストール中...${NC}"
    apt-get update && apt-get install -y wireguard wireguard-tools
fi

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

#-------------------------------------------------------------------------------
# [1/3] wg-farmers用 サーバキーペア生成
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1/3] wg-farmers キーペア生成...${NC}"

FARMERS_KEY_FILE="/etc/wireguard/wg-farmers-private.key"
FARMERS_PUB_FILE="/etc/wireguard/wg-farmers-public.key"

if [[ -f "$FARMERS_KEY_FILE" ]]; then
    echo -e "${YELLOW}⚠ 既存の wg-farmers キーが見つかりました。再利用します。${NC}"
    FARMERS_PRIVATE_KEY=$(cat "$FARMERS_KEY_FILE")
    FARMERS_PUBLIC_KEY=$(cat "$FARMERS_PUB_FILE")
else
    FARMERS_PRIVATE_KEY=$(wg genkey)
    echo "$FARMERS_PRIVATE_KEY" > "$FARMERS_KEY_FILE"
    FARMERS_PUBLIC_KEY=$(echo "$FARMERS_PRIVATE_KEY" | wg pubkey)
    echo "$FARMERS_PUBLIC_KEY" > "$FARMERS_PUB_FILE"
    chmod 600 "$FARMERS_KEY_FILE"
fi

echo -e "${GREEN}wg-farmers 公開鍵: ${FARMERS_PUBLIC_KEY}${NC}"

#-------------------------------------------------------------------------------
# [2/3] wg-farmers.conf 生成
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[2/3] /etc/wireguard/wg-farmers.conf 生成...${NC}"

FARMERS_CONF="/etc/wireguard/wg-farmers.conf"

if [[ -f "$FARMERS_CONF" ]]; then
    echo -e "${YELLOW}⚠ 既存の wg-farmers.conf が見つかりました。${NC}"
    echo "  バックアップ後に[Interface]のみ更新（[Peer]は保持）"
    cp "$FARMERS_CONF" "${FARMERS_CONF}.bak.$(date +%Y%m%d%H%M%S)"
    EXISTING_PEERS=$(awk '/^\[Peer\]/,0' "$FARMERS_CONF")
else
    EXISTING_PEERS=""
fi

cat > "$FARMERS_CONF" << EOF
# WireGuard 農家用サーバ設定 (wg-farmers)
# VPS側: 農家RPi接続用WGサーバ
# 生成: $(date)

[Interface]
Address = ${FARMERS_WG_IP}
ListenPort = ${FARMERS_WG_PORT}
PrivateKey = ${FARMERS_PRIVATE_KEY}

# iptables FORWARD for wg-farmers clients
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT

# 農家Peerは onboarding.py register_pubkey で自動追加される
EOF

if [[ -n "$EXISTING_PEERS" ]]; then
    echo "$EXISTING_PEERS" >> "$FARMERS_CONF"
fi

chmod 600 "$FARMERS_CONF"
echo -e "${GREEN}wg-farmers.conf 生成完了${NC}"

#-------------------------------------------------------------------------------
# [3/3] ファイアウォール開放 + WG起動 + systemd有効化
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[3/3] ファイアウォール開放 + WG起動...${NC}"

# UFW がある場合はポート開放
if command -v ufw &>/dev/null; then
    ufw allow "${FARMERS_WG_PORT}/udp" comment "WireGuard wg-farmers"
    echo -e "${GREEN}UFW: ${FARMERS_WG_PORT}/udp 許可済み${NC}"
fi

# wg-farmers 起動
if wg show "$FARMERS_WG_IF" &>/dev/null 2>&1; then
    echo -e "${YELLOW}wg-farmers は既に稼働中です。再起動します。${NC}"
    wg-quick down "$FARMERS_WG_IF" 2>/dev/null || true
fi
wg-quick up "$FARMERS_WG_IF"

# systemd 永続化
systemctl enable "wg-quick@${FARMERS_WG_IF}"
echo -e "${GREEN}wg-farmers 起動完了 + systemd有効化${NC}"

#-------------------------------------------------------------------------------
# QR画像配信ディレクトリ作成
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}QR画像配信ディレクトリ作成...${NC}"
mkdir -p /var/www/qr
chown www-data:www-data /var/www/qr
chmod 755 /var/www/qr
echo -e "${GREEN}/var/www/qr 作成完了${NC}"

#-------------------------------------------------------------------------------
# サマリー
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  セットアップ完了！${NC}"
echo -e "${GREEN}========================================${NC}"

echo -e "\n${YELLOW}wg-farmers 情報:${NC}"
echo "  インターフェースIP: ${FARMERS_WG_IP}"
echo "  リッスンポート: ${FARMERS_WG_PORT}/UDP"
echo "  公開鍵: ${FARMERS_PUBLIC_KEY}"
echo "  設定ファイル: /etc/wireguard/wg-farmers.conf"

echo -e "\n${YELLOW}次のステップ:${NC}"
echo "1. nginx設定: /etc/nginx/sites-available/toiso.fit を作成"
echo "2. LINE Bot systemdサービス: agriha-linebot.service を作成"
echo "3. .env にWG公開鍵を設定:"
echo "   WG_SERVER_PUBLIC_KEY=${FARMERS_PUBLIC_KEY}"
echo "   WG_SERVER_ENDPOINT=$(hostname -I | awk '{print $1}'):${FARMERS_WG_PORT}"
echo ""
echo "4. WGステータス確認:"
echo "   sudo wg show wg-farmers"
