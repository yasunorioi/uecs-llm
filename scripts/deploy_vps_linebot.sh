#!/bin/bash
#===============================================================================
# deploy_vps_linebot.sh -- VPS LINE Bot setup / update script
#
# Run on VPS after git clone. Edit config/vps.conf before running.
#
# Usage:
#   git clone https://github.com/yourname/uecs-llm.git /opt/uecs-llm
#   cd /opt/uecs-llm
#   vi config/vps.conf                                 # edit settings
#   sudo bash scripts/deploy_vps_linebot.sh --setup    # initial setup
#   sudo bash scripts/deploy_vps_linebot.sh            # update (git pull + restart)
#   sudo bash scripts/deploy_vps_linebot.sh --remove   # uninstall everything
#
# Prerequisites:
#   - nginx, certbot, python3-venv installed on VPS
#   - run with sudo
#===============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VPS_CONF="${REPO_ROOT}/config/vps.conf"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SETUP=false
REMOVE=false
case "${1:-}" in
    --setup)  SETUP=true ;;
    --remove) REMOVE=true ;;
esac

# root check
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}ERROR: run with sudo${NC}"
    echo "  sudo bash $0 $*"
    exit 1
fi

# load vps.conf
if [[ ! -f "${VPS_CONF}" ]]; then
    echo -e "${RED}ERROR: ${VPS_CONF} not found${NC}"
    echo "Edit config/vps.conf first."
    exit 1
fi
# shellcheck source=../config/vps.conf
source "${VPS_CONF}"

# required variable check
for var in DOMAIN APP_PORT DEPLOY_DIR QR_DIR WG_INTERFACE WG_PORT WG_SERVER_IP; do
    if [[ -z "${!var:-}" ]]; then
        echo -e "${RED}ERROR: ${var} not set in vps.conf${NC}"
        exit 1
    fi
done

VPS_SRC="${REPO_ROOT}/src/agriha/vps"

echo -e "${GREEN}=== VPS LINE Bot Deploy ===${NC}"
echo "Repo:   ${REPO_ROOT}"
echo "Domain: ${DOMAIN}"
echo "Port:   ${APP_PORT}"

#-------------------------------------------------------------------------------
# Template expansion
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
    echo "  ${src} -> ${dst}"
}

#-------------------------------------------------------------------------------
# --remove: uninstall everything
#-------------------------------------------------------------------------------
if $REMOVE; then
    echo -e "\n${RED}=== Removing VPS LINE Bot ===${NC}"

    # [1] Stop and disable service
    echo -e "${YELLOW}[1] Stopping service...${NC}"
    systemctl stop agriha-linebot 2>/dev/null || true
    systemctl disable agriha-linebot 2>/dev/null || true
    rm -f /etc/systemd/system/agriha-linebot.service
    systemctl daemon-reload
    echo "  agriha-linebot service removed"

    # [2] Remove nginx config
    echo -e "${YELLOW}[2] Removing nginx config...${NC}"
    rm -f "/etc/nginx/sites-enabled/${DOMAIN}"
    rm -f "/etc/nginx/sites-available/${DOMAIN}"
    if nginx -t 2>/dev/null; then
        systemctl reload nginx
    fi
    echo "  nginx config for ${DOMAIN} removed"

    # [3] Remove sudoers
    echo -e "${YELLOW}[3] Removing sudoers...${NC}"
    rm -f /etc/sudoers.d/agriha-wg
    echo "  sudoers rule removed"

    # [4] Remove cron job
    echo -e "${YELLOW}[4] Removing cron job...${NC}"
    (crontab -u www-data -l 2>/dev/null | grep -v cleanup_qr) | crontab -u www-data - 2>/dev/null || true
    echo "  cleanup_qr cron removed"

    # [5] Remove symlink
    echo -e "${YELLOW}[5] Removing symlink...${NC}"
    if [[ -L "${DEPLOY_DIR}" ]]; then
        rm -f "${DEPLOY_DIR}"
        echo "  ${DEPLOY_DIR} symlink removed"
    else
        echo "  ${DEPLOY_DIR} is not a symlink, skipping"
    fi

    # [6] Remove venv
    echo -e "${YELLOW}[6] Removing venv...${NC}"
    if [[ -d "${VPS_SRC}/.venv" ]]; then
        rm -rf "${VPS_SRC}/.venv"
        echo "  .venv removed"
    else
        echo "  no .venv found"
    fi

    # Note: QR dir, .env, WireGuard, and git repo are NOT removed
    echo -e "\n${GREEN}=== Remove complete ===${NC}"
    echo -e "${YELLOW}Not removed (manual cleanup if needed):${NC}"
    echo "  ${QR_DIR}          (QR images)"
    echo "  ${VPS_SRC}/.env    (secrets)"
    echo "  /etc/wireguard/    (WireGuard config, use wg-quick down ${WG_INTERFACE})"
    echo "  ${REPO_ROOT}       (git repo)"
    exit 0
fi

#-------------------------------------------------------------------------------
# [1] Symlink (src/agriha/vps -> DEPLOY_DIR)
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1] Preparing deploy directory...${NC}"
if [[ -L "${DEPLOY_DIR}" ]]; then
    echo "Symlink exists: $(readlink "${DEPLOY_DIR}")"
elif [[ -d "${DEPLOY_DIR}" ]]; then
    echo -e "${YELLOW}WARN: ${DEPLOY_DIR} is a real directory. Backing up and replacing.${NC}"
    mv "${DEPLOY_DIR}" "${DEPLOY_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    ln -sf "${VPS_SRC}" "${DEPLOY_DIR}"
else
    ln -sf "${VPS_SRC}" "${DEPLOY_DIR}"
fi
echo "  ${DEPLOY_DIR} -> ${VPS_SRC}"

#-------------------------------------------------------------------------------
# Initial setup (--setup only)
#-------------------------------------------------------------------------------
if $SETUP; then
    echo -e "\n${GREEN}[2] Initial setup...${NC}"

    # venv + pip install
    echo -e "${YELLOW}venv + pip install...${NC}"
    cd "${VPS_SRC}"
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt

    # nginx (template expansion)
    echo -e "${YELLOW}nginx config...${NC}"
    expand_template \
        "${REPO_ROOT}/config/nginx-vps.conf.template" \
        "/etc/nginx/sites-available/${DOMAIN}"
    ln -sf "/etc/nginx/sites-available/${DOMAIN}" /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx

    # systemd service (template expansion)
    echo -e "${YELLOW}systemd config...${NC}"
    expand_template \
        "${REPO_ROOT}/systemd/agriha-linebot.service.template" \
        /etc/systemd/system/agriha-linebot.service
    systemctl daemon-reload
    systemctl enable agriha-linebot

    # sudoers (www-data needs wg set)
    echo -e "${YELLOW}sudoers config...${NC}"
    echo "www-data ALL=(root) NOPASSWD: /usr/bin/wg set ${WG_INTERFACE} peer *" > /etc/sudoers.d/agriha-wg
    chmod 440 /etc/sudoers.d/agriha-wg
    visudo -c

    # QR image directory
    mkdir -p "${QR_DIR}"
    chown www-data:www-data "${QR_DIR}"

    # config directory
    mkdir -p "${VPS_SRC}/config"

    # QR cleanup cron (daily 3am)
    echo -e "${YELLOW}cron job...${NC}"
    (crontab -u www-data -l 2>/dev/null | grep -v cleanup_qr; echo "0 3 * * * curl -s -X POST http://127.0.0.1:${APP_PORT}/api/cleanup_qr > /dev/null") | crontab -u www-data -

    echo -e "${GREEN}Initial setup complete${NC}"

    echo -e "\n${YELLOW}Remaining steps:${NC}"
    echo "1. Create .env:"
    echo "   sudo cp ${DEPLOY_DIR}/.env.example ${DEPLOY_DIR}/.env"
    echo "   sudo vi ${DEPLOY_DIR}/.env"
    echo "   sudo chown www-data:www-data ${DEPLOY_DIR}/.env"
    echo ""
    echo "2. WireGuard farmer VPN setup:"
    echo "   sudo bash ${REPO_ROOT}/scripts/wg_farmers_vps_setup.sh"
    echo ""
    echo "3. Start service:"
    echo "   sudo systemctl start agriha-linebot"
    echo "   sudo systemctl status agriha-linebot"
    echo ""
    echo "4. Verify:"
    echo "   curl https://${DOMAIN}/health"
else
    #---------------------------------------------------------------------------
    # Code update (git pull + pip + restart)
    #---------------------------------------------------------------------------
    echo -e "\n${GREEN}[2] git pull...${NC}"
    cd "${REPO_ROOT}"
    sudo -u "$(stat -c '%U' .git)" git pull

    echo -e "\n${GREEN}[3] pip install (diff)...${NC}"
    cd "${VPS_SRC}"
    if [[ ! -d .venv ]]; then
        echo -e "${YELLOW}No .venv found. Creating (run --setup for full initial setup)...${NC}"
        python3 -m venv .venv
        .venv/bin/pip install --upgrade pip
    fi
    .venv/bin/pip install -q -r requirements.txt

    echo -e "\n${GREEN}[4] Restarting service...${NC}"
    systemctl restart agriha-linebot

    echo -e "\n${GREEN}[5] Status check...${NC}"
    systemctl status agriha-linebot --no-pager -l || true
fi

echo -e "\n${GREEN}=== Deploy complete ===${NC}"
