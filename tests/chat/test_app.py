"""tests/chat/test_app.py — dashboard API追加エンドポイントのテスト (cmd_312)

テスト対象:
- GET /api/flags    フラグファイル存在チェック
- GET /api/plan     current_plan.json 返却
- GET /api/dashboard 集約エンドポイント
- GET /api/logs     ログtail
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import agriha.chat.app as app_module
from agriha.chat.app import app


def _basic_auth(user: str = "testadmin", pw: str = "testpass") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncClient:
    with (
        patch.object(app_module, "UI_AUTH_USER", "testadmin"),
        patch.object(app_module, "UI_AUTH_PASS", "testpass"),
        patch.object(app_module, "AGRIHA_FLAG_DIR", str(tmp_path)),
        patch.object(app_module, "CURRENT_PLAN_PATH", str(tmp_path / "current_plan.json")),
        patch.object(app_module, "AGRIHA_LOG_DIR", str(tmp_path)),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=_basic_auth(),
        ) as ac:
            yield ac


# ── GET /api/flags ──────────────────────────────────────────────────────────

async def test_get_flags_all_false(client: AsyncClient, tmp_path: Path) -> None:
    """フラグファイルが存在しない場合、全て false を返す。"""
    r = await client.get("/api/flags")
    assert r.status_code == 200
    data = r.json()
    assert data == {"lockout": False, "rain_flag": False, "wind_flag": False}


async def test_get_flags_lockout_true(client: AsyncClient, tmp_path: Path) -> None:
    """lockout ファイルが存在する場合、lockout が true になる。"""
    (tmp_path / "lockout").touch()
    r = await client.get("/api/flags")
    assert r.status_code == 200
    data = r.json()
    assert data["lockout"] is True
    assert data["rain_flag"] is False
    assert data["wind_flag"] is False


async def test_get_flags_all_true(client: AsyncClient, tmp_path: Path) -> None:
    """全フラグファイルが存在する場合、全て true を返す。"""
    (tmp_path / "lockout").touch()
    (tmp_path / "rain_flag").touch()
    (tmp_path / "wind_flag").touch()
    r = await client.get("/api/flags")
    assert r.status_code == 200
    data = r.json()
    assert data == {"lockout": True, "rain_flag": True, "wind_flag": True}


# ── GET /api/plan ───────────────────────────────────────────────────────────

async def test_get_plan_no_file(client: AsyncClient, tmp_path: Path) -> None:
    """current_plan.json がない場合、plan: null を返す。"""
    r = await client.get("/api/plan")
    assert r.status_code == 200
    data = r.json()
    assert data["plan"] is None


async def test_get_plan_with_file(client: AsyncClient, tmp_path: Path) -> None:
    """current_plan.json が存在する場合、その内容を返す。"""
    plan = {
        "generated_at": "2026-03-05T00:00:00",
        "valid_until": "2026-03-05T01:00:00",
        "actions": [{"time": "2026-03-05T00:30:00", "relay": {"ch": 5, "value": 1}}],
        "summary": "テスト計画",
    }
    (tmp_path / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    r = await client.get("/api/plan")
    assert r.status_code == 200
    data = r.json()
    assert data["summary"] == "テスト計画"
    assert len(data["actions"]) == 1


# ── GET /api/dashboard ──────────────────────────────────────────────────────

async def test_get_dashboard_structure(client: AsyncClient, tmp_path: Path) -> None:
    """集約エンドポイントが必須キーを含む JSON を返す。"""
    with patch("agriha.chat.app.fetch_sensors", return_value={"indoor_temp": 22.0}):
        r = await client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert "sensors" in data
    assert "plan" in data
    assert "flags" in data
    assert "timestamp" in data


async def test_get_dashboard_flags_embedded(client: AsyncClient, tmp_path: Path) -> None:
    """フラグファイルが存在する場合、flags フィールドに反映される。"""
    (tmp_path / "rain_flag").touch()
    with patch("agriha.chat.app.fetch_sensors", return_value={}):
        r = await client.get("/api/dashboard")
    assert r.status_code == 200
    assert r.json()["flags"]["rain_flag"] is True


# ── GET /api/logs ───────────────────────────────────────────────────────────

async def test_get_logs_no_files(client: AsyncClient, tmp_path: Path) -> None:
    """ログファイルが存在しない場合、各キーが空リストを返す。"""
    r = await client.get("/api/logs")
    assert r.status_code == 200
    data = r.json()
    assert data["control_log"] == []
    assert data["search_log"] == []
    assert data["forecast_log"] == []
    assert data["emergency_log"] == []


async def test_get_logs_with_content(client: AsyncClient, tmp_path: Path) -> None:
    """ログファイルが存在する場合、末尾行を返す。"""
    log_file = tmp_path / "emergency.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
    r = await client.get("/api/logs?lines=2")
    assert r.status_code == 200
    data = r.json()
    assert data["emergency_log"] == ["line2", "line3"]


async def test_get_logs_lines_clamped(client: AsyncClient, tmp_path: Path) -> None:
    """lines パラメータが 200 を超えた場合、200 にクランプされる。"""
    log_file = tmp_path / "control.log"
    log_file.write_text("\n".join(f"line{i}" for i in range(300)), encoding="utf-8")
    r = await client.get("/api/logs?lines=9999")
    assert r.status_code == 200
    assert len(r.json()["control_log"]) == 200
