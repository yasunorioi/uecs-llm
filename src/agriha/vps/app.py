"""
LINE Bot Webhook Handler — FastAPI

VPSはメッセージ転送のみ。LLM選択はRPi側AgriHAの設定画面で行う。
LINE Webhook → RPi agriha_chat.py (WG経由) → LINE Reply
"""

import os
import sqlite3
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    Configuration,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

from onboarding import cleanup_expired_qr, handle_follow, register_pubkey
from router import route_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# テストチャネル設定
LINE_CHANNEL_SECRET_TEST = os.getenv("LINE_CHANNEL_SECRET_TEST", "placeholder_test_secret")
LINE_CHANNEL_ACCESS_TOKEN_TEST = os.getenv("LINE_CHANNEL_ACCESS_TOKEN_TEST", "placeholder_test_token")

configuration_test = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN_TEST)
handler_test = WebhookHandler(LINE_CHANNEL_SECRET_TEST)

DB_PATH = os.getenv("CONVERSATION_DB_PATH", "/app/data/conversations.db")


def init_db() -> None:
    """SQLite DBを初期化し、conversationsテーブルを作成する（存在しなければ）。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id   TEXT NOT NULL,
                role      TEXT NOT NULL,
                message   TEXT NOT NULL,
                model     TEXT NOT NULL,
                session_id TEXT
            )
        """)
        conn.commit()
    logger.info(f"DB initialized: {DB_PATH}")


def log_message(user_id: str, role: str, message: str, model: str, session_id: str | None = None) -> None:
    """会話ログをSQLiteに書き込む。失敗してもメッセージ処理を妨げない。"""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO conversations (timestamp, user_id, role, message, model, session_id) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, user_id, role, message, model, session_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to log message to DB: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="agriha-linebot", lifespan=lifespan)


@app.get("/health")
async def health():
    """ヘルスチェック"""
    return {"status": "ok"}


@app.post("/callback")
async def callback(request: Request):
    """LINE Webhook受信エンドポイント"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return {"status": "ok"}


@app.post("/callback/test")
async def callback_test(request: Request):
    """LINE Webhook受信エンドポイント（テストチャネル）"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        handler_test.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return {"status": "ok"}


@handler_test.add(MessageEvent, message=TextMessageContent)
def handle_text_message_test(event: MessageEvent):
    """テストチャネル: 本番と同じくRPi転送。"""
    user_text = event.message.text
    user_id = event.source.user_id or "unknown"
    logger.info(f"[TEST] Received: {user_text[:80]}")

    log_message(user_id=user_id, role="user", message=user_text, model="router", session_id="test")
    route_message(reply_token=event.reply_token, user_id=user_id, message=user_text)
    logger.info(f"[TEST] Routed: user_id={user_id}")


@handler.add(FollowEvent)
def handle_follow_event(event: FollowEvent):
    """友達追加イベント: onboarding.py経由でWG設定ブロックをLINE返信する。"""
    user_id = event.source.user_id or "unknown"
    logger.info(f"Follow event: user_id={user_id}")
    handle_follow(reply_token=event.reply_token, user_id=user_id)


@app.post("/api/cleanup_qr")
async def api_cleanup_qr():
    """期限切れQR画像のクリーンアップ。cronから呼ぶ想定。"""
    removed = cleanup_expired_qr()
    return {"removed": removed}


@app.post("/api/register_pubkey")
async def api_register_pubkey(request: Request):
    """
    RPi側からWG公開鍵を受け取り、wg-farmers.confとfarmers_secrets.yamlを更新する。

    Body: {"farmer_id": "farmer_a", "public_key": "<WG_PUBLIC_KEY>"}
    """
    data = await request.json()
    farmer_id = data.get("farmer_id")
    public_key = data.get("public_key")
    if not farmer_id or not public_key:
        raise HTTPException(status_code=400, detail="farmer_id and public_key are required")
    result = register_pubkey(farmer_id=farmer_id, public_key=public_key)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    """テキストメッセージをRPi agriha_chat.pyに転送する。"""
    user_text = event.message.text
    user_id = event.source.user_id or "unknown"
    logger.info(f"Received: {user_text[:80]}")

    log_message(user_id=user_id, role="user", message=user_text, model="router")
    route_message(reply_token=event.reply_token, user_id=user_id, message=user_text)
    logger.info(f"Routed: user_id={user_id}")
