"""
LINE Bot オンボーディング処理

follow event → userId仮登録 → Base64設定ブロック生成 → LINE返信
POST /api/register_pubkey → wg-farmers.conf更新 + farmers_secrets.yaml更新

設計書: docs/multi_farmer_design.md §5
"""

import base64
import logging
import os
import subprocess
from pathlib import Path

import yaml
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", Path(__file__).parent.parent / "config"))
WG_CONF_PATH = Path(os.getenv("WG_CONF_PATH", "/etc/wireguard/wg-farmers.conf"))
# MBP WGサーバの公開鍵（環境変数で設定）
WG_SERVER_PUBLIC_KEY = os.getenv("WG_SERVER_PUBLIC_KEY", "")
# VPS経由のWGサーバエンドポイント（例: "203.0.113.1:51821"）
WG_SERVER_ENDPOINT = os.getenv("WG_SERVER_ENDPOINT", "")
# RPiがMBP APIに到達するためのエンドポイント
MBP_API_ENDPOINT = os.getenv("MBP_API_ENDPOINT", "http://10.20.0.1:5000")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


def _load_secrets() -> dict:
    path = CONFIG_DIR / "farmers_secrets.yaml"
    if not path.exists():
        return {"farmers": {}}
    with open(path) as f:
        return yaml.safe_load(f) or {"farmers": {}}


def _save_secrets(secrets: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_DIR / "farmers_secrets.yaml", "w") as f:
        yaml.dump(secrets, f, allow_unicode=True, default_flow_style=False)


def _next_wg_ip(secrets: dict) -> str:
    """既存農家のWG IPから次の割当IPを決定する（10.20.0.N0、N=1,2,3,...）"""
    used_ips = {v.get("wg_ip") for v in secrets.get("farmers", {}).values()}
    for n in range(1, 100):
        candidate = f"10.20.0.{n * 10}"
        if candidate not in used_ips:
            return candidate
    raise ValueError("割当可能なWG IPが枯渇しました")


def _next_farmer_id(secrets: dict) -> str:
    """既存農家IDから次のfarmer_idを生成する（farmer_a, farmer_b, ...）"""
    existing = set(secrets.get("farmers", {}).keys())
    for n in range(26):
        fid = f"farmer_{chr(ord('a') + n)}"
        if fid not in existing:
            return fid
    return f"farmer_{len(existing) + 1}"


def handle_follow(reply_token: str, user_id: str) -> None:
    """
    友達追加イベント処理:
    1. 既登録チェック
    2. farmers_secrets.yaml に pending 追記
    3. Base64設定ブロック生成（★秘密鍵は含めない★）
    4. LINE返信「設定コードをRPiに貼ってね」+ Base64ブロック
    """
    secrets = _load_secrets()

    # 既登録チェック
    for fid, sec in secrets.get("farmers", {}).items():
        if sec.get("line_user_id") == user_id:
            logger.info("既登録ユーザーがfollow: %s → %s", user_id, fid)
            _reply(
                reply_token,
                f"すでに登録済みです（{fid}）。設定済みのRPiからご利用ください。",
            )
            return

    farmer_id = _next_farmer_id(secrets)
    wg_ip = _next_wg_ip(secrets)

    # farmers_secrets.yaml に pending 追記
    secrets.setdefault("farmers", {})[farmer_id] = {
        "line_user_id": user_id,
        "wg_ip": wg_ip,
        "wg_public_key": None,  # RPiから受け取るまで None
        "status": "pending",
    }
    _save_secrets(secrets)
    logger.info("仮登録: user_id=%s → farmer_id=%s wg_ip=%s", user_id, farmer_id, wg_ip)

    # Base64設定ブロック生成（★wg_client_private_key は含めない★）
    config_block = {
        "farmer_id": farmer_id,
        "wg_server_public_key": WG_SERVER_PUBLIC_KEY,
        "wg_server_endpoint": WG_SERVER_ENDPOINT,
        "wg_client_ip": f"{wg_ip}/32",
        "api_endpoint": MBP_API_ENDPOINT,
    }
    b64 = base64.b64encode(
        yaml.dump(config_block, allow_unicode=True, default_flow_style=False).encode()
    ).decode()

    msg = (
        "登録ありがとうございます！\n"
        "以下の設定コードをRPiのWeb画面（設定タブ）に貼り付けてください。\n\n"
        f"{b64}"
    )
    _reply(reply_token, msg)


def register_pubkey(farmer_id: str, public_key: str) -> dict:
    """
    RPiからの公開鍵受信API処理:
    1. wg-farmers.conf に Peer セクション追加
    2. `wg set` で動的にPeer追加（wg-quick restart不要）
    3. farmers_secrets.yaml を pending → active に更新
    4. LINE push通知「接続完了」
    """
    secrets = _load_secrets()
    farmer_sec = secrets.get("farmers", {}).get(farmer_id)
    if farmer_sec is None:
        return {"error": "farmer_not_found", "farmer_id": farmer_id}

    wg_ip = farmer_sec["wg_ip"]

    # wg-farmers.conf に Peer セクション追加（永続化用）
    peer_block = (
        f"\n[Peer]\n"
        f"# farmer_id: {farmer_id}\n"
        f"PublicKey = {public_key}\n"
        f"AllowedIPs = {wg_ip}/32\n"
    )
    try:
        with open(WG_CONF_PATH, "a") as f:
            f.write(peer_block)
        logger.info("wg-farmers.conf に Peer 追加: %s (%s)", farmer_id, wg_ip)
    except Exception as e:
        logger.error("wg-farmers.conf 書き込み失敗: %s", e)
        return {"error": "wg_conf_write_failed", "message": str(e)}

    # 動的にPeer追加（インターフェースの再起動不要）
    try:
        subprocess.run(
            ["sudo", "wg", "set", "wg-farmers", "peer", public_key, "allowed-ips", f"{wg_ip}/32"],
            check=True,
            capture_output=True,
        )
        logger.info("wg set peer 成功: %s", farmer_id)
    except Exception as e:
        logger.warning("wg set peer 失敗（手動 wg-quick reload が必要）: %s", e)

    # farmers_secrets.yaml を pending → active に更新
    farmer_sec["wg_public_key"] = public_key
    farmer_sec["status"] = "active"
    _save_secrets(secrets)
    logger.info("農家登録完了: %s", farmer_id)

    # LINE push通知
    _push(farmer_sec["line_user_id"], "接続完了！チャットタブから話しかけてください。")

    return {"status": "registered", "farmer_id": farmer_id, "wg_ip": wg_ip}


def _reply(reply_token: str, text: str) -> None:
    with ApiClient(_configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def _push(user_id: str, text: str) -> None:
    try:
        with ApiClient(_configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=text)],
                )
            )
    except Exception as e:
        logger.warning("LINE push失敗: %s", e)
