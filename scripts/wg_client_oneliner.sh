#!/bin/bash
#===============================================================================
# WireGuard Client One-Liner Generator for Greenhouse (Raspbian/Debian)
#
# This script generates a one-liner that can be copied and pasted on greenhouse
# to set up WireGuard client automatically.
#
# Usage:
#   1. Run this script on the server after wg_server_setup.sh
#   2. Copy the generated one-liner
#   3. Paste and execute on greenhouse (Raspberry Pi)
#
# Or use the pre-configured one-liner at the bottom with your own keys.
#===============================================================================

set -e

KEY_DIR="/etc/wireguard/keys"
CONFIG_FILE="$KEY_DIR/greenhouse_wg0.conf"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  WireGuard Client One-Liner Generator${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if config exists
if [ -f "$CONFIG_FILE" ]; then
    echo -e "${YELLOW}Found existing greenhouse config. Generating one-liner...${NC}\n"

    # Read config content and escape for shell
    CONFIG_CONTENT=$(cat "$CONFIG_FILE" | base64 -w 0)

    echo -e "${GREEN}=== ONE-LINER FOR GREENHOUSE ===${NC}"
    echo -e "${YELLOW}Copy and paste this entire command on greenhouse:${NC}\n"

    cat << 'ONELINER_START'
# WireGuard Setup One-Liner for Greenhouse (Raspberry Pi / Debian)
# Run as root or with sudo

ONELINER_START

    echo "curl -fsSL https://raw.githubusercontent.com/your-repo/wg-setup.sh | bash -s -- '${CONFIG_CONTENT}'"

    echo -e "\n${YELLOW}Or use this self-contained version:${NC}\n"
fi

#-------------------------------------------------------------------------------
# Generate self-contained one-liner (no external dependencies)
#-------------------------------------------------------------------------------

echo -e "${GREEN}=== SELF-CONTAINED ONE-LINER ===${NC}"
echo -e "${YELLOW}This version works offline - just update the CONFIG variable:${NC}\n"

cat << 'EOF'
# ============================================================================
# WireGuard Client Setup One-Liner for Greenhouse (Raspbian/Debian)
# ============================================================================
#
# INSTRUCTIONS:
# 1. Replace the CONFIG section below with your actual configuration
# 2. Copy the entire block (from sudo bash to the final EOF)
# 3. Paste into terminal on greenhouse and press Enter
#
# ============================================================================

sudo bash -c '
# --- Configuration (EDIT THESE VALUES) ---
SERVER_IP="YOUR_SERVER_IP"          # Sakura Cloud VPS IP
SERVER_PORT="51820"                  # WireGuard port
SERVER_PUBKEY="YOUR_SERVER_PUBLIC_KEY"
CLIENT_PRIVKEY="YOUR_GREENHOUSE_PRIVATE_KEY"
CLIENT_IP="10.10.0.10"
PSK="YOUR_PRESHARED_KEY"             # Optional but recommended
# --- End Configuration ---

set -e
echo "[1/5] Installing WireGuard..."
apt-get update && apt-get install -y wireguard wireguard-tools

echo "[2/5] Creating configuration..."
mkdir -p /etc/wireguard
cat > /etc/wireguard/wg0.conf << WGCONF
[Interface]
Address = ${CLIENT_IP}/24
PrivateKey = ${CLIENT_PRIVKEY}

[Peer]
PublicKey = ${SERVER_PUBKEY}
PresharedKey = ${PSK}
Endpoint = ${SERVER_IP}:${SERVER_PORT}
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
WGCONF

chmod 600 /etc/wireguard/wg0.conf

echo "[3/5] Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1
grep -q "net.ipv4.ip_forward" /etc/sysctl.conf || echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf

echo "[4/5] Enabling WireGuard service..."
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

echo "[5/5] Verifying connection..."
sleep 2
wg show wg0
ping -c 3 10.10.0.1 && echo "SUCCESS: VPN connection established!" || echo "WARNING: Cannot ping server. Check firewall."
'
EOF

echo ""

#-------------------------------------------------------------------------------
# Alternative: Direct config paste version
#-------------------------------------------------------------------------------

echo -e "\n${GREEN}=== ALTERNATIVE: DIRECT CONFIG VERSION ===${NC}"
echo -e "${YELLOW}If you have the full config file, use this simpler version:${NC}\n"

cat << 'EOF'
# ============================================================================
# Direct Config Paste Version
# ============================================================================
# Paste your wg0.conf content where indicated

sudo bash -c '
apt-get update && apt-get install -y wireguard wireguard-tools

cat > /etc/wireguard/wg0.conf << "WGCONF"
# PASTE YOUR CONFIG HERE - example:
[Interface]
Address = 10.10.0.10/24
PrivateKey = YOUR_PRIVATE_KEY_HERE

[Peer]
PublicKey = YOUR_SERVER_PUBLIC_KEY_HERE
PresharedKey = YOUR_PSK_HERE
Endpoint = YOUR_SERVER_IP:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
WGCONF

chmod 600 /etc/wireguard/wg0.conf
systemctl enable wg-quick@wg0 && systemctl start wg-quick@wg0
wg show wg0
'
EOF

echo ""

#-------------------------------------------------------------------------------
# Quick copy version with actual config from server
#-------------------------------------------------------------------------------

if [ -f "$CONFIG_FILE" ]; then
    echo -e "\n${GREEN}=== READY-TO-USE ONE-LINER (with your server's config) ===${NC}"
    echo -e "${YELLOW}This includes your actual keys from the server setup:${NC}\n"

    # Extract values from config
    PRIVKEY=$(grep "PrivateKey" "$CONFIG_FILE" | awk '{print $3}')
    PUBKEY=$(grep "PublicKey" "$CONFIG_FILE" | awk '{print $3}')
    PSK=$(grep "PresharedKey" "$CONFIG_FILE" | awk '{print $3}')
    ENDPOINT=$(grep "Endpoint" "$CONFIG_FILE" | awk '{print $3}')

    echo "sudo bash -c '"
    echo "apt-get update && apt-get install -y wireguard wireguard-tools"
    echo "cat > /etc/wireguard/wg0.conf << \"WGCONF\""
    cat "$CONFIG_FILE"
    echo "WGCONF"
    echo "chmod 600 /etc/wireguard/wg0.conf"
    echo "systemctl enable wg-quick@wg0 && systemctl start wg-quick@wg0"
    echo "sleep 2 && wg show wg0"
    echo "'"
fi

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  One-liner generation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
