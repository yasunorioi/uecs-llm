#!/bin/bash
# ═══════════════════════════════════════════════════
# AgriHA v2 セットアップスクリプト
# git clone 後にこれ1発で環境構築が完了する
# Usage: cd ~/uecs-llm && ./setup.sh
# ═══════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
ENV_FILE="${SCRIPT_DIR}/.env"
CONFIG_DIR="/etc/agriha"
DATA_DIR="/var/lib/agriha"
AGRIHA_USER="agriha"

echo "=== AgriHA v2 セットアップ開始 ==="
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
# 既存ファイルは上書きしない（農家が手動編集した設定を守る）
for f in config/thresholds.yaml config/layer2_config.yaml config/layer3_config.yaml config/crop_irrigation.yaml; do
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
    sudo cp "${SCRIPT_DIR}/src/uecs_llm/system_prompt.txt" "${CONFIG_DIR}/system_prompt.txt"
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

# Step 4: systemd サービスファイルインストール + enable
echo "[4/6] systemdサービスインストール..."
sudo cp "${SCRIPT_DIR}/systemd/unipi-daemon.service" /etc/systemd/system/
sudo cp "${SCRIPT_DIR}/systemd/agriha-ui.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable unipi-daemon agriha-ui
echo "  → unipi-daemon, agriha-ui を有効化"

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

echo ""
echo "=== セットアップ完了 ==="
echo "次のステップ:"
echo "  1. nano .env  ← ANTHROPIC_API_KEY を記入"
echo "  2. sudo nano /etc/agriha/unipi_daemon.yaml  ← ハードウェア設定確認"
echo "  3. sudo systemctl start unipi-daemon  ← デーモン起動"
echo "  4. sudo systemctl start agriha-ui  ← WebUI起動"
