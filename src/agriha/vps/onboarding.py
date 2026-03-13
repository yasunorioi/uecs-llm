"""
LINE Bot オンボーディング処理

follow event → userId仮登録 → Base64設定ブロック生成 → LINE返信
POST /api/register_pubkey → wg-farmers.conf更新 + farmers_secrets.yaml更新

設計書: docs/multi_farmer_design.md §5
"""

import base64
import hashlib
import logging
import os
import subprocess
import time
from pathlib import Path

import qrcode
import yaml
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
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
# QR画像の配信設定
QR_DIR = Path(os.getenv("QR_DIR", "/var/www/qr"))
QR_BASE_URL = os.getenv("QR_BASE_URL", "")

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


def _generate_qr_png(data: str, farmer_id: str, user_id: str) -> str:
    """Base64文字列からQR画像を生成し、URLを返す。

    Args:
        data: QRコードに埋め込むデータ（Base64エンコード済み設定ブロック）
        farmer_id: ファイル名のプレフィックス用
        user_id: LINE userId（ファイル名の一意性保証 + URL推測防止）

    Returns:
        QR画像の公開URL
    """
    QR_DIR.mkdir(parents=True, exist_ok=True)
    # farmer_id + user_id で決定的にファイル名を生成（同時リクエストでも衝突しない）
    token = hashlib.sha256(f"{farmer_id}:{user_id}".encode()).hexdigest()[:12]
    filename = f"{farmer_id}_{token}.png"
    filepath = QR_DIR / filename

    img = qrcode.make(data, box_size=10, border=4)
    img.save(str(filepath))
    logger.info("QR画像生成: %s", filepath)
    return f"{QR_BASE_URL}/{filename}"


def cleanup_expired_qr(max_age_hours: int = 24) -> int:
    """指定時間以上経過したQR画像を削除する。

    Returns:
        削除したファイル数
    """
    if not QR_DIR.exists():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in QR_DIR.glob("*.png"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        logger.info("期限切れQR画像を%d件削除", removed)
    return removed


def _next_farmer_id(secrets: dict) -> str:
    """既存農家IDから次のfarmer_idを生成する（farmer_a, farmer_b, ...）"""
    existing = set(secrets.get("farmers", {}).keys())
    for n in range(26):
        fid = f"farmer_{chr(ord('a') + n)}"
        if fid not in existing:
            return fid
    return f"farmer_{len(existing) + 1}"


def _send_onboarding_qr(reply_token: str, user_id: str, farmer_id: str, wg_ip: str) -> None:
    """Base64設定ブロック + QR画像を生成してLINE返信する。

    新規登録とpending再followの両方から呼ばれる共通処理。
    """
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

    # QR画像生成 → ImageMessage送信
    text_msg = (
        "登録ありがとうございます！\n"
        "以下のQRコードをRPiのWeb画面（設定タブ）で読み取ってください。\n"
        "QRが読めない場合は、下のテキストコードを貼り付けてください。"
    )
    try:
        qr_url = _generate_qr_png(b64, farmer_id, user_id)
        _reply_with_image(reply_token, text_msg, qr_url, fallback_text=b64)
    except Exception as e:
        logger.warning("QR生成失敗、テキストのみ送信: %s", e)
        msg = f"{text_msg}\n\n{b64}"
        _reply(reply_token, msg)


def handle_follow(reply_token: str, user_id: str) -> None:
    """
    友達追加イベント処理:
    1. 既登録チェック（active → 案内、pending → QR再送）
    2. 新規: farmers_secrets.yaml に pending 追記
    3. Base64設定ブロック + QR生成 → LINE返信
    """
    secrets = _load_secrets()

    # 既登録チェック
    for fid, sec in secrets.get("farmers", {}).items():
        if sec.get("line_user_id") == user_id:
            status = sec.get("status", "pending")
            if status == "active":
                logger.info("active農家が再follow: %s → %s", user_id, fid)
                _reply(
                    reply_token,
                    f"すでに登録済みです（{fid}）。設定済みのRPiからご利用ください。",
                )
                return
            # pending: ブロック解除→再follow等。QR/Base64を再送する
            logger.info("pending農家が再follow、QR再送: %s → %s", user_id, fid)
            _send_onboarding_qr(reply_token, user_id, fid, sec["wg_ip"])
            return

    farmer_id = _next_farmer_id(secrets)
    try:
        wg_ip = _next_wg_ip(secrets)
    except ValueError:
        logger.error("WG IP枯渇: 新規登録不可 user_id=%s", user_id)
        _reply(
            reply_token,
            "申し訳ありません。現在新規登録を受け付けられません。管理者にお問い合わせください。",
        )
        return

    # farmers_secrets.yaml に pending 追記
    secrets.setdefault("farmers", {})[farmer_id] = {
        "line_user_id": user_id,
        "wg_ip": wg_ip,
        "wg_public_key": None,  # RPiから受け取るまで None
        "status": "pending",
    }
    _save_secrets(secrets)
    logger.info("仮登録: user_id=%s → farmer_id=%s wg_ip=%s", user_id, farmer_id, wg_ip)

    _send_onboarding_qr(reply_token, user_id, farmer_id, wg_ip)


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


def _reply_with_image(
    reply_token: str, text: str, image_url: str, fallback_text: str | None = None
) -> None:
    """TextMessage + ImageMessage を1回のreplyで送信する。"""
    messages = [TextMessage(text=text)]
    messages.append(
        ImageMessage(
            original_content_url=image_url,
            preview_image_url=image_url,
        )
    )
    if fallback_text:
        messages.append(TextMessage(text=fallback_text))
    with ApiClient(_configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
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
