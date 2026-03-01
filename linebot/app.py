"""
LINE Bot Webhook Handler — FastAPI

LINE Webhook → LLM API → LINE Reply
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
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# TODO: v2 LLMクライアントに差し替え
# from llm_client import generate_response_sync, check_llm_health, MODEL_NAME
MODEL_NAME = "placeholder"  # v2では agriha_client.py 等に差し替え


async def check_llm_health() -> bool:  # TODO: v2 LLMヘルスチェックに差し替え
    return True


def generate_response_sync(user_text: str) -> str:  # TODO: v2 LLM呼び出しに差し替え
    return "LLMクライアント未設定です。v2クライアントを設定してください。"
from quiz_scenarios import get_random_quiz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
    ok = await check_llm_health()
    if ok:
        logger.info("LLM client is ready.")
    else:
        logger.warning("LLM client is NOT ready.")
    yield


app = FastAPI(title="agriha-linebot", lifespan=lifespan)


@app.get("/health")
async def health():
    """ヘルスチェック"""
    llm_ok = await check_llm_health()
    return {"status": "ok", "llm": "up" if llm_ok else "down"}


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


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    """
    テキストメッセージを受信し、LLMに投げてLINE Replyする。
    WebhookHandlerは同期ハンドラのみ対応のため、同期APIを使用。
    """
    user_text = event.message.text
    user_id = event.source.user_id or "unknown"
    logger.info(f"Received: {user_text[:80]}")

    # ユーザーメッセージをDBに記録
    log_message(user_id=user_id, role="user", message=user_text, model=MODEL_NAME)

    # 「問題集」キーワードでランダム出題
    if user_text.strip() in ("問題集", "問題", "クイズ", "quiz"):
        llm_response = get_random_quiz()
        used_model = "quiz"
        logger.info("Quiz mode: random scenario selected")
    else:
        used_model = MODEL_NAME
        try:
            llm_response = generate_response_sync(user_text)

            if not llm_response:
                llm_response = "申し訳ありません、応答を生成できませんでした。"

        except Exception as e:
            logger.error(f"LLM error: {e}")
            llm_response = "エラーが発生しました。しばらくしてから再度お試しください。"

    # LINE Reply（同期API）
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=llm_response)],
            )
        )

    # アシスタント応答をDBに記録
    log_message(user_id=user_id, role="assistant", message=llm_response, model=used_model)

    logger.info(f"Replied: {llm_response[:80]}")
