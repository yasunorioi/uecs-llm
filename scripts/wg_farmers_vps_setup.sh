#!/bin/bash
#===============================================================================
# wg_farmers_vps_setup.sh - VPS WireGuard server setup for farmer RPi connections
#
# Usage: sudo bash wg_farmers_vps_setup.sh
#
# Sets up wg-farmers interface on VPS.
# Reads settings from config/vps.conf if available, otherwise uses defaults.
#
# Generated files:
#   /etc/wireguard/wg-farmers.conf  : farmer RPi WG server
#
# Design: docs/multi_farmer_design.md section 3
#===============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

#-------------------------------------------------------------------------------
# Load vps.conf
#-------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VPS_CONF="${SCRIPT_DIR}/../config/vps.conf"

if [[ -f "${VPS_CONF}" ]]; then
    # shellcheck source=../config/vps.conf
    source "${VPS_CONF}"
fi

# Use vps.conf values (fallback to defaults)
FARMERS_WG_IF="${WG_INTERFACE:-wg-farmers}"
FARMERS_WG_PORT="${WG_PORT:-51821}"
FARMERS_WG_IP="${WG_SERVER_IP:-10.20.0.1/24}"
FARMERS_QR_DIR="${QR_DIR:-/var/www/qr}"

#-------------------------------------------------------------------------------
# Root check
#-------------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: run with sudo${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  VPS Farmer WG Server Setup${NC}"
echo -e "${GREEN}========================================${NC}"

#-------------------------------------------------------------------------------
# WireGuard install check
#-------------------------------------------------------------------------------
if ! command -v wg &>/dev/null; then
    echo -e "\n${YELLOW}WireGuard not installed. Installing...${NC}"
    apt-get update && apt-get install -y wireguard wireguard-tools
fi

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

#-------------------------------------------------------------------------------
# [1/3] Generate wg-farmers server keypair
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1/3] Generating wg-farmers keypair...${NC}"

FARMERS_KEY_FILE="/etc/wireguard/wg-farmers-private.key"
FARMERS_PUB_FILE="/etc/wireguard/wg-farmers-public.key"

if [[ -f "$FARMERS_KEY_FILE" ]]; then
    echo -e "${YELLOW}Existing wg-farmers key found. Reusing.${NC}"
    FARMERS_PRIVATE_KEY=$(cat "$FARMERS_KEY_FILE")
    FARMERS_PUBLIC_KEY=$(cat "$FARMERS_PUB_FILE")
else
    FARMERS_PRIVATE_KEY=$(wg genkey)
    echo "$FARMERS_PRIVATE_KEY" > "$FARMERS_KEY_FILE"
    FARMERS_PUBLIC_KEY=$(echo "$FARMERS_PRIVATE_KEY" | wg pubkey)
    echo "$FARMERS_PUBLIC_KEY" > "$FARMERS_PUB_FILE"
    chmod 600 "$FARMERS_KEY_FILE"
fi

echo -e "${GREEN}wg-farmers public key: ${FARMERS_PUBLIC_KEY}${NC}"

#-------------------------------------------------------------------------------
# [2/3] Generate wg-farmers.conf
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[2/3] Generating /etc/wireguard/wg-farmers.conf...${NC}"

FARMERS_CONF="/etc/wireguard/wg-farmers.conf"

if [[ -f "$FARMERS_CONF" ]]; then
    echo -e "${YELLOW}Existing wg-farmers.conf found.${NC}"
    echo "  Backing up, updating [Interface] only (preserving [Peer] sections)"
    cp "$FARMERS_CONF" "${FARMERS_CONF}.bak.$(date +%Y%m%d%H%M%S)"
    EXISTING_PEERS=$(awk '/^\[Peer\]/,0' "$FARMERS_CONF")
else
    EXISTING_PEERS=""
fi

cat > "$FARMERS_CONF" << EOF
# WireGuard farmer VPN server (wg-farmers)
# Generated: $(date)

[Interface]
Address = ${FARMERS_WG_IP}
ListenPort = ${FARMERS_WG_PORT}
PrivateKey = ${FARMERS_PRIVATE_KEY}

# iptables FORWARD for wg-farmers clients
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT

# Farmer peers are added automatically via onboarding.py register_pubkey
EOF

if [[ -n "$EXISTING_PEERS" ]]; then
    echo "$EXISTING_PEERS" >> "$FARMERS_CONF"
fi

chmod 600 "$FARMERS_CONF"
echo -e "${GREEN}wg-farmers.conf generated${NC}"

#-------------------------------------------------------------------------------
# [3/3] Firewall + start + systemd enable
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[3/3] Firewall + WG start...${NC}"

# Open port if UFW is available
if command -v ufw &>/dev/null; then
    ufw allow "${FARMERS_WG_PORT}/udp" comment "WireGuard wg-farmers"
    echo -e "${GREEN}UFW: ${FARMERS_WG_PORT}/udp allowed${NC}"
fi

# Start wg-farmers
if wg show "$FARMERS_WG_IF" &>/dev/null 2>&1; then
    echo -e "${YELLOW}wg-farmers already running. Restarting.${NC}"
    wg-quick down "$FARMERS_WG_IF" 2>/dev/null || true
fi
wg-quick up "$FARMERS_WG_IF"

# systemd persist
systemctl enable "wg-quick@${FARMERS_WG_IF}"
echo -e "${GREEN}wg-farmers started + systemd enabled${NC}"

#-------------------------------------------------------------------------------
# QR image directory
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}Creating QR image directory...${NC}"
mkdir -p "${FARMERS_QR_DIR}"
chown www-data:www-data "${FARMERS_QR_DIR}"
chmod 755 "${FARMERS_QR_DIR}"
echo -e "${GREEN}${FARMERS_QR_DIR} created${NC}"

#-------------------------------------------------------------------------------
# Summary
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}========================================${NC}"

echo -e "\n${YELLOW}wg-farmers info:${NC}"
echo "  Interface IP:  ${FARMERS_WG_IP}"
echo "  Listen port:   ${FARMERS_WG_PORT}/UDP"
echo "  Public key:    ${FARMERS_PUBLIC_KEY}"
echo "  Config file:   /etc/wireguard/wg-farmers.conf"

echo -e "\n${YELLOW}Next steps:${NC}"
echo "1. LINE Bot + nginx + systemd setup:"
echo "   sudo bash scripts/deploy_vps_linebot.sh --setup"
echo "2. Set WG public key in .env:"
echo "   WG_SERVER_PUBLIC_KEY=${FARMERS_PUBLIC_KEY}"
echo "   WG_SERVER_ENDPOINT=$(hostname -I | awk '{print $1}'):${FARMERS_WG_PORT}"
echo ""
echo "3. Check WG status:"
echo "   sudo wg show wg-farmers"
