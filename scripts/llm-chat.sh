#!/usr/bin/env python3
"""llm-chat — llama-server 対話シェル (system_prompt.txt 自動注入)"""

import json
import os
import sys
import readline  # noqa: F401 — enables arrow keys / history in input()

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    pass

LLAMA_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081"
ENDPOINT = f"{LLAMA_URL}/v1/chat/completions"
PROMPT_FILE = os.environ.get("SYSTEM_PROMPT_PATH", "/etc/agriha/system_prompt.txt")


def load_system_prompt() -> str:
    try:
        with open(PROMPT_FILE, encoding="utf-8") as f:
            text = f.read()
        line_count = text.count("\n")
        print(f"System prompt: {PROMPT_FILE} ({line_count} lines)")
        return text
    except FileNotFoundError:
        print(f"System prompt: (default — {PROMPT_FILE} not found)")
        return "あなたは温室環境制御AIです。"


def chat_request(messages: list) -> str:
    payload = json.dumps({
        "messages": messages,
        "stream": False,
        "max_tokens": 1024,
    }).encode("utf-8")

    if HAS_HTTPX:
        resp = httpx.post(ENDPOINT, content=payload,
                          headers={"Content-Type": "application/json"},
                          timeout=120.0)
        data = resp.json()
    else:
        req = Request(ENDPOINT, data=payload,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    return data["choices"][0]["message"]["content"]


def main():
    system_prompt = load_system_prompt()
    history: list[dict] = []

    print("=========================================")
    print(f"  AgriHA LLM Chat ({LLAMA_URL})")
    print("  Ctrl+C or 'quit' to exit")
    print("=========================================")
    print()

    while True:
        try:
            user_input = input("\033[1;34m殿>\033[0m ")
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input.strip():
            continue
        if user_input.strip() in ("quit", "exit"):
            break

        history.append({"role": "user", "content": user_input})

        messages = [{"role": "system", "content": system_prompt}] + history

        print("\033[1;32mAI>\033[0m ", end="", flush=True)
        try:
            content = chat_request(messages)
            print(content)
            history.append({"role": "assistant", "content": content})
        except Exception as e:
            print(f"(error: {e})")
            history.pop()  # remove failed user message

        print()

    print("\n終了")


if __name__ == "__main__":
    main()
