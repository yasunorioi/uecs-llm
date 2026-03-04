"""agriha_ui/app.py - AgriHA Web UI バックエンド (FastAPI)

ダッシュボード + 設定画面 + 制御履歴
ポート: 8502
設計書: docs/v2_three_layer_design.md
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── 環境変数（デフォルト値は設計書§6.1に従う）─────────────────────────────
UNIPI_API_URL = os.getenv("UNIPI_API_URL", "http://localhost:8080")
SYSTEM_PROMPT_PATH = os.getenv("SYSTEM_PROMPT_PATH", "/etc/agriha/system_prompt.txt")
AGRIHA_THRESHOLDS_PATH = os.getenv("AGRIHA_THRESHOLDS_PATH", "/etc/agriha/thresholds.yaml")
CONTROL_LOG_DB = os.getenv("CONTROL_LOG_DB", "/var/lib/agriha/control_log.db")
UI_AUTH_USER = os.getenv("UI_AUTH_USER", "admin")
UI_AUTH_PASS = os.getenv("UI_AUTH_PASS", "agriha")

# ── ランタイムデータファイルパス ───────────────────────────────────────────
RULE_ENGINE_STATE_PATH = os.getenv("RULE_ENGINE_STATE_PATH", "/var/lib/agriha/rule_engine_state.json")
CURRENT_PLAN_PATH = os.getenv("CURRENT_PLAN_PATH", "/var/lib/agriha/current_plan.json")
AGRIHA_FLAG_DIR = os.getenv("AGRIHA_FLAG_DIR", "/var/lib/agriha")
AGRIHA_LOG_DIR = os.getenv("AGRIHA_LOG_DIR", "/var/log/agriha")

# ── FastAPI / テンプレート設定 ─────────────────────────────────────────────
_HERE = Path(__file__).parent

app = FastAPI(title="AgriHA UI", version="2.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))

security = HTTPBasic()


# ── Basic 認証 ─────────────────────────────────────────────────────────────
def verify_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    """Basic 認証チェック。失敗時は 401 を返す。"""
    ok = secrets.compare_digest(credentials.username, UI_AUTH_USER) and \
         secrets.compare_digest(credentials.password, UI_AUTH_PASS)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証に失敗しました",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── ヘルパー関数 ───────────────────────────────────────────────────────────

def load_thresholds(path: str = AGRIHA_THRESHOLDS_PATH) -> dict[str, Any]:
    """thresholds.yaml を読み込む。ファイルがなければデフォルト値を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data
    except FileNotFoundError:
        return {
            "emergency": {"high_temp": 27.0, "low_temp": 16.0},
            "co2": {"target_ppm": 700},
        }


def save_thresholds(path: str, data: dict[str, Any]) -> None:
    """thresholds.yaml に書き込む。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def load_system_prompt(path: str = SYSTEM_PROMPT_PATH) -> str:
    """system_prompt.txt を読み込む。ファイルがなければ空文字を返す。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save_system_prompt(path: str, text: str) -> None:
    """system_prompt.txt に書き込む（バックアップ付き）。"""
    p = Path(path)
    if p.exists():
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        p.rename(f"{path}.bak.{ts}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def fetch_sensors(api_url: str = UNIPI_API_URL) -> dict[str, Any]:
    """GET /api/sensors を取得する。失敗時は空 dict を返す。
    unipi-daemon は {"sensors": {...}} を返すので中身を取り出す。"""
    try:
        r = httpx.get(f"{api_url}/api/sensors", timeout=5.0)
        r.raise_for_status()
        data = r.json()
        return data.get("sensors", data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def fetch_status(api_url: str = UNIPI_API_URL) -> dict[str, Any]:
    """GET /api/status を取得する。失敗時は空 dict を返す。"""
    try:
        r = httpx.get(f"{api_url}/api/status", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def fetch_relay(api_url: str = UNIPI_API_URL) -> dict[str, Any]:
    """GET /api/relay を取得する。失敗時は空 dict を返す。"""
    try:
        r = httpx.get(f"{api_url}/api/relay", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def load_json_file(path: str) -> dict[str, Any]:
    """JSON ファイルを読み込む。失敗時は空 dict を返す。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def query_decisions(db_path: str = CONTROL_LOG_DB, hours: int = 24) -> list[dict]:
    """control_log.db から直近 N 時間の判断ログを取得する。DB不在時は空リスト。"""
    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = con.execute(
            "SELECT timestamp, summary, actions_taken FROM decisions "
            "WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 200",
            (since,),
        ).fetchall()
        con.close()
        result = []
        for row in rows:
            summary = row["summary"] or ""
            actions = row["actions_taken"] or ""
            # layer 推定: summary / actions_taken の内容から判断
            lower = (summary + actions).lower()
            if "emergency" in lower:
                layer = "layer1"
            elif any(k in lower for k in ("rule", "threshold", "rain", "wind", "solar")):
                layer = "layer2"
            else:
                layer = "layer3"
            result.append({
                "timestamp": row["timestamp"],
                "layer": layer,
                "action": actions[:100],
                "detail": summary[:200],
            })
        return result
    except Exception:
        return []


def _build_dashboard_context() -> dict[str, Any]:
    """ダッシュボード用テンプレート変数を構築する。"""
    sensors = fetch_sensors()
    status_data = fetch_status()
    rule_state = load_json_file(RULE_ENGINE_STATE_PATH)
    plan = load_json_file(CURRENT_PLAN_PATH)

    # lockout セクション（GET /api/status から）
    lockout = {
        "active": bool(status_data.get("locked_out", False)),
        "reason": "CommandGate" if status_data.get("locked_out") else "",
        "remaining_sec": int(status_data.get("lockout_remaining_sec", 0)),
    }

    # rule_engine セクション（rule_engine_state.json から）
    relay_actions = rule_state.get("relay_actions", [])
    actions_list = [
        f"ch{a['channel']} → {'ON' if a.get('value') else 'OFF'}"
        for a in relay_actions
        if isinstance(a, dict)
    ]
    rule_engine = {
        "last_run": rule_state.get("last_run_at", "N/A"),
        "actions": actions_list,
    }

    # forecast セクション（current_plan.json から）
    forecast = {
        "generated_at": plan.get("generated_at", "N/A"),
        "summary": (plan.get("summary") or "")[:200],
        "action_count": len(plan.get("actions", [])),
    }

    return {
        "sensors": sensors,
        "lockout": lockout,
        "rule_engine": rule_engine,
        "forecast": forecast,
        "relays": status_data.get("relay_state") or {},
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── API ヘルパー ────────────────────────────────────────────────────────────

def _get_flags_data() -> dict[str, bool]:
    """フラグファイルの存在チェック結果を返す（実行時にモジュール変数を参照）。

    §3.5定義: rain, wind, lockout（_flag サフィックスなし）
    """
    flag_dir = AGRIHA_FLAG_DIR
    return {
        "lockout": os.path.exists(os.path.join(flag_dir, "lockout")),
        "rain": os.path.exists(os.path.join(flag_dir, "rain_flag")),
        "wind": os.path.exists(os.path.join(flag_dir, "wind_flag")),
    }


def _get_plan_data() -> dict[str, Any]:
    """current_plan.json を読み込む（実行時にモジュール変数を参照）。ファイルなし時は {"plan": None} を返す。"""
    plan_path = CURRENT_PLAN_PATH
    if os.path.exists(plan_path):
        try:
            with open(plan_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"plan": None, "message": "No active plan"}


def _tail_log(path: str, lines: int) -> list[str]:
    """ログファイルの末尾 N 行を返す。ファイルなし時は空リスト。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return [line.rstrip() for line in all_lines[-lines:]]
    except FileNotFoundError:
        return []


# ── ルート定義 ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: None = Depends(verify_auth),
) -> HTMLResponse:
    """ダッシュボード画面。"""
    ctx = _build_dashboard_context()
    return templates.TemplateResponse("dashboard.html", {"request": request, **ctx})


@app.get("/api/dashboard-partial", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    _: None = Depends(verify_auth),
) -> HTMLResponse:
    """htmx 用部分テンプレート（dashboard.html の <main> 内部のみ）。"""
    ctx = _build_dashboard_context()
    return templates.TemplateResponse(
        "dashboard_partial.html", {"request": request, **ctx}
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings(
    request: Request,
    saved: str = "",
    error: str = "",
    _: None = Depends(verify_auth),
) -> HTMLResponse:
    """設定画面。"""
    thresholds = load_thresholds()
    system_prompt = load_system_prompt()
    flash: str | None = None
    if saved:
        flash = "設定を保存しました"
    elif error:
        flash = "保存に失敗しました"
    ctx = {
        "request": request,
        "system_prompt": system_prompt,
        "thresholds": thresholds,
        "flash": flash,
    }
    return templates.TemplateResponse("settings.html", ctx)


@app.post("/settings/prompt")
async def save_prompt(
    prompt_text: str = Form(...),
    _: None = Depends(verify_auth),
) -> RedirectResponse:
    """system_prompt.txt を保存する（バックアップ付き）。"""
    try:
        save_system_prompt(SYSTEM_PROMPT_PATH, prompt_text)
        return RedirectResponse(url="/settings?saved=1", status_code=303)
    except Exception:
        return RedirectResponse(url="/settings?error=1", status_code=303)


@app.post("/settings/thresholds")
async def save_thresholds_route(
    high_temp: float = Form(...),
    low_temp: float = Form(...),
    co2_target: int = Form(...),
    _: None = Depends(verify_auth),
) -> RedirectResponse:
    """thresholds.yaml を保存する。バリデーション: high > low, 0 < co2 < 5000。"""
    try:
        if high_temp <= low_temp:
            return RedirectResponse(url="/settings?error=1", status_code=303)
        if not (0 < co2_target < 5000):
            return RedirectResponse(url="/settings?error=1", status_code=303)
        data = {
            "emergency": {"high_temp": high_temp, "low_temp": low_temp},
            "co2": {"target_ppm": co2_target},
        }
        save_thresholds(AGRIHA_THRESHOLDS_PATH, data)
        return RedirectResponse(url="/settings?saved=1", status_code=303)
    except Exception:
        return RedirectResponse(url="/settings?error=1", status_code=303)


@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    _: None = Depends(verify_auth),
) -> HTMLResponse:
    """制御履歴画面（直近24時間）。"""
    decisions = query_decisions()
    layer_counts: dict[str, int] = {"layer1": 0, "layer2": 0, "layer3": 0}
    for d in decisions:
        key = d.get("layer", "layer3")
        if key in layer_counts:
            layer_counts[key] += 1
    ctx = {
        "request": request,
        "decisions": decisions,
        "layer_counts": layer_counts,
    }
    return templates.TemplateResponse("history.html", ctx)


@app.get("/api/flags")
async def get_flags(_: None = Depends(verify_auth)) -> dict[str, bool]:
    """フラグファイル (lockout, rain_flag, wind_flag) の存在状態を返す。"""
    return _get_flags_data()


@app.get("/api/plan")
async def get_plan(_: None = Depends(verify_auth)) -> dict[str, Any]:
    """current_plan.json の内容を返す。ファイルなし時は {"plan": null} を返す。"""
    return _get_plan_data()


@app.get("/api/dashboard")
async def get_dashboard_data(_: None = Depends(verify_auth)) -> dict[str, Any]:
    """センサー・計画・relay・フラグ・logsを集約して返す（§3.5 集約エンドポイント）。"""
    log_dir = AGRIHA_LOG_DIR
    return {
        "sensors": fetch_sensors(),
        "plan": _get_plan_data(),
        "relay": fetch_relay(),
        "flags": _get_flags_data(),
        "logs": {
            "control_log": _tail_log(os.path.join(log_dir, "control.log"), 10),
            "search_log": _tail_log(os.path.join(log_dir, "search_log.jsonl"), 10),
            "forecast_log": _tail_log(os.path.join(log_dir, "forecast.log"), 10),
            "emergency_log": _tail_log(os.path.join(log_dir, "emergency.log"), 10),
        },
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/logs")
async def get_logs(
    lines: int = 50,
    _: None = Depends(verify_auth),
) -> dict[str, list[str]]:
    """4ログファイルの末尾 N 行を返す (max 200行)。"""
    n = min(max(1, lines), 200)
    log_dir = AGRIHA_LOG_DIR
    return {
        "control_log": _tail_log(os.path.join(log_dir, "control.log"), n),
        "search_log": _tail_log(os.path.join(log_dir, "search_log.jsonl"), n),
        "forecast_log": _tail_log(os.path.join(log_dir, "forecast.log"), n),
        "emergency_log": _tail_log(os.path.join(log_dir, "emergency.log"), n),
    }
