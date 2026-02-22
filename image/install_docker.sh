#!/bin/bash
#===============================================================================
# Docker & Docker Compose インストールスクリプト
#===============================================================================

set -e

echo "Installing Docker..."

# 既存のDockerパッケージを削除
apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# 必要なパッケージ
apt-get update
apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Docker公式GPGキー追加
mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

# リポジトリ追加
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Docker インストール
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Docker サービス有効化
systemctl enable docker
systemctl start docker

# docker compose コマンドのエイリアス（古い形式との互換性）
if ! command -v docker-compose &> /dev/null; then
    ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose 2>/dev/null || true
fi

echo "Docker installed successfully"
docker --version
docker compose version
