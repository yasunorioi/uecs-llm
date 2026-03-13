#!/bin/bash
#===============================================================================
# WireGuard Server Setup Script for Sakura Cloud VPS
#
# Usage: sudo bash wg_server_setup.sh
#
# This script will:
#   1. Install WireGuard
#   2. Generate server keys
#   3. Generate client keys (Arsprout, Dev PC)
#   4. Configure wg0 interface
#   5. Enable IP forwarding
#   6. Configure firewall (UFW)
#   7. Enable WireGuard service
#===============================================================================

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Network configuration
VPN_SUBNET="10.10.0.0/24"
SERVER_VPN_IP="10.10.0.1"
ARSPROUT_VPN_IP="10.10.0.10"
DEVPC_VPN_IP="10.10.0.100"
WG_PORT="51820"
WG_INTERFACE="wg0"

# Key directory
KEY_DIR="/etc/wireguard/keys"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  WireGuard Server Setup Script${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}Error: This script must be run as root${NC}"
   exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    echo -e "${RED}Error: Cannot detect OS${NC}"
    exit 1
fi

echo -e "${YELLOW}Detected OS: $OS${NC}"

#-------------------------------------------------------------------------------
# Step 1: Install WireGuard
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[1/7] Installing WireGuard...${NC}"

case $OS in
    ubuntu|debian)
        apt-get update
        apt-get install -y wireguard wireguard-tools qrencode
        ;;
    centos|rhel|rocky|almalinux)
        dnf install -y epel-release
        dnf install -y wireguard-tools qrencode
        ;;
    *)
        echo -e "${RED}Error: Unsupported OS: $OS${NC}"
        exit 1
        ;;
esac

echo -e "${GREEN}WireGuard installed successfully${NC}"

#-------------------------------------------------------------------------------
# Step 2: Generate Server Keys
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[2/7] Generating server keys...${NC}"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

# Server keys
wg genkey | tee "$KEY_DIR/server_private.key" | wg pubkey > "$KEY_DIR/server_public.key"
chmod 600 "$KEY_DIR/server_private.key"

SERVER_PRIVATE_KEY=$(cat "$KEY_DIR/server_private.key")
SERVER_PUBLIC_KEY=$(cat "$KEY_DIR/server_public.key")

echo -e "${GREEN}Server keys generated${NC}"

#-------------------------------------------------------------------------------
# Step 3: Generate Client Keys
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[3/7] Generating client keys...${NC}"

# Arsprout keys
wg genkey | tee "$KEY_DIR/arsprout_private.key" | wg pubkey > "$KEY_DIR/arsprout_public.key"
chmod 600 "$KEY_DIR/arsprout_private.key"

ARSPROUT_PRIVATE_KEY=$(cat "$KEY_DIR/arsprout_private.key")
ARSPROUT_PUBLIC_KEY=$(cat "$KEY_DIR/arsprout_public.key")

# Dev PC keys
wg genkey | tee "$KEY_DIR/devpc_private.key" | wg pubkey > "$KEY_DIR/devpc_public.key"
chmod 600 "$KEY_DIR/devpc_private.key"

DEVPC_PRIVATE_KEY=$(cat "$KEY_DIR/devpc_private.key")
DEVPC_PUBLIC_KEY=$(cat "$KEY_DIR/devpc_public.key")

# Generate preshared keys for extra security
wg genpsk > "$KEY_DIR/arsprout_psk.key"
wg genpsk > "$KEY_DIR/devpc_psk.key"
chmod 600 "$KEY_DIR/arsprout_psk.key" "$KEY_DIR/devpc_psk.key"

ARSPROUT_PSK=$(cat "$KEY_DIR/arsprout_psk.key")
DEVPC_PSK=$(cat "$KEY_DIR/devpc_psk.key")

echo -e "${GREEN}Client keys generated${NC}"

#-------------------------------------------------------------------------------
# Step 4: Configure WireGuard Server
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[4/7] Configuring WireGuard server...${NC}"

# Detect default network interface
DEFAULT_IFACE=$(ip route | grep default | awk '{print $5}' | head -1)
echo -e "${YELLOW}Default interface: $DEFAULT_IFACE${NC}"

cat > /etc/wireguard/${WG_INTERFACE}.conf << EOF
# WireGuard Server Configuration
# Generated: $(date)

[Interface]
Address = ${SERVER_VPN_IP}/24
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIVATE_KEY}

# Enable NAT for VPN clients
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -o ${DEFAULT_IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -o ${DEFAULT_IFACE} -j MASQUERADE

# Save config on shutdown
SaveConfig = false

#-------------------------------------------------------------------------------
# Peer: Arsprout (Raspberry Pi + UniPi, Starlink)
#-------------------------------------------------------------------------------
[Peer]
# Arsprout - Production IoT Device
PublicKey = ${ARSPROUT_PUBLIC_KEY}
PresharedKey = ${ARSPROUT_PSK}
AllowedIPs = ${ARSPROUT_VPN_IP}/32
# PersistentKeepalive is set by client (behind NAT/Starlink)

#-------------------------------------------------------------------------------
# Peer: Development PC
#-------------------------------------------------------------------------------
[Peer]
# Dev PC - Development Machine
PublicKey = ${DEVPC_PUBLIC_KEY}
PresharedKey = ${DEVPC_PSK}
AllowedIPs = ${DEVPC_VPN_IP}/32
# PersistentKeepalive is set by client if behind NAT
EOF

chmod 600 /etc/wireguard/${WG_INTERFACE}.conf

echo -e "${GREEN}WireGuard server configured${NC}"

#-------------------------------------------------------------------------------
# Step 5: Enable IP Forwarding
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[5/7] Enabling IP forwarding...${NC}"

# Enable immediately
sysctl -w net.ipv4.ip_forward=1

# Make persistent
if grep -q "^net.ipv4.ip_forward" /etc/sysctl.conf; then
    sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward = 1/' /etc/sysctl.conf
else
    echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf
fi

# Also add to sysctl.d for reliability
cat > /etc/sysctl.d/99-wireguard.conf << EOF
# WireGuard IP Forwarding
net.ipv4.ip_forward = 1
EOF

echo -e "${GREEN}IP forwarding enabled${NC}"

#-------------------------------------------------------------------------------
# Step 6: Configure Firewall
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[6/7] Configuring firewall...${NC}"

# Check if UFW is available
if command -v ufw &> /dev/null; then
    echo -e "${YELLOW}Configuring UFW...${NC}"

    # Allow WireGuard port
    ufw allow ${WG_PORT}/udp comment 'WireGuard VPN'

    # Allow traffic from VPN subnet
    ufw allow from ${VPN_SUBNET} comment 'WireGuard VPN subnet'

    # Enable UFW if not already
    ufw --force enable

    echo -e "${GREEN}UFW configured${NC}"
else
    echo -e "${YELLOW}UFW not found, configuring iptables directly...${NC}"

    # Allow WireGuard port
    iptables -A INPUT -p udp --dport ${WG_PORT} -j ACCEPT

    # Allow traffic from VPN subnet
    iptables -A INPUT -s ${VPN_SUBNET} -j ACCEPT
    iptables -A FORWARD -s ${VPN_SUBNET} -j ACCEPT
    iptables -A FORWARD -d ${VPN_SUBNET} -j ACCEPT

    # Save iptables rules
    if command -v iptables-save &> /dev/null; then
        iptables-save > /etc/iptables.rules
        echo -e "${YELLOW}Note: Add 'iptables-restore < /etc/iptables.rules' to /etc/rc.local for persistence${NC}"
    fi

    echo -e "${GREEN}iptables configured${NC}"
fi

#-------------------------------------------------------------------------------
# Step 7: Enable and Start WireGuard
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}[7/7] Enabling WireGuard service...${NC}"

systemctl enable wg-quick@${WG_INTERFACE}
systemctl start wg-quick@${WG_INTERFACE}

echo -e "${GREEN}WireGuard service started${NC}"

#-------------------------------------------------------------------------------
# Generate Client Configurations
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}Generating client configuration files...${NC}"

# Get server's public IP
SERVER_PUBLIC_IP=$(curl -s https://api.ipify.org || echo "YOUR_SERVER_IP")

# Arsprout client config
cat > "$KEY_DIR/arsprout_wg0.conf" << EOF
# WireGuard Client Configuration - Arsprout (Raspberry Pi)
# Generated: $(date)
# Copy this to /etc/wireguard/wg0.conf on Arsprout

[Interface]
Address = ${ARSPROUT_VPN_IP}/24
PrivateKey = ${ARSPROUT_PRIVATE_KEY}
# Optional: DNS = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey = ${SERVER_PUBLIC_KEY}
PresharedKey = ${ARSPROUT_PSK}
Endpoint = ${SERVER_PUBLIC_IP}:${WG_PORT}
AllowedIPs = ${VPN_SUBNET}
# Keep connection alive (important for Starlink/NAT)
PersistentKeepalive = 25
EOF

chmod 600 "$KEY_DIR/arsprout_wg0.conf"

# Dev PC client config
cat > "$KEY_DIR/devpc_wg0.conf" << EOF
# WireGuard Client Configuration - Development PC
# Generated: $(date)
#
# Windows: Import this in WireGuard app
# macOS: Import this in WireGuard app
# Linux: Copy to /etc/wireguard/wg0.conf

[Interface]
Address = ${DEVPC_VPN_IP}/24
PrivateKey = ${DEVPC_PRIVATE_KEY}
# Optional: DNS = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey = ${SERVER_PUBLIC_KEY}
PresharedKey = ${DEVPC_PSK}
Endpoint = ${SERVER_PUBLIC_IP}:${WG_PORT}
AllowedIPs = ${VPN_SUBNET}
# Keep connection alive if behind NAT
PersistentKeepalive = 25
EOF

chmod 600 "$KEY_DIR/devpc_wg0.conf"

#-------------------------------------------------------------------------------
# Summary
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"

echo -e "\n${YELLOW}Server Information:${NC}"
echo -e "  VPN IP: ${SERVER_VPN_IP}"
echo -e "  Public IP: ${SERVER_PUBLIC_IP}"
echo -e "  Port: ${WG_PORT}/UDP"
echo -e "  Public Key: ${SERVER_PUBLIC_KEY}"

echo -e "\n${YELLOW}Client Configurations:${NC}"
echo -e "  Arsprout: ${KEY_DIR}/arsprout_wg0.conf"
echo -e "  Dev PC:   ${KEY_DIR}/devpc_wg0.conf"

echo -e "\n${YELLOW}Key Files:${NC}"
echo -e "  ${KEY_DIR}/"

echo -e "\n${YELLOW}VPN Status:${NC}"
wg show ${WG_INTERFACE}

echo -e "\n${YELLOW}Next Steps:${NC}"
echo -e "1. Copy ${KEY_DIR}/arsprout_wg0.conf to Arsprout"
echo -e "2. Copy ${KEY_DIR}/devpc_wg0.conf to your dev PC"
echo -e "3. Update Endpoint if ${SERVER_PUBLIC_IP} is not correct"

echo -e "\n${GREEN}Generate QR codes for mobile clients:${NC}"
echo -e "  qrencode -t ansiutf8 < ${KEY_DIR}/devpc_wg0.conf"

# Display QR code for Arsprout if qrencode is available
if command -v qrencode &> /dev/null; then
    echo -e "\n${YELLOW}QR Code for Arsprout:${NC}"
    qrencode -t ansiutf8 < "$KEY_DIR/arsprout_wg0.conf"
fi
