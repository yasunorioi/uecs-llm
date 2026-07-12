"""replay_driver.py — sensor_log.db 由来スナップショットで DSL interpreter を replay 実行.

Pi4 の rule_engine が本流制御に参加していないので (実制御は arsprout-ccm)、
interpreter vs evaluate_rules の A/B は成立しない。代わりにこの driver で
実センサデータを対象に **interpreter-only trace** を取り、日中/夜間/湿度スパイク時に
どの automation がどの opening_pct を要求するかを蓄積する。

エントリは `shadow_report.py` が読む jsonl フォーマットで、mode="replay" を立てる。
Report 側は shadow モードとは別 lane で集計する。

依存: 標準ライブラリ + pyyaml + automation_interpreter (同ディレクトリに配置)。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# 配置想定は 2 通り:
#   1) yasu-hp: automation_interpreter.py をこのファイルと同ディレクトリに配置
#   2) dev (uecs-llm repo): src/agriha/control/automation_interpreter.py を参照
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_REPO_SRC = _HERE.parent / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))

try:
    from automation_interpreter import (  # noqa: E402
        Interpreter,
        Result,
        load_automations,
    )
except ImportError:
    from agriha.control.automation_interpreter import (  # type: ignore  # noqa: E402
        Interpreter,
        Result,
        load_automations,
    )

_JST = ZoneInfo("Asia/Tokyo")

# ══════════════════════════════════════════════
# MQTT topic → interpreter sensor 名 マッピング
# ══════════════════════════════════════════════
#
# 実データ source は Pi4 の /home/pi/agriha_history.db (agriha_logger.py が
# agriha/# を wildcard subscribe して SQLite series/samples に蓄積したもの)。
# 旧設計は uecs-llm の /var/lib/agriha/sensor_log.db (3 metric しか無い劣化コピー)
# を読んでいたが、DSL の replay 実データ充実のため 2026-07-12 に切替。
#
# house_id を引数に取り、per-house sensor topic + farm 共通 weather topic を
# interpreter sensor 名 (indoor_temp / outdoor_temp / ...) にマップする。

def build_metric_map(house_id: str) -> dict[str, str]:
    """agriha_history.db series.key → interpreter sensor 名。"""
    return {
        # house 固有
        f"agriha/{house_id}/sensor/InAirTemp":   "indoor_temp",
        f"agriha/{house_id}/sensor/InAirHumid": "indoor_humidity",
        f"agriha/{house_id}/sensor/InAirCO2":   "indoor_co2",
        f"agriha/{house_id}/sensor/InRadiation": "insolar",  # 室内日射 (代替として)
        # farm 共通
        "agriha/farm/weather/WAirTemp":    "outdoor_temp",
        "agriha/farm/weather/WWindSpeed":  "wind_speed",
        "agriha/farm/weather/WWindDir16":  "wind_direction",
        "agriha/farm/weather/WRainfallAmt": "rainfall",
    }


# ══════════════════════════════════════════════
# SQLite 読取
# ══════════════════════════════════════════════

def read_latest_snapshot(
    db_path: Path, house_id: str = "1", max_age_min: int = 60
) -> tuple[dict[str, float | None], str | None]:
    """agriha_history.db の series/samples から最新値を取り、interpreter
    sensor 名にマップした dict を返す。max_age_min を超えた series は捨てる。

    Returns: (sensors_dict, latest_ts_iso)
    """
    mapping = build_metric_map(house_id)
    now = int(datetime.now(_JST).timestamp())
    max_age_sec = max_age_min * 60

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    snapshot: dict[str, float | None] = {}
    latest_ts_epoch = 0
    for topic, sname in mapping.items():
        row = cur.execute(
            """
            SELECT sa.value, sa.ts FROM samples sa
            JOIN series s ON s.id = sa.sid
            WHERE s.key = ?
            ORDER BY sa.ts DESC LIMIT 1
            """,
            (topic,),
        ).fetchone()
        if row is None:
            continue
        value, ts = row
        if (now - int(ts)) > max_age_sec:
            continue  # stale
        snapshot[sname] = float(value)
        latest_ts_epoch = max(latest_ts_epoch, int(ts))
    conn.close()

    # 欠損は interpreter が strict=False で skip できるよう defaults を埋める。
    # rain/wind は 0 固定 (P1/P2 が誤発火しないため)、outdoor/insolar は None
    snapshot.setdefault("rainfall", 0.0)
    snapshot.setdefault("wind_speed", 0.0)
    snapshot.setdefault("wind_direction", 0)
    snapshot.setdefault("outdoor_temp", None)
    snapshot.setdefault("insolar", None)

    latest_ts_iso = (
        datetime.fromtimestamp(latest_ts_epoch, _JST).isoformat(timespec="seconds")
        if latest_ts_epoch else None
    )
    return snapshot, latest_ts_iso


# ══════════════════════════════════════════════
# minimal primitives (rule_engine 未 import)
# ══════════════════════════════════════════════

def _time_of_day_period(now: datetime) -> str:
    """cfg の temperature_schedule と同じ label を time-of-day だけで近似。"""
    h = now.hour
    if h < 4:
        return "night"
    if h < 6:
        return "pre_dawn"
    if h < 11:
        return "morning"
    if h < 15:
        return "afternoon"
    if h < 18:
        return "evening"
    return "night"


def build_simple_primitives(cfg: dict, crop_cfg: dict) -> dict:
    """rule_engine helper を import せずに済む最小 primitive セット。

    - rise_rate / solar_accumulated_mj / tide_forecast_humidity_1h → None
      (履歴/予測が無いので該当 automation は strict=False で skip される)
    - schedule_target_temp / is_night → cfg + 時刻から近似
    - window_step_lookup → cfg.window_step_table lookup
    - solar_threshold / irrigation_duration → crop cfg の値
    """
    schedule = cfg.get("temperature_schedule", {})

    def _schedule_target_temp() -> float:
        period = _time_of_day_period(datetime.now(_JST))
        node = schedule.get(period) or schedule.get("night") or {}
        return float(node.get("target", 22.0))

    def _is_night() -> bool:
        return _time_of_day_period(datetime.now(_JST)) == "night"

    def _window_step_lookup(outdoor_temp: float | None) -> int:
        if outdoor_temp is None:
            return 0
        table = cfg.get("window_step_table") or {}
        pairs = sorted((int(k), int(v)) for k, v in table.items())
        if not pairs:
            return 0
        if outdoor_temp <= pairs[0][0]:
            return pairs[0][1]
        if outdoor_temp >= pairs[-1][0]:
            return pairs[-1][1]
        for i in range(len(pairs) - 1):
            t0, p0 = pairs[i]
            t1, p1 = pairs[i + 1]
            if t0 <= outdoor_temp <= t1:
                ratio = (outdoor_temp - t0) / (t1 - t0) if t1 != t0 else 0
                return int(p0 + ratio * (p1 - p0))
        return 0

    def _solar_threshold() -> float:
        return float(crop_cfg.get("solar_threshold_mj", 1.5))

    def _irrigation_duration() -> int:
        return int(crop_cfg.get("irrigation_duration_sec", 60))

    return {
        "rise_rate": lambda: None,
        "solar_accumulated_mj": lambda: 0.0,
        "schedule_period": lambda: _time_of_day_period(datetime.now(_JST)),
        "schedule_target_temp": _schedule_target_temp,
        "is_night": _is_night,
        "sunset_offset_min": lambda: 0,
        "tide_forecast_humidity_1h": lambda: None,
        "window_step_lookup": _window_step_lookup,
        "solar_threshold": _solar_threshold,
        "irrigation_duration": _irrigation_duration,
    }


# ══════════════════════════════════════════════
# entry 組立
# ══════════════════════════════════════════════

def build_entry(
    result: Result, sensors: dict, snapshot_ts: str | None
) -> dict:
    ts = datetime.now(_JST).isoformat()
    return {
        "ts": ts,
        "mode": "replay",
        "match": None,
        "sensors": sensors,
        "snapshot_ts": snapshot_ts,
        "interp": {
            "triggered": result.triggered,
            "target_opening_pct": result.target_opening_pct,
            "close_all_windows": result.close_all_windows,
            "close_by_wind_direction": result.close_by_wind_direction,
            "relay_pulses": result.relay_pulses,
            "skipped": result.skipped,
            "active_after": sorted(result.active_after),
        },
        "live": None,
        "delta": None,
    }


# ══════════════════════════════════════════════
# main
# ══════════════════════════════════════════════

def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_active_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()).get("active", []))
    except Exception:
        return set()


def save_active_state(path: Path, active: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"active": sorted(active),
                    "saved_at": datetime.now(_JST).isoformat()})
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replay_driver")
    p.add_argument("--sensor-db", required=True,
                   help="agriha_history.db path (Pi4 /home/pi/agriha_history.db)")
    p.add_argument("--house", default="1",
                   help="対象ハウス ID (agriha/{house}/sensor/... を読む)")
    p.add_argument("--automations", required=True, help="automations.yaml path")
    p.add_argument("--config", required=True, help="rules.yaml path")
    p.add_argument("--crop", required=True, help="crop_irrigation.yaml path")
    p.add_argument("--out", required=True, help="shadow_log.jsonl (append)")
    p.add_argument("--active-state", default=None,
                   help="active_state.json for hysteresis persistence")
    p.add_argument("--max-age-min", type=int, default=60,
                   help="センサ値の許容 age (min) — 超過は stale として drop")
    p.add_argument("--strict", action="store_true",
                   help="式評価失敗時に raise (default: skip)")
    args = p.parse_args(argv)

    cfg = load_yaml(Path(args.config))
    crop_cfg = load_yaml(Path(args.crop))
    automations_data = load_yaml(Path(args.automations))
    automations = load_automations(automations_data.get("automations", []))

    sensors, snapshot_ts = read_latest_snapshot(
        Path(args.sensor_db), house_id=args.house, max_age_min=args.max_age_min
    )

    active_path = Path(args.active_state) if args.active_state else None
    active_before = load_active_state(active_path) if active_path else set()

    interp = Interpreter(automations)
    result = interp.evaluate(
        sensors=sensors,
        state={},  # replay: state 副作用は無視 (jsonl に載せて後で分析)
        config=cfg,
        crop_config=crop_cfg,
        primitives=build_simple_primitives(cfg, crop_cfg),
        active_before=active_before,
        strict=args.strict,
    )

    if active_path:
        save_active_state(active_path, result.active_after)

    entry = build_entry(result, sensors, snapshot_ts)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # stderr に 1 行 summary (cron ログ用)
    print(
        f"replay: sensors={list(sensors.keys())} "
        f"opening={result.target_opening_pct}% "
        f"triggered={result.triggered} skipped={len(result.skipped)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
