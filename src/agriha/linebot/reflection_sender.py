"""LINE Bot 反省会メッセージ送受信 (cmd_315 W4b).

LINE Messaging API (Push Message) を使い、農家への反省会メッセージを
Quick Reply A/B/C 付きで送信し、Postback 回答を処理する。

依存: urllib.request のみ（line-bot-sdk 不要）
環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャネルアクセストークン
  LINE_FARMER_USER_ID        農家の LINE user ID
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("reflection_sender")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_FARMER_USER_ID = os.environ.get("LINE_FARMER_USER_ID", "")

# Quick Reply ボタンラベル (≤20文字)
CHOICE_LABELS: dict[str, str] = {
    "A": "A: 正解！採用",
    "B": "B: 見直したい",
    "C": "C: わからない",
}

_MAX_MESSAGES_PER_REQUEST = 5  # LINE API上限


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------


def _push_message(to: str, messages: list[dict[str, Any]], token: str = "") -> None:
    """LINE Push Message API を呼び出す。messages は最大5件ずつバッチ送信。

    Args:
        to: 送信先 LINE user ID。
        messages: LINE message オブジェクトのリスト。
        token: アクセストークン。空の場合は環境変数 LINE_CHANNEL_ACCESS_TOKEN を使用。

    Raises:
        RuntimeError: アクセストークンが未設定の場合。
        urllib.error.HTTPError: LINE API がエラーを返した場合。
    """
    access_token = token or LINE_CHANNEL_ACCESS_TOKEN
    if not access_token:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    # 5件ずつバッチ送信
    for i in range(0, len(messages), _MAX_MESSAGES_PER_REQUEST):
        batch = messages[i : i + _MAX_MESSAGES_PER_REQUEST]
        body = json.dumps({"to": to, "messages": batch}).encode("utf-8")
        req = urllib.request.Request(LINE_PUSH_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            logger.error("LINE Push失敗: %s %s", exc.code, exc.read())
            raise


# ---------------------------------------------------------------------------
# メッセージ構築
# ---------------------------------------------------------------------------


def build_reflection_message(memo: dict[str, Any]) -> dict[str, Any]:
    """reflection_memo 1件から LINE TextMessage（Quick Reply A/B/C 付き）を構築する。

    Args:
        memo: reflection_memo レコード。
              期待フィールド: memo_id, query, frequency, confidence, question_text

    Returns:
        LINE Messaging API の TextMessage オブジェクト (dict)。
    """
    memo_id = memo.get("memo_id") or memo.get("id", 0)
    query = memo.get("query", "")
    frequency = memo.get("frequency", 0)
    confidence = memo.get("confidence", 0.0)
    question = memo.get("question_text") or memo.get("question", f"「{query}」パターンを採用しますか？")

    text = (
        f"📊 反省会 #{memo_id}\n"
        f"クエリ: {query}\n"
        f"頻度: {frequency}回 / 確度: {confidence:.0%}\n"
        f"\n{question}"
    )

    quick_reply_items = [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": CHOICE_LABELS[choice],
                "data": json.dumps(
                    {"action": "reflection", "memo_id": memo_id, "answer": choice},
                    ensure_ascii=False,
                ),
                "displayText": CHOICE_LABELS[choice],
            },
        }
        for choice in ("A", "B", "C")
    ]

    return {
        "type": "text",
        "text": text,
        "quickReply": {"items": quick_reply_items},
    }


# ---------------------------------------------------------------------------
# 送信
# ---------------------------------------------------------------------------


def send_reflection(
    memos: list[dict[str, Any]],
    user_id: str = "",
    token: str = "",
) -> None:
    """reflection_memo リストを LINE Push Message で送信する。

    各 memo を 1 メッセージとして build_reflection_message で構築し、
    LINE Push API へ送信する（5件ずつバッチ）。

    Args:
        memos: reflection_memo レコードのリスト。
        user_id: 送信先 LINE user ID。空の場合は LINE_FARMER_USER_ID を使用。
        token: アクセストークン。空の場合は環境変数を使用。
    """
    to = user_id or LINE_FARMER_USER_ID
    if not to:
        logger.warning("LINE_FARMER_USER_ID が未設定。送信をスキップします")
        return

    messages = [build_reflection_message(memo) for memo in memos]
    if not messages:
        logger.info("送信する memo がありません")
        return

    _push_message(to, messages, token=token)
    logger.info("反省会メッセージ送信完了: %d件", len(messages))


# ---------------------------------------------------------------------------
# Postback 受信
# ---------------------------------------------------------------------------


def handle_postback(
    event_data: dict[str, Any],
    process_answer_fn: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """LINE Postback イベントデータを解析し、回答を処理する。

    Args:
        event_data: LINE Webhook の Postback event オブジェクト。
                    期待構造: {"postback": {"data": "<JSON string>"}, ...}
        process_answer_fn: 回答処理コールバック。
                           シグネチャ: (memo_id: int, answer: str) -> None
                           None の場合は agriha.control.reflection.process_answer を遅延インポート。

    Returns:
        {"status": "ok", "memo_id": N, "answer": "A"} or {"status": "skipped"}

    Raises:
        ValueError: postback data が不正な形式の場合。
    """
    try:
        raw_data = event_data.get("postback", {}).get("data", "")
        if not raw_data:
            return {"status": "skipped"}

        data = json.loads(raw_data)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError(f"Postback data のパースに失敗しました: {exc}") from exc

    if data.get("action") != "reflection":
        return {"status": "skipped"}

    memo_id = int(data["memo_id"])
    answer = str(data["answer"])

    if process_answer_fn is not None:
        process_answer_fn(memo_id, answer)
    else:
        try:
            from agriha.control.reflection import process_answer  # type: ignore[import]
            process_answer(memo_id, answer)
        except ImportError:
            logger.warning("agriha.control.reflection が見つかりません（subtask_737 待ち）")

    logger.info("Postback処理完了: memo_id=%d answer=%s", memo_id, answer)
    return {"status": "ok", "memo_id": memo_id, "answer": answer}


# ---------------------------------------------------------------------------
# ナッジ・切替通知
# ---------------------------------------------------------------------------


def send_nag_message(user_id: str = "", token: str = "") -> None:
    """2週無反応時のナッジメッセージを送信する。

    Args:
        user_id: 送信先 LINE user ID。空の場合は LINE_FARMER_USER_ID を使用。
        token: アクセストークン。空の場合は環境変数を使用。
    """
    to = user_id or LINE_FARMER_USER_ID
    if not to:
        logger.warning("LINE_FARMER_USER_ID が未設定。ナッジ送信をスキップします")
        return

    message = {
        "type": "text",
        "text": "先週も先々週も反省会のご回答をいただけていません…😢\n"
                "お忙しいのはわかるんですが、少しだけ時間をいただけると助かります🌱",
    }
    _push_message(to, [message], token=token)
    logger.info("ナッジメッセージ送信完了")


def send_downgrade_notice(user_id: str = "", token: str = "") -> None:
    """4週無反応時の月次切替通知を送信する。

    Args:
        user_id: 送信先 LINE user ID。空の場合は LINE_FARMER_USER_ID を使用。
        token: アクセストークン。空の場合は環境変数を使用。
    """
    to = user_id or LINE_FARMER_USER_ID
    if not to:
        logger.warning("LINE_FARMER_USER_ID が未設定。切替通知をスキップします")
        return

    message = {
        "type": "text",
        "text": "4週間ご回答がありませんでした。\n"
                "しばらくお休みしますね。月1回の頻度にさせていただきます🌿\n"
                "また再開したいときはいつでもお知らせください！",
    }
    _push_message(to, [message], token=token)
    logger.info("月次切替通知送信完了")
