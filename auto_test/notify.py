#!/usr/bin/env python3
"""
notify.py - 通知送信モジュール

対応サービス:
    - LINE Notify
    - Slack Webhook
    - Discord Webhook
    - Email (SMTP)
"""

import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore

logger = logging.getLogger(__name__)


def send_line_notify(token: str, message: str) -> bool:
    """
    LINE Notifyでメッセージを送信

    Args:
        token: LINE Notify アクセストークン
        message: 送信するメッセージ（最大1000文字）

    Returns:
        送信成功時True

    取得方法:
        1. https://notify-bot.line.me/ja/ にアクセス
        2. ログイン -> マイページ -> トークン発行
        3. トークン名と通知先を選択して発行
    """
    if not requests:
        logger.error("requests module not installed")
        return False

    if not token:
        logger.error("LINE token is empty")
        return False

    # メッセージ長制限
    if len(message) > 1000:
        message = message[:997] + "..."

    headers = {"Authorization": f"Bearer {token}"}
    data = {"message": message}

    try:
        response = requests.post(
            "https://notify-api.line.me/api/notify",
            headers=headers,
            data=data,
            timeout=10
        )

        if response.status_code == 200:
            logger.info("LINE notification sent successfully")
            return True
        else:
            logger.error(f"LINE API error: {response.status_code} - {response.text}")
            return False

    except requests.exceptions.Timeout:
        logger.error("LINE API timeout")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"LINE API request failed: {e}")
        return False


def send_slack_webhook(webhook_url: str, message: str, username: str = "IoT Test Bot") -> bool:
    """
    Slack Webhookでメッセージを送信

    Args:
        webhook_url: Slack Incoming Webhook URL
        message: 送信するメッセージ
        username: 表示名

    Returns:
        送信成功時True
    """
    if not requests:
        logger.error("requests module not installed")
        return False

    payload = {
        "text": message,
        "username": username
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            logger.info("Slack notification sent successfully")
            return True
        else:
            logger.error(f"Slack API error: {response.status_code}")
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f"Slack API request failed: {e}")
        return False


def send_discord_webhook(webhook_url: str, message: str, username: str = "IoT Test Bot") -> bool:
    """
    Discord Webhookでメッセージを送信

    Args:
        webhook_url: Discord Webhook URL
        message: 送信するメッセージ（最大2000文字）
        username: 表示名

    Returns:
        送信成功時True
    """
    if not requests:
        logger.error("requests module not installed")
        return False

    # メッセージ長制限
    if len(message) > 2000:
        message = message[:1997] + "..."

    payload = {
        "content": message,
        "username": username
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10
        )

        if response.status_code in [200, 204]:
            logger.info("Discord notification sent successfully")
            return True
        else:
            logger.error(f"Discord API error: {response.status_code}")
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f"Discord API request failed: {e}")
        return False


def send_email(
    smtp_host: str,
    smtp_port: int,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_tls: bool = True
) -> bool:
    """
    SMTPでメールを送信

    Args:
        smtp_host: SMTPサーバーホスト
        smtp_port: SMTPポート
        from_addr: 送信元アドレス
        to_addr: 送信先アドレス
        subject: 件名
        body: 本文
        username: SMTP認証ユーザー名
        password: SMTP認証パスワード
        use_tls: TLS使用フラグ

    Returns:
        送信成功時True
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if use_tls:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)

        logger.info(f"Email sent to {to_addr}")
        return True

    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return False
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


# テスト用
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python3 notify.py <service> <token/url> [message]")
        print("  service: line, slack, discord")
        sys.exit(1)

    service = sys.argv[1]
    credential = sys.argv[2]
    message = sys.argv[3] if len(sys.argv) > 3 else "Test notification from IoT Auto Test"

    if service == "line":
        success = send_line_notify(credential, message)
    elif service == "slack":
        success = send_slack_webhook(credential, message)
    elif service == "discord":
        success = send_discord_webhook(credential, message)
    else:
        print(f"Unknown service: {service}")
        sys.exit(1)

    print("Success" if success else "Failed")
    sys.exit(0 if success else 1)
