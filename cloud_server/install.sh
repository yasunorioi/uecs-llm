#!/bin/bash
#===============================================================================
# UniPi Agri HA - クラウドサーバー ワンライナーインストーラー
#
# 使用方法:
#   curl -fsSL https://raw.githubusercontent.com/xxx/install.sh | bash
#   または
#   wget -qO- https://raw.githubusercontent.com/xxx/install.sh | bash
#
# インストールされるもの:
#   - WireGuard VPNサーバー
#   - InfluxDB（センサーデータ長期保存）
#   - Grafana（可視化）
#   - セットアップUI（Node.js）
#===============================================================================

set -e

# カラー出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

INSTALL_DIR="/opt/unipi-agri-cloud"
SETUP_PORT=8080
WG_PORT=31820

echo -e "${GREEN}"
echo "╔════════════════════════════════════════════════════════════╗"
echo "║     UniPi Agri HA - Cloud Server Installer                 ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

#-------------------------------------------------------------------------------
# 前提条件チェック
#-------------------------------------------------------------------------------
echo -e "${YELLOW}[1/6] Checking prerequisites...${NC}"

# root権限チェック
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: Please run as root (sudo)${NC}"
    exit 1
fi

# OS検出
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    echo -e "${RED}Error: Cannot detect OS${NC}"
    exit 1
fi

echo "  OS: $OS"
echo "  Install directory: $INSTALL_DIR"

#-------------------------------------------------------------------------------
# パッケージインストール
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[2/6] Installing packages...${NC}"

case $OS in
    ubuntu|debian)
        apt-get update
        apt-get install -y \
            wireguard \
            wireguard-tools \
            docker.io \
            docker-compose \
            nodejs \
            npm \
            curl \
            jq \
            qrencode
        ;;
    centos|rhel|rocky|almalinux)
        dnf install -y epel-release
        dnf install -y \
            wireguard-tools \
            docker \
            docker-compose \
            nodejs \
            npm \
            curl \
            jq \
            qrencode
        systemctl enable --now docker
        ;;
    *)
        echo -e "${RED}Error: Unsupported OS: $OS${NC}"
        exit 1
        ;;
esac

echo -e "${GREEN}  Packages installed${NC}"

#-------------------------------------------------------------------------------
# ディレクトリ作成
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[3/6] Creating directories...${NC}"

mkdir -p "$INSTALL_DIR"/{config,data,setup-ui}
mkdir -p /etc/wireguard/keys
chmod 700 /etc/wireguard/keys

#-------------------------------------------------------------------------------
# WireGuard設定
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[4/6] Configuring WireGuard...${NC}"

# サーバーキー生成
if [ ! -f /etc/wireguard/keys/server_private.key ]; then
    wg genkey | tee /etc/wireguard/keys/server_private.key | wg pubkey > /etc/wireguard/keys/server_public.key
    chmod 600 /etc/wireguard/keys/server_private.key
fi

SERVER_PRIVATE_KEY=$(cat /etc/wireguard/keys/server_private.key)
SERVER_PUBLIC_KEY=$(cat /etc/wireguard/keys/server_public.key)

# サーバー設定
cat > /etc/wireguard/wg0.conf << EOF
# UniPi Agri HA - WireGuard Server
# Generated: $(date)

[Interface]
Address = 10.10.0.1/24
ListenPort = $WG_PORT
PrivateKey = $SERVER_PRIVATE_KEY

# IP forwarding
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

# Clients will be added below by setup UI
EOF

chmod 600 /etc/wireguard/wg0.conf

# IP forwarding有効化
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-wireguard.conf
sysctl -p /etc/sysctl.d/99-wireguard.conf

# WireGuard起動
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0 || true

echo -e "${GREEN}  WireGuard configured${NC}"
echo "  Server Public Key: $SERVER_PUBLIC_KEY"

#-------------------------------------------------------------------------------
# Docker Compose設定
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[5/6] Setting up Docker services...${NC}"

cat > "$INSTALL_DIR/docker-compose.yaml" << 'DOCKER_EOF'
version: '3.8'

services:
  influxdb:
    image: influxdb:2.7
    container_name: influxdb
    restart: unless-stopped
    ports:
      - "8086:8086"
    volumes:
      - ./data/influxdb:/var/lib/influxdb2
    environment:
      - DOCKER_INFLUXDB_INIT_MODE=setup
      - DOCKER_INFLUXDB_INIT_USERNAME=admin
      - DOCKER_INFLUXDB_INIT_PASSWORD=CHANGE_ME_PLACEHOLDER
      - DOCKER_INFLUXDB_INIT_ORG=unipi-agri
      - DOCKER_INFLUXDB_INIT_BUCKET=sensors

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    volumes:
      - ./data/grafana:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=CHANGE_ME_PLACEHOLDER
      - GF_USERS_ALLOW_SIGN_UP=false
    depends_on:
      - influxdb
DOCKER_EOF

echo -e "${GREEN}  Docker Compose configured${NC}"

#-------------------------------------------------------------------------------
# セットアップUI
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[6/6] Installing Setup UI...${NC}"

# package.json
cat > "$INSTALL_DIR/setup-ui/package.json" << 'EOF'
{
  "name": "unipi-agri-cloud-setup",
  "version": "1.0.0",
  "scripts": {
    "start": "node server.js"
  },
  "dependencies": {
    "express": "^4.18.2",
    "body-parser": "^1.20.2"
  }
}
EOF

# セットアップUIサーバー
cat > "$INSTALL_DIR/setup-ui/server.js" << 'SERVERJS'
const express = require('express');
const bodyParser = require('body-parser');
const { execSync, exec } = require('child_process');
const fs = require('fs');
const path = require('path');

const app = express();
const PORT = process.env.SETUP_PORT || 8080;
const CONFIG_FILE = '/opt/unipi-agri-cloud/config/settings.json';
const WG_CONF = '/etc/wireguard/wg0.conf';

app.use(bodyParser.json());
app.use(express.static(path.join(__dirname, 'public')));

// 設定ファイル読み込み
function loadConfig() {
    try {
        return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
    } catch {
        return { initialized: false, clients: [] };
    }
}

// 設定ファイル保存
function saveConfig(config) {
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
}

// サーバー情報取得
app.get('/api/server-info', (req, res) => {
    try {
        const publicKey = fs.readFileSync('/etc/wireguard/keys/server_public.key', 'utf8').trim();
        const publicIP = execSync('curl -s https://api.ipify.org').toString().trim();
        const config = loadConfig();
        res.json({
            initialized: config.initialized,
            publicKey,
            publicIP,
            wgPort: 31820,
            clientCount: config.clients?.length || 0
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// 初期設定
app.post('/api/initialize', (req, res) => {
    try {
        const { adminPassword } = req.body;
        if (!adminPassword || adminPassword.length < 8) {
            return res.status(400).json({ error: 'Password must be at least 8 characters' });
        }

        // Docker Compose のパスワード更新
        const composePath = '/opt/unipi-agri-cloud/docker-compose.yaml';
        let compose = fs.readFileSync(composePath, 'utf8');
        compose = compose.replace(/CHANGE_ME_PLACEHOLDER/g, adminPassword);
        fs.writeFileSync(composePath, compose);

        // Docker起動
        execSync('cd /opt/unipi-agri-cloud && docker-compose up -d', { stdio: 'inherit' });

        // 設定保存
        const config = loadConfig();
        config.initialized = true;
        config.initializedAt = new Date().toISOString();
        saveConfig(config);

        res.json({ success: true, message: 'Initialization complete' });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// クライアント追加
app.post('/api/clients', (req, res) => {
    try {
        const { name, ipAddress } = req.body;
        if (!name || !ipAddress) {
            return res.status(400).json({ error: 'Name and IP address required' });
        }

        // キー生成
        const privateKey = execSync('wg genkey').toString().trim();
        const publicKey = execSync(`echo "${privateKey}" | wg pubkey`).toString().trim();
        const psk = execSync('wg genpsk').toString().trim();
        const serverPublicKey = fs.readFileSync('/etc/wireguard/keys/server_public.key', 'utf8').trim();
        const serverIP = execSync('curl -s https://api.ipify.org').toString().trim();

        // WireGuard設定にpeer追加
        const peerConfig = `
# Client: ${name}
[Peer]
PublicKey = ${publicKey}
PresharedKey = ${psk}
AllowedIPs = ${ipAddress}/32
`;
        fs.appendFileSync(WG_CONF, peerConfig);

        // WireGuard再読み込み
        execSync('wg syncconf wg0 <(wg-quick strip wg0)', { shell: '/bin/bash' });

        // クライアント設定生成
        const clientConfig = `[Interface]
Address = ${ipAddress}/24
PrivateKey = ${privateKey}

[Peer]
PublicKey = ${serverPublicKey}
PresharedKey = ${psk}
Endpoint = ${serverIP}:31820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25`;

        // 設定保存
        const config = loadConfig();
        config.clients = config.clients || [];
        config.clients.push({
            name,
            ipAddress,
            publicKey,
            createdAt: new Date().toISOString()
        });
        saveConfig(config);

        // QRコード生成
        let qrCode = '';
        try {
            qrCode = execSync(`echo "${clientConfig}" | qrencode -t UTF8`).toString();
        } catch {}

        res.json({
            success: true,
            client: { name, ipAddress, publicKey },
            config: clientConfig,
            qrCode
        });
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
});

// クライアント一覧
app.get('/api/clients', (req, res) => {
    const config = loadConfig();
    res.json(config.clients || []);
});

// 静的ファイル（index.html）
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
    console.log(`Setup UI running on http://0.0.0.0:${PORT}`);
});
SERVERJS

# HTML UI
mkdir -p "$INSTALL_DIR/setup-ui/public"
cat > "$INSTALL_DIR/setup-ui/public/index.html" << 'HTMLEOF'
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UniPi Agri HA - Cloud Setup</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; min-height: 100vh; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        h1 { text-align: center; margin: 40px 0; color: #4ecca3; }
        .card { background: #16213e; border-radius: 12px; padding: 24px; margin-bottom: 20px; }
        .card h2 { color: #4ecca3; margin-bottom: 16px; font-size: 1.2em; }
        .step { display: none; }
        .step.active { display: block; }
        label { display: block; margin-bottom: 8px; color: #aaa; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; border: 1px solid #333; border-radius: 8px; background: #0f0f23; color: #fff; font-size: 16px; margin-bottom: 16px; }
        input:focus { outline: none; border-color: #4ecca3; }
        button { width: 100%; padding: 14px; background: #4ecca3; color: #1a1a2e; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; }
        button:hover { background: #3db892; }
        button:disabled { background: #555; cursor: not-allowed; }
        .info-box { background: #0f0f23; border-radius: 8px; padding: 16px; margin: 16px 0; }
        .info-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #333; }
        .info-row:last-child { border-bottom: none; }
        .info-label { color: #888; }
        .info-value { color: #4ecca3; font-family: monospace; word-break: break-all; }
        .config-box { background: #0f0f23; border-radius: 8px; padding: 16px; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; }
        .qr-box { text-align: center; background: #fff; color: #000; padding: 10px; border-radius: 8px; font-family: monospace; font-size: 8px; line-height: 1; white-space: pre; }
        .success { color: #4ecca3; text-align: center; font-size: 1.2em; margin: 20px 0; }
        .error { color: #ff6b6b; margin-bottom: 16px; }
        .client-list { margin-top: 20px; }
        .client-item { background: #0f0f23; border-radius: 8px; padding: 12px; margin-bottom: 8px; display: flex; justify-content: space-between; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🌱 UniPi Agri HA</h1>

        <!-- Step 1: Initialize -->
        <div class="step active" id="step1">
            <div class="card">
                <h2>Step 1: 管理者パスワード設定</h2>
                <p style="color:#888;margin-bottom:20px;">InfluxDBとGrafanaの管理者パスワードを設定します。</p>
                <div class="info-box" id="server-info">
                    <div class="info-row"><span class="info-label">読み込み中...</span></div>
                </div>
                <label>管理者パスワード（8文字以上）</label>
                <input type="password" id="adminPassword" placeholder="********">
                <label>パスワード確認</label>
                <input type="password" id="adminPasswordConfirm" placeholder="********">
                <div class="error" id="step1-error" style="display:none;"></div>
                <button onclick="initialize()">初期化を開始</button>
            </div>
        </div>

        <!-- Step 2: Add Clients -->
        <div class="step" id="step2">
            <div class="card">
                <h2>Step 2: クライアント追加</h2>
                <p style="color:#888;margin-bottom:20px;">VPN接続するデバイスを追加します。</p>
                <div class="info-box" id="server-info-2"></div>
                <label>デバイス名</label>
                <input type="text" id="clientName" placeholder="例: greenhouse-1">
                <label>VPN IPアドレス</label>
                <input type="text" id="clientIP" placeholder="例: 10.10.0.10">
                <div class="error" id="step2-error" style="display:none;"></div>
                <button onclick="addClient()">クライアントを追加</button>

                <div class="client-list" id="client-list"></div>
            </div>
        </div>

        <!-- Step 3: Client Config -->
        <div class="step" id="step3">
            <div class="card">
                <h2>クライアント設定</h2>
                <p style="color:#888;margin-bottom:20px;">以下の設定をデバイスにコピーしてください。</p>
                <label>設定ファイル (wg0.conf)</label>
                <div class="config-box" id="client-config"></div>
                <div style="margin-top:20px;">
                    <label>QRコード</label>
                    <div class="qr-box" id="qr-code"></div>
                </div>
                <button onclick="showStep(2)" style="margin-top:20px;">別のクライアントを追加</button>
            </div>
        </div>
    </div>

    <script>
        let serverInfo = {};

        async function loadServerInfo() {
            try {
                const res = await fetch('/api/server-info');
                serverInfo = await res.json();

                const infoHtml = `
                    <div class="info-row"><span class="info-label">Public IP</span><span class="info-value">${serverInfo.publicIP}</span></div>
                    <div class="info-row"><span class="info-label">WireGuard Port</span><span class="info-value">${serverInfo.wgPort}</span></div>
                    <div class="info-row"><span class="info-label">Server Public Key</span><span class="info-value">${serverInfo.publicKey}</span></div>
                `;
                document.getElementById('server-info').innerHTML = infoHtml;
                document.getElementById('server-info-2').innerHTML = infoHtml;

                if (serverInfo.initialized) {
                    showStep(2);
                    loadClients();
                }
            } catch (err) {
                document.getElementById('server-info').innerHTML = '<div class="error">サーバー情報の取得に失敗しました</div>';
            }
        }

        async function initialize() {
            const password = document.getElementById('adminPassword').value;
            const confirm = document.getElementById('adminPasswordConfirm').value;
            const errorEl = document.getElementById('step1-error');

            if (password !== confirm) {
                errorEl.textContent = 'パスワードが一致しません';
                errorEl.style.display = 'block';
                return;
            }
            if (password.length < 8) {
                errorEl.textContent = 'パスワードは8文字以上必要です';
                errorEl.style.display = 'block';
                return;
            }

            try {
                const res = await fetch('/api/initialize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ adminPassword: password })
                });
                const data = await res.json();
                if (data.success) {
                    showStep(2);
                } else {
                    errorEl.textContent = data.error;
                    errorEl.style.display = 'block';
                }
            } catch (err) {
                errorEl.textContent = 'エラーが発生しました';
                errorEl.style.display = 'block';
            }
        }

        async function addClient() {
            const name = document.getElementById('clientName').value;
            const ip = document.getElementById('clientIP').value;
            const errorEl = document.getElementById('step2-error');

            if (!name || !ip) {
                errorEl.textContent = '全ての項目を入力してください';
                errorEl.style.display = 'block';
                return;
            }

            try {
                const res = await fetch('/api/clients', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, ipAddress: ip })
                });
                const data = await res.json();
                if (data.success) {
                    document.getElementById('client-config').textContent = data.config;
                    document.getElementById('qr-code').textContent = data.qrCode || '(QRコード生成失敗)';
                    showStep(3);
                    document.getElementById('clientName').value = '';
                    document.getElementById('clientIP').value = '';
                } else {
                    errorEl.textContent = data.error;
                    errorEl.style.display = 'block';
                }
            } catch (err) {
                errorEl.textContent = 'エラーが発生しました';
                errorEl.style.display = 'block';
            }
        }

        async function loadClients() {
            try {
                const res = await fetch('/api/clients');
                const clients = await res.json();
                const listEl = document.getElementById('client-list');
                if (clients.length > 0) {
                    listEl.innerHTML = '<h3 style="margin:20px 0 10px;color:#4ecca3;">登録済みクライアント</h3>' +
                        clients.map(c => `<div class="client-item"><span>${c.name}</span><span style="color:#888">${c.ipAddress}</span></div>`).join('');
                }
            } catch {}
        }

        function showStep(n) {
            document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
            document.getElementById('step' + n).classList.add('active');
            if (n === 2) loadClients();
        }

        loadServerInfo();
    </script>
</body>
</html>
HTMLEOF

# npm install
cd "$INSTALL_DIR/setup-ui"
npm install --production

# systemdサービス
cat > /etc/systemd/system/unipi-agri-setup.service << EOF
[Unit]
Description=UniPi Agri HA Cloud Setup UI
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/setup-ui
ExecStart=/usr/bin/node server.js
Restart=always
Environment=SETUP_PORT=$SETUP_PORT

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable unipi-agri-setup
systemctl start unipi-agri-setup

echo -e "${GREEN}  Setup UI installed${NC}"

#-------------------------------------------------------------------------------
# ファイアウォール設定
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}Configuring firewall...${NC}"

if command -v ufw &> /dev/null; then
    ufw allow $WG_PORT/udp comment 'WireGuard'
    ufw allow $SETUP_PORT/tcp comment 'Setup UI'
    ufw allow 3000/tcp comment 'Grafana'
    ufw allow 8086/tcp comment 'InfluxDB'
    ufw --force enable
fi

#-------------------------------------------------------------------------------
# 完了
#-------------------------------------------------------------------------------
PUBLIC_IP=$(curl -s https://api.ipify.org || echo "YOUR_SERVER_IP")

echo -e "\n${GREEN}"
echo "╔════════════════════════════════════════════════════════════╗"
echo "║     Installation Complete!                                 ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

echo -e "${BLUE}Next Steps:${NC}"
echo ""
echo "  1. Open Setup UI in browser:"
echo -e "     ${GREEN}http://${PUBLIC_IP}:${SETUP_PORT}${NC}"
echo ""
echo "  2. Set admin password"
echo ""
echo "  3. Add VPN clients (greenhouses)"
echo ""
echo -e "${BLUE}Services:${NC}"
echo "  - Setup UI:  http://${PUBLIC_IP}:${SETUP_PORT}"
echo "  - Grafana:   http://${PUBLIC_IP}:3000 (after init)"
echo "  - InfluxDB:  http://${PUBLIC_IP}:8086 (after init)"
echo "  - WireGuard: ${PUBLIC_IP}:${WG_PORT}/udp"
echo ""
echo -e "${BLUE}Server Public Key:${NC}"
echo "  $(cat /etc/wireguard/keys/server_public.key)"
echo ""
