#!/bin/bash
#===============================================================================
# UniPi Agri HA カスタムイメージ ビルドスクリプト
#
# 使用方法:
#   sudo ./build_image.sh <raspbian_image.img>
#
# 必要なパッケージ:
#   apt-get install qemu-user-static systemd-container
#===============================================================================

set -e

# カラー出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 設定
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MOUNT_POINT="/mnt/rpi"
BOOT_MOUNT="/mnt/rpi-boot"

# 引数チェック
if [ $# -lt 1 ]; then
    echo -e "${RED}Usage: $0 <raspbian_image.img>${NC}"
    echo "Example: $0 2024-03-15-raspios-bookworm-arm64-lite.img"
    exit 1
fi

IMAGE_FILE="$1"
if [ ! -f "$IMAGE_FILE" ]; then
    echo -e "${RED}Error: Image file not found: $IMAGE_FILE${NC}"
    exit 1
fi

# root権限チェック
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run as root${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  UniPi Agri HA Image Builder${NC}"
echo -e "${GREEN}========================================${NC}"

#-------------------------------------------------------------------------------
# イメージのマウント
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[1/8] Mounting image...${NC}"

# ループデバイスのセットアップ
LOOP_DEV=$(losetup -fP --show "$IMAGE_FILE")
echo "Loop device: $LOOP_DEV"

# マウントポイント作成
mkdir -p "$MOUNT_POINT" "$BOOT_MOUNT"

# パーティションマウント
mount "${LOOP_DEV}p2" "$MOUNT_POINT"
mount "${LOOP_DEV}p1" "$BOOT_MOUNT"

echo -e "${GREEN}Image mounted${NC}"

#-------------------------------------------------------------------------------
# クリーンアップ関数
#-------------------------------------------------------------------------------
cleanup() {
    echo -e "\n${YELLOW}Cleaning up...${NC}"
    umount "$BOOT_MOUNT" 2>/dev/null || true
    umount "$MOUNT_POINT/boot" 2>/dev/null || true
    umount "$MOUNT_POINT/proc" 2>/dev/null || true
    umount "$MOUNT_POINT/sys" 2>/dev/null || true
    umount "$MOUNT_POINT/dev/pts" 2>/dev/null || true
    umount "$MOUNT_POINT/dev" 2>/dev/null || true
    umount "$MOUNT_POINT" 2>/dev/null || true
    losetup -d "$LOOP_DEV" 2>/dev/null || true
    echo -e "${GREEN}Cleanup complete${NC}"
}
trap cleanup EXIT

#-------------------------------------------------------------------------------
# chroot準備
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[2/8] Preparing chroot environment...${NC}"

# /boot をバインドマウント
mount --bind "$BOOT_MOUNT" "$MOUNT_POINT/boot"

# システムディレクトリのバインドマウント
mount --bind /dev "$MOUNT_POINT/dev"
mount --bind /dev/pts "$MOUNT_POINT/dev/pts"
mount --bind /proc "$MOUNT_POINT/proc"
mount --bind /sys "$MOUNT_POINT/sys"

# QEMUバイナリをコピー（ARM64エミュレーション用）
cp /usr/bin/qemu-aarch64-static "$MOUNT_POINT/usr/bin/"

# DNS設定
cp /etc/resolv.conf "$MOUNT_POINT/etc/resolv.conf"

echo -e "${GREEN}Chroot environment ready${NC}"

#-------------------------------------------------------------------------------
# /boot/config.txt の設定
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[3/8] Configuring boot settings...${NC}"

# I2C有効化
if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_MOUNT/config.txt"; then
    echo "dtparam=i2c_arm=on" >> "$BOOT_MOUNT/config.txt"
fi

# SPI有効化（EVOKで必要）
if ! grep -q "^dtparam=spi=on" "$BOOT_MOUNT/config.txt"; then
    echo "dtparam=spi=on" >> "$BOOT_MOUNT/config.txt"
fi

# 1-Wire有効化（オプション）
if ! grep -q "^dtoverlay=w1-gpio" "$BOOT_MOUNT/config.txt"; then
    echo "dtoverlay=w1-gpio" >> "$BOOT_MOUNT/config.txt"
fi

# UART有効化（RS485用）
if ! grep -q "^enable_uart=1" "$BOOT_MOUNT/config.txt"; then
    echo "enable_uart=1" >> "$BOOT_MOUNT/config.txt"
fi

# SSH有効化
touch "$BOOT_MOUNT/ssh"

echo -e "${GREEN}Boot settings configured${NC}"

#-------------------------------------------------------------------------------
# 初回起動スクリプトのコピー
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[4/8] Copying first boot scripts...${NC}"

# スクリプトディレクトリ作成
mkdir -p "$MOUNT_POINT/opt/uecs-llm/scripts"

# スクリプトをコピー
cp "$PROJECT_DIR/scripts/first_boot.sh" "$MOUNT_POINT/opt/uecs-llm/scripts/"
cp "$PROJECT_DIR/scripts/install_evok.sh" "$MOUNT_POINT/opt/uecs-llm/scripts/"
cp "$PROJECT_DIR/scripts/install_docker.sh" "$MOUNT_POINT/opt/uecs-llm/scripts/"
cp "$PROJECT_DIR/scripts/backup_config.sh" "$MOUNT_POINT/opt/uecs-llm/scripts/"
chmod +x "$MOUNT_POINT/opt/uecs-llm/scripts/"*.sh

# Docker Compose設定
mkdir -p "$MOUNT_POINT/opt/uecs-llm/docker"
cp -r "$PROJECT_DIR/docker/"* "$MOUNT_POINT/opt/uecs-llm/docker/"

# 設定テンプレート
mkdir -p "$MOUNT_POINT/opt/uecs-llm/config"
cp "$PROJECT_DIR/config/"* "$MOUNT_POINT/opt/uecs-llm/config/" 2>/dev/null || true

# Node-RED Setup Wizardフロー
mkdir -p "$MOUNT_POINT/opt/uecs-llm/nodered"
if [ -d "$PROJECT_DIR/nodered" ]; then
    cp -r "$PROJECT_DIR/nodered/"* "$MOUNT_POINT/opt/uecs-llm/nodered/"
fi

echo -e "${GREEN}Scripts copied${NC}"

#-------------------------------------------------------------------------------
# systemdサービス作成（初回起動用）
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[5/8] Creating first-boot service...${NC}"

cat > "$MOUNT_POINT/etc/systemd/system/first-boot.service" << 'EOF'
[Unit]
Description=UniPi Agri HA First Boot Setup
After=network-online.target
Wants=network-online.target
ConditionPathExists=/opt/uecs-llm/.first-boot-pending

[Service]
Type=oneshot
ExecStart=/opt/uecs-llm/scripts/first_boot.sh
ExecStartPost=/bin/rm -f /opt/uecs-llm/.first-boot-pending
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

# 初回起動フラグ
touch "$MOUNT_POINT/opt/uecs-llm/.first-boot-pending"

# サービス有効化
chroot "$MOUNT_POINT" systemctl enable first-boot.service

echo -e "${GREEN}First-boot service created${NC}"

#-------------------------------------------------------------------------------
# ユーザー設定
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[6/8] Configuring user...${NC}"

# パスワードハッシュ生成
PASS_HASH=$(openssl passwd -6 "changeme123")

# /etc/passwd, /etc/shadow, /etc/group に直接追記
# UID/GID 1001 でoperatorユーザー作成
if ! grep -q "^operator:" "$MOUNT_POINT/etc/passwd"; then
    echo "operator:x:1001:1001:Operator,,,:/home/operator:/bin/bash" >> "$MOUNT_POINT/etc/passwd"
fi

if ! grep -q "^operator:" "$MOUNT_POINT/etc/group"; then
    echo "operator:x:1001:" >> "$MOUNT_POINT/etc/group"
fi

if ! grep -q "^operator:" "$MOUNT_POINT/etc/shadow"; then
    echo "operator:${PASS_HASH}:19000:0:99999:7:::" >> "$MOUNT_POINT/etc/shadow"
fi

# ホームディレクトリ作成
mkdir -p "$MOUNT_POINT/home/operator"
cp -r "$MOUNT_POINT/etc/skel/." "$MOUNT_POINT/home/operator/" 2>/dev/null || true
chroot "$MOUNT_POINT" chown -R 1001:1001 /home/operator

# sudo, i2c, dialout グループに追加
sed -i 's/^\(sudo:.*\)/\1,operator/' "$MOUNT_POINT/etc/group"
sed -i 's/^\(i2c:.*\)/\1,operator/' "$MOUNT_POINT/etc/group" 2>/dev/null || true
sed -i 's/^\(dialout:.*\)/\1,operator/' "$MOUNT_POINT/etc/group"

# sudoパスワードなし設定
echo "operator ALL=(ALL) NOPASSWD:ALL" > "$MOUNT_POINT/etc/sudoers.d/operator"
chmod 440 "$MOUNT_POINT/etc/sudoers.d/operator"

# SSH公開鍵ディレクトリ
mkdir -p "$MOUNT_POINT/home/operator/.ssh"
chmod 700 "$MOUNT_POINT/home/operator/.ssh"
chroot "$MOUNT_POINT" chown -R operator:operator /home/operator/.ssh

echo -e "${GREEN}User configured${NC}"

#-------------------------------------------------------------------------------
# 基本パッケージのインストール
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[7/8] Installing base packages...${NC}"

chroot "$MOUNT_POINT" apt-get update
chroot "$MOUNT_POINT" apt-get install -y \
    i2c-tools \
    python3-pip \
    python3-smbus \
    git \
    curl \
    wget \
    vim \
    htop \
    wireguard \
    wireguard-tools \
    sqlite3 \
    jq \
    avahi-daemon \
    libnss-mdns

# ホスト名設定（unipi-agri）
echo "unipi-agri" > "$MOUNT_POINT/etc/hostname"
sed -i 's/raspberrypi/unipi-agri/g' "$MOUNT_POINT/etc/hosts"

# avahi有効化
chroot "$MOUNT_POINT" systemctl enable avahi-daemon

echo -e "${GREEN}Base packages installed${NC}"

#-------------------------------------------------------------------------------
# クリーンアップ
#-------------------------------------------------------------------------------
echo -e "\n${YELLOW}[8/8] Cleaning up image...${NC}"

# apt キャッシュクリア
chroot "$MOUNT_POINT" apt-get clean
chroot "$MOUNT_POINT" rm -rf /var/lib/apt/lists/*

# QEMUバイナリ削除
rm -f "$MOUNT_POINT/usr/bin/qemu-aarch64-static"

# 履歴削除
rm -f "$MOUNT_POINT/root/.bash_history"
rm -f "$MOUNT_POINT/home/operator/.bash_history"

echo -e "${GREEN}Cleanup complete${NC}"

#-------------------------------------------------------------------------------
# 完了
#-------------------------------------------------------------------------------
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Image build complete!${NC}"
echo -e "${GREEN}========================================${NC}"

echo -e "\n${YELLOW}Output: $IMAGE_FILE${NC}"
echo -e "\n${YELLOW}Default credentials:${NC}"
echo -e "  User: operator"
echo -e "  Pass: changeme123"
echo -e "\n${YELLOW}First boot will:${NC}"
echo -e "  1. Install Docker + Docker Compose"
echo -e "  2. Install EVOK (UniPi 1.1 support)"
echo -e "  3. Start Home Assistant + Node-RED + Mosquitto"
echo -e "  4. Configure WireGuard VPN"
echo -e "\n${YELLOW}Access after first boot:${NC}"
echo -e "  Home Assistant: http://<IP>:8123"
echo -e "  Node-RED: http://<IP>:1880"
