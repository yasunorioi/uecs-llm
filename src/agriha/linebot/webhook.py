"""LINE Webhook 受信エンドポイント (cmd_315 W4b).

FastAPI APIRouter として実装。
POST /webhook/line で LINE Messaging API からの Webhook を受信し、
X-Line-Signature ヘッダで HMAC-SHA256 署名検証を行う。

環境変数:
  LINE_CHANNEL_SECRET  チャネルシークレット（署名検証用）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from agriha.linebot.reflection_sender import handle_postback

logger = logging.getLogger("linebot_webhook")

router = APIRouter()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")


def verify_signature(body: bytes, signature: str, channel_secret: str = "") -> bool:
    """X-Line-Signature ヘッダの HMAC-SHA256 署名を検証する。

    Args:
        body: リクエストボディ (raw bytes)。
        signature: X-Line-Signature ヘッダ値 (Base64 encoded)。
        channel_secret: チャネルシークレット。空の場合は環境変数を使用。

    Returns:
        署名が正しければ True、それ以外は False。
    """
    secret = channel_secret or LINE_CHANNEL_SECRET
    if not secret:
        logger.warning("LINE_CHANNEL_SECRET が未設定。署名検証をスキップします")
        return True  # 開発環境では通過

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _dispatch_event(event: dict[str, Any]) -> None:
    """イベント種別に応じたハンドラへ振り分ける。

    Args:
        event: LINE Webhook event オブジェクト。
    """
    event_type = event.get("type")

    if event_type == "postback":
        try:
            result = handle_postback(event)
            logger.info("Postback処理: %s", result)
        except ValueError as exc:
            logger.warning("Postback解析エラー: %s", exc)

    elif event_type == "message":
        # 既存のチャットハンドラへ委譲（将来実装）
        logger.debug("MessageEvent 受信（チャットハンドラ未実装）: %s", event.get("message", {}).get("text", ""))

    else:
        logger.debug("未対応イベント: type=%s", event_type)


@router.post("/webhook/line")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(default=""),
) -> dict[str, str]:
    """LINE Webhook エンドポイント。

    Args:
        request: FastAPI Request オブジェクト。
        x_line_signature: X-Line-Signature ヘッダ。

    Returns:
        {"status": "ok"}

    Raises:
        HTTPException 400: 署名検証失敗時。
    """
    body = await request.body()

    if not verify_signature(body, x_line_signature):
        logger.warning("署名検証失敗。不正なリクエストを拒否します")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Webhook JSON パースエラー: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    events = payload.get("events", [])
    for event in events:
        _dispatch_event(event)

    return {"status": "ok"}
