# 側窓予測制御 設計書

> **Version**: 2.1 | **Date**: 2026-03-13
> **Status**: **実装済み** — rule_engine.py Rule 6c/6d + window_position.py + テスト23件PASS
> **対象**: rule_engine.py (Layer 2) の Rule 6d 拡張

## 背景

現状の Layer 2 温度制御は **バイナリ開閉**（target+2℃で全開、target-1℃で全閉）のみ。
ピタゴラススイッチ（段階制御）は Layer 3 (LLM) 依存だが、LLMは1時間に1回しか判断しない。

**目標**: 温度トレンド予測に基づく段階制御を Layer 2 に降ろし、LLMの判断対象をCO2・湿度に限定する。

## 実機の物理制約

### 巻き上げモーター方式

```
リミットスイッチ(全閉) ←── モーター通電 ──→ リミットスイッチ(全開)
                     ↑
              通電秒数で開度が決まる
```

- **open_channel に N秒通電** → N秒分だけ開方向に動く
- **close_channel に N秒通電** → N秒分だけ閉方向に動く
- **リミットスイッチ**: 全開/全閉でモーター自動停止（過負荷防止）
- **エンコーダなし**: 現在位置を直接知る手段がない
- **全開走行時間**: ユーザ設定（例: 60秒）。全閉→全開に要する時間

### 開度制御の原理

巻き上げ式のため、**重力の影響で開と閉の走行時間が異なる**:
- **開（巻き上げ）**: 重力に逆らう → `open_travel_sec`（例: 65秒）
- **閉（巻き下ろし）**: 重力アシスト → `close_travel_sec`（例: 50秒）

```
開方向: 開度50% = open_travel_sec × 0.5 だけ開方向に通電
閉方向: 開度50%→0% = close_travel_sec × 0.5 だけ閉方向に通電
```

**両方とも実機でストップウォッチ計測が必須。**

### ソフトウェア位置管理

エンコーダがないため、**ソフトウェアで推定位置を管理**する必要がある：

```python
estimated_position = {
    "north": 0.0,  # 0.0=全閉, 1.0=全開
    "south": 0.0,
    "last_calibrated_at": "2026-03-13T06:00:00+09:00"
}
```

**キャリブレーション**:
- 全閉リミットスイッチ到達時 → position = 0.0 にリセット
- 全開リミットスイッチ到達時 → position = 1.0 にリセット
- 毎朝の夜間全閉（Rule 6c）後に自動キャリブレーション（閉方向に全開走行時間+余裕を通電）

## 設計方針

### ピタゴラススイッチを開度 % で定義

```
<25℃:    全閉 (0%)
25-27℃:  南 30%, 北 0%
27-30℃:  南 50%, 北 50%
30-32℃:  南 80%, 北 80%
>32℃:    全開 (100%)
```

### 差分制御（現在位置 → 目標位置）

5分ごとの rule_engine 実行で:
1. ピタゴラスステージから **目標開度** を算出
2. 推定現在位置との **差分** を計算
3. 差分 × 全開走行時間 = **通電秒数** を決定
4. `duration_sec` 付きでリレーON → 自動OFF

```python
# 例: 現在 south=0.3 (30%), 目標 south=0.5 (50%)
delta = 0.5 - 0.3  # = +0.2 (開方向)
duration = delta * open_travel_sec  # = 0.2 × 65 = 13秒（巻き上げなので open_travel 使用）
→ open_channel ON, duration_sec=13

# 例: 現在 south=0.5 (50%), 目標 south=0.3 (30%)
delta = 0.5 - 0.3  # = 0.2 (閉方向)
duration = delta * close_travel_sec  # = 0.2 × 50 = 10秒（巻き下ろしなので close_travel 使用）
→ close_channel ON, duration_sec=10
```

### 先読み制御（Predictive Opening）

既存の `compute_temperature_trend()` を活用。温度上昇速度に応じて目標開度を嵩上げ。

```
trend > +3.0℃/h → 2段階先取り（急上昇）
trend > +1.5℃/h → 1段階先取り（通常上昇）
trend ≤ +1.5℃/h → 現在ステージのまま
```

### 早朝オフセット

既存 `early_morning_offset: -1.0` を活用。5-8時は全段階を-1℃シフト。

## 実装設計

### 新モジュール: `window_position.py`

推定位置の管理を専用モジュールに分離。

```python
"""window_position.py — 側窓推定位置管理

エンコーダなしの巻き上げモーターの開度をソフトウェアで追跡する。
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_JST = ZoneInfo("Asia/Tokyo")
DEFAULT_POSITION_PATH = "/var/lib/agriha/window_position.json"

def load_position(path: str = DEFAULT_POSITION_PATH) -> dict:
    """推定位置を読み込む。ファイルなし時は全閉(0.0)を返す。"""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "north": 0.0,
            "south": 0.0,
            "last_calibrated_at": None,
            "last_updated_at": None,
        }

def save_position(pos: dict, path: str = DEFAULT_POSITION_PATH) -> None:
    """推定位置を保存する。"""
    pos["last_updated_at"] = datetime.now(tz=_JST).isoformat()
    Path(path).write_text(json.dumps(pos, ensure_ascii=False, indent=2))

def update_position(
    pos: dict,
    group_name: str,
    direction: str,  # "open" | "close"
    duration_sec: float,
    open_travel_sec: float,
    close_travel_sec: float,
) -> dict:
    """通電実績から推定位置を更新する。

    開と閉で走行時間が異なる（重力の影響）:
    - 開（巻き上げ）: 重力に逆らう → open_travel_sec（長い）
    - 閉（巻き下ろし）: 重力アシスト → close_travel_sec（短い）
    """
    key = "north" if "北" in group_name else "south"
    if direction == "open":
        delta = duration_sec / open_travel_sec
        pos[key] = min(1.0, pos[key] + delta)
    else:
        delta = duration_sec / close_travel_sec
        pos[key] = max(0.0, pos[key] - delta)
    return pos

def calibrate_closed(pos: dict, group_name: str) -> dict:
    """全閉キャリブレーション（リミットスイッチ到達後に呼ぶ）"""
    key = "north" if "北" in group_name else "south"
    pos[key] = 0.0
    pos["last_calibrated_at"] = datetime.now(tz=_JST).isoformat()
    return pos

def calibrate_open(pos: dict, group_name: str) -> dict:
    """全開キャリブレーション"""
    key = "north" if "北" in group_name else "south"
    pos[key] = 1.0
    pos["last_calibrated_at"] = datetime.now(tz=_JST).isoformat()
    return pos

def compute_move(
    current_pos: float,
    target_pos: float,
    open_travel_sec: float,
    close_travel_sec: float,
    deadband: float = 0.05,
) -> tuple[str | None, float]:
    """現在位置→目標位置の移動を計算する。

    開と閉で走行時間が異なる（重力の影響）:
    - 開方向: open_travel_sec を使用（巻き上げ、重力に逆らうので遅い）
    - 閉方向: close_travel_sec を使用（巻き下ろし、重力アシストで速い）

    Returns:
        (direction, duration_sec): ("open", 12.0) or ("close", 8.0) or (None, 0)
    """
    delta = target_pos - current_pos
    if abs(delta) < deadband:
        return None, 0  # 不感帯内 → 動かさない
    if delta > 0:
        return "open", delta * open_travel_sec
    else:
        return "close", abs(delta) * close_travel_sec
```

### 新関数: `_compute_pitagorasu_stage()`

```python
def _compute_pitagorasu_stage(
    temp: float,
    trend: float | None,
    hour: int,
    early_offset: float = -1.0,
    stages: list[dict] | None = None,
    predictive_cfg: dict | None = None,
) -> dict:
    """ピタゴラススイッチの段階と目標開度を返す。

    Returns:
        {
            "stage": 0-4,
            "stage_name": str,
            "south_target": 0.0-1.0,  # 目標開度
            "north_target": 0.0-1.0,
            "reason": str,
        }
    """
    # デフォルトステージ定義
    if stages is None:
        stages = [
            {"max_temp": 25.0, "south": 0.0, "north": 0.0, "name": "closed"},
            {"max_temp": 27.0, "south": 0.3, "north": 0.0, "name": "south_micro"},
            {"max_temp": 30.0, "south": 0.5, "north": 0.5, "name": "both_medium"},
            {"max_temp": 32.0, "south": 0.8, "north": 0.8, "name": "both_wide"},
            {"max_temp": 999,  "south": 1.0, "north": 1.0, "name": "full_open"},
        ]

    # 早朝オフセット
    offset = early_offset if 5 <= hour < 8 else 0.0

    # 先読み補正
    predictive_bonus = 0.0
    if trend is not None and trend > 0:
        p = predictive_cfg or {}
        rapid = p.get("rapid_trend_threshold", 3.0)
        mild = p.get("mild_trend_threshold", 1.5)
        if trend > rapid:
            predictive_bonus = p.get("rapid_bonus", 2.0)
        elif trend > mild:
            predictive_bonus = p.get("mild_bonus", 1.0)

    effective_temp = temp + predictive_bonus + offset

    # 段階判定
    for i, s in enumerate(stages):
        if effective_temp < s["max_temp"]:
            return {
                "stage": i,
                "stage_name": s["name"],
                "south_target": s["south"],
                "north_target": s["north"],
                "reason": f"T_eff={effective_temp:.1f}℃ → {s['name']} "
                          f"(S:{s['south']:.0%} N:{s['north']:.0%})",
            }

    # フォールバック（全開）
    last = stages[-1]
    return {
        "stage": len(stages) - 1,
        "stage_name": last["name"],
        "south_target": last["south"],
        "north_target": last["north"],
        "reason": f"T_eff={effective_temp:.1f}℃ → {last['name']} (フォールバック)",
    }
```

### Rule 6d 変更箇所 (evaluate_rules 内)

```python
if not layer3_active:
    if indoor_temp is not None:
        trend = compute_temperature_trend(temp_history)
        hour = now.hour
        pitagorasu = _compute_pitagorasu_stage(
            indoor_temp, trend, hour,
            early_offset=temp_cfg.get("early_morning_offset", -1.0),
            stages=pitagorasu_cfg.get("stages"),
            predictive_cfg=pitagorasu_cfg.get("predictive"),
        )
        triggered_rules.append(f"pitagorasu_stage_{pitagorasu['stage']}")
        logger.info("Rule 6d: %s", pitagorasu["reason"])

        # 推定位置を読み込み
        win_pos = load_position()
        open_travel = pitagorasu_cfg.get("open_travel_sec", 65)
        close_travel = pitagorasu_cfg.get("close_travel_sec", 50)
        deadband = pitagorasu_cfg.get("deadband", 0.05)

        for g in groups:
            if _group_acted(g):
                continue
            is_south = "南" in g["name"]
            key = "south" if is_south else "north"
            current = win_pos.get(key, 0.0)
            target = pitagorasu[f"{key}_target"]

            direction, dur = compute_move(current, target, open_travel, close_travel, deadband)
            if direction is None:
                logger.info("Rule 6d: %s 不感帯内 (%.0f%%→%.0f%%) → 維持",
                            g["name"], current*100, target*100)
                continue

            if direction == "open":
                relay_actions.append((g["open_channel"], 1, int(dur)))
                relay_actions.append((g["close_channel"], 0, None))
            else:
                relay_actions.append((g["close_channel"], 1, int(dur)))
                relay_actions.append((g["open_channel"], 0, None))

            logger.info("Rule 6d: %s %s %.0f%%→%.0f%% (%s %d秒)",
                        g["name"], direction, current*100, target*100,
                        direction, int(dur))

            # 推定位置を更新
            update_position(win_pos, g["name"], direction, dur, open_travel, close_travel)

        save_position(win_pos)
```

### 夜間キャリブレーション

Rule 6c（夜間全閉）実行時に、全閉走行時間+10%の余裕を持って閉方向に通電し、
リミットスイッチ到達後にソフトウェア位置をリセット:

```python
# Rule 6c 変更箇所
if nighttime:
    triggered_rules.append("nighttime_close")
    close_travel = pitagorasu_cfg.get("close_travel_sec", 50)
    calibration_dur = int(close_travel * 1.1)  # +10%余裕（閉方向なのでclose_travel使用）
    for g in groups:
        if not _group_acted(g):
            relay_actions.append((g["close_channel"], 1, calibration_dur))
            relay_actions.append((g["open_channel"], 0, None))
            calibrate_closed(win_pos, g["name"])
    save_position(win_pos)
    logger.info("Rule 6c: 夜間全閉 + キャリブレーション (%d秒)", calibration_dur)
```

## rules.yaml 追加設定

```yaml
temperature:
  # 既存
  target_day: 26.0
  target_night: 17.0
  margin_open: 2.0
  margin_close: 1.0
  attention_temp: 30.0
  emergency_temp: 32.0
  early_morning_offset: -1.0

  # 新規: ピタゴラススイッチ段階制御
  pitagorasu:
    enabled: true
    open_travel_sec: 65        # 全閉→全開の走行時間（秒）★実測必須★ 重力に逆らうので遅い
    close_travel_sec: 50       # 全開→全閉の走行時間（秒）★実測必須★ 重力アシストで速い
    deadband: 0.05             # 5%以下の差分は動かさない（モーター保護）
    stages:
      - {max_temp: 25.0, south: 0.0, north: 0.0, name: "closed"}
      - {max_temp: 27.0, south: 0.3, north: 0.0, name: "south_micro"}
      - {max_temp: 30.0, south: 0.5, north: 0.5, name: "both_medium"}
      - {max_temp: 32.0, south: 0.8, north: 0.8, name: "both_wide"}
      - {max_temp: 999,  south: 1.0, north: 1.0, name: "full_open"}
    predictive:
      mild_trend_threshold: 1.5    # ℃/h — 1段階先読み
      rapid_trend_threshold: 3.0   # ℃/h — 2段階先読み
      mild_bonus: 1.0              # ℃ equivalent
      rapid_bonus: 2.0             # ℃ equivalent
```

## system_prompt.txt からの削減

ピタゴラススイッチがLayer 2に降りるので、system_prompt.txtから以下を**簡素化**:

- Rule 2 (ピタゴラススイッチ段階制御) → 「Layer 2で自動段階制御中。LLMは監視のみ。異常時のみ介入」
- Rule 7 (早朝先行換気) → 「Layer 2で自動実施」

**LLMに残す判断**:
- Rule 3: 湿度優先（朝6時台、湿度85%超 → 温度より換気優先）
- Rule 4: CO2パルス換気（300ppm以下 → 全窓全開5-10分）
- 天気予報との複合判断（曇り→換気不要の判断等）
- pid_override.json による目標温度変更（今後実装）

## 位置ずれの蓄積対策

ソフトウェア推定位置は通電秒数の積算なので、誤差が蓄積する。対策:

| 対策 | タイミング | 方法 |
|------|-----------|------|
| **夜間キャリブレーション** | 毎晩 Rule 6c | 全閉方向に travel_time×1.1 秒通電→position=0.0 |
| **全開時キャリブレーション** | stage 4 (≥32℃) | 全開方向に travel_time×1.1 秒通電→position=1.0 |
| **リミットスイッチ到達** | 全閉/全開通過 | duration > remaining_distance の場合、自動クランプ |
| **定期リセット** | 朝6時 | 夜間全閉後なので position=0.0 を再確認 |

## テスト計画

| テストケース | 入力 | 期待出力 |
|-------------|------|---------|
| 24℃安定, pos=0% | temp=24, trend=0, pos=0.0 | 動作なし（既に全閉） |
| 26℃上昇中, pos=0% | temp=26, trend=+2.0, pos=0.0 | south open 30秒 (0→50%, 先読み+1℃→27℃相当→stage2) |
| 28℃安定, pos=50% | temp=28, trend=0, pos=0.5 | 動作なし（目標50%=現在50%、不感帯内） |
| 28℃→31℃, pos=50% | temp=31, trend=+1.0, pos=0.5 | south/north open 18秒 (50%→80%) |
| 33℃, pos=80% | temp=33, pos=0.8 | south/north open 12秒+余裕 (80%→100%, キャリブレーション) |
| 下降26℃, pos=50% | temp=26, trend=-2.0, pos=0.5 | south close 12秒 (50%→30%), north close 30秒 (50%→0%) |
| 早朝6時25℃, pos=0% | temp=25, trend=+1.5, hour=6 | south open 18秒 (0→30%, offset-1→24℃+bonus1→25℃→stage1) |
| 夜間全閉 | nighttime=true, pos=0.7 | close 66秒 (travel_time×1.1), pos→0.0 |
| 不感帯 | target=0.5, current=0.48 | 動作なし (delta=0.02 < deadband=0.05) |

## 影響範囲

| ファイル | 変更内容 |
|---------|---------|
| `window_position.py` (新規) | 推定位置管理モジュール |
| `rule_engine.py` | `_compute_pitagorasu_stage()` 新規、Rule 6d/6c書き換え |
| `config/rules.yaml` | `pitagorasu` セクション追加 |
| `config/system_prompt.txt` | Rule 2, 7 を簡素化 |
| `tests/control/test_rule_engine.py` | 段階制御テスト9件以上追加 |
| `tests/control/test_window_position.py` (新規) | 位置管理ユニットテスト |

## リスク

| リスク | 対策 |
|--------|------|
| 段階制御が攻撃的すぎる | `pitagorasu.enabled: false` で即座に旧ロジック復帰 |
| 先読みが過剰開放 | `predictive.mild_bonus` / `rapid_bonus` を0にすれば先読み無効 |
| Layer 3との競合 | `layer3_active` チェックは既存のまま。Layer 3有効時はLayer 2段階制御もスキップ |
| 位置推定ずれ蓄積 | 夜間キャリブレーション+全開時キャリブレーション+不感帯 |
| travel_time設定ミス | 初回デプロイ時に実機で全閉→全開・全開→全閉の両方をストップウォッチ計測 |
| 開閉速度の非対称 | open_travel_sec / close_travel_sec を個別設定。重力による差を吸収 |
| モーター過負荷 | deadband=5%で頻繁な微調整を防止。最小通電≒deadband×close_travel=2.5秒 |
