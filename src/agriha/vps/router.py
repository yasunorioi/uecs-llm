"""
LINE Bot メッセージルーター

VPSはメッセージ転送のみ。LLM選択はRPi側AgriHAの設定画面で行う。

LINE userId → farmers_secrets.yaml → farmer_id 特定
→ farmers.yaml → rpi_host 取得
→ HTTP POST で RPi agriha_chat.py に転送
→ LINE reply API で農家に返信
"""

import logging
import os
from pathlib import Path

import httpx
import yaml
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", Path(__file__).parent.parent / "config"))
RPI_CHAT_TIMEOUT = float(os.getenv("RPI_CHAT_TIMEOUT", "30"))

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


def _load_farmers() -> tuple[dict, dict]:
    """farmers.yaml + farmers_secrets.yaml を読み込む"""
    with open(CONFIG_DIR / "farmers.yaml") as f:
        farmers = yaml.safe_load(f)
    with open(CONFIG_DIR / "farmers_secrets.yaml") as f:
        secrets = yaml.safe_load(f)
    return farmers, secrets


def _resolve_farmer_id(user_id: str, secrets: dict) -> str | None:
    """LINE userID → farmer_id を特定する。未登録なら None を返す。"""
    for fid, sec in secrets.get("farmers", {}).items():
        if sec.get("line_user_id") == user_id:
            return fid
    return None


def route_message(reply_token: str, user_id: str, message: str) -> None:
    """
    メッセージをRPi agriha_chat.py に転送し、LINE reply APIで返信する。

    LLM選択はRPi側（AgriHA設定画面）で行う。VPSは転送のみ。
    未登録ユーザー・非アクティブ農家・通信エラー時は適切なエラーメッセージを返す。
    """
    try:
        farmers, secrets = _load_farmers()
    except Exception as e:
        logger.error("farmers設定読み込み失敗: %s", e)
        _reply(reply_token, "設定の読み込みに失敗しました。管理者にお問い合わせください。")
        return

    farmer_id = _resolve_farmer_id(user_id, secrets)
    if farmer_id is None:
        logger.warning("未登録ユーザー: %s", user_id)
        _reply(reply_token, "登録されていないユーザーです。管理者にお問い合わせください。")
        return

    farmer = farmers.get("farmers", {}).get(farmer_id)
    if farmer is None:
        logger.error("farmers.yaml に farmer_id 未登録: %s", farmer_id)
        _reply(reply_token, "農家設定が見つかりません。管理者にお問い合わせください。")
        return

    if farmer.get("status") != "active":
        logger.warning("非アクティブ農家: %s status=%s", farmer_id, farmer.get("status"))
        _reply(reply_token, "アカウントは現在ご利用できません。管理者にお問い合わせください。")
        return

    rpi_host = farmer["rpi_host"]
    rpi_port = farmer.get("rpi_chat_port", 8502)
    rpi_url = f"http://{rpi_host}:{rpi_port}/api/chat"

    try:
        with httpx.Client(timeout=RPI_CHAT_TIMEOUT) as client:
            resp = client.post(rpi_url, json={"message": message, "user_id": user_id})
            resp.raise_for_status()
            data = resp.json()
            response_text = data.get("response", "応答を受信できませんでした。")
    except httpx.ConnectError:
        logger.warning("RPi接続失敗: %s", rpi_url)
        response_text = "ハウスとの通信に失敗しました。しばらくしてから再度お試しください。"
    except httpx.TimeoutException:
        logger.warning("RPiタイムアウト: %s", rpi_url)
        response_text = "ハウスからの応答がタイムアウトしました。"
    except Exception as e:
        logger.error("RPi通信エラー: %s", e)
        response_text = "エラーが発生しました。しばらくしてから再度お試しください。"

    _reply(reply_token, response_text)


def _reply(reply_token: str, text: str) -> None:
    """LINE reply APIで返信する"""
    with ApiClient(_configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
