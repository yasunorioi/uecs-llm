"""automation_interpreter.py — rule_engine automations DSL 用インタプリタ (prototype).

`rules.yaml` の `automations:` セクションを list[dict] として受け取り、センサ + 状態 +
config + primitives を与えて評価し、Result (target_opening_pct、close フラグ、
relay_pulses、副作用リスト、ヒステリシス state) を返す純ライブラリ。I/O 副作用なし。

設計書: docs/dsl_prototype.md
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# ══════════════════════════════════════════════
# 式評価サンドボックス (AST whitelist)
# ══════════════════════════════════════════════

_ALLOWED_AST_NODES = frozenset(
    {
        ast.Expression,
        ast.Load,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Compare,
        ast.Gt,
        ast.Lt,
        ast.GtE,
        ast.LtE,
        ast.Eq,
        ast.NotEq,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.Call,
        ast.Name,
        ast.Attribute,
        ast.Constant,
    }
)


class ExpressionError(ValueError):
    """DSL 式のコンパイル or 評価失敗を表す。"""


def compile_expr(expr: str) -> Any:
    """式文字列を code object にコンパイル。whitelist 外のノードは拒否。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error in {expr!r}: {e}") from e
    for sub in ast.walk(tree):
        if type(sub) not in _ALLOWED_AST_NODES:
            raise ExpressionError(
                f"disallowed AST node {type(sub).__name__} in {expr!r}"
            )
    return compile(tree, "<automation_expr>", "eval")


def eval_expr(expr: str, ctx: dict[str, Any]) -> Any:
    """式を評価。ctx が globals 相当、__builtins__ は空。"""
    code = compile_expr(expr)
    return eval(code, {"__builtins__": {}}, ctx)


# ══════════════════════════════════════════════
# DotDict: dict を attribute アクセスで公開
# ══════════════════════════════════════════════

class DotDict:
    """`config.rain.threshold_mm_h` のような attribute アクセスを可能にする wrapper。

    先頭 underscore のキーはブロックする(sandbox 補強)。
    """

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "_d", d if d is not None else {})

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            v = self._d[key]
        except KeyError as e:
            raise AttributeError(f"missing key: {key}") from e
        return DotDict(v) if isinstance(v, dict) else v

    def __repr__(self) -> str:
        return f"DotDict({self._d!r})"


# ══════════════════════════════════════════════
# スキーマ
# ══════════════════════════════════════════════

@dataclass
class Automation:
    id: str
    priority: int
    trigger: str
    action: dict[str, Any]
    release: str | None = None
    hard_stop: bool = False
    on_fire: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Automation:
        return cls(
            id=d["id"],
            priority=int(d["priority"]),
            trigger=d["trigger"],
            action=d["action"],
            release=d.get("release"),
            hard_stop=bool(d.get("hard_stop", False)),
            on_fire=d.get("on_fire"),
        )


# action.kind → 出力グループ (hard_stop はグループ内でのみ効く)
_ACTION_GROUP = {
    "close_all_windows": "windows",
    "close_windows_by_wind_direction": "windows",
    "set_min_opening": "windows",
    "pulse_relay": "pulse",
}


@dataclass
class Result:
    triggered: list[str] = field(default_factory=list)
    target_opening_pct: int = 0
    close_all_windows: bool = False
    close_by_wind_direction: bool = False
    relay_pulses: list[dict[str, Any]] = field(default_factory=list)
    hard_stopped_groups: set[str] = field(default_factory=set)
    state_resets: list[str] = field(default_factory=list)
    state_increments: list[str] = field(default_factory=list)
    state_timestamps: list[str] = field(default_factory=list)
    active_after: set[str] = field(default_factory=set)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)


# ══════════════════════════════════════════════
# インタプリタ本体
# ══════════════════════════════════════════════

class Interpreter:
    def __init__(self, automations: Iterable[Automation]) -> None:
        # priority 降順で評価
        self.automations = sorted(automations, key=lambda a: -a.priority)

    def evaluate(
        self,
        sensors: dict[str, Any],
        state: dict[str, Any],
        config: dict[str, Any],
        crop_config: dict[str, Any] | None = None,
        primitives: dict[str, Callable[..., Any]] | None = None,
        active_before: set[str] | None = None,
        strict: bool = True,
    ) -> Result:
        """
        strict=True: 式評価失敗 (None 比較 / 未定義センサ等) は ExpressionError で raise
        strict=False: 失敗した automation は result.skipped に記録して continue
                      (現場運用でセンサ欠損時に interpreter が rule_engine を巻き込まないため)
        """
        ctx = self._build_ctx(sensors, state, config, crop_config, primitives)
        active = set(active_before or ())
        result = Result()

        for auto in self.automations:
            group = _ACTION_GROUP.get(auto.action["kind"], "unknown")
            if group in result.hard_stopped_groups:
                continue
            try:
                fired = self._is_firing(auto, ctx, active)
            except ExpressionError as e:
                if strict:
                    raise
                result.skipped.append((auto.id, f"trigger: {e}"))
                continue
            if not fired:
                continue
            try:
                self._apply_action(auto, ctx, result)
            except ExpressionError as e:
                if strict:
                    raise
                result.skipped.append((auto.id, f"action: {e}"))
                continue
            result.triggered.append(auto.id)
            if auto.hard_stop:
                result.hard_stopped_groups.add(group)
            if auto.on_fire:
                self._collect_side_effects(auto.on_fire, result)

        result.active_after = active
        return result

    # ── ctx 構築 ───────────────────────────────

    def _build_ctx(
        self,
        sensors: dict[str, Any],
        state: dict[str, Any],
        config: dict[str, Any],
        crop_config: dict[str, Any] | None,
        primitives: dict[str, Callable[..., Any]] | None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = dict(sensors)
        ctx["config"] = DotDict(config)
        ctx["crop"] = DotDict(crop_config or {})
        ctx["state"] = DotDict(state)
        for name, fn in (primitives or {}).items():
            ctx[name] = fn
        return ctx

    # ── 発火判定 (ヒステリシス含む) ─────────────

    def _is_firing(
        self, auto: Automation, ctx: dict[str, Any], active: set[str]
    ) -> bool:
        try:
            if auto.id in active:
                # 現在 active — release チェック
                if auto.release is None:
                    # release 定義なし → 毎回 trigger 再評価
                    if bool(eval_expr(auto.trigger, ctx)):
                        return True
                    active.discard(auto.id)
                    return False
                if bool(eval_expr(auto.release, ctx)):
                    active.discard(auto.id)
                    return False
                return True
            # まだ active でない
            if bool(eval_expr(auto.trigger, ctx)):
                if auto.release is not None:
                    active.add(auto.id)
                return True
            return False
        except Exception as e:
            if isinstance(e, ExpressionError):
                raise
            raise ExpressionError(f"[{auto.id}] evaluation failed: {e}") from e

    # ── action 適用 ─────────────────────────────

    def _apply_action(
        self, auto: Automation, ctx: dict[str, Any], result: Result
    ) -> None:
        action = auto.action
        kind = action["kind"]
        if kind == "close_all_windows":
            result.close_all_windows = True
            result.target_opening_pct = 0
        elif kind == "close_windows_by_wind_direction":
            result.close_by_wind_direction = True
            result.target_opening_pct = 0
        elif kind == "set_min_opening":
            value = self._resolve_scalar(action, ctx)
            result.target_opening_pct = max(
                result.target_opening_pct, int(value)
            )
        elif kind == "pulse_relay":
            ch_expr = action.get("ch_expr")
            duration_expr = action.get("duration_expr")
            if ch_expr is None or duration_expr is None:
                raise ExpressionError(
                    f"[{auto.id}] pulse_relay requires ch_expr and duration_expr"
                )
            ch = int(eval_expr(ch_expr, ctx))
            duration = int(eval_expr(duration_expr, ctx))
            result.relay_pulses.append(
                {"ch": ch, "duration_sec": duration, "from_id": auto.id}
            )
        else:
            raise ExpressionError(f"[{auto.id}] unknown action kind: {kind}")

    def _resolve_scalar(
        self, action: dict[str, Any], ctx: dict[str, Any]
    ) -> Any:
        if "value" in action:
            return action["value"]
        if "value_expr" in action:
            return eval_expr(action["value_expr"], ctx)
        raise ExpressionError(
            f"set_min_opening requires value or value_expr (got {action!r})"
        )

    # ── on_fire 副作用収集 ──────────────────────

    def _collect_side_effects(
        self, on_fire: dict[str, Any], result: Result
    ) -> None:
        for key in on_fire.get("reset", []):
            result.state_resets.append(key)
        for key in on_fire.get("increment", []):
            result.state_increments.append(key)
        for key in on_fire.get("set_timestamp", []):
            result.state_timestamps.append(key)


# ══════════════════════════════════════════════
# 便利関数
# ══════════════════════════════════════════════

def load_automations(
    schema_list: Iterable[dict[str, Any]],
) -> list[Automation]:
    """dict のリスト (yaml 由来など) から Automation リストへ変換。"""
    return [Automation.from_dict(d) for d in schema_list]
