#!/bin/bash
# start-tmux.sh — AgriHA 温室制御 tmux セッション起動
#
# Usage: ./scripts/start-tmux.sh
#
# RPi (AgriHA OS) オンボード v2 レイアウト:
#   ┌──────────────────┬──────────────────┐
#   │ 0: agriha-guard  │ 2: agriha-ui     │
#   │    (Layer1ログ)  │    (WebUIログ)   │
#   ├──────────────────┼──────────────────┤
#   │ 3: MQTT monitor  │ 1: REST API      │
#   │                  │    (curl監視)    │
#   └──────────────────┴──────────────────┘

set -euo pipefail

SESSION="agriha"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 設定（環境変数で上書き可）
RPI_HOST="${RPI_HOST:-10.10.0.10}"
MQTT_HOST="${MQTT_HOST:-${RPI_HOST}}"
REST_API="${REST_API:-http://localhost:8080}"

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

# コマンド送信
# pane 0: agriha-guard ログ (Layer 1 緊急制御)
tmux send-keys -t "${SESSION}:main.0" \
    "echo '=== [0] agriha-guard log (Layer 1) ===' && journalctl -f -u agriha-guard --no-hostname" Enter

# pane 1: MQTT monitor (RPiのブローカーに接続)
tmux send-keys -t "${SESSION}:main.1" \
    "echo '=== [1] MQTT monitor (${MQTT_HOST}) ===' && mosquitto_sub -h ${MQTT_HOST} -t '#' -v" Enter

# pane 2: agriha-ui ログ (WebUI)
tmux send-keys -t "${SESSION}:main.2" \
    "echo '=== [2] agriha-ui log (WebUI) ===' && journalctl -f -u agriha-ui --no-hostname" Enter

# pane 3: REST API 状態監視 (10秒おきにセンサー+ステータスを取得)
tmux send-keys -t "${SESSION}:main.3" \
    "echo '=== [3] REST API monitor (${REST_API}) ===' && while true; do echo '--- sensors ---'; curl -s ${REST_API}/api/sensors 2>/dev/null | python3 -m json.tool 2>/dev/null || echo '(connection failed)'; echo '--- status ---'; curl -s ${REST_API}/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null || echo '(connection failed)'; sleep 10; done" Enter

# pane 0 にフォーカス
tmux select-pane -t "${SESSION}:main.0"

echo "Session '$SESSION' created."
if [ -t 0 ]; then
    exec tmux attach -t "$SESSION"
else
    echo "Run: tmux attach -t $SESSION"
fi
