#!/bin/bash
# ═══════════════════════════════════════════════════
# AgriHA v4 セットアップスクリプト
# git clone 後にこれ1発で環境構築が完了する
# Usage: sudo git clone https://github.com/yasunorioi/uecs-llm.git /opt/agriha && cd /opt/agriha && sudo bash setup.sh
# ═══════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
ENV_FILE="${SCRIPT_DIR}/.env"
CONFIG_DIR="/etc/agriha"
DATA_DIR="/var/lib/agriha"
AGRIHA_USER="agriha"

echo "=== AgriHA v4 セットアップ開始 ==="
echo "リポジトリパス: ${SCRIPT_DIR}"

# Step 1: Python venv作成 + pip install
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/6] venv作成中..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/6] venv既存 → スキップ"
fi
echo "[1/6] pip install（daemon extras）..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -e "${SCRIPT_DIR}[daemon]" --quiet
echo "  → pip install 完了"

# Step 2: /etc/agriha/ ディレクトリ作成 + 設定ファイルコピー
echo "[2/6] 設定ディレクトリ作成..."
sudo mkdir -p "$CONFIG_DIR"
sudo chown "${AGRIHA_USER}:${AGRIHA_USER}" "$CONFIG_DIR"
# 既存ファイルは上書きしない（農家が手動編集した設定を守る）
for f in config/emergency.conf config/rules.yaml config/forecast.yaml config/channel_map.yaml config/crop_irrigation.yaml config/thresholds.yaml config/network.yaml; do
    fname=$(basename "$f")
    if [ ! -f "${CONFIG_DIR}/${fname}" ]; then
        sudo cp "${SCRIPT_DIR}/${f}" "${CONFIG_DIR}/${fname}"
        echo "  → ${fname} コピー"
    else
        echo "  → ${fname} 既存 → スキップ"
    fi
done
# unipi_daemon.yaml（example から。ハードウェア設定は後で手動編集が必要）
if [ ! -f "${CONFIG_DIR}/unipi_daemon.yaml" ]; then
    sudo cp "${SCRIPT_DIR}/config/unipi_daemon.example.yaml" "${CONFIG_DIR}/unipi_daemon.yaml"
    echo "  → unipi_daemon.yaml コピー（要編集: I2Cアドレス・GPIOピン等）"
else
    echo "  → unipi_daemon.yaml 既存 → スキップ"
fi
# system_prompt.txt（農家の怒りが蓄積された制御ロジックの本体）
if [ ! -f "${CONFIG_DIR}/system_prompt.txt" ]; then
    sudo cp "${SCRIPT_DIR}/config/system_prompt.txt" "${CONFIG_DIR}/system_prompt.txt"
    echo "  → system_prompt.txt コピー"
else
    echo "  → system_prompt.txt 既存 → スキップ"
fi
# Step 3: agriha ユーザー作成 + /var/lib/agriha/ データディレクトリ作成
echo "[3/6] データディレクトリ作成..."
# agriha ユーザーが存在しない場合のみ作成
if ! id "$AGRIHA_USER" &>/dev/null; then
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$AGRIHA_USER"
    echo "  → agriha システムユーザー作成完了"
else
    echo "  → agriha ユーザー既存 → スキップ"
fi
sudo mkdir -p "$DATA_DIR"
sudo chown "${AGRIHA_USER}:${AGRIHA_USER}" "$DATA_DIR"
echo "  → ${DATA_DIR} 作成完了"
sudo mkdir -p /var/log/agriha
sudo chown "${AGRIHA_USER}:${AGRIHA_USER}" /var/log/agriha
echo "  → /var/log/agriha 作成完了"
sudo chown -R "${AGRIHA_USER}:${AGRIHA_USER}" "${SCRIPT_DIR}"
echo "  → ${SCRIPT_DIR} 所有権を ${AGRIHA_USER} に変更"
# ダッシュボードから編集するファイルはagrihaユーザーに書き込み権限を付与
for f in rules.yaml channel_map.yaml system_prompt.txt crop_irrigation.yaml forecast.yaml thresholds.yaml network.yaml; do
    if [ -f "${CONFIG_DIR}/${f}" ]; then
        sudo chown "${AGRIHA_USER}:${AGRIHA_USER}" "${CONFIG_DIR}/${f}"
        sudo chmod 664 "${CONFIG_DIR}/${f}"
    fi
done
echo "  → ダッシュボード編集対象ファイルの権限設定完了"

# Step 4: systemd サービスファイルインストール + enable
# __REPO_DIR__ を実際のリポジトリパスに置換してからインストール
echo "[4/6] systemdサービスインストール..."
for svc in unipi-daemon.service agriha-ui.service; do
    sed "s|__REPO_DIR__|${SCRIPT_DIR}|g" "${SCRIPT_DIR}/systemd/${svc}" \
        | sudo tee "/etc/systemd/system/${svc}" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable unipi-daemon agriha-ui
echo "  → unipi-daemon, agriha-ui を有効化（パス: ${SCRIPT_DIR}）"

# Step 5: cron設定（三層制御用）
# cron内のハードコードパス（/home/agriha/uecs-llm）をこのリポジトリのパスに置換
echo "[5/6] cron設定..."
sudo sed "s|/home/agriha/uecs-llm|${SCRIPT_DIR}|g" \
    "${SCRIPT_DIR}/systemd/agriha-cron" \
    | sudo tee /etc/cron.d/agriha > /dev/null
sudo chmod 644 /etc/cron.d/agriha
echo "  → 三層制御cron設定完了（パス: ${SCRIPT_DIR}）"

# Step 6: .env ファイル準備
if [ ! -f "$ENV_FILE" ]; then
    cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
    echo "[6/6] .env.example → .env コピー済み"
    echo "  ※ nano .env で ANTHROPIC_API_KEY 等を記入してください"
else
    echo "[6/6] .env 既存 → スキップ"
fi

# Step 7: Nginx設定デプロイ
echo "[7/7] Nginx設定..."
if [ -f "${SCRIPT_DIR}/config/nginx.conf" ]; then
    sudo cp "${SCRIPT_DIR}/config/nginx.conf" /etc/nginx/sites-available/agriha
    sudo ln -sf /etc/nginx/sites-available/agriha /etc/nginx/sites-enabled/agriha
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t && sudo systemctl reload nginx
    echo "  → Nginx設定完了（agriha サイト有効化）"
else
    echo "  → config/nginx.conf なし → スキップ"
fi

echo ""
echo "=== AgriHA v4 セットアップ完了 ==="
echo "セットアップ完了。.envにAPI KEYを設定後、sudo systemctl start unipi-daemon agriha-ui で起動"
