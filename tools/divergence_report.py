"""divergence_report.py — DSL primary の判断 vs agriha-controller の実 command 差分レポート.

DSL daemon が Pi4 の /home/pi/ogms-dsl-runtime/daemon.jsonl に append する
`interpreter_tick` event と、agriha-controller が MQTT に publish する
retained state (`agriha/{h}/actuator/VenSdWinrcA` 等) を突合し、
「同じ sensor 入力に対して 2 系統が別々の action を判断した」divergence を検出。

yasu-hp から Pi4 の MQTT に接続 (broker 100.102.95.37:1883) して retained を取得、
daemon.jsonl は ssh 経由 or ローカル rsync 済みの前提。

Usage:
    python3 divergence_report.py --jsonl /srv/shadow/daemon.jsonl \
        --broker 100.102.95.37 --house 2

    # ssh 経由 (yasu-hp 以外から):
    ssh pi@100.102.95.37 cat /home/pi/ogms-dsl-runtime/daemon.jsonl \
        | python3 divergence_report.py --jsonl - --broker 100.102.95.37 --house 2

出力: text summary (最新 N tick の DSL 判断 + 現行 MQTT actuator state + diff)

依存: 標準ライブラリ + paho-mqtt (optional: --broker 省略なら MQTT step skip)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ══════════════════════════════════════════════
# daemon.jsonl 読み込み
# ══════════════════════════════════════════════

@dataclass
class DslTick:
    ts: str
    now_ts: float
    sensors: dict[str, Any]
    triggered: list[str]
    target_opening_pct: int
    close_all_windows: bool
    close_by_wind_direction: bool
    health: dict[str, str]
    skipped_for_health: list[list]
    alerts: list[list]


def read_dsl_ticks(jsonl_path: Path | str, limit: int = 20) -> list[DslTick]:
    """daemon.jsonl から最新 N 個の interpreter_tick を新→古の順で返す。"""
    if jsonl_path == "-" or jsonl_path == Path("-"):
        stream = sys.stdin
        need_close = False
    else:
        p = Path(jsonl_path)
        if not p.exists():
            return []
        stream = p.open(encoding="utf-8")
        need_close = True
    ticks: list[DslTick] = []
    try:
        for ln in stream:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("event") != "interpreter_tick":
                continue
            ticks.append(DslTick(
                ts=e.get("ts", ""),
                now_ts=float(e.get("now_ts", 0.0)),
                sensors=e.get("sensors") or {},
                triggered=list(e.get("triggered") or []),
                target_opening_pct=int(e.get("target_opening_pct", 0)),
                close_all_windows=bool(e.get("close_all_windows", False)),
                close_by_wind_direction=bool(e.get("close_by_wind_direction", False)),
                health=e.get("health") or {},
                skipped_for_health=list(e.get("skipped_for_health") or []),
                alerts=list(e.get("alerts") or []),
            ))
    finally:
        if need_close:
            stream.close()
    return ticks[-limit:][::-1]  # 新→古


# ══════════════════════════════════════════════
# MQTT retained 取得
# ══════════════════════════════════════════════

def fetch_mqtt_retained(
    broker: str,
    port: int,
    prefix: str,
    house: int,
    timeout_sec: float = 3.0,
) -> dict[str, Any]:
    """subscribe → retained だけ受け取って disconnect、値 dict を返す。

    `{prefix}/{house}/actuator/#` を wildcard で subscribe して actuator 系
    (agriha-controller が publish する VenSdWin/Relay/Alert 等) 全部拾う。
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[divergence] paho-mqtt not installed, MQTT step skipped", file=sys.stderr)
        return {}

    wildcard = f"{prefix}/{house}/actuator/#"
    result: dict[str, Any] = {}

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            result[msg.topic] = payload.get("value", payload)
        except Exception:
            result[msg.topic] = msg.payload.decode(errors="replace")

    # paho v2 requires CallbackAPIVersion、v1 fallback
    client_id = f"divergence-report-{int(time.time())}"
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    else:
        client = mqtt.Client(client_id=client_id)
    client.on_message = on_message
    try:
        client.connect(broker, port, keepalive=int(timeout_sec) + 2)
    except OSError as e:
        print(f"[divergence] MQTT connect failed: {e}", file=sys.stderr)
        return {}
    client.subscribe(wildcard, qos=0)
    client.loop_start()
    # retained は subscribe 直後にまとめて配信される、timeout_sec 待って回収
    time.sleep(timeout_sec)
    client.loop_stop()
    client.disconnect()
    return result


# ══════════════════════════════════════════════
# report format
# ══════════════════════════════════════════════

def format_report(
    ticks: list[DslTick],
    mqtt: dict[str, Any],
    house: int,
) -> str:
    lines: list[str] = []
    lines.append("═" * 70)
    lines.append(f" DSL daemon vs agriha-controller divergence (house={house})")
    lines.append("═" * 70)

    if not ticks:
        lines.append("(no DSL interpreter_tick events in daemon.jsonl)")
        return "\n".join(lines)

    latest = ticks[0]
    lines.append(f"latest DSL tick: {latest.ts}")
    lines.append(f"  sensors:  {_short_dict(latest.sensors)}")
    lines.append(f"  health:   {latest.health}")
    if latest.skipped_for_health:
        lines.append(f"  skipped_for_health: {latest.skipped_for_health}")
    if latest.alerts:
        lines.append(f"  alerts:   {latest.alerts}")
    lines.append(f"  DSL says: target_opening_pct={latest.target_opening_pct} "
                 f"close_all={latest.close_all_windows} triggered={latest.triggered}")

    lines.append("")
    lines.append("── real (agriha-controller via MQTT retained) ──")
    if not mqtt:
        lines.append("  (MQTT unreachable or no retained data)")
    else:
        for topic in sorted(mqtt.keys()):
            lines.append(f"  {topic}: {mqtt[topic]}")

    lines.append("")
    lines.append("── divergence ──")
    real_vensdwin = mqtt.get(f"agriha/{house}/actuator/VenSdWinrcA")
    if real_vensdwin is None:
        lines.append("  (real VenSdWinrcA MQTT topic not found)")
    else:
        try:
            real_val = float(real_vensdwin)
            dsl_val = float(latest.target_opening_pct)
            diff = dsl_val - real_val
            marker = "✓ match" if abs(diff) < 0.5 else f"⚠ diff={diff:+.0f}"
            lines.append(f"  VenSdWinrcA: DSL={dsl_val:.0f}  real={real_val:.0f}  {marker}")
        except (TypeError, ValueError):
            lines.append(f"  VenSdWinrcA (non-numeric compare): DSL={latest.target_opening_pct} real={real_vensdwin}")

    lines.append("")
    lines.append("── DSL tick history (newer → older) ──")
    for t in ticks[:10]:
        rh = t.sensors.get("indoor_humidity", "?")
        temp = t.sensors.get("indoor_temp", "?")
        trig = ",".join(t.triggered) if t.triggered else "-"
        skh = f" skipH={len(t.skipped_for_health)}" if t.skipped_for_health else ""
        lines.append(f"  {t.ts[11:19]}  RH={rh}  T={temp}  opening={t.target_opening_pct}  triggered=[{trig}]{skh}")

    return "\n".join(lines)


def _short_dict(d: dict) -> str:
    return "{" + ", ".join(f"{k}={v}" for k, v in sorted(d.items())) + "}"


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="divergence_report",
        description="DSL daemon vs agriha-controller の判断差分レポート"
    )
    p.add_argument("--jsonl", required=True,
                   help="daemon.jsonl path (- で stdin)")
    p.add_argument("--broker", default=None,
                   help="MQTT broker host (省略で MQTT step skip、DSL 側のみ表示)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--prefix", default="agriha",
                   help="MQTT topic prefix")
    p.add_argument("--house", type=int, default=2,
                   help="対象ハウス (1/2/3)")
    p.add_argument("--limit", type=int, default=20,
                   help="表示する DSL tick 数")
    p.add_argument("--timeout", type=float, default=3.0,
                   help="MQTT retained 取得 timeout (秒)")
    args = p.parse_args(argv)

    ticks = read_dsl_ticks(args.jsonl, limit=args.limit)

    mqtt_data: dict[str, Any] = {}
    if args.broker:
        mqtt_data = fetch_mqtt_retained(
            broker=args.broker, port=args.port, prefix=args.prefix,
            house=args.house, timeout_sec=args.timeout,
        )

    print(format_report(ticks, mqtt_data, args.house))
    return 0


if __name__ == "__main__":
    sys.exit(main())
