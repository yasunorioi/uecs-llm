"""tests/chat/test_app.py — dashboard API + 計画タイムライン + rules設定 + channel_map設定 のテスト

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
- _load_channel_map_text / save_channel_map  channel_map.yaml 読み書き
- POST /settings/channel_map  チャンネルマップ保存エンドポイント
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
from agriha.chat.app import (
    _build_plan_timeline,
    _load_channel_map_text,
    _load_forecast_config_text,
    _load_rules_text,
    app,
    mask_api_key,
    read_env_file,
    save_channel_map,
    save_forecast_config,
    save_rules,
    write_env_key,
)


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
    """save_rules はテキストを書き込む。"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text("temperature:\n  target_day: 26.0\n", encoding="utf-8")

    new_content = "temperature:\n  target_day: 28.0\n"
    save_rules(str(rules_file), new_content)

    assert rules_file.read_text(encoding="utf-8") == new_content


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


# ── _load_channel_map_text / save_channel_map ─────────────────────────────────

def test_load_channel_map_text_from_file(tmp_path: Path) -> None:
    """channel_map.yaml が存在する場合、そのテキストを返す。"""
    ch_file = tmp_path / "channel_map.yaml"
    content = "irrigation:\n  channel: 4\n"
    ch_file.write_text(content, encoding="utf-8")
    result = _load_channel_map_text(str(ch_file))
    assert result == content


def test_load_channel_map_text_missing_file_returns_empty(tmp_path: Path) -> None:
    """channel_map.yaml が存在しない場合、空文字を返す（フォールバックも不在）。"""
    with patch.object(app_module, "_CHANNEL_MAP_FALLBACK_PATH", str(tmp_path / "fallback.yaml")):
        result = _load_channel_map_text(str(tmp_path / "nonexistent.yaml"))
    assert result == ""


def test_save_channel_map_writes_text_and_creates_backup(tmp_path: Path) -> None:
    """save_channel_map はテキストを書き込む。"""
    ch_file = tmp_path / "channel_map.yaml"
    ch_file.write_text("irrigation:\n  channel: 4\n", encoding="utf-8")

    new_content = "irrigation:\n  channel: 4\nvalid_channels:\n  min: 1\n  max: 8\n"
    save_channel_map(str(ch_file), new_content)

    assert ch_file.read_text(encoding="utf-8") == new_content


# ── POST /settings/channel_map ─────────────────────────────────────────────────

@pytest.fixture
async def settings_client_with_channel_map(tmp_path: Path) -> AsyncClient:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "temperature:\n  target_day: 26.0\n  target_night: 17.0\n",
        encoding="utf-8",
    )
    ch_file = tmp_path / "channel_map.yaml"
    ch_file.write_text(
        "irrigation:\n  channel: 4\nvalid_channels:\n  min: 1\n  max: 8\n",
        encoding="utf-8",
    )

    with (
        patch.object(app_module, "UI_AUTH_USER", "testadmin"),
        patch.object(app_module, "UI_AUTH_PASS", "testpass"),
        patch.object(app_module, "AGRIHA_FLAG_DIR", str(tmp_path)),
        patch.object(app_module, "CURRENT_PLAN_PATH", str(tmp_path / "current_plan.json")),
        patch.object(app_module, "AGRIHA_LOG_DIR", str(tmp_path)),
        patch.object(app_module, "RULES_CONFIG_PATH", str(rules_file)),
        patch.object(app_module, "CHANNEL_MAP_PATH", str(ch_file)),
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


async def test_post_channel_map_valid_yaml(
    settings_client_with_channel_map: AsyncClient,
) -> None:
    """正常な YAML テキストで POST すると saved=1 にリダイレクトされファイルが保存される。"""
    new_yaml = "irrigation:\n  channel: 4\nvalid_channels:\n  min: 1\n  max: 8\n"
    r = await settings_client_with_channel_map.post(
        "/settings/channel_map", data={"channel_map_text": new_yaml}
    )
    assert r.status_code == 200
    assert "saved=1" in str(r.url)


async def test_post_channel_map_invalid_yaml_redirects_error(
    settings_client_with_channel_map: AsyncClient,
) -> None:
    """YAML 構文エラーの場合は error=1 にリダイレクトされる。"""
    r = await settings_client_with_channel_map.post(
        "/settings/channel_map",
        data={"channel_map_text": "irrigation:\n  channel: [\nbad yaml"},
    )
    assert r.status_code == 200
    assert "error=1" in str(r.url)


async def test_post_channel_map_invalid_yaml_does_not_write_file(
    settings_client_with_channel_map: AsyncClient, tmp_path: Path,
) -> None:
    """YAML 構文エラーの場合はファイルが書き込まれない。"""
    ch_file = tmp_path / "channel_map.yaml"
    original = ch_file.read_text(encoding="utf-8")

    await settings_client_with_channel_map.post(
        "/settings/channel_map",
        data={"channel_map_text": "irrigation:\n  channel: [\nbad yaml"},
    )

    assert ch_file.read_text(encoding="utf-8") == original


async def test_settings_page_renders_channel_map_textarea(
    settings_client_with_channel_map: AsyncClient,
) -> None:
    """GET /settings がチャンネルマップ設定セクション（textarea）を描画する。"""
    with (
        patch("agriha.chat.app.fetch_sensors", return_value={}),
        patch("agriha.chat.app.get_relay_labels", return_value={}),
    ):
        r = await settings_client_with_channel_map.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "チャンネルマップ設定" in html
    assert "channel_map_text" in html
    assert "channel: 4" in html


# ── _load_forecast_config_text / save_forecast_config ─────────────────────

def test_load_forecast_config_text_from_file(tmp_path: Path) -> None:
    """forecast.yaml が存在する場合、そのテキストを返す。"""
    fc_file = tmp_path / "forecast.yaml"
    content = "llm:\n  model: claude-haiku-4-5-20251001\n"
    fc_file.write_text(content, encoding="utf-8")
    result = _load_forecast_config_text(str(fc_file))
    assert result == content


def test_load_forecast_config_text_missing_file_returns_empty(tmp_path: Path) -> None:
    """forecast.yaml が存在しない場合、空文字を返す（フォールバックも不在）。"""
    with patch.object(app_module, "_FORECAST_CONFIG_FALLBACK_PATH", str(tmp_path / "fallback.yaml")):
        result = _load_forecast_config_text(str(tmp_path / "nonexistent.yaml"))
    assert result == ""


def test_save_forecast_config_writes_text_and_creates_backup(tmp_path: Path) -> None:
    """save_forecast_config はテキストを書き込む。"""
    fc_file = tmp_path / "forecast.yaml"
    fc_file.write_text("llm:\n  model: old-model\n", encoding="utf-8")

    new_content = "llm:\n  model: claude-haiku-4-5-20251001\n  base_url: ''\n"
    save_forecast_config(str(fc_file), new_content)

    assert fc_file.read_text(encoding="utf-8") == new_content


# ── read_env_file / write_env_key / mask_api_key ──────────────────────────

def test_read_env_file_parses_keys(tmp_path: Path) -> None:
    """read_env_file は .env ファイルを正しくパースする。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-ant-test123\n"
        "# comment\n"
        "OPENAI_API_KEY=sk-openai-test456\n"
        "\n"
        "GOOGLE_API_KEY=AIza-test789\n",
        encoding="utf-8",
    )
    result = read_env_file(str(env_file))
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test123"
    assert result["OPENAI_API_KEY"] == "sk-openai-test456"
    assert result["GOOGLE_API_KEY"] == "AIza-test789"
    assert "# comment" not in result


def test_read_env_file_missing_returns_empty(tmp_path: Path) -> None:
    """ファイルが存在しない場合は空 dict を返す。"""
    result = read_env_file(str(tmp_path / "nonexistent.env"))
    assert result == {}


def test_write_env_key_new_key(tmp_path: Path) -> None:
    """存在しないキーは末尾に追記する。"""
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_KEY=value\n", encoding="utf-8")
    write_env_key(str(env_file), "ANTHROPIC_API_KEY", "sk-new-key")
    content = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-new-key" in content
    assert "EXISTING_KEY=value" in content


def test_write_env_key_updates_existing_key(tmp_path: Path) -> None:
    """既存キーは値を上書きする。"""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=old-key\nOTHER=x\n", encoding="utf-8")
    write_env_key(str(env_file), "ANTHROPIC_API_KEY", "new-key")
    result = read_env_file(str(env_file))
    assert result["ANTHROPIC_API_KEY"] == "new-key"
    assert result["OTHER"] == "x"
    # 重複しないこと
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert sum("ANTHROPIC_API_KEY" in l for l in lines) == 1


def test_write_env_key_creates_file_if_missing(tmp_path: Path) -> None:
    """ファイルが存在しない場合は新規作成する。"""
    env_file = tmp_path / "new.env"
    write_env_key(str(env_file), "MY_KEY", "my-value")
    assert env_file.exists()
    result = read_env_file(str(env_file))
    assert result["MY_KEY"] == "my-value"


def test_read_env_file_export_prefix(tmp_path: Path) -> None:
    """export KEY=value 形式のパースに対応する。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "export ANTHROPIC_API_KEY=sk-ant-test123\n"
        "# comment\n"
        "export VC_API_KEY=vc-test456\n"
        "PLAIN_KEY=plain-value\n",
        encoding="utf-8",
    )
    result = read_env_file(str(env_file))
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-test123"
    assert result["VC_API_KEY"] == "vc-test456"
    assert result["PLAIN_KEY"] == "plain-value"


def test_write_env_key_preserves_export_prefix(tmp_path: Path) -> None:
    """export 付き行の更新時に export プレフィックスを維持する。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "export ANTHROPIC_API_KEY=old-key\nexport OTHER=x\n",
        encoding="utf-8",
    )
    write_env_key(str(env_file), "ANTHROPIC_API_KEY", "new-key")
    content = env_file.read_text(encoding="utf-8")
    assert "export ANTHROPIC_API_KEY=new-key" in content
    assert "export OTHER=x" in content
    result = read_env_file(str(env_file))
    assert result["ANTHROPIC_API_KEY"] == "new-key"


def test_mask_api_key_normal() -> None:
    """通常の長さのキーは末尾4文字のみ表示する。"""
    assert mask_api_key("sk-ant-abcdefghijklmn") == "****klmn"
    assert mask_api_key("sk-ant-test1234") == "****1234"


def test_mask_api_key_short() -> None:
    """4文字以下のキーは全マスクする。"""
    assert mask_api_key("abcd") == "****"
    assert mask_api_key("ab") == "****"
    assert mask_api_key("") == "****"


# ── POST /settings/forecast + POST /settings/api_keys ────────────────────

@pytest.fixture
async def settings_client_with_forecast(tmp_path: Path) -> AsyncClient:
    """forecast.yaml / .env を含む settings テスト用クライアント。"""
    fc_file = tmp_path / "forecast.yaml"
    fc_file.write_text(
        "llm:\n  provider: anthropic\n  model: claude-haiku-4-5-20251001\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-existing1234\n", encoding="utf-8")

    with (
        patch.object(app_module, "UI_AUTH_USER", "testadmin"),
        patch.object(app_module, "UI_AUTH_PASS", "testpass"),
        patch.object(app_module, "AGRIHA_FLAG_DIR", str(tmp_path)),
        patch.object(app_module, "CURRENT_PLAN_PATH", str(tmp_path / "current_plan.json")),
        patch.object(app_module, "AGRIHA_LOG_DIR", str(tmp_path)),
        patch.object(app_module, "RULES_CONFIG_PATH", str(tmp_path / "rules.yaml")),
        patch.object(app_module, "CHANNEL_MAP_PATH", str(tmp_path / "channel_map.yaml")),
        patch.object(app_module, "FORECAST_CONFIG_PATH", str(fc_file)),
        patch.object(app_module, "ENV_FILE", str(env_file)),
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


async def test_post_forecast_valid_yaml(
    settings_client_with_forecast: AsyncClient,
) -> None:
    """正常な YAML で POST すると saved=1 にリダイレクトされファイルが保存される。"""
    new_yaml = "llm:\n  model: gpt-4o\n  base_url: 'https://api.openai.com/v1/'\n"
    r = await settings_client_with_forecast.post(
        "/settings/forecast", data={"forecast_config_text": new_yaml}
    )
    assert r.status_code == 200
    assert "saved=1" in str(r.url)


async def test_post_forecast_invalid_yaml_redirects_error(
    settings_client_with_forecast: AsyncClient,
) -> None:
    """YAML 構文エラーの場合は error=1 にリダイレクトされる。"""
    r = await settings_client_with_forecast.post(
        "/settings/forecast",
        data={"forecast_config_text": "llm:\n  model: [\nbad yaml"},
    )
    assert r.status_code == 200
    assert "error=1" in str(r.url)


async def test_post_forecast_invalid_yaml_does_not_write_file(
    settings_client_with_forecast: AsyncClient, tmp_path: Path,
) -> None:
    """YAML 構文エラーの場合はファイルが書き込まれない。"""
    fc_file = tmp_path / "forecast.yaml"
    original = fc_file.read_text(encoding="utf-8")

    await settings_client_with_forecast.post(
        "/settings/forecast",
        data={"forecast_config_text": "llm:\n  model: [\nbad yaml"},
    )

    assert fc_file.read_text(encoding="utf-8") == original


async def test_post_llm_provider_saves_forecast_and_env(
    settings_client_with_forecast: AsyncClient, tmp_path: Path,
) -> None:
    """プロバイダー選択 + APIキーを POST すると forecast.yaml と .env が更新される。"""
    r = await settings_client_with_forecast.post(
        "/settings/llm_provider",
        data={
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "sk-openai-newkey6789",
        },
    )
    assert r.status_code == 200
    assert "saved=1" in str(r.url)

    # forecast.yaml が更新されている
    fc_file = tmp_path / "forecast.yaml"
    fc_data = yaml.safe_load(fc_file.read_text(encoding="utf-8"))
    assert fc_data["llm"]["provider"] == "openai"
    assert fc_data["llm"]["model"] == "gpt-4o"
    assert fc_data["llm"]["api_key_env"] == "OPENAI_API_KEY"

    # .env にキーが保存されている
    env_file = tmp_path / ".env"
    result = read_env_file(str(env_file))
    assert result["OPENAI_API_KEY"] == "sk-openai-newkey6789"


async def test_post_llm_provider_empty_api_key_keeps_existing(
    settings_client_with_forecast: AsyncClient, tmp_path: Path,
) -> None:
    """APIキー空欄で POST すると .env の既存キーは変更されない。"""
    env_file = tmp_path / ".env"
    original = env_file.read_text(encoding="utf-8")

    await settings_client_with_forecast.post(
        "/settings/llm_provider",
        data={"provider": "anthropic", "model": "", "api_key": ""},
    )

    # .env は変更されていない（既存キーが維持される）
    assert env_file.read_text(encoding="utf-8") == original


async def test_post_llm_provider_invalid_provider_returns_error(
    settings_client_with_forecast: AsyncClient,
) -> None:
    """不正なプロバイダーIDで POST すると error=1 にリダイレクトされる。"""
    r = await settings_client_with_forecast.post(
        "/settings/llm_provider",
        data={"provider": "nonexistent", "model": "", "api_key": ""},
    )
    assert r.status_code == 200
    assert "error=1" in str(r.url)


async def test_settings_page_renders_provider_select_and_api_key_status(
    settings_client_with_forecast: AsyncClient,
) -> None:
    """GET /settings がプロバイダー選択・APIキー状態を描画する。"""
    with (
        patch("agriha.chat.app.fetch_sensors", return_value={}),
        patch("agriha.chat.app.get_relay_labels", return_value={}),
    ):
        r = await settings_client_with_forecast.get("/settings")
    assert r.status_code == 200
    html = r.text
    # プロバイダー選択セクション
    assert "LLM" in html
    assert "Anthropic" in html
    assert "OpenAI" in html
    assert "NullClaw" in html
    # forecast.yaml直接編集（詳細設定）
    assert "forecast_config_text" in html
    # NullClawがデフォルト（APIキー未設定でも動作するバナーが表示される）
    assert "ローカル" in html

