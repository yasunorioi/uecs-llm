#!/bin/bash
# start-tmux.sh — AgriHA 温室制御 tmux セッション起動
#
# Usage: ./scripts/start-tmux.sh
#
# nuc.local (LLM制御サーバー) 用レイアウト:
#   ┌──────────────────┬──────────────────┐
#   │ 0: llama-server  │ 1: agriha-control│
#   │    (journalctl)  │    (制御ログ)     │
#   ├──────────────────┼──────────────────┤
#   │ 2: MQTT monitor  │ 3: REST API      │
#   │   (RPi経由)      │    (curl監視)     │
#   ├──────────────────┴──────────────────┤
#   │ 4: LLM Chat (対話窓)               │
#   └─────────────────────────────────────┘

set -euo pipefail

SESSION="agriha"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 設定（環境変数で上書き可）
LLAMA_URL="${LLAMA_URL:-http://localhost:8081}"
RPI_HOST="${RPI_HOST:-10.10.0.10}"
MQTT_HOST="${MQTT_HOST:-${RPI_HOST}}"
REST_API="${REST_API:-http://${RPI_HOST}:8080}"

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
tmux split-window -t "${SESSION}:main.0" -v

# Step 2: 上段を左右に分割
tmux split-window -t "${SESSION}:main.0" -h

# Step 3: 左上を上下に分割
tmux split-window -t "${SESSION}:main.0" -v

# Step 4: 右上を上下に分割
tmux split-window -t "${SESSION}:main.2" -v

# Chat窓のサイズ調整
tmux resize-pane -t "${SESSION}:main.4" -y 12

# コマンド送信
# pane 0: llama-server ログ (systemdで稼働中)
tmux send-keys -t "${SESSION}:main.0" \
    "echo '=== [0] llama-server log ===' && journalctl -f -u agriha-llm --no-hostname" Enter

# pane 1: MQTT monitor (RPiのブローカーに接続)
tmux send-keys -t "${SESSION}:main.1" \
    "echo '=== [1] MQTT monitor (${MQTT_HOST}) ===' && mosquitto_sub -h ${MQTT_HOST} -t '#' -v" Enter

# pane 2: agriha-control ログ (cron制御ループ)
tmux send-keys -t "${SESSION}:main.2" \
    "echo '=== [2] agriha-control log ===' && journalctl -f -t agriha-control --no-hostname 2>/dev/null || echo 'Waiting for cron output... (tail syslog)' && tail -f /var/log/syslog 2>/dev/null | grep -i agriha" Enter

# pane 3: REST API 状態監視 (5秒おきにセンサー+ステータスを取得)
tmux send-keys -t "${SESSION}:main.3" \
    "echo '=== [3] REST API monitor (${REST_API}) ===' && while true; do echo '--- sensors ---'; curl -s ${REST_API}/api/sensors 2>/dev/null | python3 -m json.tool 2>/dev/null || echo '(connection failed)'; echo '--- status ---'; curl -s ${REST_API}/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null || echo '(connection failed)'; sleep 10; done" Enter

# pane 4: LLM Chat
tmux send-keys -t "${SESSION}:main.4" \
    "${SCRIPT_DIR}/llm-chat.sh ${LLAMA_URL}" Enter

# Chat窓にフォーカス
tmux select-pane -t "${SESSION}:main.4"

echo "Session '$SESSION' created."
if [ -t 0 ]; then
    exec tmux attach -t "$SESSION"
else
    echo "Run: tmux attach -t $SESSION"
fi
