#!/bin/bash
#===============================================================================
# deploy_vps_linebot.sh — VPS LINE Botセットアップ・更新スクリプト
#
# VPS上で実行する。git clone 済みのリポジトリ内から実行すること。
# config/vps.conf を事前に編集してから実行する。
#
# Usage:
#   git clone https://github.com/yourname/uecs-llm.git /opt/uecs-llm
#   cd /opt/uecs-llm
#   vi config/vps.conf                                 # 環境設定を編集
#   sudo bash scripts/deploy_vps_linebot.sh --setup    # 初回セットアップ
#   sudo bash scripts/deploy_vps_linebot.sh            # コード更新（git pull + 再起動）
#
# 前提:
#   - VPSにnginx, certbot, python3-venv がインストール済み
#   - sudoで実行（systemd/nginx/sudoers設定のため）
#===============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_CONF="${REPO_ROOT}/config/vps.conf"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SETUP=false
if [[ "${1:-}" == "--setup" ]]; then
    SETUP=true
fi

# root チェック
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}ERROR: sudo で実行してください${NC}"
    echo "  sudo bash $0 $*"
    exit 1
fi

# vps.conf 読み込み
if [[ ! -f "${VPS_CONF}" ]]; then
    echo -e "${RED}ERROR: ${VPS_CONF} が見つかりません${NC}"
    echo "config/vps.conf を編集してから実行してください。"
    exit 1
fi
# shellcheck source=../config/vps.conf
source "${VPS_CONF}"

# 必須変数チェック
for var in DOMAIN APP_PORT DEPLOY_DIR QR_DIR WG_INTERFACE WG_PORT WG_SERVER_IP; do
    if [[ -z "${!var:-}" ]]; then
        echo -e "${RED}ERROR: ${var} が vps.conf で未設定です${NC}"
        exit 1
    fi
done

VPS_SRC="${REPO_ROOT}/src/agriha/vps"

echo -e "${GREEN}=== VPS LINE Bot デプロイ ===${NC}"
echo "リポジトリ: ${REPO_ROOT}"
echo "ドメイン:   ${DOMAIN}"
echo "ポート:     ${APP_PORT}"

#-------------------------------------------------------------------------------
# テンプレート展開関数
#-------------------------------------------------------------------------------
expand_template() {
    local src="$1"
    local dst="$2"
    sed \
        -e "s|__DOMAIN__|${DOMAIN}|g" \
        -e "s|__APP_PORT__|${APP_PORT}|g" \
        -e "s|__DEPLOY_DIR__|${DEPLOY_DIR}|g" \
        -e "s|__QR_DIR__|${QR_DIR}|g" \
        -e "s|__WG_INTERFACE__|${WG_INTERFACE}|g" \
        -e "s|__WG_PORT__|${WG_PORT}|g" \
        -e "s|__WG_SERVER_IP__|${WG_SERVER_IP}|g" \
        -e "s|__SSL_CERT__|${SSL_CERT}|g" \
        -e "s|__SSL_KEY__|${SSL_KEY}|g" \
        "${src}" > "${dst}"
    echo "  ${src} → ${dst}"
}

#-------------------------------------------------------------------------------
# [1] シンボリックリンク作成（src/agriha/vps → DEPLOY_DIR）
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1] デプロイディレクトリ準備...${NC}"
if [[ -L "${DEPLOY_DIR}" ]]; then
    echo "シンボリックリンク既存: $(readlink "${DEPLOY_DIR}")"
elif [[ -d "${DEPLOY_DIR}" ]]; then
    echo -e "${YELLOW}WARN: ${DEPLOY_DIR} が実ディレクトリとして存在。バックアップして置換${NC}"
    mv "${DEPLOY_DIR}" "${DEPLOY_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    ln -sf "${VPS_SRC}" "${DEPLOY_DIR}"
else
    ln -sf "${VPS_SRC}" "${DEPLOY_DIR}"
fi
echo "  ${DEPLOY_DIR} → ${VPS_SRC}"

#-------------------------------------------------------------------------------
# 初回セットアップ（--setup時のみ）
#-------------------------------------------------------------------------------
if $SETUP; then
    echo -e "\n${GREEN}[2] 初回セットアップ...${NC}"

    # venv作成 + pip install
    echo -e "${YELLOW}venv + pip install...${NC}"
    cd "${VPS_SRC}"
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt

    # nginx設定（テンプレート展開）
    echo -e "${YELLOW}nginx設定...${NC}"
    expand_template \
        "${REPO_ROOT}/config/nginx-vps.conf.template" \
        "/etc/nginx/sites-available/${DOMAIN}"
    ln -sf "/etc/nginx/sites-available/${DOMAIN}" /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx

    # systemd サービス（テンプレート展開）
    echo -e "${YELLOW}systemd設定...${NC}"
    expand_template \
        "${REPO_ROOT}/systemd/agriha-linebot.service.template" \
        /etc/systemd/system/agriha-linebot.service
    systemctl daemon-reload
    systemctl enable agriha-linebot

    # sudoers（www-dataがwg setを実行するため）
    echo -e "${YELLOW}sudoers設定...${NC}"
    echo "www-data ALL=(root) NOPASSWD: /usr/bin/wg set ${WG_INTERFACE} peer *" > /etc/sudoers.d/agriha-wg
    chmod 440 /etc/sudoers.d/agriha-wg
    visudo -c

    # QR画像ディレクトリ
    mkdir -p "${QR_DIR}"
    chown www-data:www-data "${QR_DIR}"

    # configディレクトリ
    mkdir -p "${VPS_SRC}/config"

    # QRクリーンアップ cron（毎日3時）
    echo -e "${YELLOW}cronジョブ設定...${NC}"
    (crontab -u www-data -l 2>/dev/null | grep -v cleanup_qr; echo "0 3 * * * curl -s -X POST http://127.0.0.1:${APP_PORT}/api/cleanup_qr > /dev/null") | crontab -u www-data -

    echo -e "${GREEN}初回セットアップ完了${NC}"

    echo -e "\n${YELLOW}残り手順:${NC}"
    echo "1. .env を作成:"
    echo "   sudo cp ${DEPLOY_DIR}/.env.example ${DEPLOY_DIR}/.env"
    echo "   sudo vi ${DEPLOY_DIR}/.env  # 実際の値を設定"
    echo "   sudo chown www-data:www-data ${DEPLOY_DIR}/.env"
    echo ""
    echo "2. WG農家インターフェースのセットアップ:"
    echo "   sudo bash ${REPO_ROOT}/scripts/wg_farmers_vps_setup.sh"
    echo ""
    echo "3. サービス起動:"
    echo "   sudo systemctl start agriha-linebot"
    echo "   sudo systemctl status agriha-linebot"
    echo ""
    echo "4. 動作確認:"
    echo "   curl https://${DOMAIN}/health"
else
    #---------------------------------------------------------------------------
    # コード更新（git pull + pip差分 + サービス再起動）
    #---------------------------------------------------------------------------
    echo -e "\n${GREEN}[2] git pull...${NC}"
    cd "${REPO_ROOT}"
    sudo -u "$(stat -c '%U' .git)" git pull

    echo -e "\n${GREEN}[3] pip install (差分)...${NC}"
    cd "${VPS_SRC}"
    .venv/bin/pip install -q -r requirements.txt

    echo -e "\n${GREEN}[4] サービス再起動...${NC}"
    systemctl restart agriha-linebot

    echo -e "\n${GREEN}[5] ステータス確認...${NC}"
    systemctl status agriha-linebot --no-pager -l || true
fi

echo -e "\n${GREEN}=== デプロイ完了 ===${NC}"
