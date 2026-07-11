"""shadow_report.py — DSL shadow-run 差分ログの集計/表示ツール.

`automation_shadow.log_shadow_result` が吐く jsonl (通常
`/var/lib/agriha/shadow_log.jsonl`) を読み、interpreter と evaluate_rules の
action 一致率・divergence の傾向を集計する。

Subcommands:
  summary — 集計を text で標準出力
  html    — self-contained HTML を --out に書き出す (nginx で serve する用途)
  sample  — 開発用の synthetic jsonl を --out に書く

I/O 以外 stdlib のみ。yasu-hp (Python 3.14) にそのままコピーして動く前提。
"""

from __future__ import annotations

import argparse
import html
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ══════════════════════════════════════════════
# 読み込み
# ══════════════════════════════════════════════

def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """jsonl を list[dict] に。壊れた行は stderr に warn して skip。"""
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(
                    f"warn: {path}:{lineno} malformed json: {e}",
                    file=sys.stderr,
                )
    return entries


# ══════════════════════════════════════════════
# 集計モデル
# ══════════════════════════════════════════════

@dataclass
class Summary:
    total: int = 0
    matched: int = 0
    diverged: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    delta_key_counts: Counter[str] = field(default_factory=Counter)
    delta_pattern_counts: dict[str, Counter[str]] = field(default_factory=dict)
    interp_trigger_counts: Counter[str] = field(default_factory=Counter)
    live_trigger_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)

    @property
    def match_rate(self) -> float:
        return (self.matched / self.total) if self.total else 0.0


def summarize(entries: Iterable[dict[str, Any]]) -> Summary:
    s = Summary()
    for e in entries:
        s.total += 1
        ts = e.get("ts")
        if ts:
            if s.first_ts is None or ts < s.first_ts:
                s.first_ts = ts
            if s.last_ts is None or ts > s.last_ts:
                s.last_ts = ts

        if e.get("match"):
            s.matched += 1
        else:
            s.diverged += 1

        delta = e.get("delta") or {}
        for key, pair in delta.items():
            s.delta_key_counts[key] += 1
            bucket = s.delta_pattern_counts.setdefault(key, Counter())
            bucket[_format_pattern(pair)] += 1

        interp = e.get("interp") or {}
        for tid in interp.get("triggered") or []:
            s.interp_trigger_counts[tid] += 1
        for pair in interp.get("skipped") or []:
            aid = pair[0] if isinstance(pair, list) and pair else str(pair)
            s.skipped_counts[aid] += 1

        live = e.get("live") or {}
        for tid in live.get("triggered") or []:
            s.live_trigger_counts[tid] += 1

    return s


def _format_pattern(pair: Any) -> str:
    if isinstance(pair, dict) and "interp" in pair and "live" in pair:
        return f"interp={pair['interp']!r} live={pair['live']!r}"
    return repr(pair)


# ══════════════════════════════════════════════
# text 出力
# ══════════════════════════════════════════════

def render_text(s: Summary, recent: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("═" * 60)
    lines.append("shadow-run summary")
    lines.append("═" * 60)
    lines.append(f"range   : {s.first_ts}  →  {s.last_ts}")
    lines.append(
        f"entries : total={s.total}  matched={s.matched}  "
        f"diverged={s.diverged}  match_rate={s.match_rate:.1%}"
    )
    lines.append("")

    lines.append("── divergence by delta key ──")
    if not s.delta_key_counts:
        lines.append("  (none)")
    else:
        for key, n in s.delta_key_counts.most_common():
            lines.append(f"  {key:30s} {n:6d}")
            patterns = s.delta_pattern_counts.get(key, Counter())
            for pat, pn in patterns.most_common(5):
                lines.append(f"      {pn:5d} × {pat}")
    lines.append("")

    lines.append("── interp trigger counts (top 15) ──")
    for tid, n in s.interp_trigger_counts.most_common(15):
        lines.append(f"  {tid:30s} {n:6d}")
    if not s.interp_trigger_counts:
        lines.append("  (none)")
    lines.append("")

    if s.skipped_counts:
        lines.append("── skipped automations (interpreter strict=False) ──")
        for aid, n in s.skipped_counts.most_common():
            lines.append(f"  {aid:30s} {n:6d}")
        lines.append("")

    lines.append(f"── recent divergences (last {len(recent)}) ──")
    for e in recent:
        ts = e.get("ts", "?")
        delta = e.get("delta") or {}
        lines.append(f"  {ts}")
        for key, pair in delta.items():
            lines.append(f"      {key}: {_format_pattern(pair)}")
    if not recent:
        lines.append("  (no divergences)")
    lines.append("")
    return "\n".join(lines)


def take_recent_divergences(
    entries: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    div = [e for e in entries if not e.get("match")]
    return div[-limit:] if limit > 0 else div


# ══════════════════════════════════════════════
# HTML 出力 (self-contained, vanilla JS)
# ══════════════════════════════════════════════

_HTML_TEMPLATE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<title>shadow-run report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;margin:1.5rem;max-width:1100px;color:#222}
 h1{font-size:1.3rem;margin:0 0 .3rem}
 .meta{color:#666;font-size:.9rem;margin-bottom:1rem}
 .cards{display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:1rem}
 .card{background:#f4f4f4;padding:.5rem .8rem;border-radius:.4rem;font-size:.9rem}
 .card b{font-size:1.1rem}
 .ok{color:#2a7}
 .ng{color:#c33}
 table{border-collapse:collapse;width:100%;font-size:.85rem;margin-top:.4rem}
 th,td{border:1px solid #ddd;padding:.25rem .5rem;text-align:left;vertical-align:top}
 th{background:#eee}
 tr.match td:first-child{border-left:3px solid #2a7}
 tr.diverge td:first-child{border-left:3px solid #c33}
 details{margin:.6rem 0}
 .filter{margin:.6rem 0;display:flex;gap:.6rem;align-items:center;font-size:.9rem}
 code{font-family:ui-monospace,monospace;font-size:.85rem}
 .pat{color:#555}
</style></head>
<body>
<h1>shadow-run report</h1>
<div class="meta">
  <span>source: <code>__SOURCE__</code></span> ·
  <span>generated: <code>__GENERATED__</code></span> ·
  <span>range: <code>__RANGE__</code></span>
</div>

<div class="cards">
  <div class="card">total <b>__TOTAL__</b></div>
  <div class="card">matched <b class="ok">__MATCHED__</b></div>
  <div class="card">diverged <b class="ng">__DIVERGED__</b></div>
  <div class="card">match rate <b>__MATCHRATE__</b></div>
</div>

<details open><summary><b>divergence by delta key</b></summary>
__DELTA_TABLE__
</details>

<details><summary><b>interp trigger counts</b></summary>
__TRIGGER_TABLE__
</details>

__SKIPPED_BLOCK__

<h2 style="font-size:1.1rem;margin-top:1.4rem">entries</h2>
<div class="filter">
  <label><input type="checkbox" id="only-diverge"> diverged only</label>
  <label>show latest <input type="number" id="limit" value="200" min="1" max="10000" style="width:5rem"></label>
  <span id="visible-count" style="color:#666"></span>
</div>

<table id="entries">
  <thead>
    <tr>
      <th>ts</th><th>match</th><th>interp opening</th>
      <th>live opening</th><th>interp triggered</th><th>delta</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>

<script>
const ENTRIES = __ENTRIES_JSON__;
const tbody = document.querySelector("#entries tbody");
const cbDiv = document.getElementById("only-diverge");
const limitInput = document.getElementById("limit");
const visibleCount = document.getElementById("visible-count");

function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

function render(){
  const only = cbDiv.checked;
  const limit = Math.max(1, parseInt(limitInput.value||"200",10));
  let list = ENTRIES;
  if(only) list = list.filter(e => !e.match);
  list = list.slice(-limit);
  const rows = list.map(e => {
    const interp = e.interp || {};
    const live = e.live || {};
    const delta = e.delta || {};
    const dstr = Object.entries(delta).map(([k,v]) =>
      `<div><b>${esc(k)}</b>: <span class="pat">${esc(JSON.stringify(v))}</span></div>`
    ).join("") || "<span style=color:#999>—</span>";
    const trig = (interp.triggered||[]).join(", ");
    const cls = e.match ? "match" : "diverge";
    return `<tr class="${cls}"><td>${esc(e.ts||"")}</td>`
      + `<td>${e.match ? '<span class=ok>OK</span>' : '<span class=ng>NG</span>'}</td>`
      + `<td>${esc(interp.target_opening_pct ?? "")}</td>`
      + `<td>${esc(live.target_opening_pct ?? "")}</td>`
      + `<td>${esc(trig)}</td>`
      + `<td>${dstr}</td></tr>`;
  });
  tbody.innerHTML = rows.join("");
  visibleCount.textContent = `showing ${list.length} of ${ENTRIES.length}`;
}
cbDiv.addEventListener("change", render);
limitInput.addEventListener("input", render);
render();
</script>
</body></html>
"""


def render_html(
    s: Summary,
    entries: list[dict[str, Any]],
    source: str,
    generated_at: str,
) -> str:
    def esc(v: Any) -> str:
        return html.escape(str(v), quote=True)

    delta_table = _render_delta_html(s)
    trigger_table = _render_kv_table(s.interp_trigger_counts, "trigger", "count")
    if s.skipped_counts:
        skipped_html = (
            "<details><summary><b>skipped (interpreter strict=False)</b></summary>"
            + _render_kv_table(s.skipped_counts, "automation", "count")
            + "</details>"
        )
    else:
        skipped_html = ""

    return (
        _HTML_TEMPLATE
        .replace("__SOURCE__", esc(source))
        .replace("__GENERATED__", esc(generated_at))
        .replace("__RANGE__", esc(f"{s.first_ts} → {s.last_ts}"))
        .replace("__TOTAL__", esc(s.total))
        .replace("__MATCHED__", esc(s.matched))
        .replace("__DIVERGED__", esc(s.diverged))
        .replace("__MATCHRATE__", esc(f"{s.match_rate:.1%}"))
        .replace("__DELTA_TABLE__", delta_table)
        .replace("__TRIGGER_TABLE__", trigger_table)
        .replace("__SKIPPED_BLOCK__", skipped_html)
        .replace("__ENTRIES_JSON__", json.dumps(entries, ensure_ascii=False))
    )


def _render_delta_html(s: Summary) -> str:
    if not s.delta_key_counts:
        return "<p>(no divergences)</p>"
    rows: list[str] = ["<table><thead><tr><th>delta key</th><th>count</th><th>top patterns</th></tr></thead><tbody>"]
    for key, n in s.delta_key_counts.most_common():
        patterns = s.delta_pattern_counts.get(key, Counter()).most_common(5)
        pat_html = "<br>".join(
            f'<span class="pat">{html.escape(p)}</span> × {c}' for p, c in patterns
        )
        rows.append(
            f"<tr><td><code>{html.escape(key)}</code></td>"
            f"<td>{n}</td><td>{pat_html}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_kv_table(counter: Counter[str], key_col: str, val_col: str) -> str:
    if not counter:
        return "<p>(none)</p>"
    rows: list[str] = [f"<table><thead><tr><th>{html.escape(key_col)}</th><th>{html.escape(val_col)}</th></tr></thead><tbody>"]
    for k, n in counter.most_common(30):
        rows.append(
            f"<tr><td><code>{html.escape(k)}</code></td><td>{n}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


# ══════════════════════════════════════════════
# sample generator
# ══════════════════════════════════════════════

def gen_sample_entries(
    count: int, diverge_rate: float, seed: int = 42
) -> list[dict[str, Any]]:
    """開発用の synthetic jsonl。実 log の shape に合わせる。"""
    rng = random.Random(seed)
    entries: list[dict[str, Any]] = []
    for i in range(count):
        ts = f"2026-07-{(i // 144) + 1:02d}T{(i % 144) // 6:02d}:{(i % 6) * 10:02d}:00+09:00"
        interp_open = rng.choice([0, 0, 0, 30, 30, 60])
        live_open = interp_open
        diverge = rng.random() < diverge_rate
        delta: dict[str, Any] = {}
        interp_trig: list[str] = []
        live_trig: list[str] = []
        if interp_open == 30:
            interp_trig.append(rng.choice(["humidity_vent", "co2_vent"]))
            live_trig.append("humidity_ventilation")
        elif interp_open == 60:
            interp_trig.append("co2_critical_vent")
            live_trig.append("co2_critical")
        if diverge:
            live_open = 10 if interp_open == 0 else max(0, interp_open - 10)
            if live_open != interp_open:
                delta["target_opening_pct"] = {
                    "interp": interp_open, "live": live_open,
                }
        entries.append({
            "ts": ts,
            "match": not delta,
            "interp": {
                "triggered": interp_trig,
                "target_opening_pct": interp_open,
                "close_all_windows": False,
                "close_by_wind_direction": False,
                "relay_pulses": [],
                "skipped": [] if rng.random() > 0.05 else [
                    ["tide_preemptive_vent", "trigger: missing config.tide"],
                ],
                "active_after": [],
            },
            "live": {
                "triggered": live_trig,
                "target_opening_pct": live_open,
                "schedule_period": "morning",
            },
            "delta": delta,
        })
    return entries


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════

def _cmd_summary(args: argparse.Namespace) -> int:
    entries = load_jsonl(Path(args.log))
    s = summarize(entries)
    recent = take_recent_divergences(entries, args.limit)
    sys.stdout.write(render_text(s, recent))
    return 0


def _cmd_html(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone
    entries = load_jsonl(Path(args.log))
    s = summarize(entries)
    tail = entries[-args.limit :] if args.limit > 0 else entries
    doc = render_html(
        s,
        tail,
        source=str(args.log),
        generated_at=datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out} ({len(tail)} entries embedded, {s.total} total scanned)")
    return 0


def _cmd_sample(args: argparse.Namespace) -> int:
    entries = gen_sample_entries(args.count, args.diverge_rate, args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {args.count} sample entries → {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shadow_report",
        description="Summarize / render DSL shadow-run divergence log.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("summary", help="text summary to stdout")
    ps.add_argument("--log", required=True, help="path to shadow_log.jsonl")
    ps.add_argument("--limit", type=int, default=20, help="recent divergences to list")
    ps.set_defaults(func=_cmd_summary)

    ph = sub.add_parser("html", help="write self-contained HTML report")
    ph.add_argument("--log", required=True)
    ph.add_argument("--out", required=True, help="output .html path")
    ph.add_argument("--limit", type=int, default=2000, help="max entries embedded")
    ph.set_defaults(func=_cmd_html)

    psg = sub.add_parser("sample", help="generate synthetic jsonl for dev")
    psg.add_argument("--out", required=True)
    psg.add_argument("--count", type=int, default=200)
    psg.add_argument("--diverge-rate", type=float, default=0.08)
    psg.add_argument("--seed", type=int, default=42)
    psg.set_defaults(func=_cmd_sample)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
