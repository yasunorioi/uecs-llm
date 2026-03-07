"""retry_helper.py のユニットテスト.

テスト方針:
  - sleep はモック DI で即時実行（実際には待機しない）
  - LINE 通知は urllib.request.urlopen をモック
  - httpx エラー種別の分類を網羅
"""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from agriha.control.retry_helper import (
    RETRY_DELAYS_LOCAL_SEC,
    RETRY_DELAYS_SEC,
    RETRY_MAX_ATTEMPTS,
    is_retryable_error,
    notify_line_failure,
    retry_with_backoff,
)


# ---------------------------------------------------------------------------
# is_retryable_error テスト
# ---------------------------------------------------------------------------

class TestIsRetryableError:
    def test_connection_error_retryable(self):
        assert is_retryable_error(ConnectionError("refused")) is True

    def test_timeout_error_retryable(self):
        assert is_retryable_error(TimeoutError("timeout")) is True

    def test_httpx_connect_error_retryable(self):
        assert is_retryable_error(httpx.ConnectError("refused")) is True

    def test_httpx_read_timeout_retryable(self):
        req = httpx.Request("GET", "http://example.com")
        assert is_retryable_error(httpx.ReadTimeout("timeout", request=req)) is True

    def test_httpx_network_error_retryable(self):
        assert is_retryable_error(httpx.NetworkError("network")) is True

    def test_httpx_5xx_retryable(self):
        req = httpx.Request("GET", "http://example.com")
        resp = httpx.Response(500, request=req)
        exc = httpx.HTTPStatusError("error", request=req, response=resp)
        assert is_retryable_error(exc) is True

    def test_httpx_503_retryable(self):
        req = httpx.Request("GET", "http://example.com")
        resp = httpx.Response(503, request=req)
        exc = httpx.HTTPStatusError("error", request=req, response=resp)
        assert is_retryable_error(exc) is True

    def test_httpx_4xx_not_retryable(self):
        req = httpx.Request("GET", "http://example.com")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("error", request=req, response=resp)
        assert is_retryable_error(exc) is False

    def test_httpx_404_not_retryable(self):
        req = httpx.Request("GET", "http://example.com")
        resp = httpx.Response(404, request=req)
        exc = httpx.HTTPStatusError("error", request=req, response=resp)
        assert is_retryable_error(exc) is False

    def test_urllib_url_error_retryable(self):
        exc = urllib.error.URLError("connection refused")
        assert is_retryable_error(exc) is True

    def test_urllib_http_error_5xx_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=500, msg="Server Error",
            hdrs=None, fp=None,  # type: ignore[arg-type]
        )
        assert is_retryable_error(exc) is True

    def test_urllib_http_error_4xx_not_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=403, msg="Forbidden",
            hdrs=None, fp=None,  # type: ignore[arg-type]
        )
        assert is_retryable_error(exc) is False

    def test_value_error_not_retryable(self):
        assert is_retryable_error(ValueError("bad value")) is False

    def test_key_error_not_retryable(self):
        assert is_retryable_error(KeyError("key")) is False


# ---------------------------------------------------------------------------
# retry_with_backoff テスト
# ---------------------------------------------------------------------------

class TestRetryWithBackoff:
    def test_success_on_first_attempt(self):
        """初回成功ならリトライなし・sleepなし。"""
        func = MagicMock(return_value="ok")
        sleep = MagicMock()
        result = retry_with_backoff(func, sleep_fn=sleep, error_label="test")
        assert result == "ok"
        func.assert_called_once()
        sleep.assert_not_called()

    def test_success_on_second_attempt(self):
        """1回失敗→2回目成功。sleep 1回、遅延は delays[0]。"""
        sleep = MagicMock()
        func = MagicMock(side_effect=[ConnectionError("down"), "ok"])
        result = retry_with_backoff(
            func, delays=[5, 10, 20], sleep_fn=sleep, error_label="test"
        )
        assert result == "ok"
        assert func.call_count == 2
        sleep.assert_called_once_with(5)

    def test_success_on_third_attempt(self):
        """2回失敗→3回目成功。sleep 2回。"""
        sleep = MagicMock()
        func = MagicMock(side_effect=[
            ConnectionError("down"), TimeoutError("timeout"), "ok"
        ])
        result = retry_with_backoff(
            func, delays=[5, 10, 20], sleep_fn=sleep, error_label="test"
        )
        assert result == "ok"
        assert func.call_count == 3
        assert sleep.call_args_list == [call(5), call(10)]

    def test_all_fail_raises_last_exception(self):
        """全試行失敗 → 最後の例外を raise。"""
        sleep = MagicMock()
        err = ConnectionError("down")
        func = MagicMock(side_effect=err)
        with pytest.raises(ConnectionError):
            retry_with_backoff(
                func, max_attempts=3, delays=[1, 2, 4], sleep_fn=sleep,
                error_label="test", notify_on_exceeded=False,
            )
        assert func.call_count == 3

    def test_all_fail_notifies_line(self):
        """全試行失敗かつ notify_on_exceeded=True → LINE 通知が呼ばれる。"""
        sleep = MagicMock()
        func = MagicMock(side_effect=ConnectionError("down"))
        with patch("agriha.control.retry_helper.notify_line_failure") as mock_notify:
            with pytest.raises(ConnectionError):
                retry_with_backoff(
                    func, max_attempts=2, delays=[1, 2], sleep_fn=sleep,
                    error_label="テスト操作", notify_on_exceeded=True,
                )
            mock_notify.assert_called_once()
            args = mock_notify.call_args[0][0]
            assert "テスト操作" in args
            assert "2" in args  # max_attempts

    def test_no_notify_when_notify_off(self):
        """notify_on_exceeded=False なら LINE 通知しない。"""
        sleep = MagicMock()
        func = MagicMock(side_effect=ConnectionError("down"))
        with patch("agriha.control.retry_helper.notify_line_failure") as mock_notify:
            with pytest.raises(ConnectionError):
                retry_with_backoff(
                    func, max_attempts=2, delays=[1], sleep_fn=sleep,
                    error_label="test", notify_on_exceeded=False,
                )
            mock_notify.assert_not_called()

    def test_non_retryable_error_no_retry(self):
        """リトライ対象外エラー（4xx 等）は即 raise、retry なし。"""
        sleep = MagicMock()
        req = httpx.Request("GET", "http://example.com")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("auth failed", request=req, response=resp)
        func = MagicMock(side_effect=exc)
        with pytest.raises(httpx.HTTPStatusError):
            retry_with_backoff(
                func, max_attempts=3, delays=[1, 2], sleep_fn=sleep,
                error_label="test",
            )
        func.assert_called_once()
        sleep.assert_not_called()

    def test_delays_list_shorter_uses_last(self):
        """delays リストが短い場合、最後の値を繰り返す。"""
        sleep = MagicMock()
        func = MagicMock(side_effect=[
            ConnectionError("e1"), ConnectionError("e2"), ConnectionError("e3")
        ])
        with pytest.raises(ConnectionError):
            retry_with_backoff(
                func, max_attempts=3, delays=[5], sleep_fn=sleep,
                error_label="test", notify_on_exceeded=False,
            )
        # delays=[5] → 1回目: sleep(5), 2回目: sleep(5)（最後の値を繰り返す）
        assert sleep.call_args_list == [call(5), call(5)]

    def test_default_delays_constants(self):
        """デフォルト定数が正しく設定されている。"""
        assert RETRY_MAX_ATTEMPTS == 3
        assert RETRY_DELAYS_SEC == [30, 60, 120]
        assert RETRY_DELAYS_LOCAL_SEC == [2, 5, 10]


# ---------------------------------------------------------------------------
# notify_line_failure テスト
# ---------------------------------------------------------------------------

class TestNotifyLineFailure:
    def test_no_token_skips_request(self):
        """トークン未設定時はリクエストを送らない。"""
        with patch.dict("os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "", "LINE_USER_ID": ""}):
            with patch("urllib.request.urlopen") as mock_open:
                notify_line_failure("test message")
                mock_open.assert_not_called()

    def test_no_user_id_skips_request(self):
        """USER_ID 未設定時はリクエストを送らない。"""
        with patch.dict("os.environ", {
            "LINE_CHANNEL_ACCESS_TOKEN": "token123",
            "LINE_USER_ID": "",
        }):
            with patch("urllib.request.urlopen") as mock_open:
                notify_line_failure("test message")
                mock_open.assert_not_called()

    def test_sends_line_push_when_configured(self):
        """トークンと USER_ID が設定されている場合 LINE API に送信する。"""
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch.dict("os.environ", {
            "LINE_CHANNEL_ACCESS_TOKEN": "mytoken",
            "LINE_USER_ID": "U1234567890",
        }):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
                notify_line_failure("接続できません")
                mock_open.assert_called_once()
                req_arg = mock_open.call_args[0][0]
                assert isinstance(req_arg, urllib.request.Request)
                assert "Bearer mytoken" in req_arg.get_header("Authorization")

    def test_urlopen_error_does_not_raise(self):
        """LINE API 送信エラーが発生してもサイレント失敗（例外を外に出さない）。"""
        with patch.dict("os.environ", {
            "LINE_CHANNEL_ACCESS_TOKEN": "mytoken",
            "LINE_USER_ID": "U1234567890",
        }):
            with patch("urllib.request.urlopen", side_effect=Exception("network error")):
                # 例外が外に伝播しないこと
                notify_line_failure("message")  # 例外が出なければOK
