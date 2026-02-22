#!/bin/bash
# llm-chat.sh — llama-server 対話シェル
# system_prompt.txt を自動注入してチャット
#
# Usage: ./llm-chat.sh [llama-server-url]
# Default: http://localhost:8081

set -euo pipefail

LLAMA_URL="${1:-http://localhost:8081}"
PROMPT_FILE="${SYSTEM_PROMPT_PATH:-/etc/agriha/system_prompt.txt}"
ENDPOINT="${LLAMA_URL}/v1/chat/completions"

# system_prompt 読み込み
if [[ -f "$PROMPT_FILE" ]]; then
    SYSTEM_PROMPT=$(cat "$PROMPT_FILE")
    echo "System prompt: ${PROMPT_FILE} ($(wc -l < "$PROMPT_FILE") lines)"
else
    SYSTEM_PROMPT="あなたは温室環境制御AIです。"
    echo "System prompt: (default - ${PROMPT_FILE} not found)"
fi

# JSON文字列エスケープ
json_escape() {
    python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))"
}

SYSTEM_JSON=$(echo -n "$SYSTEM_PROMPT" | json_escape)

# 会話履歴（system以外）
MESSAGES="[]"

echo "========================================="
echo "  AgriHA LLM Chat (${LLAMA_URL})"
echo "  Ctrl+C or 'quit' to exit"
echo "========================================="
echo ""

while true; do
    printf "\033[1;34m殿>\033[0m "
    read -r INPUT || break
    [[ -z "$INPUT" ]] && continue
    [[ "$INPUT" == "quit" || "$INPUT" == "exit" ]] && break

    USER_JSON=$(echo -n "$INPUT" | json_escape)

    # 会話履歴に追加
    MESSAGES=$(echo "$MESSAGES" | python3 -c "
import json, sys
msgs = json.load(sys.stdin)
msgs.append({'role': 'user', 'content': ${USER_JSON}})
print(json.dumps(msgs))
")

    # リクエスト構築
    FULL_MESSAGES=$(echo "$MESSAGES" | python3 -c "
import json, sys
msgs = json.load(sys.stdin)
full = [{'role': 'system', 'content': ${SYSTEM_JSON}}] + msgs
print(json.dumps(full))
")

    PAYLOAD=$(python3 -c "
import json
msgs = json.loads('''${FULL_MESSAGES}''')
print(json.dumps({'messages': msgs, 'stream': False, 'max_tokens': 1024}))
")

    printf "\033[1;32mAI>\033[0m "
    RESPONSE=$(curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    if [[ $? -ne 0 || -z "$RESPONSE" ]]; then
        echo "(llama-server に接続できません: ${ENDPOINT})"
        continue
    fi

    CONTENT=$(echo "$RESPONSE" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
    print(r['choices'][0]['message']['content'])
except Exception as e:
    print(f'(parse error: {e})')
" 2>/dev/null)

    echo "$CONTENT"
    echo ""

    # 会話履歴にAI応答を追加
    ASSISTANT_JSON=$(echo -n "$CONTENT" | json_escape)
    MESSAGES=$(echo "$MESSAGES" | python3 -c "
import json, sys
msgs = json.load(sys.stdin)
msgs.append({'role': 'assistant', 'content': ${ASSISTANT_JSON}})
print(json.dumps(msgs))
")
done

echo ""
echo "終了"
