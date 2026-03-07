"""リトライヘルパー — 指数バックオフ + LINE通知.

forecast_engine / rule_engine から使用するリトライユーティリティ。

デフォルト動作:
  初回失敗: 30秒後にリトライ（外部API）/ 2秒後（ローカルAPI）
  2回目失敗: 60秒後 / 5秒後
  3回目失敗: 120秒後 / 10秒後
  最大リトライ: 3回（AGRIHA_RETRY_MAX 環境変数で変更可能）
  上限超過時: LINE Bot で農家に通知（トークン未設定時はログのみ）
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Callable, TypeVar

logger = logging.getLogger("retry_helper")

# ---------------------------------------------------------------------------
# 定数（環境変数で上書き可能）
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS: int = int(os.environ.get("AGRIHA_RETRY_MAX", "3"))

# 外部API（LLM・クラウドAPI等）用: SORACOM SIM断を想定した長めの間隔
RETRY_DELAYS_SEC: list[int] = [30, 60, 120]

# ローカルAPI（unipi daemon等）用: 短め（5分cronサイクル内に収まるように）
RETRY_DELAYS_LOCAL_SEC: list[int] = [2, 5, 10]


# ---------------------------------------------------------------------------
# エラー分類
# ---------------------------------------------------------------------------

def is_retryable_error(exc: BaseException) -> bool:
    """リトライ対象のエラーかどうかを判定する。

    リトライ対象:
      - ネットワーク接続エラー（SORACOM断・DNS失敗等）
      - タイムアウト
      - HTTP 5xx エラー

    リトライしない:
      - HTTP 4xx エラー（認証失敗・リクエスト不正等）
      - その他の予期しないエラー
    """
    import httpx  # 遅延インポート（未インストール環境でもモジュール読み込み可能に）

    # httpx: 接続・タイムアウト・ネットワーク系
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
        return True

    # httpx: HTTPStatusError — 5xx のみリトライ
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500

    # urllib: URLError（接続不可・DNS失敗等）※ HTTPError のスーパークラスなので順序に注意
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True

    # Python 組み込み: 接続・タイムアウト
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    # openai SDK: 接続・タイムアウト・5xx（オプション: 未インストール時はスキップ）
    try:
        import openai  # type: ignore[import]
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return True
        if isinstance(exc, openai.InternalServerError):
            return True
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError,
                             openai.BadRequestError)):
            return False
    except (ImportError, AttributeError):
        pass

    return False


# ---------------------------------------------------------------------------
# LINE通知
# ---------------------------------------------------------------------------

def notify_line_failure(message: str) -> None:
    """LINE Messaging API でリトライ失敗を農家に通知する。

    LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID が未設定の場合は警告ログのみ。
    通知失敗時もサイレント失敗（通知エラーで制御フローを止めない）。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        logger.warning("LINE通知スキップ（トークン未設定）: %s", message)
        return

    body = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("LINE通知送信完了: status=%d", resp.status)
    except Exception as exc:
        logger.error("LINE通知送信失敗: %s", exc)


# ---------------------------------------------------------------------------
# リトライ実行
# ---------------------------------------------------------------------------

T = TypeVar("T")


def retry_with_backoff(
    func: Callable[[], T],
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    delays: list[int] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    error_label: str = "操作",
    notify_on_exceeded: bool = True,
) -> T:
    """指数バックオフ付きリトライで func を実行する。

    Args:
        func: 引数なしの呼び出し可能オブジェクト。
        max_attempts: 最大試行回数（初回含む）。デフォルト RETRY_MAX_ATTEMPTS(3)。
        delays: リトライ前の待機秒数リスト。デフォルト RETRY_DELAYS_SEC([30, 60, 120])。
        sleep_fn: テスト用 DI（time.sleep の代わり）。
        error_label: ログ・通知メッセージ用のラベル文字列。
        notify_on_exceeded: 上限超過時に LINE 通知するか（ローカルAPIはFalse推奨）。

    Returns:
        func の戻り値（成功時）。

    Raises:
        最後の例外（max_attempts 回全て失敗、またはリトライ対象外エラー時）。
    """
    _delays = delays if delays is not None else list(RETRY_DELAYS_SEC)
    _sleep = sleep_fn if sleep_fn is not None else time.sleep

    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc

            if not is_retryable_error(exc):
                logger.error(
                    "%s 失敗（リトライ対象外: %s）: %s",
                    error_label, type(exc).__name__, exc,
                )
                raise

            if attempt >= max_attempts:
                logger.error(
                    "%s: 最大リトライ (%d回) 超過: %s",
                    error_label, max_attempts, exc,
                )
                if notify_on_exceeded:
                    notify_line_failure(
                        f"【AgriHA警告】{error_label}が {max_attempts} 回リトライしましたが"
                        f"接続できません。\nエラー: {exc}\n"
                        f"次の定期実行サイクルまで待機します。"
                    )
                raise

            delay = _delays[attempt - 1] if attempt - 1 < len(_delays) else _delays[-1]
            logger.warning(
                "%s 失敗 (%d/%d回目): %s → %d秒後にリトライ",
                error_label, attempt, max_attempts, exc, delay,
            )
            _sleep(delay)

    # 到達不可だが型チェック用
    assert last_exc is not None
    raise last_exc
