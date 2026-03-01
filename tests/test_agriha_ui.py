"""
tests/test_agriha_ui.py — agriha_ui 全ルート pytest テスト (cmd_287)

テスト対象:
- GET /          ダッシュボード画面
- GET /api/dashboard-partial  部分テンプレート (htmx用)
- GET /settings  設定画面
- POST /settings/prompt       システムプロンプト保存
- POST /settings/thresholds   閾値保存 + バリデーション
- GET /history   制御履歴画面
- Basic Auth 未認証

モック方針:
- fetch_sensors / fetch_status はモジュール変数パッチ
- ファイルI/O は tmp_path で一時パスにパッチ
- control_log.db は sqlite3 で一時DBを作成
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

import agriha_ui.app as app_module
from agriha_ui.app import app

# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _basic_auth(user: str = "testadmin", pw: str = "testpass") -> dict[str, str]:
    """Basic認証ヘッダーを生成する。"""
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _make_db(db_path: Path) -> None:
    """テスト用 control_log.db を作成してデータを挿入する。"""
    con = sqlite3.connect(str(db_path))
    con.execute(
        """CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            summary TEXT,
            actions_taken TEXT
        )"""
    )
    con.execute(
        "INSERT INTO decisions VALUES (1, '2026-03-01T12:00:00', 'emergency stop', 'relay off')"
    )
    con.execute(
        "INSERT INTO decisions VALUES (2, '2026-03-01T13:00:00', 'rule engine ran', 'rule threshold open window')"
    )
    con.execute(
        "INSERT INTO decisions VALUES (3, '2026-03-01T14:00:00', 'llm forecast', 'plan applied')"
    )
    con.commit()
    con.close()


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path: Path) -> dict[str, str]:
    """一時設定ファイル群を作成する。"""
    thresholds_file = tmp_path / "thresholds.yaml"
    thresholds_file.write_text(
        yaml.dump({
            "emergency": {"high_temp": 27.0, "low_temp": 16.0},
            "co2": {"target_ppm": 700},
        }),
        encoding="utf-8",
    )
    prompt_file = tmp_path / "system_prompt.txt"
    prompt_file.write_text("テスト用プロンプト", encoding="utf-8")
    db_file = tmp_path / "control_log.db"
    _make_db(db_file)
    return {
        "thresholds": str(thresholds_file),
        "prompt": str(prompt_file),
        "db": str(db_file),
        "dir": str(tmp_path),
    }


@pytest.fixture
async def client(tmp_config: dict[str, str]) -> AsyncClient:
    """モジュール変数をパッチしたテスト用 AsyncClient を返す。"""
    auth_hdr = _basic_auth()
    with (
        patch.object(app_module, "UI_AUTH_USER", "testadmin"),
        patch.object(app_module, "UI_AUTH_PASS", "testpass"),
        patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", tmp_config["thresholds"]),
        patch.object(app_module, "SYSTEM_PROMPT_PATH", tmp_config["prompt"]),
        patch.object(app_module, "CONTROL_LOG_DB", tmp_config["db"]),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=auth_hdr,
        ) as ac:
            yield ac


# ── テストケース ──────────────────────────────────────────────────────────────

# 1. GET / → 200, ダッシュボード画面が表示される
async def test_dashboard_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    # テンプレートは sensors[key].value を参照するため、空dictで「---」表示にする
    with (
        patch("agriha_ui.app.fetch_sensors", return_value={}),
        patch("agriha_ui.app.fetch_status", return_value={"locked_out": False, "relay_state": {}}),
    ):
        r = await client.get("/")
    assert r.status_code == 200
    assert "AgriHA" in r.text


# 2. GET / (unipi-daemon API不達) → 200, デフォルト値で表示される
async def test_dashboard_api_down(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    with (
        patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", tmp_config["thresholds"]),
        patch.object(app_module, "SYSTEM_PROMPT_PATH", tmp_config["prompt"]),
        patch.object(app_module, "CONTROL_LOG_DB", tmp_config["db"]),
        patch("agriha_ui.app.fetch_sensors", return_value={}),
        patch("agriha_ui.app.fetch_status", return_value={}),
    ):
        r = await client.get("/")
    assert r.status_code == 200


# 3. GET /api/dashboard-partial → 200, HTML断片が返る（base.html非継承）
async def test_dashboard_partial_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    # テンプレートは sensors[key].value を参照するため、空dictで「---」表示にする
    with (
        patch("agriha_ui.app.fetch_sensors", return_value={}),
        patch("agriha_ui.app.fetch_status", return_value={}),
    ):
        r = await client.get("/api/dashboard-partial")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


# 4. GET /settings → 200, system_promptとthresholdsが表示される
async def test_settings_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    # load_system_prompt/load_thresholds はデフォルト引数がモジュール定義時に評価されるため
    # 関数自体をパッチして期待値を返す
    with (
        patch("agriha_ui.app.load_system_prompt", return_value="テスト用プロンプト"),
        patch("agriha_ui.app.load_thresholds", return_value={
            "emergency": {"high_temp": 27.0, "low_temp": 16.0},
            "co2": {"target_ppm": 700},
        }),
    ):
        r = await client.get("/settings")
    assert r.status_code == 200
    assert "テスト用プロンプト" in r.text
    assert "27" in r.text  # high_temp


# 5. GET /settings (system_prompt.txt不在) → 200, 空テキストで表示
async def test_settings_no_prompt_file(client: AsyncClient, tmp_config: dict[str, str], tmp_path: Path) -> None:
    nonexistent = str(tmp_path / "nonexistent_prompt.txt")
    with (
        patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", tmp_config["thresholds"]),
        patch.object(app_module, "SYSTEM_PROMPT_PATH", nonexistent),
    ):
        r = await client.get("/settings")
    assert r.status_code == 200


# 6. POST /settings/prompt → 302 redirect, system_prompt.txtが更新される
async def test_save_prompt_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    new_text = "新しいプロンプトテキスト"
    prompt_path = tmp_config["prompt"]
    with patch.object(app_module, "SYSTEM_PROMPT_PATH", prompt_path):
        r = await client.post(
            "/settings/prompt",
            data={"prompt_text": new_text},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]
    # ファイルが新しいテキストで書き換えられているか確認
    # バックアップがあるため、元のpathが書き換わっているか
    written = Path(prompt_path).read_text(encoding="utf-8")
    assert written == new_text


# 7. POST /settings/prompt → バックアップファイルが作成される
async def test_save_prompt_backup_created(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    prompt_path = tmp_config["prompt"]
    with patch.object(app_module, "SYSTEM_PROMPT_PATH", prompt_path):
        await client.post(
            "/settings/prompt",
            data={"prompt_text": "更新後テキスト"},
            follow_redirects=False,
        )
    parent = Path(prompt_path).parent
    bak_files = list(parent.glob("*.bak.*"))
    assert len(bak_files) >= 1


# 8. POST /settings/thresholds → 302 redirect, thresholds.yamlが更新される
async def test_save_thresholds_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    thr_path = tmp_config["thresholds"]
    with patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", thr_path):
        r = await client.post(
            "/settings/thresholds",
            data={"high_temp": "30.0", "low_temp": "15.0", "co2_target": "800"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "saved=1" in r.headers["location"]
    saved = yaml.safe_load(Path(thr_path).read_text(encoding="utf-8"))
    assert saved["emergency"]["high_temp"] == pytest.approx(30.0)
    assert saved["co2"]["target_ppm"] == 800


# 9. POST /settings/thresholds (不正値: high < low) → エラーリダイレクト
async def test_save_thresholds_invalid(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    thr_path = tmp_config["thresholds"]
    with patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", thr_path):
        r = await client.post(
            "/settings/thresholds",
            data={"high_temp": "15.0", "low_temp": "20.0", "co2_target": "700"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "error=1" in r.headers["location"]


# 10. GET /history → 200, 制御履歴テーブルが表示される
async def test_history_ok(client: AsyncClient, tmp_config: dict[str, str]) -> None:
    with patch.object(app_module, "CONTROL_LOG_DB", tmp_config["db"]):
        r = await client.get("/history")
    assert r.status_code == 200
    assert "emergency" in r.text or "layer" in r.text.lower()


# 11. GET /history (DB不在) → 200, 空リストで表示される
async def test_history_no_db(client: AsyncClient, tmp_path: Path) -> None:
    nonexistent_db = str(tmp_path / "no_such.db")
    with patch.object(app_module, "CONTROL_LOG_DB", nonexistent_db):
        r = await client.get("/history")
    assert r.status_code == 200


# 12. Basic Auth未認証 → 401
async def test_auth_required() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/")
    assert r.status_code == 401
