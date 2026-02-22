#!/bin/bash
# =============================================================================
# camera_upload.sh — RPiカメラ定期撮影 + VPS送信
# =============================================================================
# RPi Camera Module (IMX708 NoIR) で撮影し、VPSにscpで送信する。
# Grafanaダッシュボードで最新画像を表示するために latest.jpg も送信。
#
# セットアップ:
#   1. RPi に SSH鍵を作成: ssh-keygen -t ed25519 -C "rpi-camera"
#   2. VPS に公開鍵を登録: ssh-copy-id debian@YOUR_VPS_IP
#   3. VPS にディレクトリ作成: ssh debian@YOUR_VPS_IP "mkdir -p ~/pi-camera"
#   4. cron 設定: crontab -e で以下を追加
#      */30 * * * * /home/unipi/camera_upload.sh >> /tmp/camera_upload.log 2>&1
#
# 必要パッケージ:
#   - rpicam-still (Bookworm標準、libcamera-stillではない)
#
# 注意:
#   - Bookworm では libcamera-still ではなく rpicam-still を使う
#   - NoIR カメラは赤外線カットフィルタなし（夜間撮影向き）
# =============================================================================
set -euo pipefail

# --- 設定 ---
VPS_HOST="${VPS_SSH_USER:-debian}@${VPS_HOST_IP:?Set VPS_HOST_IP}"
VPS_DIR="${VPS_CAMERA_DIR:-/home/debian/pi-camera}"
LOCAL_TMP="/tmp/camera_capture.jpg"

# --- 撮影 ---
DATE_DIR=$(date +%Y-%m-%d)
FILENAME=$(date +%H-%M).jpg

rpicam-still \
  -o "$LOCAL_TMP" \
  --width 2304 \
  --height 1296 \
  -t 2000 \
  --nopreview \
  -q 85 \
  2>/dev/null

# --- VPSへ送信 ---
ssh -o ConnectTimeout=10 "$VPS_HOST" "mkdir -p ${VPS_DIR}/${DATE_DIR}"
scp -o ConnectTimeout=10 "$LOCAL_TMP" "${VPS_HOST}:${VPS_DIR}/${DATE_DIR}/${FILENAME}"
scp -o ConnectTimeout=10 "$LOCAL_TMP" "${VPS_HOST}:${VPS_DIR}/latest.jpg"

# --- クリーンアップ ---
rm -f "$LOCAL_TMP"
logger -t camera_upload "Uploaded ${DATE_DIR}/${FILENAME}"
