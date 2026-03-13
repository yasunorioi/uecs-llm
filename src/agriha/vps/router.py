"""
LINE Bot メッセージルーター

LINE userId → farmers_secrets.yaml → farmer_id 特定
→ farmers.yaml → rpi_host 取得
→ HTTP POST で RPi agriha_chat.py に転送
→ LINE reply API で農家に返信

backend == "ollama" の場合は MBP localhost:11434 に直接POST
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
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))

# デフォルトLLMバックエンド設定（環境変数）
_DEFAULT_BACKEND = os.getenv("LLM_BACKEND", "claude")  # claude | ollama
_DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# ユーザーごとバックエンド設定（インメモリ）
# {user_id: {"backend": "ollama", "model": "qwen3:8b"}}
user_backends: dict[str, dict] = {}


def get_user_backend(user_id: str) -> dict:
    """ユーザーのバックエンド設定を返す。未設定時はデフォルト値。"""
    if user_id in user_backends:
        return user_backends[user_id]
    return {"backend": _DEFAULT_BACKEND, "model": _DEFAULT_OLLAMA_MODEL}


def set_user_backend(user_id: str, backend: str, model: str | None = None) -> None:
    """ユーザーのバックエンド設定を更新する。"""
    user_backends[user_id] = {
        "backend": backend,
        "model": model or _DEFAULT_OLLAMA_MODEL,
    }


def get_available_models() -> list[str]:
    """ollamaで利用可能なモデル一覧を返す。接続失敗時は空リスト。"""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [m["name"] for m in models]
    except Exception as e:
        logger.warning("ollama /api/tags 取得失敗: %s", e)
        return []


def handle_model_command(user_id: str, text: str) -> str:
    """
    /model コマンドを処理し、返答テキストを返す。

    /model           → 現在の設定を表示
    /models          → 利用可能モデル一覧
    /model claude    → claudeバックエンドに切替
    /model qwen3:8b  → ollama + qwen3:8b に切替
    /model ollama qwen3:32b → ollama + qwen3:32b に切替
    """
    parts = text.strip().split()
    cmd = parts[0].lower()  # /model または /models

    if cmd == "/models":
        models = get_available_models()
        if not models:
            return "ollamaへの接続に失敗しました。利用可能モデルを取得できません。"
        return "利用可能なモデル:\n" + "\n".join(f"- {m}" for m in models)

    # /model コマンド
    if len(parts) == 1:
        # 現在の設定を表示
        cfg = get_user_backend(user_id)
        if cfg["backend"] == "claude":
            return f"現在のバックエンド: claude (RPi経由)\n切替: /model qwen3:8b"
        else:
            return f"現在のバックエンド: ollama ({cfg['model']})\n切替: /model claude"

    arg1 = parts[1].lower()

    if arg1 == "claude":
        set_user_backend(user_id, "claude")
        return "バックエンドを claude (RPi経由) に切替えました。"

    if arg1 == "ollama":
        model = parts[2] if len(parts) >= 3 else _DEFAULT_OLLAMA_MODEL
        set_user_backend(user_id, "ollama", model)
        return f"バックエンドを ollama ({model}) に切替えました。"

    # /model qwen3:8b 形式（ollama省略）
    model_name = parts[1]
    set_user_backend(user_id, "ollama", model_name)
    return f"バックエンドを ollama ({model_name}) に切替えました。"


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


def _call_ollama(message: str, model: str) -> str:
    """ollamaにメッセージを送信して応答を返す。"""
    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": message}],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
    except httpx.ConnectError:
        logger.warning("ollama接続失敗: %s", OLLAMA_URL)
        return "ローカルLLMへの接続に失敗しました。"
    except httpx.TimeoutException:
        logger.warning("ollamaタイムアウト: model=%s", model)
        return "ローカルLLMの応答がタイムアウトしました。"
    except Exception as e:
        logger.error("ollama呼び出しエラー: %s", e)
        return "エラーが発生しました。しばらくしてから再度お試しください。"


def route_message(reply_token: str, user_id: str, message: str) -> None:
    """
    メッセージをバックエンドに転送し、LINE reply APIで返信する。

    backend == "claude": RPi agriha_chat.py に転送（既存動作）
    backend == "ollama": MBP localhost:11434 に直接POST

    未登録ユーザー・非アクティブ農家・通信エラー時は適切なエラーメッセージを返す。
    """
    cfg = get_user_backend(user_id)
    backend = cfg["backend"]

    if backend == "ollama":
        model = cfg["model"]
        logger.info("ollama backend: user=%s model=%s", user_id, model)
        response_text = _call_ollama(message, model)
        _reply(reply_token, response_text)
        return

    # backend == "claude": RPi転送（既存動作）
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
