#!/bin/bash
#===============================================================================
# deploy_vps_linebot.sh — VPSにLINE Botをデプロイ
#
# ローカル(MBP等)から実行。rsyncでコードを転送し、VPS上でセットアップ。
#
# Usage: bash scripts/deploy_vps_linebot.sh [--setup]
#   --setup  初回セットアップ（venv作成、nginx設定、systemd登録、sudoers設定）
#   引数なし  コード更新のみ（rsync + サービス再起動）
#
# 前提:
#   - SSH鍵認証で debian@VPS に接続可能
#   - VPSにWireGuard, nginx, certbot, python3-venv がインストール済み
#===============================================================================

set -euo pipefail

VPS_HOST="${VPS_HOST:-debian@153.127.46.167}"
DEPLOY_DIR="/opt/agriha-linebot"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_SRC="${REPO_ROOT}/src/agriha/vps"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SETUP=false
if [[ "${1:-}" == "--setup" ]]; then
    SETUP=true
fi

echo -e "${GREEN}=== VPS LINE Bot デプロイ ===${NC}"

#-------------------------------------------------------------------------------
# コード転送（rsync）
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1] rsync: コード転送...${NC}"
ssh "${VPS_HOST}" "sudo mkdir -p ${DEPLOY_DIR} && sudo chown debian:debian ${DEPLOY_DIR}"

rsync -avz --delete \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.venv' \
    --exclude='.env' \
    --exclude='tests/' \
    "${VPS_SRC}/" "${VPS_HOST}:${DEPLOY_DIR}/"

# config ディレクトリ（farmers.yaml等を配置する場所）
ssh "${VPS_HOST}" "mkdir -p ${DEPLOY_DIR}/config"

echo -e "${GREEN}転送完了${NC}"

#-------------------------------------------------------------------------------
# 初回セットアップ（--setup時のみ）
#-------------------------------------------------------------------------------
if $SETUP; then
    echo -e "\n${GREEN}[2] 初回セットアップ...${NC}"

    # venv作成 + pip install
    echo -e "${YELLOW}venv + pip install...${NC}"
    ssh "${VPS_HOST}" "
        cd ${DEPLOY_DIR}
        python3 -m venv .venv
        .venv/bin/pip install --upgrade pip
        .venv/bin/pip install -r requirements.txt
    "

    # nginx設定
    echo -e "${YELLOW}nginx設定...${NC}"
    scp "${REPO_ROOT}/config/nginx-vps-toiso.conf" "${VPS_HOST}:/tmp/toiso.fit"
    ssh "${VPS_HOST}" "
        sudo mv /tmp/toiso.fit /etc/nginx/sites-available/toiso.fit
        sudo ln -sf /etc/nginx/sites-available/toiso.fit /etc/nginx/sites-enabled/
        sudo rm -f /etc/nginx/sites-enabled/default
        sudo nginx -t && sudo systemctl reload nginx
    "

    # systemd サービス
    echo -e "${YELLOW}systemd設定...${NC}"
    scp "${REPO_ROOT}/systemd/agriha-linebot.service" "${VPS_HOST}:/tmp/agriha-linebot.service"
    ssh "${VPS_HOST}" "
        sudo mv /tmp/agriha-linebot.service /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable agriha-linebot
    "

    # sudoers（www-dataがwg setを実行するため）
    echo -e "${YELLOW}sudoers設定...${NC}"
    ssh "${VPS_HOST}" "
        echo 'www-data ALL=(root) NOPASSWD: /usr/bin/wg set wg-farmers peer *' | sudo tee /etc/sudoers.d/agriha-wg > /dev/null
        sudo chmod 440 /etc/sudoers.d/agriha-wg
        sudo visudo -c
    "

    # QR画像ディレクトリ
    ssh "${VPS_HOST}" "
        sudo mkdir -p /var/www/qr
        sudo chown www-data:www-data /var/www/qr
    "

    # QRクリーンアップ cron（毎日3時）
    echo -e "${YELLOW}cronジョブ設定...${NC}"
    ssh "${VPS_HOST}" "
        (crontab -l 2>/dev/null | grep -v cleanup_qr; echo '0 3 * * * curl -s -X POST http://127.0.0.1:8443/api/cleanup_qr > /dev/null') | crontab -
    "

    echo -e "${GREEN}初回セットアップ完了${NC}"

    echo -e "\n${YELLOW}残り手順:${NC}"
    echo "1. VPSで .env を作成:"
    echo "   ssh ${VPS_HOST}"
    echo "   sudo cp ${DEPLOY_DIR}/.env.example ${DEPLOY_DIR}/.env"
    echo "   sudo vi ${DEPLOY_DIR}/.env  # 実際の値を設定"
    echo "   sudo chown www-data:www-data ${DEPLOY_DIR}/.env"
    echo ""
    echo "2. WG農家インターフェースのセットアップ:"
    echo "   sudo bash ${DEPLOY_DIR}/../scripts/wg_farmers_vps_setup.sh"
    echo "   # ※ または事前に scripts/wg_farmers_vps_setup.sh を転送済みなら直接実行"
    echo ""
    echo "3. サービス起動:"
    echo "   sudo systemctl start agriha-linebot"
    echo "   sudo systemctl status agriha-linebot"
    echo ""
    echo "4. 動作確認:"
    echo "   curl https://toiso.fit/health"
else
    #---------------------------------------------------------------------------
    # コード更新のみ → サービス再起動
    #---------------------------------------------------------------------------
    echo -e "\n${GREEN}[2] pip install (差分)...${NC}"
    ssh "${VPS_HOST}" "cd ${DEPLOY_DIR} && .venv/bin/pip install -q -r requirements.txt"

    echo -e "\n${GREEN}[3] サービス再起動...${NC}"
    ssh "${VPS_HOST}" "sudo systemctl restart agriha-linebot"

    echo -e "\n${GREEN}[4] ステータス確認...${NC}"
    ssh "${VPS_HOST}" "sudo systemctl status agriha-linebot --no-pager -l" || true
fi

echo -e "\n${GREEN}=== デプロイ完了 ===${NC}"
