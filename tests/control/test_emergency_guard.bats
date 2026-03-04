#!/usr/bin/env bats
# tests/v2_control/test_emergency_guard.bats
# Layer 1 emergency_guard.sh bats テスト（設計書 §7.1）

GUARD_SCRIPT="${BATS_TEST_DIRNAME}/../../src/v2_control/emergency_guard.sh"

setup() {
    TEST_DIR="$(mktemp -d)"

    # テスト用環境変数（emergency_guard.sh が参照）
    export AGRIHA_CONFIG="${TEST_DIR}/layer1.env"
    export AGRIHA_LOCKOUT="${TEST_DIR}/lockout_state.json"
    export AGRIHA_LOG="${TEST_DIR}/emergency.log"

    # curl ログ/モック用
    export MOCK_LOG_DIR="${TEST_DIR}"
    export MOCK_TEMP="20.0"
    export MOCK_CG_LOCKED="false"
    export MOCK_SENSOR_FAIL="false"
    export MOCK_SENSOR_MISSING_CCM="false"
    export MOCK_LINE_FAIL="false"

    # テスト用 layer1.env（LINE有効、テスト用URLを使用）
    cat > "${TEST_DIR}/layer1.env" << 'EOF'
HIGH_TEMP_THRESHOLD=27
LOW_TEMP_THRESHOLD=16
WINDOW_CHANNELS="5 6 7 8"
LOCKOUT_DURATION_SEC=300
SENSOR_FALLBACK=true
UNIPI_API_BASE_URL="http://test-api.invalid"
UNIPI_API_KEY=""
CURL_TIMEOUT=5
LINE_CHANNEL_ACCESS_TOKEN="test_token"
LINE_GROUP_ID="test_group"
LINE_ENABLED=true
LOCKOUT_FILE="/var/lib/agriha/lockout_state.json"
EOF

    # curl モックスクリプト（PATH先頭に置くことでシェルから使用される）
    # POSIX sh 互換。dash で動作。
    cat > "${TEST_DIR}/curl" << 'CURL_EOF'
#!/bin/sh
# curl モック: 全引数をログに記録し、テスト用レスポンスを返す
echo "CALL: $*" >> "${MOCK_LOG_DIR}/curl_calls.log"
args="$*"

# センサー取得失敗モード
if [ "${MOCK_SENSOR_FAIL:-false}" = "true" ]; then
    case "$args" in
        *api/sensors*)
            exit 1
            ;;
    esac
fi

case "$args" in
    *api/sensors*)
        if [ "${MOCK_SENSOR_MISSING_CCM:-false}" = "true" ]; then
            # CCMなし。Misol のみ返す
            printf '{"sensors":{"agriha/farm/weather/misol":{"temperature_c":%s}}}\n' \
                "${MOCK_TEMP:-20.0}"
        else
            printf '{"sensors":{"agriha/h01/ccm/InAirTemp":{"value":%s}}}\n' \
                "${MOCK_TEMP:-20.0}"
        fi
        ;;
    *api/status*)
        printf '{"locked_out":%s}\n' "${MOCK_CG_LOCKED:-false}"
        ;;
    *api/relay*)
        printf '{"queued":true}\n'
        ;;
    *line.me*)
        if [ "${MOCK_LINE_FAIL:-false}" = "true" ]; then
            exit 1
        fi
        printf '{"message":"ok"}\n'
        ;;
    *)
        printf '{"error":"unknown"}\n'
        ;;
esac
CURL_EOF
    chmod +x "${TEST_DIR}/curl"

    # PATH の先頭に TEST_DIR を追加（モック curl が優先使用される）
    export PATH="${TEST_DIR}:${PATH}"

    # curl ログ初期化
    touch "${TEST_DIR}/curl_calls.log"
}

teardown() {
    rm -rf "${TEST_DIR}"
}

# ─────────────────────────────────────────────────────────────────
# テストケース 1: 正常範囲（20℃）→ 何もしない
# ─────────────────────────────────────────────────────────────────
@test "1: 正常温度範囲（20℃）では何もしない" {
    export MOCK_TEMP="20.0"

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    # EMERGENCY ログが出ていないこと
    [[ ! "$output" =~ "EMERGENCY" ]]
    # リレーが操作されていないこと
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 0 ]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 2: 高温超過（28℃）→ ch5-8 全開 + LINE 通知
# ─────────────────────────────────────────────────────────────────
@test "2: 高温超過（28℃）→ ch5-8 全開 + LINE 通知" {
    export MOCK_TEMP="28.0"

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" =~ "emergency_open" ]]
    # ch5-8 全て POST されていること（4回）
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 4 ]
    # value=1（全開）が含まれること
    grep -q '"value": 1' "${TEST_DIR}/curl_calls.log"
    # LINE 通知が送られていること
    grep -q "line.me" "${TEST_DIR}/curl_calls.log"
}

# ─────────────────────────────────────────────────────────────────
# テストケース 3: 低温超過（15℃）→ ch5-8 全閉 + LINE 通知
# ─────────────────────────────────────────────────────────────────
@test "3: 低温超過（15℃）→ ch5-8 全閉 + LINE 通知" {
    export MOCK_TEMP="15.0"

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" =~ "emergency_close" ]]
    # ch5-8 全て POST されていること（4回）
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 4 ]
    # value=0（全閉）が含まれること
    grep -q '"value": 0' "${TEST_DIR}/curl_calls.log"
    # LINE 通知が送られていること
    grep -q "line.me" "${TEST_DIR}/curl_calls.log"
}

# ─────────────────────────────────────────────────────────────────
# テストケース 4: センサーデータ取得失敗 → ログ出力して終了
# ─────────────────────────────────────────────────────────────────
@test "4: センサーデータ取得失敗 → ログ出力して終了" {
    export MOCK_SENSOR_FAIL="true"
    export MOCK_TEMP="28.0"

    run sh "$GUARD_SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" =~ "ERROR" ]]
    # リレーが操作されていないこと
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 0 ]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 5: ロックアウト中 → スキップ
# ─────────────────────────────────────────────────────────────────
@test "5: Layer 1 ロックアウト中 → スキップ" {
    # 将来のロックアウト期限を持つ lockout_state.json を事前作成
    python3 - << 'PYEOF'
import json, os
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except ImportError:
    from datetime import timezone, timedelta as td
    _JST = timezone(td(hours=9))
until = (datetime.now(_JST) + timedelta(minutes=3)).isoformat()
data = {
    "layer1_lockout_until": until,
    "last_action": "emergency_open",
    "last_temp": 28.0,
    "last_triggered_at": datetime.now(_JST).isoformat(),
}
lockout_file = os.environ["AGRIHA_LOCKOUT"]
with open(lockout_file, "w") as f:
    json.dump(data, f)
PYEOF

    export MOCK_TEMP="28.0"  # 高温だが、ロックアウト中なのでスキップ

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" =~ "ロックアウト中" ]]
    # リレーが操作されていないこと
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 0 ]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 6: Layer 1 ロックアウト連打防止 → 5分以内は再発動しない
# ─────────────────────────────────────────────────────────────────
@test "6: Layer 1 ロックアウト連打防止: 5分以内は再発動しない" {
    export MOCK_TEMP="28.0"

    # 1回目: 緊急全開が発動し、lockout_state.json が書き込まれる
    run sh "$GUARD_SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "emergency_open" ]]
    # lockout_state.json が作成されていること
    [ -f "${AGRIHA_LOCKOUT}" ]

    # curl ログをリセット
    > "${TEST_DIR}/curl_calls.log"

    # 2回目: ロックアウト中なのでスキップ
    run sh "$GUARD_SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" =~ "ロックアウト中" ]]
    # リレーが操作されていないこと（2回目では操作なし）
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 0 ]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 7: CommandGate ロックアウト（423応答）→ スキップ
# ─────────────────────────────────────────────────────────────────
@test "7: CommandGate ロックアウト（locked_out=true）→ スキップ" {
    export MOCK_CG_LOCKED="true"
    export MOCK_TEMP="28.0"  # 高温だが、CommandGate ロックアウト中

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" =~ "CommandGate" ]]
    # リレーが操作されていないこと
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 0 ]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 8: LINE 通知失敗 → 制御アクション自体は成功
# ─────────────────────────────────────────────────────────────────
@test "8: LINE 通知失敗でも制御アクション自体は成功（exit 0）" {
    export MOCK_TEMP="28.0"
    export MOCK_LINE_FAIL="true"

    run sh "$GUARD_SCRIPT"

    # リレー制御は成功しているので exit 0
    [ "$status" -eq 0 ]
    [[ "$output" =~ "emergency_open" ]]
    # リレーは操作されていること
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 4 ]
    # LINE 通知失敗の警告が出ていること
    [[ "$output" =~ "WARNING" ]] || [[ "$output" =~ "LINE通知失敗" ]]
}

# ─────────────────────────────────────────────────────────────────
# テストケース 9: CCM データなし → Misol 外気温で代替判断
# ─────────────────────────────────────────────────────────────────
@test "9: CCM データなし → Misol 外気温（15℃）で代替判断 → 全閉" {
    # CCM なし、Misol のみ（低温15℃）
    export MOCK_SENSOR_MISSING_CCM="true"
    export MOCK_TEMP="15.0"

    run sh "$GUARD_SCRIPT"

    [ "$status" -eq 0 ]
    [[ "$output" =~ "emergency_close" ]]
    # ch5-8 全て POST されていること
    relay_calls=$(grep -c "api/relay" "${TEST_DIR}/curl_calls.log" || true)
    [ "${relay_calls:-0}" -eq 4 ]
}
