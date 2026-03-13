#!/bin/bash
#===============================================================================
# gen_farmer_config.sh - 農家追加スクリプト
#
# Usage: bash gen_farmer_config.sh <farmer_id> <farmer_name> <wg_client_ip>
#
# 例:
#   bash scripts/gen_farmer_config.sh farmer_a 田中農園 10.20.0.10
#   bash scripts/gen_farmer_config.sh farmer_b 鈴木ファーム 10.20.0.20
#
# 処理内容:
#   1. config/farmers.yaml にエントリ追加
#   2. config/farmers_secrets.yaml にpendingエントリ追加（公開鍵はRPiから後日受信）
#   3. /etc/wireguard/wg-farmers.conf にPeerセクション追加（公開鍵はplaceholder）
#
# 設計書: docs/multi_farmer_design.md §5, §8
#   - 農家RPiの秘密鍵はRPiローカルで生成。このスクリプトでは扱わない
#   - RPiがBase64デコード後に公開鍵をMBPに POST /api/register_pubkey で通知する
#===============================================================================

set -euo pipefail

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

#-------------------------------------------------------------------------------
# 引数チェック
#-------------------------------------------------------------------------------
usage() {
    echo "Usage: bash $0 <farmer_id> <farmer_name> <wg_client_ip>"
    echo ""
    echo "  farmer_id      農家ID (例: farmer_a, tono)"
    echo "  farmer_name    農家名 (例: 田中農園)"
    echo "  wg_client_ip   農家RPiのWG IP (例: 10.20.0.10)"
    echo ""
    echo "IPアドレス割り当て規則: 10.20.0.N0 (N=農家番号 1-24)"
    echo "  農家1: 10.20.0.10"
    echo "  農家2: 10.20.0.20"
    echo "  ..."
}

if [[ $# -ne 3 ]]; then
    echo -e "${RED}Error: 引数が正しくありません。${NC}"
    usage
    exit 1
fi

FARMER_ID="$1"
FARMER_NAME="$2"
WG_CLIENT_IP="$3"

# IPアドレスのフォーマット確認
if ! echo "$WG_CLIENT_IP" | grep -qE '^10\.20\.0\.[0-9]+$'; then
    echo -e "${RED}Error: WG IPは 10.20.0.x 形式で指定してください。${NC}"
    echo "  例: 10.20.0.10"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FARMERS_YAML="$REPO_ROOT/config/farmers.yaml"
FARMERS_SECRETS="$REPO_ROOT/config/farmers_secrets.yaml"
WG_CONF="/etc/wireguard/wg-farmers.conf"
REGISTERED_AT="$(date +%Y-%m-%d)"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  農家追加スクリプト${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "  農家ID:   ${FARMER_ID}"
echo -e "  農家名:   ${FARMER_NAME}"
echo -e "  WG IP:    ${WG_CLIENT_IP}/32"

#-------------------------------------------------------------------------------
# 重複チェック
#-------------------------------------------------------------------------------
if [[ -f "$FARMERS_YAML" ]] && grep -q "^  ${FARMER_ID}:" "$FARMERS_YAML"; then
    echo -e "${RED}Error: farmer_id '${FARMER_ID}' は既に farmers.yaml に存在します。${NC}"
    exit 1
fi

if [[ -f "$FARMERS_SECRETS" ]] && grep -q "^  ${FARMER_ID}:" "$FARMERS_SECRETS"; then
    echo -e "${RED}Error: farmer_id '${FARMER_ID}' は既に farmers_secrets.yaml に存在します。${NC}"
    exit 1
fi

#-------------------------------------------------------------------------------
# [1/3] config/farmers.yaml にエントリ追加
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1/3] config/farmers.yaml にエントリ追加...${NC}"

mkdir -p "$REPO_ROOT/config"

if [[ ! -f "$FARMERS_YAML" ]]; then
    # 新規作成
    cat > "$FARMERS_YAML" << EOF
# config/farmers.yaml
# 農家マスタ（git管理）
# 生成: $(date)
farmers:
EOF
fi

cat >> "$FARMERS_YAML" << EOF
  ${FARMER_ID}:
    name: "${FARMER_NAME}"
    rpi_host: "${WG_CLIENT_IP}"
    rpi_chat_port: 8502
    system_prompt_path: "/home/pi/arsprout-llama/config/system_prompt.txt"
    status: pending       # pending → active (WG疎通確認後)
    registered_at: "${REGISTERED_AT}"
EOF

echo -e "${GREEN}farmers.yaml 更新完了${NC}"

#-------------------------------------------------------------------------------
# [2/3] config/farmers_secrets.yaml にpendingエントリ追加
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[2/3] config/farmers_secrets.yaml にpendingエントリ追加...${NC}"

if [[ ! -f "$FARMERS_SECRETS" ]]; then
    cat > "$FARMERS_SECRETS" << EOF
# config/farmers_secrets.yaml
# 秘密情報（.gitignore対象 — git管理しない）
# 注意: wg_client_private_key は保存しない。秘密鍵はRPiローカルのみ。
# 生成: $(date)
farmers:
EOF
    chmod 600 "$FARMERS_SECRETS"
fi

cat >> "$FARMERS_SECRETS" << EOF
  ${FARMER_ID}:
    line_user_id: ""          # follow event時に自動設定
    wg_public_key: ""         # RPiから POST /api/register_pubkey で受信後に設定
    wg_ip: "${WG_CLIENT_IP}"
    status: pending           # pending → active (公開鍵受信+WG疎通確認後)
EOF

echo -e "${GREEN}farmers_secrets.yaml 更新完了${NC}"

#-------------------------------------------------------------------------------
# [3/3] /etc/wireguard/wg-farmers.conf にPeerセクション追加
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[3/3] wg-farmers.conf にPeer placeholder追加...${NC}"

if [[ ! -f "$WG_CONF" ]]; then
    echo -e "${YELLOW}⚠ wg-farmers.conf が見つかりません。${NC}"
    echo "  先に sudo bash scripts/wg_farmer_setup.sh を実行してください。"
    echo "  wg-farmers.conf は手動でPeerを追加してください:"
    echo ""
    echo "  [Peer]"
    echo "  # farmer_id: ${FARMER_ID}  status: pending"
    echo "  # PublicKey = <RPiから POST /api/register_pubkey で受信後に設定>"
    echo "  AllowedIPs = ${WG_CLIENT_IP}/32"
else
    # root権限確認
    if [[ $EUID -ne 0 ]]; then
        echo -e "${YELLOW}⚠ wg-farmers.conf への書き込みにはroot権限が必要です。${NC}"
        echo "  以下をroot権限で手動追加してください:"
        echo ""
        echo "  sudo tee -a /etc/wireguard/wg-farmers.conf << 'WGEOF'"
        echo "  [Peer]"
        echo "  # farmer_id: ${FARMER_ID}  status: pending"
        echo "  # PublicKey は RPi登録後に wg set wg-farmers peer <pubkey> allowed-ips ${WG_CLIENT_IP}/32 で設定"
        echo "  # AllowedIPs = ${WG_CLIENT_IP}/32"
        echo "  WGEOF"
    else
        cat >> "$WG_CONF" << EOF

[Peer]
# farmer_id: ${FARMER_ID}  status: pending  registered: ${REGISTERED_AT}
# PublicKey はRPiから POST /api/register_pubkey 受信後に以下で設定:
#   sudo wg set wg-farmers peer <RPi_PUBLIC_KEY> allowed-ips ${WG_CLIENT_IP}/32
# AllowedIPs = ${WG_CLIENT_IP}/32
EOF
        echo -e "${GREEN}wg-farmers.conf にplaceholder追加完了${NC}"
    fi
fi

#-------------------------------------------------------------------------------
# サマリー
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  農家追加完了！${NC}"
echo -e "${GREEN}========================================${NC}"

echo -e "\n${YELLOW}次のステップ:${NC}"
echo "1. 農家にLINE BotのQRコードを共有"
echo "2. 農家がBot友達追加 → follow eventで設定コード(Base64)が自動送信される"
echo "3. 農家がRPi Web UIに設定コードを貼り付け"
echo "4. RPiがWG接続後、公開鍵をPOST /api/register_pubkey で通知"
echo "5. MBP側でwg-farmers.confのPeer公開鍵を更新:"
echo "   sudo wg set wg-farmers peer <RPi_PUBLIC_KEY> allowed-ips ${WG_CLIENT_IP}/32"
echo "6. farmers.yaml / farmers_secrets.yaml のstatus を active に変更"
echo ""
echo "状態確認: sudo wg show wg-farmers"
