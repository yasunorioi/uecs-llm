"""linebot/router.py のユニットテスト。

外部依存（LINE API, httpx, YAML I/O）は全てmock/patchする。
"""

from __future__ import annotations

import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_config(tmp_path: Path):
    """テスト用 farmers.yaml + farmers_secrets.yaml を作成する。"""
    farmers = {
        "farmers": {
            "farmer_a": {
                "name": "田中農園",
                "rpi_host": "10.20.0.10",
                "rpi_chat_port": 8502,
                "status": "active",
            },
            "farmer_b": {
                "name": "鈴木ファーム",
                "rpi_host": "10.20.0.20",
                "rpi_chat_port": 8502,
                "status": "pending",
            },
        }
    }
    secrets = {
        "farmers": {
            "farmer_a": {
                "line_user_id": "U_FARMER_A",
                "wg_ip": "10.20.0.10",
                "wg_public_key": "PUBKEY_A=",
                "status": "active",
            },
            "farmer_b": {
                "line_user_id": "U_FARMER_B",
                "wg_ip": "10.20.0.20",
                "wg_public_key": "PUBKEY_B=",
                "status": "pending",
            },
        }
    }
    (tmp_path / "farmers.yaml").write_text(yaml.dump(farmers))
    (tmp_path / "farmers_secrets.yaml").write_text(yaml.dump(secrets))
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_farmer_id
# ---------------------------------------------------------------------------


class TestResolveFarmerId:
    def test_known_user(self, tmp_config: Path) -> None:
        """既登録userIdはfarmer_idを返す。"""
        import router

        secrets = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        result = router._resolve_farmer_id("U_FARMER_A", secrets)
        assert result == "farmer_a"

    def test_unknown_user(self, tmp_config: Path) -> None:
        """未登録userIdはNoneを返す。"""
        import router

        secrets = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        result = router._resolve_farmer_id("U_UNKNOWN", secrets)
        assert result is None

    def test_empty_secrets(self) -> None:
        """farmersが空のsecreｔsはNoneを返す。"""
        import router

        result = router._resolve_farmer_id("U_ANYONE", {"farmers": {}})
        assert result is None


# ---------------------------------------------------------------------------
# route_message — 正常系
# ---------------------------------------------------------------------------


class TestRouteMessageSuccess:
    def test_routes_to_rpi_and_replies(self, tmp_config: Path) -> None:
        """正常系: RPiへのPOST成功 → LINE reply呼び出し確認。"""
        import router

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "窓を開けました"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply") as mock_reply:
            mock_http = MagicMock()
            mock_http.__enter__ = MagicMock(return_value=mock_http)
            mock_http.__exit__ = MagicMock(return_value=None)
            mock_http.post.return_value = mock_resp
            mock_http_cls.return_value = mock_http

            router.route_message(
                reply_token="TOKEN_A",
                user_id="U_FARMER_A",
                message="側窓開けて",
            )

        mock_reply.assert_called_once_with("TOKEN_A", "窓を開けました")

    def test_posts_to_correct_rpi_url(self, tmp_config: Path) -> None:
        """RPiのURLが farmers.yaml の rpi_host:rpi_chat_port から正しく構築される。"""
        import router

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "OK"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply"):
            mock_http = MagicMock()
            mock_http.__enter__ = MagicMock(return_value=mock_http)
            mock_http.__exit__ = MagicMock(return_value=None)
            mock_http.post.return_value = mock_resp
            mock_http_cls.return_value = mock_http

            router.route_message("TOKEN", "U_FARMER_A", "テスト")

            call_args = mock_http.post.call_args
            assert call_args[0][0] == "http://10.20.0.10:8502/api/chat"


# ---------------------------------------------------------------------------
# route_message — 未登録ユーザー
# ---------------------------------------------------------------------------


class TestRouteMessageUnregistered:
    def test_unregistered_user_error(self, tmp_config: Path) -> None:
        """未登録userIdはエラーメッセージをreplyして終了。"""
        import router

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply") as mock_reply:
            router.route_message("TOKEN", "U_UNKNOWN", "こんにちは")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "登録されていない" in reply_text
        mock_http_cls.assert_not_called()

    def test_inactive_farmer_error(self, tmp_config: Path) -> None:
        """非アクティブ農家（pending）はエラーメッセージをreply。"""
        import router

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply") as mock_reply:
            router.route_message("TOKEN", "U_FARMER_B", "こんにちは")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "ご利用できません" in reply_text or "非アクティブ" in reply_text or "管理者" in reply_text
        mock_http_cls.assert_not_called()


# ---------------------------------------------------------------------------
# route_message — RPi通信エラー
# ---------------------------------------------------------------------------


class TestRouteMessageRpiErrors:
    def test_rpi_connect_error(self, tmp_config: Path) -> None:
        """RPi接続失敗は接続失敗メッセージをreply。"""
        import httpx
        import router

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply") as mock_reply:
            mock_http = MagicMock()
            mock_http.__enter__ = MagicMock(return_value=mock_http)
            mock_http.__exit__ = MagicMock(return_value=None)
            mock_http.post.side_effect = httpx.ConnectError("refused")
            mock_http_cls.return_value = mock_http

            router.route_message("TOKEN", "U_FARMER_A", "テスト")

        reply_text = mock_reply.call_args[0][1]
        assert "通信に失敗" in reply_text or "接続" in reply_text

    def test_rpi_timeout(self, tmp_config: Path) -> None:
        """RPiタイムアウトはタイムアウトメッセージをreply。"""
        import httpx
        import router

        with patch.object(router, "CONFIG_DIR", tmp_config), \
             patch("router.httpx.Client") as mock_http_cls, \
             patch("router._reply") as mock_reply:
            mock_http = MagicMock()
            mock_http.__enter__ = MagicMock(return_value=mock_http)
            mock_http.__exit__ = MagicMock(return_value=None)
            mock_http.post.side_effect = httpx.TimeoutException("timeout")
            mock_http_cls.return_value = mock_http

            router.route_message("TOKEN", "U_FARMER_A", "テスト")

        reply_text = mock_reply.call_args[0][1]
        assert "タイムアウト" in reply_text

    def test_config_load_error(self, tmp_path: Path) -> None:
        """farmers.yaml が存在しない場合はエラーメッセージをreply。"""
        import router

        with patch.object(router, "CONFIG_DIR", tmp_path), \
             patch("router._reply") as mock_reply:
            router.route_message("TOKEN", "U_FARMER_A", "テスト")

        reply_text = mock_reply.call_args[0][1]
        assert "読み込み" in reply_text or "管理者" in reply_text
