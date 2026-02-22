#!/bin/bash
# start-tmux.sh — AgriHA 温室制御 tmux セッション起動
#
# Usage: ./scripts/start-tmux.sh
#
# レイアウト:
#   ┌──────────────────┬──────────────────┐
#   │ 0: llama-server  │ 1: unipi-daemon  │
#   ├──────────────────┼──────────────────┤
#   │ 2: MQTT monitor  │ 3: journalctl    │
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
    echo "Session '$SESSION' already exists. Attaching..."
    exec tmux attach -t "$SESSION"
fi

# --- セッション作成 ---

# Pane 0: llama-server
tmux new-session -d -s "$SESSION" -n main -x 200 -y 50
tmux send-keys -t "${SESSION}:main.0" "echo '=== [0] llama-server ===' && ${LLAMA_BIN} -m ${LLAMA_MODEL} --port 8081 -c 4096 -t 4 --mlock --jinja" Enter

# Pane 1: unipi-daemon (右上)
tmux split-window -t "${SESSION}:main" -h
tmux send-keys -t "${SESSION}:main.1" "echo '=== [1] unipi-daemon ===' && cd ${PROJECT_DIR} && .venv/bin/unipi-daemon --config ${DAEMON_CONFIG}" Enter

# Pane 2: MQTT monitor (左下)
tmux split-window -t "${SESSION}:main.0" -v
tmux send-keys -t "${SESSION}:main.2" "echo '=== [2] MQTT monitor ===' && mosquitto_sub -h ${MQTT_HOST} -t '#' -v" Enter

# Pane 3: journalctl (右下)
tmux split-window -t "${SESSION}:main.1" -v
tmux send-keys -t "${SESSION}:main.3" "echo '=== [3] journalctl ===' && journalctl -f -u agriha-llm -u unipi-daemon --no-hostname" Enter

# Pane 4: LLM Chat (最下段、全幅)
tmux split-window -t "${SESSION}:main" -v -l 12
tmux send-keys -t "${SESSION}:main.4" "echo '=== [4] LLM Chat ===' && ${SCRIPT_DIR}/llm-chat.sh ${LLAMA_URL}" Enter

# レイアウト調整
tmux select-pane -t "${SESSION}:main.4"

# attach
exec tmux attach -t "$SESSION"
