"""tests/chat/test_app.py — dashboard API + 計画タイムライン + rules設定 のテスト

テスト対象:
- GET /api/flags    フラグファイル存在チェック
- GET /api/plan     current_plan.json 返却
- GET /api/dashboard 集約エンドポイント
- GET /api/logs     ログtail
- _build_plan_timeline  計画タイムライン算出
- GET /             dashboard HTML レンダリング
- GET /api/dashboard-partial  partial HTML レンダリング
- load_rules / save_rules  rules.yaml 読み書き
- POST /settings/rules  制御ルール保存エンドポイント
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import yaml

import agriha.chat.app as app_module
from agriha.chat.app import _build_plan_timeline, _load_rules_text, app, save_rules


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
    assert data == {"lockout": False, "rain": False, "wind": False}


async def test_get_flags_lockout_true(client: AsyncClient, tmp_path: Path) -> None:
    """lockout ファイルが存在する場合、lockout が true になる。"""
    (tmp_path / "lockout").touch()
    r = await client.get("/api/flags")
    assert r.status_code == 200
    data = r.json()
    assert data["lockout"] is True
    assert data["rain"] is False
    assert data["wind"] is False


async def test_get_flags_all_true(client: AsyncClient, tmp_path: Path) -> None:
    """全フラグファイルが存在する場合、全て true を返す。"""
    (tmp_path / "lockout").touch()
    (tmp_path / "rain_flag").touch()
    (tmp_path / "wind_flag").touch()
    r = await client.get("/api/flags")
    assert r.status_code == 200
    data = r.json()
    assert data == {"lockout": True, "rain": True, "wind": True}


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
    assert r.json()["flags"]["rain"] is True


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


# ── _build_plan_timeline ─────────────────────────────────────────────────────

def _make_plan(
    gen_offset_min: int = 0,
    valid_offset_min: int = 60,
    actions: list[dict] | None = None,
) -> dict:
    """テスト用 plan dict を生成（naive datetime）。"""
    now = datetime.now()
    gen = now + timedelta(minutes=gen_offset_min)
    valid = now + timedelta(minutes=valid_offset_min)
    if actions is None:
        actions = [
            {
                "execute_at": (now + timedelta(minutes=15)).isoformat(),
                "relay_ch": 5,
                "value": 1,
                "duration_sec": 300,
                "reason": "換気",
            },
        ]
    return {
        "generated_at": gen.isoformat(),
        "valid_until": valid.isoformat(),
        "actions": actions,
        "summary": "テスト計画",
    }


def test_plan_timeline_empty_plan() -> None:
    """空の plan では plan_actions が空リストになる。"""
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, now_pct, gen_s, valid_s, labels = _build_plan_timeline({})
    assert actions == []
    assert now_pct is None
    assert gen_s == ""
    assert valid_s == ""


def test_plan_timeline_no_actions() -> None:
    """actions が空リストの plan では plan_actions が空になる。"""
    plan = _make_plan(actions=[])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, now_pct, gen_s, valid_s, labels = _build_plan_timeline(plan)
    assert actions == []


def test_plan_timeline_single_action() -> None:
    """アクション1件の plan が正しく変換される。"""
    plan = _make_plan(gen_offset_min=-30, valid_offset_min=30)
    relay_labels = {5: "南側窓"}
    with patch("agriha.chat.app.get_relay_labels", return_value=relay_labels):
        actions, now_pct, gen_s, valid_s, labels = _build_plan_timeline(plan)
    assert len(actions) == 1
    act = actions[0]
    assert act["relay_ch"] == 5
    assert act["label"] == "南側窓"
    assert act["status"] in ("pending", "overdue", "executed")
    assert 0 <= act["left_pct"] <= 100
    assert act["width_pct"] > 0
    assert now_pct is not None
    assert 0 <= now_pct <= 100
    assert gen_s  # HH:MM format
    assert valid_s


def test_plan_timeline_status_executed() -> None:
    """executed=true のアクションが 'executed' ステータスになる。"""
    now = datetime.now()
    plan = _make_plan(actions=[{
        "execute_at": (now + timedelta(minutes=10)).isoformat(),
        "relay_ch": 4,
        "value": 1,
        "duration_sec": 60,
        "reason": "灌水",
        "executed": True,
    }])
    with patch("agriha.chat.app.get_relay_labels", return_value={4: "灌水ポンプ"}):
        actions, *_ = _build_plan_timeline(plan)
    assert actions[0]["status"] == "executed"
    assert actions[0]["label"] == "灌水ポンプ"


def test_plan_timeline_status_skipped_rain() -> None:
    """executed='skipped_rain' のアクションが正しくマッピングされる。"""
    now = datetime.now()
    plan = _make_plan(actions=[{
        "execute_at": (now + timedelta(minutes=10)).isoformat(),
        "relay_ch": 5,
        "value": 1,
        "duration_sec": 30,
        "reason": "test",
        "executed": "skipped_rain",
    }])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, *_ = _build_plan_timeline(plan)
    assert actions[0]["status"] == "skipped_rain"


def test_plan_timeline_status_skipped_wind() -> None:
    """executed='skipped_wind' のアクションが正しくマッピングされる。"""
    now = datetime.now()
    plan = _make_plan(actions=[{
        "execute_at": (now + timedelta(minutes=10)).isoformat(),
        "relay_ch": 7,
        "value": 1,
        "duration_sec": 30,
        "reason": "test",
        "executed": "skipped_wind",
    }])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, *_ = _build_plan_timeline(plan)
    assert actions[0]["status"] == "skipped_wind"


def test_plan_timeline_status_overdue() -> None:
    """過去の未実行アクションが 'overdue' になる。"""
    now = datetime.now()
    plan = _make_plan(gen_offset_min=-60, valid_offset_min=0, actions=[{
        "execute_at": (now - timedelta(minutes=30)).isoformat(),
        "relay_ch": 1,
        "value": 1,
        "duration_sec": 60,
        "reason": "test",
    }])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, *_ = _build_plan_timeline(plan)
    assert actions[0]["status"] == "overdue"


def test_plan_timeline_relay_label_fallback() -> None:
    """relay_labels に無いチャンネルは 'chN' にフォールバックする。"""
    plan = _make_plan(actions=[{
        "execute_at": (datetime.now() + timedelta(minutes=10)).isoformat(),
        "relay_ch": 3,
        "value": 1,
        "duration_sec": 60,
        "reason": "test",
    }])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, *_ = _build_plan_timeline(plan)
    assert actions[0]["label"] == "ch3"


def test_plan_timeline_multiple_actions() -> None:
    """複数アクションが全て変換される。"""
    now = datetime.now()
    plan = _make_plan(actions=[
        {"execute_at": (now + timedelta(minutes=10)).isoformat(),
         "relay_ch": 5, "value": 1, "duration_sec": 30, "reason": "a"},
        {"execute_at": (now + timedelta(minutes=20)).isoformat(),
         "relay_ch": 4, "value": 1, "duration_sec": 300, "reason": "b"},
        {"execute_at": (now + timedelta(minutes=40)).isoformat(),
         "relay_ch": 1, "value": 0, "duration_sec": 0, "reason": "c"},
    ])
    with patch("agriha.chat.app.get_relay_labels", return_value={}):
        actions, *_ = _build_plan_timeline(plan)
    assert len(actions) == 3


# ── Dashboard HTML レンダリング ────────────────────────────────────────────────

async def test_dashboard_renders_plan_timeline(
    client: AsyncClient, tmp_path: Path,
) -> None:
    """ダッシュボードHTMLに計画タイムラインセクションが描画される。"""
    now = datetime.now()
    plan = {
        "generated_at": (now - timedelta(minutes=10)).isoformat(),
        "valid_until": (now + timedelta(minutes=50)).isoformat(),
        "actions": [{
            "execute_at": (now + timedelta(minutes=15)).isoformat(),
            "relay_ch": 5,
            "value": 1,
            "duration_sec": 300,
            "reason": "換気",
        }],
        "summary": "テスト",
    }
    (tmp_path / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with (
        patch("agriha.chat.app.fetch_sensors", return_value={}),
        patch("agriha.chat.app.fetch_status", return_value={}),
        patch("agriha.chat.app.get_relay_labels", return_value={5: "南側窓"}),
    ):
        r = await client.get("/api/dashboard-partial")
    assert r.status_code == 200
    html = r.text
    assert "計画タイムライン" in html
    assert "plan-bar" in html
    assert "南側窓" in html


async def test_dashboard_renders_empty_plan(
    client: AsyncClient, tmp_path: Path,
) -> None:
    """アクション0件では『現在の予定アクションはありません』が表示される。"""
    with (
        patch("agriha.chat.app.fetch_sensors", return_value={}),
        patch("agriha.chat.app.fetch_status", return_value={}),
        patch("agriha.chat.app.get_relay_labels", return_value={}),
    ):
        r = await client.get("/api/dashboard-partial")
    assert r.status_code == 200
    assert "現在の予定アクションはありません" in r.text


# ── _load_rules_text / save_rules ─────────────────────────────────────────────

def test_load_rules_text_from_file(tmp_path: Path) -> None:
    """rules.yaml が存在する場合、そのテキストを返す。"""
    rules_file = tmp_path / "rules.yaml"
    content = "temperature:\n  target_day: 28.0\n"
    rules_file.write_text(content, encoding="utf-8")
    result = _load_rules_text(str(rules_file))
    assert result == content


def test_load_rules_text_missing_file_returns_empty(tmp_path: Path) -> None:
    """rules.yaml が存在しない場合、空文字を返す（フォールバックも不在）。"""
    with patch.object(app_module, "_RULES_FALLBACK_PATH", str(tmp_path / "fallback.yaml")):
        result = _load_rules_text(str(tmp_path / "nonexistent.yaml"))
    assert result == ""


def test_save_rules_writes_text_and_creates_backup(tmp_path: Path) -> None:
    """save_rules はテキストを書き込み、既存ファイルをバックアップする。"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text("temperature:\n  target_day: 26.0\n", encoding="utf-8")

    new_content = "temperature:\n  target_day: 28.0\n"
    save_rules(str(rules_file), new_content)

    assert rules_file.read_text(encoding="utf-8") == new_content
    backups = list(tmp_path.glob("rules.yaml.bak.*"))
    assert len(backups) == 1


# ── POST /settings/rules ───────────────────────────────────────────────────────

@pytest.fixture
async def settings_client(tmp_path: Path) -> AsyncClient:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "temperature:\n  target_day: 26.0\n  target_night: 17.0\n",
        encoding="utf-8",
    )

    with (
        patch.object(app_module, "UI_AUTH_USER", "testadmin"),
        patch.object(app_module, "UI_AUTH_PASS", "testpass"),
        patch.object(app_module, "AGRIHA_FLAG_DIR", str(tmp_path)),
        patch.object(app_module, "CURRENT_PLAN_PATH", str(tmp_path / "current_plan.json")),
        patch.object(app_module, "AGRIHA_LOG_DIR", str(tmp_path)),
        patch.object(app_module, "RULES_CONFIG_PATH", str(rules_file)),
        patch.object(app_module, "SYSTEM_PROMPT_PATH", str(tmp_path / "system_prompt.txt")),
        patch.object(app_module, "AGRIHA_THRESHOLDS_PATH", str(tmp_path / "thresholds.yaml")),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=_basic_auth(),
            follow_redirects=True,
        ) as ac:
            yield ac


async def test_post_rules_valid_yaml(settings_client: AsyncClient, tmp_path: Path) -> None:
    """正常な YAML テキストで POST すると saved=1 にリダイレクトされファイルが保存される。"""
    new_yaml = "temperature:\n  target_day: 28.0\n  target_night: 18.0\n"
    r = await settings_client.post("/settings/rules", data={"rules_text": new_yaml})
    assert r.status_code == 200
    assert "saved=1" in str(r.url)


async def test_post_rules_invalid_yaml_redirects_error(
    settings_client: AsyncClient, tmp_path: Path,
) -> None:
    """YAML 構文エラーの場合は error=1 にリダイレクトされる。"""
    r = await settings_client.post("/settings/rules", data={
        "rules_text": "temperature:\n  target_day: [\nbad yaml",
    })
    assert r.status_code == 200
    assert "error=1" in str(r.url)


async def test_post_rules_invalid_yaml_does_not_write_file(
    settings_client: AsyncClient, tmp_path: Path,
) -> None:
    """YAML 構文エラーの場合はファイルが書き込まれない。"""
    rules_file = tmp_path / "rules.yaml"
    original = rules_file.read_text(encoding="utf-8")

    await settings_client.post("/settings/rules", data={
        "rules_text": "temperature:\n  target_day: [\nbad yaml",
    })

    assert rules_file.read_text(encoding="utf-8") == original


async def test_settings_page_renders_rules_textarea(settings_client: AsyncClient) -> None:
    """GET /settings が制御ルール設定セクション（textarea）を描画する。"""
    with (
        patch("agriha.chat.app.fetch_sensors", return_value={}),
        patch("agriha.chat.app.get_relay_labels", return_value={}),
    ):
        r = await settings_client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "制御ルール設定" in html
    assert "rules_text" in html
    assert "target_day" in html
