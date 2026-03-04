"""tests/linebot/test_reflection_sender.py — reflection_sender.py テスト (cmd_315 W4b).

テスト方針:
  - LINE API 呼び出し (_push_message) は unittest.mock でモック
  - 外部依存なし（純粋なロジックテスト）
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from agriha.linebot.reflection_sender import (
    CHOICE_LABELS,
    _MAX_MESSAGES_PER_REQUEST,
    _push_message,
    build_reflection_message,
    handle_postback,
    send_downgrade_notice,
    send_nag_message,
    send_reflection,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _make_memo(
    memo_id: int = 1,
    query: str = "winter_night_cold_clear",
    frequency: int = 8,
    confidence: float = 0.875,
    question_text: str = "このパターンを採用しますか？",
) -> dict[str, Any]:
    return {
        "memo_id": memo_id,
        "query": query,
        "frequency": frequency,
        "confidence": confidence,
        "question_text": question_text,
    }


def _make_postback_event(memo_id: int, answer: str) -> dict[str, Any]:
    return {
        "type": "postback",
        "postback": {
            "data": json.dumps(
                {"action": "reflection", "memo_id": memo_id, "answer": answer}
            )
        },
    }


# ---------------------------------------------------------------------------
# Test 1: build_reflection_message — Quick Reply 構造確認
# ---------------------------------------------------------------------------


def test_build_reflection_message_quick_reply_structure() -> None:
    """Quick Reply に A/B/C の3ボタンが含まれる。"""
    memo = _make_memo()
    msg = build_reflection_message(memo)

    assert msg["type"] == "text"
    items = msg["quickReply"]["items"]
    assert len(items) == 3
    labels = [item["action"]["label"] for item in items]
    assert labels == [CHOICE_LABELS["A"], CHOICE_LABELS["B"], CHOICE_LABELS["C"]]


# ---------------------------------------------------------------------------
# Test 2: build_reflection_message — Postback data JSON 構造
# ---------------------------------------------------------------------------


def test_build_reflection_message_postback_data() -> None:
    """Postback data に action/memo_id/answer が含まれる。"""
    memo = _make_memo(memo_id=42)
    msg = build_reflection_message(memo)

    items = msg["quickReply"]["items"]
    for item, expected_answer in zip(items, ["A", "B", "C"]):
        data = json.loads(item["action"]["data"])
        assert data["action"] == "reflection"
        assert data["memo_id"] == 42
        assert data["answer"] == expected_answer


# ---------------------------------------------------------------------------
# Test 3: build_reflection_message — テキストに query/frequency/confidence を含む
# ---------------------------------------------------------------------------


def test_build_reflection_message_text_contains_meta() -> None:
    """テキストに query, frequency, confidence が含まれる。"""
    memo = _make_memo(query="spring_morning_warm_clear", frequency=10, confidence=0.9)
    msg = build_reflection_message(memo)

    text = msg["text"]
    assert "spring_morning_warm_clear" in text
    assert "10" in text
    assert "90%" in text


# ---------------------------------------------------------------------------
# Test 4: build_reflection_message — CHOICE_LABELS ≤20文字
# ---------------------------------------------------------------------------


def test_choice_labels_length() -> None:
    """全 CHOICE_LABELS が LINE 上限 20文字以内。"""
    for key, label in CHOICE_LABELS.items():
        assert len(label) <= 20, f"CHOICE_LABELS[{key!r}]={label!r} が20文字超"


# ---------------------------------------------------------------------------
# Test 5: send_reflection — LINE API 呼び出し（モック）
# ---------------------------------------------------------------------------


def test_send_reflection_calls_push_message() -> None:
    """send_reflection が _push_message を呼び出す。"""
    memos = [_make_memo(memo_id=1), _make_memo(memo_id=2)]
    with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
        send_reflection(memos, user_id="U123", token="TOKEN")

    mock_push.assert_called_once()
    args, kwargs = mock_push.call_args
    assert args[0] == "U123"
    assert len(args[1]) == 2  # messages リスト


# ---------------------------------------------------------------------------
# Test 6: send_reflection — user_id 未指定時は LINE_FARMER_USER_ID を使用
# ---------------------------------------------------------------------------


def test_send_reflection_uses_env_user_id() -> None:
    """user_id 未指定時は LINE_FARMER_USER_ID 環境変数を使用する。"""
    with patch("agriha.linebot.reflection_sender.LINE_FARMER_USER_ID", "U_ENV"):
        with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
            send_reflection([_make_memo()], token="TOKEN")

    mock_push.assert_called_once()
    assert mock_push.call_args[0][0] == "U_ENV"


# ---------------------------------------------------------------------------
# Test 7: send_reflection — user_id 未設定かつ LINE_FARMER_USER_ID 未設定はスキップ
# ---------------------------------------------------------------------------


def test_send_reflection_skips_if_no_user_id() -> None:
    """user_id も LINE_FARMER_USER_ID も未設定ならスキップ（例外なし）。"""
    with patch("agriha.linebot.reflection_sender.LINE_FARMER_USER_ID", ""):
        with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
            send_reflection([_make_memo()])

    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: _push_message — バッチ分割（5件超）
# ---------------------------------------------------------------------------


def test_push_message_batches_large_list() -> None:
    """メッセージ6件は2回に分割して送信される。"""
    messages = [{"type": "text", "text": str(i)} for i in range(6)]
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = b"{}"

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_url:
        _push_message("U123", messages, token="TOKEN")

    assert mock_url.call_count == 2


# ---------------------------------------------------------------------------
# Test 9: _push_message — アクセストークン未設定は RuntimeError
# ---------------------------------------------------------------------------


def test_push_message_raises_without_token() -> None:
    """アクセストークン未設定時は RuntimeError を送出する。"""
    with patch("agriha.linebot.reflection_sender.LINE_CHANNEL_ACCESS_TOKEN", ""):
        with pytest.raises(RuntimeError, match="LINE_CHANNEL_ACCESS_TOKEN"):
            _push_message("U123", [{"type": "text", "text": "test"}])


# ---------------------------------------------------------------------------
# Test 10: handle_postback — 正常回答処理
# ---------------------------------------------------------------------------


def test_handle_postback_normal() -> None:
    """正常な Postback データを解析し、コールバックを呼び出す。"""
    event = _make_postback_event(memo_id=5, answer="A")
    received: list[tuple[int, str]] = []

    result = handle_postback(event, process_answer_fn=lambda mid, ans: received.append((mid, ans)))

    assert result == {"status": "ok", "memo_id": 5, "answer": "A"}
    assert received == [(5, "A")]


# ---------------------------------------------------------------------------
# Test 11: handle_postback — 全選択肢 B/C
# ---------------------------------------------------------------------------


def test_handle_postback_all_choices() -> None:
    """A/B/C いずれの回答も正しく処理される。"""
    for answer in ("A", "B", "C"):
        event = _make_postback_event(memo_id=1, answer=answer)
        result = handle_postback(event, process_answer_fn=lambda mid, ans: None)
        assert result["answer"] == answer


# ---------------------------------------------------------------------------
# Test 12: handle_postback — 不正データ → ValueError
# ---------------------------------------------------------------------------


def test_handle_postback_invalid_json() -> None:
    """Postback data が不正 JSON の場合は ValueError を送出する。"""
    event = {"type": "postback", "postback": {"data": "not-valid-json"}}
    with pytest.raises(ValueError, match="パースに失敗"):
        handle_postback(event)


# ---------------------------------------------------------------------------
# Test 13: handle_postback — action が reflection 以外はスキップ
# ---------------------------------------------------------------------------


def test_handle_postback_skip_non_reflection() -> None:
    """action が reflection 以外の Postback はスキップされる。"""
    event = {
        "type": "postback",
        "postback": {"data": json.dumps({"action": "other", "memo_id": 1, "answer": "A"})},
    }
    result = handle_postback(event, process_answer_fn=lambda mid, ans: None)
    assert result == {"status": "skipped"}


# ---------------------------------------------------------------------------
# Test 14: handle_postback — postback キーなし → スキップ
# ---------------------------------------------------------------------------


def test_handle_postback_no_postback_key() -> None:
    """postback キーが存在しない場合はスキップされる。"""
    event = {"type": "postback"}
    result = handle_postback(event, process_answer_fn=lambda mid, ans: None)
    assert result == {"status": "skipped"}


# ---------------------------------------------------------------------------
# Test 15: handle_postback — process_answer_fn=None → 遅延インポート試行
# ---------------------------------------------------------------------------


def test_handle_postback_lazy_import_fallback() -> None:
    """process_answer_fn=None の場合、ImportError が出てもクラッシュしない。"""
    event = _make_postback_event(memo_id=3, answer="B")
    # reflection モジュールが未インストールの場合でも例外なく完了
    with patch.dict("sys.modules", {"agriha.control.reflection": None}):
        result = handle_postback(event)  # process_answer_fn=None
    # ImportError が出ても status=ok が返る
    assert result["status"] == "ok"
    assert result["memo_id"] == 3


# ---------------------------------------------------------------------------
# Test 16: send_nag_message — ナッジメッセージ構造
# ---------------------------------------------------------------------------


def test_send_nag_message_content() -> None:
    """ナッジメッセージが「先週も先々週も」を含む。"""
    captured: list[list[dict[str, Any]]] = []

    def fake_push(to: str, messages: list[dict[str, Any]], token: str = "") -> None:
        captured.extend(messages)

    with patch("agriha.linebot.reflection_sender._push_message", side_effect=fake_push):
        send_nag_message(user_id="U123", token="TOKEN")

    assert len(captured) == 1
    assert "先週も先々週も" in captured[0]["text"]


# ---------------------------------------------------------------------------
# Test 17: send_downgrade_notice — 切替通知構造
# ---------------------------------------------------------------------------


def test_send_downgrade_notice_content() -> None:
    """切替通知が「月1回」を含む。"""
    captured: list[list[dict[str, Any]]] = []

    def fake_push(to: str, messages: list[dict[str, Any]], token: str = "") -> None:
        captured.extend(messages)

    with patch("agriha.linebot.reflection_sender._push_message", side_effect=fake_push):
        send_downgrade_notice(user_id="U123", token="TOKEN")

    assert len(captured) == 1
    assert "月1回" in captured[0]["text"]


# ---------------------------------------------------------------------------
# Test 18: send_nag_message — user_id 未設定はスキップ
# ---------------------------------------------------------------------------


def test_send_nag_message_skips_if_no_user_id() -> None:
    """user_id も環境変数も未設定ならナッジ送信をスキップする。"""
    with patch("agriha.linebot.reflection_sender.LINE_FARMER_USER_ID", ""):
        with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
            send_nag_message()

    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Test 19: send_downgrade_notice — user_id 未設定はスキップ
# ---------------------------------------------------------------------------


def test_send_downgrade_notice_skips_if_no_user_id() -> None:
    """user_id も環境変数も未設定なら切替通知をスキップする。"""
    with patch("agriha.linebot.reflection_sender.LINE_FARMER_USER_ID", ""):
        with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
            send_downgrade_notice()

    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Test 20: send_reflection — memos が空リストの場合は送信しない
# ---------------------------------------------------------------------------


def test_send_reflection_empty_memos() -> None:
    """memo が空リストの場合は _push_message を呼び出さない。"""
    with patch("agriha.linebot.reflection_sender._push_message") as mock_push:
        send_reflection([], user_id="U123", token="TOKEN")

    mock_push.assert_not_called()
