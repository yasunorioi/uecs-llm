"""NullClaw OpenAI互換プロキシサーバー (port 3001)

NullClaw CLI (-m) をOpenAI SDK互換エンドポイントとして公開する。
forecast_engine.pyはこのプロキシ経由でNullClawを呼び出す。

制限事項:
  - NullClaw CLIはtool callingに対応しない
  - センサーデータを事前取得してプロンプトに埋め込む方式で代替
  - レスポンスはtool_callsなしのtextのみ

起動:
  uvicorn agriha.control.nullclaw_proxy:app --host 127.0.0.1 --port 3001
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("nullclaw_proxy")

UNIPI_API_URL = os.getenv("UNIPI_API_URL", "http://localhost:8080")
NULLCLAW_TIMEOUT = int(os.getenv("NULLCLAW_TIMEOUT", "60"))

app = FastAPI(title="NullClaw OpenAI-Compatible Proxy", version="1.0")


def _fetch_sensors(api_url: str = UNIPI_API_URL) -> dict:
    """センサーデータを取得する。失敗時は空dict。"""
    try:
        r = httpx.get(f"{api_url}/api/sensors", timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning("センサー取得失敗: %s", e)
    return {}


def _fetch_status(api_url: str = UNIPI_API_URL) -> dict:
    """デーモン状態を取得する。失敗時は空dict。"""
    try:
        r = httpx.get(f"{api_url}/api/status", timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning("状態取得失敗: %s", e)
    return {}


def _build_prompt(messages: list[dict]) -> str:
    """messagesリストからNullClaw用プロンプトを構築する。

    センサーデータを事前取得してプロンプトに埋め込むことで
    tool callingの代替とする。
    """
    # system + 最後のuserメッセージを抽出
    system_content = ""
    user_content = ""
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            system_content = content[:500]  # 長すぎる場合は先頭500文字
        elif role == "user":
            user_content = content

    # センサーデータを事前取得してプロンプトに注入
    sensors = _fetch_sensors()
    status = _fetch_status()

    sensor_block = ""
    if sensors:
        sensor_block = f"\n## センサーデータ\n{json.dumps(sensors, ensure_ascii=False, indent=2)}\n"
    if status:
        sensor_block += f"\n## システム状態\n{json.dumps(status, ensure_ascii=False, indent=2)}\n"

    parts = []
    if system_content:
        parts.append(f"[システム指示]: {system_content}")
    if sensor_block:
        parts.append(sensor_block)
    if user_content:
        parts.append(user_content)

    return "\n\n".join(parts)


def _call_nullclaw(prompt: str, timeout: int = NULLCLAW_TIMEOUT) -> str:
    """nullclaw agent -m でCLIを呼び出す。失敗時はエラーメッセージを返す。"""
    try:
        result = subprocess.run(
            ["nullclaw", "agent", "-m", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("nullclaw終了コード %d: %s", result.returncode, result.stderr[:200])
        return result.stdout.strip() or result.stderr.strip() or "(NullClaw: 出力なし)"
    except FileNotFoundError:
        logger.error("nullclawバイナリが見つかりません")
        raise HTTPException(status_code=503, detail="nullclawバイナリが見つかりません。インストール確認が必要です。")
    except subprocess.TimeoutExpired:
        logger.error("nullclaw タイムアウト (%ds)", timeout)
        raise HTTPException(status_code=504, detail=f"NullClaw タイムアウト ({timeout}s)")


def _make_openai_response(content: str, model: str = "nullclaw-local") -> dict:
    """NullClaw出力をOpenAI chat.completions形式に変換する。"""
    return {
        "id": f"nullclaw-{uuid4()}",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "model": model,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
async def health() -> JSONResponse:
    """ヘルスチェック。"""
    return JSONResponse({"status": "ok"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """OpenAI互換エンドポイント。NullClaw CLIを呼び出して結果を返す。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="リクエストボディのJSONパースに失敗")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messagesが空です")

    model = body.get("model", "nullclaw-local")
    prompt = _build_prompt(messages)
    output = _call_nullclaw(prompt)

    return JSONResponse(_make_openai_response(output, model=model))
