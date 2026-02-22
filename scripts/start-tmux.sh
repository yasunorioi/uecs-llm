#!/bin/bash
# start-tmux.sh — AgriHA 温室制御 tmux セッション起動
#
# Usage: ./scripts/start-tmux.sh
#
# レイアウト:
#   ┌──────────────────┬──────────────────┐
#   │ 0: llama-server  │ 2: unipi-daemon  │
#   ├──────────────────┼──────────────────┤
#   │ 1: MQTT monitor  │ 3: journalctl    │
#   ├──────────────────┴──────────────────┤
#   │ 4: LLM Chat (対話窓)               │
#   └─────────────────────────────────────┘

set -euo pipefail

SESSION="agriha"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 設定（環境変数で上書き可）
LLAMA_URL="${LLAMA_URL:-http://localhost:8081}"
LLAMA_BIN="${LLAMA_BIN:-/opt/llama-server/bin/llama-server}"
LLAMA_MODEL="${LLAMA_MODEL:-/opt/llama-server/models/LFM2.5-1.2B-Instruct-Q4_K_M.gguf}"
DAEMON_CONFIG="${DAEMON_CONFIG:-/etc/agriha/unipi_daemon.yaml}"
MQTT_HOST="${MQTT_HOST:-localhost}"

# 既存セッションがあれば attach
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already exists."
    if [ -t 0 ]; then
        exec tmux attach -t "$SESSION"
    else
        echo "Run: tmux attach -t $SESSION"
        exit 0
    fi
fi

# --- セッション作成 (split時にサイズ指定しない — tmux 3.4互換) ---

# Step 1: 上段と下段に分割
tmux new-session -d -s "$SESSION" -n main -x 200 -y 50
#  pane 0: 上段全体
tmux split-window -t "${SESSION}:main.0" -v
#  pane 0: 上段, pane 1: 下段(Chat)

# Step 2: 上段(pane 0)を左右に分割
tmux split-window -t "${SESSION}:main.0" -h
#  pane 0: 左上, pane 1: 右上, pane 2: 下段(Chat)

# Step 3: 左上(pane 0)を上下に分割
tmux split-window -t "${SESSION}:main.0" -v
#  pane 0: 左上, pane 1: 左下, pane 2: 右上, pane 3: 下段(Chat)

# Step 4: 右上(pane 2)を上下に分割
tmux split-window -t "${SESSION}:main.2" -v
#  pane 0: 左上, pane 1: 左下, pane 2: 右上, pane 3: 右下, pane 4: 下段(Chat)

# Chat窓のサイズ調整
tmux resize-pane -t "${SESSION}:main.4" -y 12

# コマンド送信
tmux send-keys -t "${SESSION}:main.0" \
    "echo '=== [0] llama-server ===' && ${LLAMA_BIN} -m ${LLAMA_MODEL} --port 8081 -c 4096 -t 4 --mlock --jinja" Enter

tmux send-keys -t "${SESSION}:main.1" \
    "echo '=== [1] MQTT monitor ===' && mosquitto_sub -h ${MQTT_HOST} -t '#' -v" Enter

tmux send-keys -t "${SESSION}:main.2" \
    "echo '=== [2] unipi-daemon ===' && cd ${PROJECT_DIR} && .venv/bin/unipi-daemon --config ${DAEMON_CONFIG}" Enter

tmux send-keys -t "${SESSION}:main.3" \
    "echo '=== [3] journalctl ===' && journalctl -f -u agriha-llm -u unipi-daemon --no-hostname" Enter

tmux send-keys -t "${SESSION}:main.4" \
    "echo '=== [4] LLM Chat ===' && ${SCRIPT_DIR}/llm-chat.sh ${LLAMA_URL}" Enter

# Chat窓にフォーカス
tmux select-pane -t "${SESSION}:main.4"

echo "Session '$SESSION' created."
if [ -t 0 ]; then
    exec tmux attach -t "$SESSION"
else
    echo "Run: tmux attach -t $SESSION"
fi
