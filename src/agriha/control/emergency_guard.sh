#!/bin/sh
# emergency_guard.sh - Layer 1 緊急停止
# v2 三層制御 Layer 1（爆発）
# POSIX sh 互換  起動: cron 毎分 (* * * * *)
# 設計書: docs/v2_three_layer_design.md §1.1 §2.1

# ── デフォルト設定 ────────────────────────────────────────────────
HIGH_TEMP_THRESHOLD=27
LOW_TEMP_THRESHOLD=16
WINDOW_CHANNELS="5 6 7 8"
LOCKOUT_DURATION_SEC=300
SENSOR_FALLBACK=true
UNIPI_API_BASE_URL="http://localhost:8080"
UNIPI_API_KEY=""
CURL_TIMEOUT=5
LINE_CHANNEL_ACCESS_TOKEN=""
LINE_GROUP_ID=""
LINE_ENABLED=true
LOCKOUT_FILE="/var/lib/agriha/lockout_state.json"

# ── 設定ファイル読み込み ───────────────────────────────────────────
# テスト時は AGRIHA_CONFIG 環境変数でパスを上書き可能
_CFG="${AGRIHA_CONFIG:-/etc/agriha/layer1.env}"
if [ -f "$_CFG" ]; then
    . "$_CFG"
fi

# テスト用パス上書き
if [ -n "$AGRIHA_LOCKOUT" ]; then
    LOCKOUT_FILE="$AGRIHA_LOCKOUT"
fi
LOG_FILE="${AGRIHA_LOG:-/var/log/agriha/emergency.log}"

# ── ログ出力（ファイル + stdout）─────────────────────────────────────
log() {
    _ts=$(date '+%Y-%m-%dT%H:%M:%S')
    printf '%s [emergency_guard] %s\n' "$_ts" "$1" >> "$LOG_FILE"
    printf '%s [emergency_guard] %s\n' "$_ts" "$1"
}

# ─────────────────────────────────────────────────────────────────
# Step 1a: Layer 1 自身のロックアウト確認（連打防止）
# ─────────────────────────────────────────────────────────────────
if [ -f "$LOCKOUT_FILE" ]; then
    _L1_LOCKED=$(LOCKOUT_FILE="$LOCKOUT_FILE" python3 << 'PYEOF'
import json, os, sys
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone, timedelta
    _JST = timezone(timedelta(hours=9))
try:
    with open(os.environ["LOCKOUT_FILE"]) as f:
        d = json.load(f)
    until_str = d.get("layer1_lockout_until", "")
    if until_str:
        until_dt = datetime.fromisoformat(until_str)
        if datetime.now(_JST) < until_dt:
            print("locked")
            sys.exit(0)
except Exception:
    pass
print("unlocked")
PYEOF
)
    if [ "$_L1_LOCKED" = "locked" ]; then
        log "Layer 1 ロックアウト中。スキップ。"
        exit 0
    fi
fi

# ─────────────────────────────────────────────────────────────────
# Step 1b: CommandGate ロックアウト確認（GET /api/status）
# ─────────────────────────────────────────────────────────────────
_STATUS_RESP=$(curl -s -m "$CURL_TIMEOUT" "${UNIPI_API_BASE_URL}/api/status" 2>/dev/null)
_CG_LOCKED=$(STATUS_RESP="$_STATUS_RESP" python3 << 'PYEOF'
import json, os, sys
try:
    d = json.loads(os.environ.get("STATUS_RESP", "{}"))
    if d.get("locked_out", False):
        print("locked")
        sys.exit(0)
except Exception:
    pass
print("unlocked")
PYEOF
)
if [ "$_CG_LOCKED" = "locked" ]; then
    log "CommandGate ロックアウト中。スキップ。"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────
# Step 2: センサーデータ取得
# ─────────────────────────────────────────────────────────────────
_SENSOR_RESP=$(curl -s -m "$CURL_TIMEOUT" "${UNIPI_API_BASE_URL}/api/sensors" 2>/dev/null)
_CURL_EXIT=$?
if [ "$_CURL_EXIT" -ne 0 ] || [ -z "$_SENSOR_RESP" ]; then
    log "ERROR: センサーデータ取得失敗 (exit=${_CURL_EXIT})"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────
# Step 3: 内気温抽出（CCM優先、Misolフォールバック）
# ─────────────────────────────────────────────────────────────────
_TEMP=$(SENSOR_RESP="$_SENSOR_RESP" SENSOR_FALLBACK="$SENSOR_FALLBACK" python3 << 'PYEOF'
import json, os, sys
data_str = os.environ.get("SENSOR_RESP", "{}")
fallback = os.environ.get("SENSOR_FALLBACK", "true").lower() == "true"
try:
    d = json.loads(data_str)
    sensors = d.get("sensors", {})
    # CCM InAirTemp（優先）
    ccm = sensors.get("agriha/h01/ccm/InAirTemp", {})
    if ccm and ccm.get("value") is not None:
        print(ccm["value"])
        sys.exit(0)
    # Misol 外気温でフォールバック
    if fallback:
        misol = sensors.get("agriha/farm/weather/misol", {})
        if misol and misol.get("temperature_c") is not None:
            print(misol["temperature_c"])
            sys.exit(0)
except Exception:
    pass
print("")
PYEOF
)
if [ -z "$_TEMP" ]; then
    log "ERROR: 温度データ取得失敗（CCM・Misolとも取得不可）"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────
# Step 4: 閾値判定
# ─────────────────────────────────────────────────────────────────
_ACTION=$(TEMP="$_TEMP" HIGH="$HIGH_TEMP_THRESHOLD" LOW="$LOW_TEMP_THRESHOLD" python3 << 'PYEOF'
import os
temp = float(os.environ["TEMP"])
high = float(os.environ["HIGH"])
low  = float(os.environ["LOW"])
if temp > high:
    print("open")
elif temp < low:
    print("close")
else:
    print("none")
PYEOF
)
if [ "$_ACTION" = "none" ]; then
    log "正常範囲 ${_TEMP}℃。何もしない。"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────
# 緊急アクション実行
# ─────────────────────────────────────────────────────────────────
if [ "$_ACTION" = "open" ]; then
    _ACTION_NAME="emergency_open"
    _RELAY_VALUE=1
    _LINE_MSG="🚨 ${_TEMP}℃ 緊急全開"
else
    _ACTION_NAME="emergency_close"
    _RELAY_VALUE=0
    _LINE_MSG="🚨 ${_TEMP}℃ 緊急全閉"
fi

log "EMERGENCY: ${_ACTION_NAME} 温度=${_TEMP}℃"

# リレー制御（ch5-8 または WINDOW_CHANNELS）
for _CH in $WINDOW_CHANNELS; do
    if [ -n "$UNIPI_API_KEY" ]; then
        curl -s -m "$CURL_TIMEOUT" \
            -X POST \
            -H "Content-Type: application/json" \
            -H "X-API-Key: ${UNIPI_API_KEY}" \
            -d "{\"value\": ${_RELAY_VALUE}}" \
            "${UNIPI_API_BASE_URL}/api/relay/${_CH}" >> "$LOG_FILE" 2>&1
    else
        curl -s -m "$CURL_TIMEOUT" \
            -X POST \
            -H "Content-Type: application/json" \
            -d "{\"value\": ${_RELAY_VALUE}}" \
            "${UNIPI_API_BASE_URL}/api/relay/${_CH}" >> "$LOG_FILE" 2>&1
    fi
    log "リレー ch${_CH} value=${_RELAY_VALUE}"
done

# lockout_state.json 更新（5分ロックアウト）
TEMP_VAL="$_TEMP" LOCKOUT_SEC="$LOCKOUT_DURATION_SEC" \
    ACTION_NAME="$_ACTION_NAME" LOCKOUT_FILE="$LOCKOUT_FILE" \
    python3 << 'PYEOF'
import json, os
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone, timedelta as td
    _JST = timezone(td(hours=9))
now = datetime.now(_JST)
duration = int(os.environ.get("LOCKOUT_SEC", "300"))
until = now + timedelta(seconds=duration)
data = {
    "layer1_lockout_until": until.isoformat(),
    "last_action": os.environ.get("ACTION_NAME", "emergency_open"),
    "last_temp": float(os.environ.get("TEMP_VAL", "0")),
    "last_triggered_at": now.isoformat(),
}
lockout_file = os.environ.get("LOCKOUT_FILE", "/var/lib/agriha/lockout_state.json")
with open(lockout_file, "w") as f:
    json.dump(data, f, indent=2)
PYEOF
log "ロックアウト設定: ${LOCKOUT_DURATION_SEC}秒"

# LINE 通知
if [ "$LINE_ENABLED" = "true" ] && [ -n "$LINE_CHANNEL_ACCESS_TOKEN" ] && [ -n "$LINE_GROUP_ID" ]; then
    _LINE_BODY="{\"to\":\"${LINE_GROUP_ID}\",\"messages\":[{\"type\":\"text\",\"text\":\"${_LINE_MSG}\"}]}"
    curl -s -m "$CURL_TIMEOUT" \
        -X POST \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${LINE_CHANNEL_ACCESS_TOKEN}" \
        -d "$_LINE_BODY" \
        "https://api.line.me/v2/bot/message/push" >> "$LOG_FILE" 2>&1
    _LINE_EXIT=$?
    if [ "$_LINE_EXIT" -ne 0 ]; then
        log "WARNING: LINE通知失敗 (exit=${_LINE_EXIT})"
    else
        log "LINE通知送信: ${_LINE_MSG}"
    fi
fi

log "完了: ${_ACTION_NAME}"
exit 0
