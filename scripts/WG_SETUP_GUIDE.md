# WireGuard VPN Setup Guide

## Overview

This guide explains how to set up a WireGuard VPN to connect:
- **Sakura Cloud VPS** (relay server) - 10.10.0.1
- **Arsprout** (Raspberry Pi + UniPi, behind Starlink) - 10.10.0.10
- **Development PC** - 10.10.0.100

```
                    Internet
                       |
           +-----------+-----------+
           |                       |
    Starlink NAT              Your ISP
           |                       |
    +------+------+         +------+------+
    |  Arsprout   |         |   Dev PC    |
    | 10.10.0.10  |         | 10.10.0.100 |
    +------+------+         +------+------+
           |                       |
           |   WireGuard VPN       |
           |   10.10.0.0/24        |
           |                       |
           +----------+------------+
                      |
              +-------+-------+
              | Sakura Cloud  |
              |   10.10.0.1   |
              | (Public IP)   |
              +---------------+
```

## Prerequisites

- Sakura Cloud VPS with a public IP address
- SSH access to all machines
- Root or sudo access

## Step 1: Server Setup (Sakura Cloud VPS)

### 1.1 Upload the setup script

```bash
# From your local machine
scp wg_server_setup.sh root@YOUR_SERVER_IP:/root/
```

### 1.2 Run the setup script

```bash
# SSH into the server
ssh root@YOUR_SERVER_IP

# Make executable and run
chmod +x /root/wg_server_setup.sh
/root/wg_server_setup.sh
```

The script will:
1. Install WireGuard
2. Generate all keys (server + 2 clients)
3. Configure wg0 interface
4. Enable IP forwarding
5. Configure firewall (UFW or iptables)
6. Start WireGuard service

### 1.3 Verify server is running

```bash
# Check WireGuard status
wg show wg0

# Expected output:
# interface: wg0
#   public key: <server_public_key>
#   private key: (hidden)
#   listening port: 51820
```

### 1.4 Note the generated keys

The script creates client configs at:
- `/etc/wireguard/keys/arsprout_wg0.conf`
- `/etc/wireguard/keys/devpc_wg0.conf`

You'll need these for the next steps.

## Step 2: Arsprout Setup (Raspberry Pi)

### Option A: One-liner (Recommended)

SSH into Arsprout and run this one-liner (update values first):

```bash
sudo bash -c '
SERVER_IP="YOUR_SERVER_IP"
SERVER_PUBKEY="YOUR_SERVER_PUBLIC_KEY"
CLIENT_PRIVKEY="YOUR_ARSPROUT_PRIVATE_KEY"
PSK="YOUR_PRESHARED_KEY"

apt-get update && apt-get install -y wireguard wireguard-tools
cat > /etc/wireguard/wg0.conf << WGCONF
[Interface]
Address = 10.10.0.10/24
PrivateKey = ${CLIENT_PRIVKEY}

[Peer]
PublicKey = ${SERVER_PUBKEY}
PresharedKey = ${PSK}
Endpoint = ${SERVER_IP}:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
WGCONF

chmod 600 /etc/wireguard/wg0.conf
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0
wg show wg0
'
```

### Option B: Copy config file

1. Copy the config from server:
```bash
# On server
cat /etc/wireguard/keys/arsprout_wg0.conf
```

2. SSH to Arsprout and create the config:
```bash
sudo apt update && sudo apt install -y wireguard wireguard-tools
sudo nano /etc/wireguard/wg0.conf
# Paste the config content

sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
```

### Verify Arsprout connection

```bash
# On Arsprout
wg show wg0
ping 10.10.0.1  # Should reach the server
```

## Step 3: Development PC Setup

### Windows

1. Download WireGuard from https://www.wireguard.com/install/
2. Open WireGuard application
3. Click "Add Tunnel" > "Add empty tunnel..."
4. Paste the config from `/etc/wireguard/keys/devpc_wg0.conf`
5. Update `Endpoint` with actual server IP if needed
6. Click "Activate"

### macOS

1. Install WireGuard from App Store
2. Open WireGuard application
3. Click "+" > "Add Empty Tunnel..."
4. Paste the config
5. Click "Activate"

### Linux

```bash
sudo apt install wireguard wireguard-tools

# Copy the config from server
sudo nano /etc/wireguard/wg0.conf
# Paste content from devpc_wg0.conf

sudo chmod 600 /etc/wireguard/wg0.conf
sudo wg-quick up wg0

# Enable on boot (optional)
sudo systemctl enable wg-quick@wg0
```

### Verify Dev PC connection

```bash
# Check status
wg show

# Ping server
ping 10.10.0.1

# Ping Arsprout (if it's connected)
ping 10.10.0.10

# SSH to Arsprout through VPN
ssh pi@10.10.0.10
```

## Verification Commands

### On Server

```bash
# Show all connected peers
wg show wg0

# Check if both clients are connected (look for "latest handshake")
wg show wg0 | grep -A 3 "peer"
```

### On Any Client

```bash
# Show VPN status
wg show

# Test connectivity to server
ping 10.10.0.1

# Test connectivity between clients
# From Dev PC:
ping 10.10.0.10

# From Arsprout:
ping 10.10.0.100
```

## Troubleshooting

### Problem: Cannot establish handshake

1. **Check firewall on server**
```bash
# On server
ufw status
# Should show 51820/udp ALLOW
```

2. **Verify keys match**
```bash
# On server, check client's public key
grep PublicKey /etc/wireguard/wg0.conf

# On client, check private key generates matching public key
wg pubkey < /etc/wireguard/keys/your_private.key
```

3. **Check endpoint is reachable**
```bash
# From client
nc -zuv YOUR_SERVER_IP 51820
```

### Problem: Handshake succeeds but no ping

1. **Check IP forwarding on server**
```bash
cat /proc/sys/net/ipv4/ip_forward
# Should be 1
```

2. **Check iptables rules**
```bash
iptables -L -n -v
iptables -t nat -L -n -v
```

### Problem: Arsprout disconnects frequently

1. **Starlink has aggressive NAT**
   - Ensure `PersistentKeepalive = 25` is set
   - Consider reducing to 15 if still unstable

2. **Check Arsprout logs**
```bash
journalctl -u wg-quick@wg0 -f
```

### Problem: Dev PC works but Arsprout doesn't

1. **Arsprout might be offline**
   - Check on server: `wg show wg0`
   - Look for "latest handshake" - if old or missing, Arsprout is disconnected

2. **Restart WireGuard on Arsprout**
```bash
sudo systemctl restart wg-quick@wg0
```

## Security Notes

1. **Keep private keys secret**
   - Never share or commit private keys to git
   - Keys are stored in `/etc/wireguard/keys/` with 600 permissions

2. **Use preshared keys**
   - Adds post-quantum security
   - Already configured by the setup script

3. **Firewall best practices**
   - Only allow UDP 51820 from anywhere
   - Limit SSH access to VPN subnet if possible

## File Locations

### Server
```
/etc/wireguard/wg0.conf              # Server config
/etc/wireguard/keys/                  # All keys
/etc/wireguard/keys/arsprout_wg0.conf # Ready-to-use client config
/etc/wireguard/keys/devpc_wg0.conf    # Ready-to-use client config
```

### Clients
```
/etc/wireguard/wg0.conf              # Client config
```

## Quick Reference

| Device | VPN IP | Role |
|--------|--------|------|
| Sakura Cloud VPS | 10.10.0.1 | Server (relay) |
| Arsprout (Pi) | 10.10.0.10 | Client (IoT) |
| Dev PC | 10.10.0.100 | Client (development) |

| Port | Protocol | Purpose |
|------|----------|---------|
| 51820 | UDP | WireGuard |

## Maintenance

### Regenerate a client's keys

If a client's private key is compromised:

```bash
# On server
cd /etc/wireguard/keys

# Generate new keys
wg genkey | tee newclient_private.key | wg pubkey > newclient_public.key

# Update server config with new public key
nano /etc/wireguard/wg0.conf

# Restart WireGuard
systemctl restart wg-quick@wg0
```

### Add a new client

1. Generate keys on server:
```bash
wg genkey | tee /etc/wireguard/keys/newclient_private.key | wg pubkey > /etc/wireguard/keys/newclient_public.key
wg genpsk > /etc/wireguard/keys/newclient_psk.key
```

2. Add peer to server config:
```bash
cat >> /etc/wireguard/wg0.conf << EOF

[Peer]
PublicKey = $(cat /etc/wireguard/keys/newclient_public.key)
PresharedKey = $(cat /etc/wireguard/keys/newclient_psk.key)
AllowedIPs = 10.10.0.NEW_IP/32
EOF
```

3. Apply changes:
```bash
wg syncconf wg0 <(wg-quick strip wg0)
```

### Remove a client

1. Remove the `[Peer]` block from `/etc/wireguard/wg0.conf`
2. Apply: `wg syncconf wg0 <(wg-quick strip wg0)`
