"""linebot/onboarding.py のユニットテスト。

外部依存（LINE API, subprocess, YAML I/O）は全てmock/patchする。
"""

from __future__ import annotations

import base64
import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call, ANY


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_config(tmp_path: Path):
    """テスト用 config ディレクトリと farmers_secrets.yaml を作成する。"""
    secrets = {
        "farmers": {
            "farmer_a": {
                "line_user_id": "U_EXISTING",
                "wg_ip": "10.20.0.10",
                "wg_public_key": "PUBKEY_EXISTING=",
                "status": "active",
            },
        }
    }
    (tmp_path / "farmers_secrets.yaml").write_text(yaml.dump(secrets))
    return tmp_path


@pytest.fixture()
def pending_config(tmp_path: Path):
    """farmer_a が pending 状態のconfig。"""
    secrets = {
        "farmers": {
            "farmer_a": {
                "line_user_id": "U_PENDING",
                "wg_ip": "10.20.0.10",
                "wg_public_key": None,
                "status": "pending",
            },
        }
    }
    (tmp_path / "farmers_secrets.yaml").write_text(yaml.dump(secrets))
    return tmp_path


@pytest.fixture()
def empty_config(tmp_path: Path):
    """farmers_secrets.yaml が空（農家なし）のconfig。"""
    (tmp_path / "farmers_secrets.yaml").write_text(yaml.dump({"farmers": {}}))
    return tmp_path


# ---------------------------------------------------------------------------
# _next_wg_ip
# ---------------------------------------------------------------------------


class TestNextWgIp:
    def test_first_farmer(self) -> None:
        """農家なしの場合は10.20.0.10を返す。"""
        import onboarding

        result = onboarding._next_wg_ip({"farmers": {}})
        assert result == "10.20.0.10"

    def test_second_farmer(self) -> None:
        """1農家登録済みの場合は10.20.0.20を返す。"""
        import onboarding

        secrets = {"farmers": {"farmer_a": {"wg_ip": "10.20.0.10"}}}
        result = onboarding._next_wg_ip(secrets)
        assert result == "10.20.0.20"

    def test_skip_used_ip(self) -> None:
        """使用済みIPをスキップして次を返す。"""
        import onboarding

        secrets = {
            "farmers": {
                "farmer_a": {"wg_ip": "10.20.0.10"},
                "farmer_b": {"wg_ip": "10.20.0.20"},
            }
        }
        result = onboarding._next_wg_ip(secrets)
        assert result == "10.20.0.30"


# ---------------------------------------------------------------------------
# _next_farmer_id
# ---------------------------------------------------------------------------


class TestNextFarmerId:
    def test_first_farmer(self) -> None:
        import onboarding

        result = onboarding._next_farmer_id({"farmers": {}})
        assert result == "farmer_a"

    def test_second_farmer(self) -> None:
        import onboarding

        secrets = {"farmers": {"farmer_a": {}}}
        result = onboarding._next_farmer_id(secrets)
        assert result == "farmer_b"


# ---------------------------------------------------------------------------
# handle_follow — 正常系（新規ユーザー）
# ---------------------------------------------------------------------------


class TestHandleFollowNew:
    def test_pending_added_to_secrets(self, empty_config: Path) -> None:
        """新規ユーザーはfarmers_secrets.yamlにpending状態で追記される。"""
        import onboarding

        with patch.object(onboarding, "CONFIG_DIR", empty_config), \
             patch("onboarding._reply"), \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"):
            onboarding.handle_follow("TOKEN", "U_NEW_USER")

        saved = yaml.safe_load((empty_config / "farmers_secrets.yaml").read_text())
        farmers = saved["farmers"]
        assert len(farmers) == 1
        farmer_sec = list(farmers.values())[0]
        assert farmer_sec["line_user_id"] == "U_NEW_USER"
        assert farmer_sec["status"] == "pending"
        assert farmer_sec["wg_public_key"] is None

    def test_line_reply_called_with_base64(self, empty_config: Path) -> None:
        """LINE reply が Base64ブロック付きで呼ばれる。"""
        import onboarding

        with patch.object(onboarding, "CONFIG_DIR", empty_config), \
             patch("onboarding._reply") as mock_reply, \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"):
            onboarding.handle_follow("TOKEN", "U_NEW_USER")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "登録ありがとう" in reply_text
        # Base64ブロックが含まれている
        lines = reply_text.strip().split("\n")
        b64_part = lines[-1].strip()
        # デコードできることを確認
        decoded = yaml.safe_load(base64.b64decode(b64_part).decode())
        assert "farmer_id" in decoded
        assert "wg_server_public_key" in decoded

    def test_base64_does_not_contain_private_key(self, empty_config: Path) -> None:
        """★重要: Base64設定ブロックに秘密鍵が含まれないことを検証★"""
        import onboarding

        with patch.object(onboarding, "CONFIG_DIR", empty_config), \
             patch("onboarding._reply") as mock_reply, \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"):
            onboarding.handle_follow("TOKEN", "U_NEW_USER")

        reply_text = mock_reply.call_args[0][1]
        lines = reply_text.strip().split("\n")
        b64_part = lines[-1].strip()
        decoded = yaml.safe_load(base64.b64decode(b64_part).decode())

        # 秘密鍵は含まれていないこと
        assert "wg_client_private_key" not in decoded
        assert "private_key" not in decoded
        # 公開鍵のみ（サーバ側の公開鍵）
        assert "wg_server_public_key" in decoded


# ---------------------------------------------------------------------------
# handle_follow — 既登録ユーザー
# ---------------------------------------------------------------------------


class TestHandleFollowExisting:
    def test_active_user_gets_registered_message(self, tmp_config: Path) -> None:
        """active農家の再followは「登録済み」メッセージが返る。"""
        import onboarding

        with patch.object(onboarding, "CONFIG_DIR", tmp_config), \
             patch("onboarding._reply") as mock_reply:
            onboarding.handle_follow("TOKEN", "U_EXISTING")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "登録済み" in reply_text

        # farmers_secrets.yaml に農家が増えていないこと
        saved = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        assert len(saved["farmers"]) == 1

    def test_pending_user_gets_qr_resent(self, pending_config: Path, tmp_path: Path) -> None:
        """pending農家の再follow（ブロック解除等）ではQR/Base64が再送される。"""
        import onboarding

        qr_dir = tmp_path / "qr_resend"
        with patch.object(onboarding, "CONFIG_DIR", pending_config), \
             patch.object(onboarding, "QR_DIR", qr_dir), \
             patch.object(onboarding, "QR_BASE_URL", "https://example.com/qr"), \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"), \
             patch("onboarding._reply_with_image") as mock_reply_img:
            onboarding.handle_follow("TOKEN", "U_PENDING")

        # QR付きメッセージが送信されること
        mock_reply_img.assert_called_once()
        args = mock_reply_img.call_args
        assert "QRコード" in args[0][1]

        # Base64をデコードして既存のfarmer_id/wg_ipが使われていることを確認
        fallback_b64 = args[1]["fallback_text"]
        decoded = yaml.safe_load(base64.b64decode(fallback_b64).decode())
        assert decoded["farmer_id"] == "farmer_a"
        assert decoded["wg_client_ip"] == "10.20.0.10/32"

        # farmers_secrets.yaml に農家が増えていないこと（新規追加ではない）
        saved = yaml.safe_load((pending_config / "farmers_secrets.yaml").read_text())
        assert len(saved["farmers"]) == 1


# ---------------------------------------------------------------------------
# register_pubkey — 正常系
# ---------------------------------------------------------------------------


class TestRegisterPubkey:
    def test_updates_secrets_to_active(self, tmp_config: Path) -> None:
        """公開鍵受信後、farmers_secrets.yaml が active に更新される。"""
        import onboarding

        # farmer_b を pending で追加
        secrets = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        secrets["farmers"]["farmer_b"] = {
            "line_user_id": "U_FARMER_B",
            "wg_ip": "10.20.0.20",
            "wg_public_key": None,
            "status": "pending",
        }
        (tmp_config / "farmers_secrets.yaml").write_text(yaml.dump(secrets))

        wg_conf = tmp_config / "wg-farmers.conf"
        wg_conf.write_text("[Interface]\nAddress = 10.20.0.1/24\n")

        with patch.object(onboarding, "CONFIG_DIR", tmp_config), \
             patch.object(onboarding, "WG_CONF_PATH", wg_conf), \
             patch("onboarding.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("onboarding._push"):
            result = onboarding.register_pubkey("farmer_b", "PUBKEY_B_NEW=")

        assert result["status"] == "registered"
        assert result["farmer_id"] == "farmer_b"

        saved = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        assert saved["farmers"]["farmer_b"]["status"] == "active"
        assert saved["farmers"]["farmer_b"]["wg_public_key"] == "PUBKEY_B_NEW="

    def test_appends_peer_to_wg_conf(self, tmp_config: Path) -> None:
        """wg-farmers.conf に Peer セクションが追加される。"""
        import onboarding

        secrets = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        secrets["farmers"]["farmer_b"] = {
            "line_user_id": "U_FARMER_B",
            "wg_ip": "10.20.0.20",
            "wg_public_key": None,
            "status": "pending",
        }
        (tmp_config / "farmers_secrets.yaml").write_text(yaml.dump(secrets))

        wg_conf = tmp_config / "wg-farmers.conf"
        wg_conf.write_text("[Interface]\nAddress = 10.20.0.1/24\n")

        with patch.object(onboarding, "CONFIG_DIR", tmp_config), \
             patch.object(onboarding, "WG_CONF_PATH", wg_conf), \
             patch("onboarding.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("onboarding._push"):
            onboarding.register_pubkey("farmer_b", "PUBKEY_B_NEW=")

        conf_text = wg_conf.read_text()
        assert "[Peer]" in conf_text
        assert "PUBKEY_B_NEW=" in conf_text
        assert "10.20.0.20/32" in conf_text
        assert "farmer_b" in conf_text

    def test_unknown_farmer_returns_error(self, tmp_config: Path) -> None:
        """未登録 farmer_id は error を返す。"""
        import onboarding

        wg_conf = tmp_config / "wg-farmers.conf"
        wg_conf.write_text("")

        with patch.object(onboarding, "CONFIG_DIR", tmp_config), \
             patch.object(onboarding, "WG_CONF_PATH", wg_conf):
            result = onboarding.register_pubkey("farmer_unknown", "PUBKEY=")

        assert "error" in result
        assert result["error"] == "farmer_not_found"

    def test_push_notification_sent(self, tmp_config: Path) -> None:
        """LINE push通知が送信される。"""
        import onboarding

        secrets = yaml.safe_load((tmp_config / "farmers_secrets.yaml").read_text())
        secrets["farmers"]["farmer_b"] = {
            "line_user_id": "U_FARMER_B",
            "wg_ip": "10.20.0.20",
            "wg_public_key": None,
            "status": "pending",
        }
        (tmp_config / "farmers_secrets.yaml").write_text(yaml.dump(secrets))

        wg_conf = tmp_config / "wg-farmers.conf"
        wg_conf.write_text("[Interface]\n")

        with patch.object(onboarding, "CONFIG_DIR", tmp_config), \
             patch.object(onboarding, "WG_CONF_PATH", wg_conf), \
             patch("onboarding.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("onboarding._push") as mock_push:
            onboarding.register_pubkey("farmer_b", "PUBKEY_B=")

        mock_push.assert_called_once_with("U_FARMER_B", "接続完了！チャットタブから話しかけてください。")


# ---------------------------------------------------------------------------
# QR画像生成
# ---------------------------------------------------------------------------


class TestGenerateQrPng:
    def test_creates_png_file(self, tmp_path: Path) -> None:
        """QR画像がPNGファイルとして生成される。"""
        import onboarding

        with patch.object(onboarding, "QR_DIR", tmp_path), \
             patch.object(onboarding, "QR_BASE_URL", "https://example.com/qr"):
            url = onboarding._generate_qr_png("test_data_b64", "farmer_a", "U_USER_A")

        png_files = list(tmp_path.glob("*.png"))
        assert len(png_files) == 1
        assert "farmer_a" in png_files[0].name
        assert url.startswith("https://example.com/qr/")

    def test_png_is_valid_image(self, tmp_path: Path) -> None:
        """生成されたファイルがPNG画像ヘッダを持つ。"""
        import onboarding

        with patch.object(onboarding, "QR_DIR", tmp_path), \
             patch.object(onboarding, "QR_BASE_URL", "https://example.com/qr"):
            onboarding._generate_qr_png("test_data", "farmer_b", "U_USER_B")

        png_file = list(tmp_path.glob("*.png"))[0]
        header = png_file.read_bytes()[:4]
        assert header == b"\x89PNG"


class TestCleanupExpiredQr:
    def test_removes_old_files(self, tmp_path: Path) -> None:
        """max_age_hours 経過したファイルが削除される。"""
        import onboarding
        import time

        old_file = tmp_path / "farmer_a_old.png"
        old_file.write_bytes(b"fake")
        # ファイルのmtimeを2日前に設定
        old_mtime = time.time() - 48 * 3600
        import os
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = tmp_path / "farmer_b_new.png"
        new_file.write_bytes(b"fake")

        with patch.object(onboarding, "QR_DIR", tmp_path):
            removed = onboarding.cleanup_expired_qr(max_age_hours=24)

        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_no_dir_returns_zero(self, tmp_path: Path) -> None:
        """QR_DIRが存在しない場合は0を返す。"""
        import onboarding

        with patch.object(onboarding, "QR_DIR", tmp_path / "nonexistent"):
            removed = onboarding.cleanup_expired_qr()

        assert removed == 0


class TestHandleFollowQr:
    def test_follow_sends_image_message(self, empty_config: Path, tmp_path: Path) -> None:
        """新規followでQR画像付きメッセージが送信される。"""
        import onboarding

        qr_dir = tmp_path / "qr"
        with patch.object(onboarding, "CONFIG_DIR", empty_config), \
             patch.object(onboarding, "QR_DIR", qr_dir), \
             patch.object(onboarding, "QR_BASE_URL", "https://example.com/qr"), \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"), \
             patch("onboarding._reply_with_image") as mock_reply_img:
            onboarding.handle_follow("TOKEN", "U_NEW_QR")

        mock_reply_img.assert_called_once()
        args = mock_reply_img.call_args
        assert "QRコード" in args[0][1]
        assert "https://example.com/qr/" in args[0][2]
        # fallback_text にBase64が含まれる
        assert args[1]["fallback_text"] is not None

        # QR PNGファイルが生成されている
        assert len(list(qr_dir.glob("*.png"))) == 1

    def test_follow_falls_back_to_text_on_qr_error(self, empty_config: Path) -> None:
        """QR生成失敗時はテキストのみで返信する。"""
        import onboarding

        with patch.object(onboarding, "CONFIG_DIR", empty_config), \
             patch.object(onboarding, "WG_SERVER_PUBLIC_KEY", "SERVER_PUB="), \
             patch.object(onboarding, "WG_SERVER_ENDPOINT", "vps.example.com:51821"), \
             patch("onboarding._generate_qr_png", side_effect=OSError("disk full")), \
             patch("onboarding._reply") as mock_reply:
            onboarding.handle_follow("TOKEN", "U_FALLBACK")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        # テキストにBase64ブロックが含まれている
        lines = reply_text.strip().split("\n")
        b64_part = lines[-1].strip()
        decoded = yaml.safe_load(base64.b64decode(b64_part).decode())
        assert "farmer_id" in decoded
