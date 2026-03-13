#!/bin/bash
#===============================================================================
# wg_farmer_setup.sh - MBP側 農家用WG初期設定スクリプト
#
# Usage: sudo bash wg_farmer_setup.sh
#
# 生成ファイル:
#   /etc/wireguard/wg-farmers.conf  : 農家RPi用WGサーバ (10.20.0.1, port 51821)
#   /etc/wireguard/wg-vps.conf      : MBP→VPS接続用WGクライアント (Webhook転送受け)
#
# 設計: docs/multi_farmer_design.md §3, §7 参照
#   - MBPは2つのWGインターフェースを持つ
#   - VPS nginx → MBP (WG IP経由) → 農家RPi
#   - 農家RPiの秘密鍵はRPiローカルで生成。このスクリプトでは扱わない
#===============================================================================

set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

#-------------------------------------------------------------------------------
# 設定変数（環境に合わせて変更）
#-------------------------------------------------------------------------------

# --- 農家用WGサーバ (wg-farmers) ---
FARMERS_WG_IF="wg-farmers"
FARMERS_WG_PORT="51821"          # 殿用WG(51820)とは別ポート
FARMERS_WG_IP="10.20.0.1/24"

# --- VPS接続用WGクライアント (wg-vps) ---
VPS_WG_IF="wg-vps"
VPS_WG_PORT="51820"              # VPS側WGサーバのポート
# ※ 以下は実行前に手動で設定するか、引数で渡す
VPS_IP="${VPS_IP:-}"             # 例: 203.0.113.1
VPS_PUBLIC_KEY="${VPS_PUBLIC_KEY:-}"
MBP_VPS_CLIENT_IP="${MBP_VPS_CLIENT_IP:-}"  # VPS上でMBPに割り当てるIP (例: 10.10.0.2/32)

#-------------------------------------------------------------------------------
# 引数処理
#-------------------------------------------------------------------------------
usage() {
    echo "Usage: sudo VPS_IP=<ip> VPS_PUBLIC_KEY=<key> MBP_VPS_CLIENT_IP=<ip/mask> bash $0"
    echo ""
    echo "  VPS_IP             VPSのパブリックIP"
    echo "  VPS_PUBLIC_KEY     VPSのWG公開鍵 (wg pubkey < /etc/wireguard/wg0.key 等)"
    echo "  MBP_VPS_CLIENT_IP  VPS上でMBPに割り当てるIP (例: 10.10.0.2/32)"
    echo ""
    echo "例:"
    echo "  sudo VPS_IP=203.0.113.1 VPS_PUBLIC_KEY=xxxx= MBP_VPS_CLIENT_IP=10.10.0.2/32 bash $0"
}

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: root権限が必要です。sudo で実行してください。${NC}"
    exit 1
fi

if [[ -z "$VPS_IP" || -z "$VPS_PUBLIC_KEY" || -z "$MBP_VPS_CLIENT_IP" ]]; then
    echo -e "${YELLOW}⚠ 環境変数が未設定です。${NC}"
    usage
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  MBP農家用WG初期設定スクリプト${NC}"
echo -e "${GREEN}========================================${NC}"

#-------------------------------------------------------------------------------
# WireGuardインストール確認 (macOS: brew, Linux: apt)
#-------------------------------------------------------------------------------
if ! command -v wg &>/dev/null; then
    echo -e "\n${YELLOW}WireGuardが未インストールです。${NC}"
    if command -v brew &>/dev/null; then
        echo "Homebrewでインストール中..."
        brew install wireguard-tools
    elif command -v apt-get &>/dev/null; then
        apt-get update && apt-get install -y wireguard wireguard-tools
    else
        echo -e "${RED}Error: wg コマンドが見つかりません。WireGuardを手動でインストールしてください。${NC}"
        exit 1
    fi
fi

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

#-------------------------------------------------------------------------------
# [1/4] wg-farmers用 サーバキーペア生成
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1/4] wg-farmers キーペア生成...${NC}"

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
# [2/4] wg-farmers.conf 生成（農家用WGサーバ）
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[2/4] /etc/wireguard/wg-farmers.conf 生成...${NC}"

FARMERS_CONF="/etc/wireguard/wg-farmers.conf"

# 既存の[Peer]セクションを保持するため、上書きではなく[Interface]のみ更新
if [[ -f "$FARMERS_CONF" ]]; then
    echo -e "${YELLOW}⚠ 既存の wg-farmers.conf が見つかりました。${NC}"
    echo "  [Peer]セクションを保持するため、バックアップ後に[Interface]のみ更新します。"
    cp "$FARMERS_CONF" "${FARMERS_CONF}.bak.$(date +%Y%m%d%H%M%S)"
    # [Peer]セクション以降を抽出
    EXISTING_PEERS=$(awk '/^\[Peer\]/,0' "$FARMERS_CONF")
else
    EXISTING_PEERS=""
fi

cat > "$FARMERS_CONF" << EOF
# WireGuard 農家用サーバ設定 (wg-farmers)
# MBP側: 農家RPi接続用WGサーバ
# 生成: $(date)
# 設計書: docs/multi_farmer_design.md §3, §7

[Interface]
Address = ${FARMERS_WG_IP}
ListenPort = ${FARMERS_WG_PORT}
PrivateKey = ${FARMERS_PRIVATE_KEY}
# SaveConfig = false  # gen_farmer_config.sh で動的管理するため false

# 農家Peerは gen_farmer_config.sh で追加される
# 形式:
# [Peer]
# # farmer_id: farmer_a
# PublicKey = <RPiから受信した公開鍵>
# AllowedIPs = 10.20.0.10/32

EOF

# 既存Peerを保持
if [[ -n "$EXISTING_PEERS" ]]; then
    echo "$EXISTING_PEERS" >> "$FARMERS_CONF"
fi

chmod 600 "$FARMERS_CONF"
echo -e "${GREEN}wg-farmers.conf 生成完了${NC}"

#-------------------------------------------------------------------------------
# [3/4] wg-vps.conf 生成（VPS接続用WGクライアント）
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[3/4] /etc/wireguard/wg-vps.conf 生成...${NC}"

VPS_KEY_FILE="/etc/wireguard/wg-vps-private.key"
VPS_PUB_FILE="/etc/wireguard/wg-vps-public.key"

if [[ -f "$VPS_KEY_FILE" ]]; then
    echo -e "${YELLOW}⚠ 既存の wg-vps キーが見つかりました。再利用します。${NC}"
    VPS_CLIENT_PRIVATE_KEY=$(cat "$VPS_KEY_FILE")
    VPS_CLIENT_PUBLIC_KEY=$(cat "$VPS_PUB_FILE")
else
    VPS_CLIENT_PRIVATE_KEY=$(wg genkey)
    echo "$VPS_CLIENT_PRIVATE_KEY" > "$VPS_KEY_FILE"
    VPS_CLIENT_PUBLIC_KEY=$(echo "$VPS_CLIENT_PRIVATE_KEY" | wg pubkey)
    echo "$VPS_CLIENT_PUBLIC_KEY" > "$VPS_PUB_FILE"
    chmod 600 "$VPS_KEY_FILE"
fi

cat > "/etc/wireguard/${VPS_WG_IF}.conf" << EOF
# WireGuard VPS接続クライアント設定 (wg-vps)
# MBP側: VPSへのWGクライアント接続
# Webhook転送の受け口として使用
# 生成: $(date)
# 設計書: docs/multi_farmer_design.md §3, §7

[Interface]
# MBPがVPS上で持つIPアドレス（VPS側でPeer登録する際に使用するIP）
Address = ${MBP_VPS_CLIENT_IP}
PrivateKey = ${VPS_CLIENT_PRIVATE_KEY}

[Peer]
# VPS (WGサーバ)
PublicKey = ${VPS_PUBLIC_KEY}
Endpoint = ${VPS_IP}:${VPS_WG_PORT}
# VPSのWGサブネット全体を許可（Webhook受信用）
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF

chmod 600 "/etc/wireguard/${VPS_WG_IF}.conf"
echo -e "${GREEN}wg-vps.conf 生成完了${NC}"

#-------------------------------------------------------------------------------
# [4/4] WG起動・永続化
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[4/4] WireGuardインターフェース起動...${NC}"

# wg-farmers 起動
if wg show "$FARMERS_WG_IF" &>/dev/null 2>&1; then
    echo -e "${YELLOW}wg-farmers は既に稼働中です。再起動します。${NC}"
    wg-quick down "$FARMERS_WG_IF" 2>/dev/null || true
fi
wg-quick up "$FARMERS_WG_IF"
echo -e "${GREEN}wg-farmers 起動完了${NC}"

# wg-vps 起動
if wg show "$VPS_WG_IF" &>/dev/null 2>&1; then
    echo -e "${YELLOW}wg-vps は既に稼働中です。再起動します。${NC}"
    wg-quick down "$VPS_WG_IF" 2>/dev/null || true
fi
wg-quick up "$VPS_WG_IF"
echo -e "${GREEN}wg-vps 起動完了${NC}"

# 永続化 (Linux: systemctl, macOS: launchd は手動設定案内)
if command -v systemctl &>/dev/null; then
    systemctl enable "wg-quick@${FARMERS_WG_IF}"
    systemctl enable "wg-quick@${VPS_WG_IF}"
    echo -e "${GREEN}systemctl enable 完了（再起動後も自動起動）${NC}"
else
    echo -e "${YELLOW}⚠ macOS環境: WG自動起動はWireGuardアプリまたはLaunchDaemonで設定してください。${NC}"
    echo "  または: brew services start wireguard-go（要追加設定）"
fi

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

echo -e "\n${YELLOW}wg-vps 情報:${NC}"
echo "  MBPのVPS内IP: ${MBP_VPS_CLIENT_IP}"
echo "  MBP公開鍵: ${VPS_CLIENT_PUBLIC_KEY}"
echo "  設定ファイル: /etc/wireguard/wg-vps.conf"

echo -e "\n${YELLOW}次のステップ:${NC}"
echo "1. VPS側のwg0設定にMBPのPeerを追加:"
echo "   [Peer]"
echo "   PublicKey = ${VPS_CLIENT_PUBLIC_KEY}"
echo "   AllowedIPs = ${MBP_VPS_CLIENT_IP}"
echo ""
echo "2. VPS nginx の proxy_pass を MBP の wg-vps IP に設定:"
echo "   proxy_pass http://$(echo "${MBP_VPS_CLIENT_IP}" | cut -d'/' -f1):5000/webhook;"
echo ""
echo "3. 農家追加は gen_farmer_config.sh を使用:"
echo "   bash scripts/gen_farmer_config.sh farmer_a 田中農園 10.20.0.10"
echo ""
echo "4. WGステータス確認:"
echo "   sudo wg show"
