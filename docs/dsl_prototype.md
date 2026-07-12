# rules.yaml automation DSL — 紙上プロトタイプ

## 目的

現状 `src/agriha/control/rule_engine.py` の `evaluate_rules()` にハードコードされている
priority chain P1〜P7 を、`rules.yaml` の `automations:` セクションにデータとして
表現できるか検証する **紙上実験(実装前 feasibility check)**。

隠れた semantics のあぶり出し・schema 設計の初稿・DSL 化コスト見積の材料とする。

**対象:** `evaluate_rules()` L486-754 (約 270 行)
**対象外:** infra 層(`fetch_sensors`, `post_relay`, `get_sun_times` 等)は Python helper のまま維持 /
`emergency_guard.sh` / Layer 1 lockout / CommandGate ロックアウト / TiDE モデル呼び出し

---

## 1. Schema

```yaml
automations:
  - id: string             # 一意識別子 (snapshot test の key)
    priority: int          # 大きいほど先に評価
    trigger: expr          # boolean 式、true で発火
    release: expr?         # (optional) ヒステリシス用、trigger 発火後 release が true になるまで active
    action: action_dict    # 発火時の動作 (§3 の action_kind から選択)
    hard_stop: bool?       # (optional, default false) true で下位 priority を全 skip
    on_fire:               # (optional) 発火時の state 変更
      reset: [state_key]           # 指定 state を 0 or "" にリセット
      increment: [state_key]       # +1
      set_timestamp: [state_key]   # 現在時刻を書き込む
```

**Default state:** `target_opening_pct = 0` (全閉) を初期値とし、
どの automation も発火しなければ全閉となる。

**Compose 規則:** `set_min_opening` action は `target_opening_pct = max(existing, value)` の
floor set (下限セット)。上位 priority が set した値を下位が下げることは無い。

---

## 2. 式言語 (expression grammar)

Python `ast` module に **whitelist** を掛けて評価。任意コード実行は許容しない。

| 種類 | 例 |
|---|---|
| センサ参照 | `rainfall`, `wind_speed`, `wind_direction`, `indoor_temp`, `outdoor_temp`, `indoor_humidity`, `indoor_co2`, `insolar` |
| 設定参照 | `config.rain.threshold_mm_h`, `crop.solar_threshold` (dotted path) |
| 名前付き関数 | `rise_rate()`, `solar_accumulated_mj()`, `schedule_period()`, `is_night()`, `sunset_offset_min()`, `tide_forecast_humidity_1h()`, `window_step_lookup(outdoor_temp)`, `schedule_target_temp()` |
| 比較 | `>`, `<`, `>=`, `<=`, `==`, `!=` |
| 論理 | `and`, `or`, `not` |
| リテラル | 数値、文字列 |

**許容しない:** 代入、import、attribute set、for/while/if 文、lambda、任意関数定義。

---

## 3. Action kinds (fixed vocabulary)

| kind | 意味 | パラメータ |
|---|---|---|
| `close_all_windows` | 全窓グループを閉じる | — |
| `close_windows_by_wind_direction` | wind_direction にマッチする窓グループを閉じる。該当なしなら全閉 (fallback は primitive 内部) | — |
| `set_min_opening` | `target_opening_pct` の floor を上げる | `value: int` **or** `value_expr: string` |
| `pulse_relay` | 指定 ch を指定秒 ON | `ch_ref: <path>`, `duration_ref: <path>` |

**追加禁止:** action を増やす前に「本当に primitive を増やすべきか、既存で表現できないか」を必ず問う。
action 濫立が second-system syndrome の入口。

---

## 4. Named primitives (Python 側で提供する registry)

| primitive | 現行実装 | 戻り値 |
|---|---|---|
| `rise_rate()` | `get_rise_rate_per_hour(temp_history)` | float °C/h or None |
| `solar_accumulated_mj()` | `solar_acc["accumulated_mj"]` | float |
| `schedule_period()` | `get_schedule_target()[1]` | str ("pre_dawn"/"morning"/...) |
| `schedule_target_temp()` | `get_schedule_target()[0]` | float °C |
| `is_night()` | `is_nighttime(cfg, now)` | bool |
| `sunset_offset_min()` | 現在時刻から日没までの分 (負なら日没後) | int |
| `tide_forecast_humidity_1h()` | `tide_forecast["predictions"]["InAirHumid"][0]` | float % or None |
| `window_step_lookup(outdoor_temp)` | `get_target_opening(outdoor_temp, step_table)` | int 0-100 |

Interpreter 起動時に registry に登録、trigger/action 式評価時に dispatch。

---

## 5. 全 P1〜P7 の automation 表現

### P1: rain_close_all

```yaml
- id: rain_close_all
  priority: 100
  trigger: rainfall > config.rain.threshold_mm_h
  action: {kind: close_all_windows}
  hard_stop: true
```

### P2: strong_wind

```yaml
- id: strong_wind
  priority: 95
  trigger: wind_speed > config.wind.strong_wind_threshold_ms
  action: {kind: close_windows_by_wind_direction}
  hard_stop: true
```

註: 「風向不明なら全閉」は `close_windows_by_wind_direction` primitive の内部実装で吸収。

### P3: rapid_rise_vent

```yaml
- id: rapid_rise_vent
  priority: 80
  trigger: rise_rate() > config.morning_ventilation.max_rise_rate_per_hour
  action: {kind: set_min_opening, value: 60}
```

### P3.5: tide_preemptive_vent

```yaml
- id: tide_preemptive_vent
  priority: 75
  trigger: tide_forecast_humidity_1h() > config.tide.humidity_prevent_threshold
  action: {kind: set_min_opening, value: 30}
```

**副次利益:** 現状ハードコードの `TIDE_HUMIDITY_PREVENT_THRESHOLD = 80`
(モジュール定数、rule_engine.py) を rules.yaml の `tide.humidity_prevent_threshold` に
昇格させる。code smell 一件消化。

### P4: humidity_vent

```yaml
- id: humidity_vent
  priority: 60
  trigger: indoor_humidity >= config.humidity.ventilation_start   # 85
  release: indoor_humidity <= config.humidity.ventilation_stop    # 75
  action: {kind: set_min_opening, value: 30}
```

**設計改善に該当:** 現状 `evaluate_rules()` は毎回 stateless に評価しており、
75〜85% 間で opening は下がる。真のヒステリシスは **未実装**(コメントとログ上のみ)。
DSL の `release` フィールドを interpreter が「発火後 release まで active 状態を持続」
と実装すれば、**現行のチャタリング可能性が副次的に修正される** (行動変化あり、要合意)。

現行完全互換にしたければ `release` を落として下記でも動く:
```yaml
- id: humidity_vent  # 現行互換版 (ヒステリシスなし)
  priority: 60
  trigger: indoor_humidity >= config.humidity.ventilation_start
  action: {kind: set_min_opening, value: 30}
```

### P5a: co2_critical_vent

```yaml
- id: co2_critical_vent
  priority: 55
  trigger: indoor_co2 <= config.co2.critical_low     # 300
  action: {kind: set_min_opening, value: 60}
```

### P5b: co2_vent

```yaml
- id: co2_vent
  priority: 50
  trigger: indoor_co2 <= config.co2.ventilation_trigger  # 350
  action: {kind: set_min_opening, value: 30}
```

### P6: daytime_temp_too_warm_vent

```yaml
- id: daytime_temp_too_warm_vent
  priority: 40
  trigger: not is_night() and indoor_temp > schedule_target_temp()
  action:
    kind: set_min_opening
    value_expr: window_step_lookup(outdoor_temp)
```

**現行 P6 との対応:**
- **夜間ブランチ**: automation にしない。夜間は「基本閉、ただし CO2/湿度で開度維持」の semantics だが、これは **"何も指定しない = default 0"** で自然に成立(下位の CO2/湿度が floor を上げるだけ)
- **temp_too_cold ブランチ (indoor_temp < target-2)**: automation にしない。active な action がなく、default 0 と compose semantics で自然消滅
- **temp_too_warm ブランチのみ** を automation として表現

これで **P6 の 3 分岐が 1 個の active automation に還元** される。

### P7: solar_irrigation

```yaml
- id: solar_irrigation
  priority: 30
  trigger: solar_accumulated_mj() >= crop.solar_threshold
  action:
    kind: pulse_relay
    ch_ref: config.irrigation.channel
    duration_ref: crop.duration_sec
  on_fire:
    reset: [solar_acc.accumulated_mj]
    increment: [solar_acc.irrigations_today]
    set_timestamp: [solar_acc.last_irrigation_at]
```

---

## 6. 検証結果 (residuals / gaps)

**紙上プロトタイプで P1〜P7 全てが表現できた。**
以下の schema 要素が必要だったことを記録:

1. **`hard_stop`** — P1/P2 の window_skip セマンティクス表現
2. **`release`** — P4 のヒステリシス表現 (単一 trigger では表現不能、副次的にチャタリング修正)
3. **`value_expr`** action パラメータ — P6 の線形補間結果を注入
4. **`on_fire`** ブロック — P7 の solar_acc 状態副作用
5. **Default `target_opening_pct = 0` の暗黙前提** — P6 夜間/寒冷ブランチが自然消滅する
6. **Named primitives registry** — 8 個の関数を Python 側で明示登録

これら 5 要素 + 8 primitive + 4 action kind + 8 sensor + 2 dotted config で
**P1〜P7 の 100% カバレッジ**。**予想外の "隠れた semantics" は無かった**。

### 数値まとめ

| メトリクス | 数 |
|---|---|
| automation 数 (P3.5 と P5b 分割で) | 8 |
| schema 追加要素 | 5 |
| named primitives | 8 |
| action kinds | 4 |
| sensor references | 8 |

---

## 7. Snapshot テストの形

DSL 化最大の副次利益 = テストがデータになる。例:

```yaml
- name: rain_takes_precedence_over_humidity
  given:
    sensors: {rainfall: 1.0, wind_speed: 3.0, indoor_humidity: 90, indoor_co2: 400, indoor_temp: 26, outdoor_temp: 22, insolar: 100}
    state: {solar_acc.accumulated_mj: 0.5, current_window_pct: 30}
  expect:
    fired: [rain_close_all]
    hard_stopped_at: rain_close_all
    target_opening_pct: 0

- name: humidity_and_co2_stack_on_min_opening
  given:
    sensors: {rainfall: 0.0, wind_speed: 2.0, indoor_humidity: 88, indoor_co2: 280, indoor_temp: 24, outdoor_temp: 20, insolar: 500}
    state: {solar_acc.accumulated_mj: 0.5, current_window_pct: 20}
  expect:
    fired: [humidity_vent, co2_critical_vent, daytime_temp_too_warm_vent]
    target_opening_pct: 60   # co2_critical の 60 が max

- name: solar_irrigation_fires_and_resets
  given:
    sensors: {insolar: 800, ...}
    state: {solar_acc.accumulated_mj: 1.55, crop.solar_threshold: 1.50, crop.duration_sec: 120}
  expect:
    fired: [solar_irrigation]
    relay_actions_include: [{ch: 4, val: 1, duration: 120}]
    state_after: {solar_acc.accumulated_mj: 0.0, solar_acc.irrigations_today: +1}
```

これが現状 `evaluate_rules()` の 270 行モノリスに対しては書きにくい部分。

---

## 8. 何が DSL 化により変わるか

### 利益

- 新規ルール追加は YAML 追記のみ (Python 改造不要)
- `rule_compiler.py` の LLM 生成対象が **parameter values + automations** の両方に拡張、
  真の意味で「ロジックも LLM 翻訳できる」= design intent の完成
- テンプレ UI (前会話の穴埋め案) が automation schema に **direct mapping**
- **オフライン動作可能** な interpreter (runtime LLM 不要)、拠点 autonomy と整合
- Priority 順序と compose 規則が可視データ、`check_contradictions` でロジック検査可能

### コスト

- interpreter 実装 (~数百行 Python) — `evaluate_rules()` 270 行の置き換え
- Named primitive registry の設計 + 登録
- `check_contradictions` にロジック検査追加 (sensor 参照存在、priority 一意性、
  trigger 型、hard_stop 使用制限、on_fire の state_key 存在)
- 既存 `evaluate_rules` retire + migration test
- 式言語 whitelist parser (Python ast subset) 実装

**規模感:** 元コード 270 行に対し、interpreter + primitive registry + parser で 400〜500 行程度、
差し引き net 増は 100〜200 行。ただしテスト可能性・LLM 生成可能性・UI 化可能性の副次利益が桁違い。

---

## 9. 提案する次の一歩

1. **この proposal を眺めて semantics に違和感がないか確認**(特に P4 ヒステリシスの
   行動変化と P6 の 3 分岐 → 1 automation 還元)
2. OK なら別ブランチで **interpreter のスケルトン試作** (100 行程度、
   automation 1 個を評価して action を出せるレベル)
3. 全 P1〜P7 を automation にした **shadow-run モード** で既存 `evaluate_rules` と
   並行実行、action 一致を数日検証
4. 完全一致が続いたら `evaluate_rules` retire

**設計原則の再確認 (second-system syndrome 回避):**
DSL の表現力は **「テンプレ UI で表現できる範囲」に self-restrict** する。
複雑ロジック (SoC 推定、ML forecast 統合等) は Python 側の named primitive として
実装し、DSL は結果値を参照するだけに留める。**「DSL に何でも詰めない」が唯一の防波堤**。

---

# 拡張提案 (2026-07-13)

§1〜§9 は 2026-07-10 時点の単一 automation 型 (P1〜P7 相当) の紙上プロト。
実装 (`feature/dsl-interpreter`, commit ceae908) + shadow-run が数日稼働した後、
以下の 3 つの gap が観測 / 議論された:

- shadow log の `humidity_vent` が RH=100 で fire し続ける (センサ張り付きを normal tier が拾う経路がない)
- 「飽差 高くて 気温 低い時、温度下降リスク承知で 60 秒だけ換気」的な duration-limited override が表現不能
- boolean trigger は「今この瞬間 true」しか扱わないが、agri 判断の本質は duration-weighted urgency ("そろそろヤバい / まだ我慢") の連続量

§10〜§14 はこれらへの拡張提案。

## 10. 3 tier 分類 (normal / safety / fault)

automation に `tier` field を追加。既存 automation は `tier: normal` (default) として無変更で動く。

```yaml
automations:
  - id: <name>
    tier: normal | safety | fault   # 省略時 normal
    ...
```

### tier ごとの semantics

| tier | compose | 失敗時 | 想定 cadence | 現 P に対応 |
|---|---|---|---|---|
| normal | `max` floor set | skip OK | 10 min tick | P6, P7 |
| safety | `hard_stop` or 強制 override | 保守側倒し | 秒〜fast loop | P1, P2, P3, P3.5, P4 |
| fault | 他 tier の gate + 縮退 | 保守側倒し | 秒 | (未実装) |

### uecs-llm 既存 3 層との対応

| 既存階層 | 実体 | 新 tier | 対応 |
|---|---|---|---|
| Layer 1 (爆発) | `emergency_guard.sh` cron 毎分 | safety | interrupt / hard_stop / 高 cadence |
| Layer 2 (ガムテ) | `rule_engine.py` cron 10 分 | normal | priority chain |
| Layer 2 の `is_layer1_locked_out()` gate | file-based lockout | fault の**実装 pattern** | Layer 1 が書く → Layer 2 が読む |
| Layer 3 | `rule_compiler.py` (LLM) | DSL 外 | 翻訳者 |

Layer 2 が Layer 1 の `lockout_state.json` を読んで自分を skip する pattern は
「上位 tier からの下位 tier gate」の実装例 = fault tier のやりたい構造と 1:1。
fault tier evaluator は `/var/lib/agriha/gates.json` を書く形にすれば
(Layer 1 の lockout も同 file に統合、§13 参照)、将来 fast loop daemon として
別 process に切り出しても interpreter 変更不要。

---

## 11. fault tier 詳細

### 設計動機

shadow log 観測 (2026-07-12): `humidity_vent` が RH=100 で毎 tick 発火。
センサ張り付き / drift / dead が normal tier を汚染する経路を明示的に遮断する。

### 二段パス評価

```
Tick 開始
  ↓
Pass A (fault): health を確定
  ├─ health: block の declarative 健全性ルール評価 (物理 + 仮想 source 統合)
  └─ tier: fault の automation で cross-sensor 判定、mark_unhealthy action
  ↓ (/var/lib/agriha/gates.json 書き出し = Layer 1 lockout も同 file)
Pass B (safety + normal): priority 降順評価
  └─ 各 automation の requires_healthy を health dict に照合、unhealthy なら skip
```

### schema 追加

物理センサと仮想 source (CommandGate 等の非センサ健全性) は `health:` block に統合宣言:

```yaml
health:
  # 物理センサ (stuck_over/stale_after/substitute が意味を持つ)
  indoor_humidity:
    healthy: indoor_humidity is not None and 0 < indoor_humidity < 100
    stuck_over: "30m"           # rate ≈ 0 が this 続けば unhealthy
    stale_after: "5m"           # 最終更新から this 経過で unhealthy
    recovery_after: "5m"        # 一度 unhealthy になったら最低 this 経過するまで維持
    substitute: null            # or 式 (unhealthy 時に代替値として使用)

  indoor_temp:
    healthy: indoor_temp is not None and -20 < indoor_temp < 60
    stuck_over: "30m"
    substitute: outdoor_temp    # emergency_guard.sh の SENSOR_FALLBACK と同 pattern

  # 仮想 source (healthy predicate のみ、時系列 field 無関係)
  command_gate:
    healthy: not command_gate_locked_out()

automations:
  # tier: fault は cross-sensor 判定用エスケープハッチ
  - id: indoor_temp_vs_outdoor_sanity
    tier: fault
    trigger: |
      indoor_temp is not None and outdoor_temp is not None
      and indoor_temp < outdoor_temp - 10
    action: {kind: mark_unhealthy, source: indoor_temp, reason: "indoor < outdoor - 10 は常識外"}

  # normal/safety tier は requires_healthy で gate 宣言 (物理/仮想 source 混在可)
  - id: humidity_vent
    tier: normal
    priority: 60
    requires_healthy: [indoor_humidity, command_gate]
    trigger: indoor_humidity >= config.humidity.ventilation_start
    release: indoor_humidity <= config.humidity.ventilation_stop
    action: {kind: set_min_opening, value: 30}
```

### field 対応表

| field | 意味 | 実装方針 |
|---|---|---|
| `healthy: expr` | 生値の健全性判定 (毎 tick 評価) | expression eval |
| `stuck_over: "N m"` | 値変化が eps 未満で N 続けば unhealthy | `is_stuck(name, N)` primitive |
| `stale_after: "N m"` | 最終更新から N 経過で unhealthy | `is_stale(name, N)` primitive |
| `recovery_after: "N m"` | 一度 unhealthy 後 最低 N 経過するまで復帰させない | state に `last_unhealthy_at` |
| `substitute: expr` | unhealthy 時、ctx で `<sensor>` は expr 評価結果に差し替え | ctx patching |
| `requires_healthy: [name, ...]` | 指定 source (物理/仮想 混在可) のどれか unhealthy なら automation skip | `ctx.health` dict 参照 |
| `mark_unhealthy` action | tier: fault automation が cross-sensor 判定で unhealthy 宣言 | health dict 書き込み |

### recovery_after: minimum-dwell 方式 (cooldown と共通機構)

`emergency_guard.sh` の `LOCKOUT_DURATION_SEC=300` と同 pattern:

- 一度 unhealthy 認定 → state に `last_unhealthy_at` を書く
- 次 tick 以降、healthy predicate が true でも `now - last_unhealthy_at < recovery_after` なら unhealthy 維持
- 経過後 healthy predicate true の tick で healthy 復帰

sustained-healthy 方式 (healthy 判定が N 継続で復帰) より単純で、既存 pattern と一致。
実装は §12 の automation `cooldown` と **共通の minimum-dwell 機構** (per-id timing state
`last_event_at`) を使い回す。field 名は用途 (health / cooldown) で分けるが、
executor 側 code path は 1 本。

### substitute 伝播: transparent 方式

- sensor が unhealthy + substitute 定義済 → automation の ctx で `<sensor>` は substitute 評価結果に差し替え
- automation が `requires_healthy: [X]` を宣言してたら、substitute があっても **skip** (author の「生センサ必須」明示)
- `requires_healthy` 宣言なし automation は substitute された値で普通に評価

= `requires_healthy` は「生センサ厳格要求」、substitute は「暗黙 fallback」の 2 段構造。

### fault tier がやらないこと

- **safe_action** (fault 発動時に強制 action) — safety tier の役割、責務分離。fault tier は「gate + alert」のみ
- **alert 発火経路** — Result に `alerts: [(source, reason), ...]` を載せるだけ、通知は rule_engine 側の LINE handler が既存経路で拾う (emergency_guard.sh の LINE 送信と同構造)
- **段階的 health grading** (`ok`/`drifting`/`stuck` の 3-4 段) — 二値 `ok`/`unhealthy` で開始、必要になったら段階足す (§14 D4)

---

## 12. pulse action + cooldown (時間限定 override)

### 設計動機

現行 `set_min_opening` は「今 tick 以降ずっと floor」で、時間限定 override が表現不能。
実運用例: 「飽差高くて気温低い時、温度下降リスク承知で 60 秒だけ窓開放」
(agriha-controller の `timer_repeat` に相当、ただし one-shot 版)。

### action 統合 (旧 pulse_relay と統合)

既存の `pulse_relay` action と新規の pulse-windows use case を **単一の `pulse` kind**
に統合、target field で切り替える (action vocabulary を 1 個削減):

| kind | target | パラメータ | 意味 |
|---|---|---|---|
| `pulse` | `windows` | `value: int`, `duration: "60s"`, `override_lower: bool?` | opening を N% で T 秒維持後、自動解除 |
| `pulse` | `relay` | `ch_ref: <path>`, `duration_ref: <path>` | 指定 ch を指定秒 ON (旧 pulse_relay と等価) |

§3 の action 表の `pulse_relay` は §10 以降で `pulse` (target: relay) にマージ。

### 新 field

| field | 意味 |
|---|---|
| `cooldown: "N m"` | 一度 fire したら N 経過するまで再 fire 不可 (chatter 防止)。§11 `recovery_after` と共通 minimum-dwell 機構 |
| `intent: string` | 実行に影響しない metadata。LLM 翻訳者 (rule_compiler) が「農家の怒り」を automation 生成する時に意図を残す先、UI 表示にも使う |

### 例 (boolean 版 VPD relief)

```yaml
- id: vpd_relief_pulse
  tier: normal
  priority: 45                       # 温度スケジュール(40) より上、湿度換気(60) より下
  requires_healthy: [indoor_hd, indoor_temp]
  trigger: |
    indoor_hd > config.vpd.pulse_high_threshold
    and indoor_temp < config.vpd.low_temp_ceiling
    and indoor_temp > 17             # safety(<16 で強制閉) との buffer
  action:
    kind: pulse
    target: windows
    value: 30
    duration: "60s"
    override_lower: true             # 下位 automation の floor を無視
  cooldown: "10m"
  intent: "低温 VPD 相殺 (温度低下リスクを承知)"
```

### override_lower semantic

- `override_lower: true` の pulse が fire → その action group (`windows`) の下位 automation の compose 結果を全捨て、pulse value をそのまま target とする
- 動的な hard_stop に近いが、pulse 期間内のみ、cooldown 経過後は自然に外れる
- 「温度基準を破壊して開放」の DSL 表現

pulse の秒スケール管理は interpreter の責務外 → §13 の executor に移譲。

---

## 12.5 score field 一般化 (duration-weighted trigger)

### 設計動機

boolean trigger は「今この瞬間 true」しか表現しない。
agri 判断の本質は duration-weighted urgency の連続量。
特に VPD, CO2 欠乏, 湿度カビリスク等の **soft target** で顕著。
score 型により「飽差 > 3 が 30 分続いたら換気必要」を数式で書ける。

### schema 追加

```yaml
- id: <name>
  score:
    accumulate: <expr>          # 単位: point/min (interpreter が dt 掛け算)
    threshold: <int>            # score >= threshold で発火
    decay: "30m"                # (optional) 発火なし tick で this で半減
    reset_on_fire: true         # (optional, default true) 発火時 score=0
  action: {...}
```

`trigger:` と `score:` は排他 (どちらか片方)。`trigger:` があれば boolean 評価、
`score:` があれば integrator 評価。

### 動作

- interpreter は per-automation state `<id>.score` を持つ
- 毎 tick: `score += accumulate 式評価結果 * dt_min` → decay (`score *= 2 ** (-dt/decay)`) → 範囲 clip
- score >= threshold なら trigger 相当で fire
- fire 後 `reset_on_fire: true` なら score=0
- score integrator は執行側 (§13 executor) が回す、interpreter は score field 宣言と threshold 判定のみ

### 例 (単純: 計測時間スコア)

```yaml
- id: vpd_relief_score
  tier: normal
  priority: 45
  requires_healthy: [indoor_hd, indoor_temp]
  score:
    accumulate: 1 if indoor_hd > 3 else 0     # 飽差>3 の分数を積算
    threshold: 30                              # 30 分溜まったら発火
    decay: "1h"                                # 発火なし 1h で半減
  action: {kind: pulse, target: windows, value: 30, duration: "60s", override_lower: true}
  cooldown: "10m"

- id: co2_deficit_score
  tier: normal
  priority: 50
  requires_healthy: [indoor_co2]
  score:
    accumulate: 1 if indoor_co2 < 350 else 0
    threshold: 60                              # CO2 は 1h 我慢
    decay: "2h"
  action:
    kind: pulse
    target: relay
    ch_ref: config.co2.channel
    duration_ref: config.co2.pulse_duration_sec
  cooldown: "20m"
```

### 例 (重み付け: severity × time)

```yaml
score:
  accumulate: max(0, indoor_hd - 3)   # 飽差=5 なら 2 point/min (2倍速)
  threshold: 30
```

同じ schema で書き分け可能。単純版が default 例、重み付けは必要時。

### fault tier `stuck_over` の再定義

`health.<name>.stuck_over: "30m"` は score の syntactic sugar:

```yaml
# 等価な score 表現 (共通 integrator で走る)
score:
  accumulate: 1 if abs(rate(<name>, "1m")) < eps else 0
  threshold: 30
```

= fault / normal 両 tier とも **score integrator を共有** できる。
interpreter に score field を追加すれば `stuck_over` は同機構で実装可能。
MVP で score を含める方が経済的 (後付けより初期実装の方が interpreter 差分小)。

### 意図的にやらないこと

- 多目的 utility function (§14 D1) — second-system syndrome トリガ
- case-based retrieval (§14 D2) — data quality 依存、offline 動作困難
- 動的 priority (§14 D5) — debug 困難、`override_lower` flag で代替済

---

## 13. execution model 拡張 (interpreter ≠ executor)

pulse / score / fault の全てが「interpreter は宣言、executor が秒スケール執行」という
責務分離を要求する。この分離を明示化する。

### Interpreter (純関数、10 min tick or executor-driven)

```
input:  sensors + state + config + primitives + active_before + health
output: Result {
  triggered:              [id, ...],
  target_opening_pct:     int,               # steady-state floor (max compose)
  close_all_windows:      bool,
  close_by_wind_direction: bool,
  pulses:                 [{target, value?, ch?, duration_sec, override_lower?,
                            from_id, requested_at}, ...],  # 新規 (旧 relay/window 統合)
  hard_stopped_groups:    {...},
  state_resets/increments/timestamps: [...],
  active_after:           {...},
  skipped:                [(id, reason), ...],
  skipped_for_health:     [(id, [source, ...]), ...],       # 新規
  health:                 {name: "ok"|"unhealthy", ...},    # 新規 (物理 sensor + 仮想 source)
  alerts:                 [(source, reason), ...],          # 新規
}
```

### Executor daemon (副作用、秒スケール)

- `Result.pulses` を受けて秒カウントダウン管理 (target 別に windows/relay code path)
- pulse 中は既存 opening_pct を override (`override_lower: true` なら floor 無視)
- pulse 終了時に steady-state opening_pct へ復帰
- **共通 minimum-dwell 機構**: automation cooldown と health recovery_after を同一 timing state で管理
- score integrator (accumulate 式評価 × dt、decay、per-id 保持)
- **統合 gate file** `/var/lib/agriha/gates.json` 書き出し:
    - `health: {sensor/source_name: ok|unhealthy, ...}` (fault tier 発)
    - `layer1_lockout: {until, reason}` (emergency_guard.sh 発、既存互換)
    - `commandgate_lockout: {...}` (UniPi daemon 発、既存互換)
    - 読み手 (interpreter + emergency_guard) は必要な subkey のみ参照

### 段階的移行方針

1. **stage α**: 現行 `rule_engine.py` 内に executor 相当を組み込む (subprocess で秒ループ、または 1 秒 tick 化)
2. **stage β**: executor を別 systemd unit として切り出し、file/socket 経由で interpreter Result を受け取る (agriha-controller pattern)
3. **stage γ**: fault tier evaluator も別 process 化 (最高頻度 fast loop 化)

MVP は stage α で足りる。β/γ は現場負荷が問題化したときに検討。

### 先行実装 pattern の借用

Pi4 上で稼働する別系統の 1 秒 tick daemon (JSON 宣言 rule engine、`timer_repeat`
デューティを内蔵) が既に同 pattern の executor を実装済。この構造を uecs-llm
executor に借用する。ただし先行実装は logic type 4 種の固定 vocabulary、
本 DSL は score/pulse/tier を扱う点で表現力が拡張されている。

---

## 14. 未消化課題 (docs 化のみ、実装せず)

### D1: 多目的 utility function 化 (MPC-lite)

各 concern を objective として並置、action の utility 総和を最小化するもの選ぶ:

```yaml
# 概念スケッチ (実装しない)
objectives:
  vpd_control:  {metric: max(0, indoor_hd - 3),    weight: 1.0, horizon: "30m"}
  co2_growth:   {metric: max(0, 350 - indoor_co2), weight: 0.8, horizon: "1h"}
  temp_track:   {metric: abs(indoor_temp - target()), weight: 1.5, horizon: "10m"}
actions:
  open_windows_30pct:
    effect: {vpd_control: -0.5, co2_growth: +0.3, temp_track: +0.2}
```

**表現力**: 「VPD 相殺のために CO2 と温度を少し犠牲」が数式で明示。
**代償**: weight/effect 行列の tuning が魔法数字、explainability が Σ 分解に還元、
agri 用語翻訳困難、Home Assistant Jinja2 地獄の別バリエーション。
当面採用しない (§12.5 の並列 score automation で代替)。

### D2: 過去事例検索 (case-based retrieval)

`agriha_history.db` の 211 series × 数年分から現在 state と類似する過去 state を
kNN retrieval、その時の action を LLM 判断者に選ばせる。
LLM 翻訳者 (rule_compiler) → LLM 判断者 (runtime) への拡張。
data quality / label 精度に依存、offline 動作困難。data 溜まってから (数年後) 検討。

### D3: safe_action strategy (safety tier 強制 action)

fault 発動時に safety tier 相当の強制 action を出す拡張。
**役割分離の原則違反** (fault = gate, safety = action) なので当面 skip。
代替: safety tier に該当 automation を独立に書く (emergency_guard.sh 相当が既にこの形)。

### D4: 段階的 health grading

`ok` / `drifting` / `stuck` / `dead` の 3-4 段。
二値で開始、必要になった時点で拡張。

### D5: 動的 priority (score が effective_priority を押し上げる)

`override_lower` flag の代替として、score / threshold 比率で priority を実行時上書き。
表現力大だが debug 困難、utility 化への slippery slope。
`override_lower` action flag で機能的に十分。

### D6: score の非線形累積

`accumulate: max(0, indoor_hd - 3) ** 2` のような非線形式は expression として書けるが、
tuning 困難で LLM 生成対象外。expression 自由度としては許容、default 例には出さない。

### D7: automation `disabled: true` flag (縮退運用)

現場観測: センサが絶対値 drift してるとき、operator が特定 automation を意図的に
切って運用してるケース。この「安全側倒しでの部分縮退」を DSL で表現できる一級市民に
すべきか、という論点。実装は 1 行追加で済むが、「どこで disabled を書くか」
(YAML 直書き / 別 override file / MQTT topic による runtime toggle) の要件が
明確化してから採用判断する。

---

## 15. 実装優先順位 (更新版 next steps)

§9 の「次の一歩」は既に完了 (interpreter + shadow-run 実装済、`feature/dsl-interpreter`)。
以下は §10-14 拡張の実装順:

1. **shadow log の persistent skipped 掃除** — `config.tide.humidity_prevent_threshold`
   を rules.yaml に昇格、`rise_rate()` / `tide_forecast_humidity_1h()` の None セーフガード。
   純粋 divergence だけが log に残る状態にする (interpreter primary 昇格の前段)
2. **score field 実装** — interpreter に per-id score state 追加、accumulate/decay/threshold 実装。
   `stuck_over` の内部実装を score integrator に統一 (§16 統合 (2))
3. **fault tier 実装** — `health:` block (物理 + 仮想 source 統合、§16 統合 (3))、
   requires_healthy / substitute / recovery_after、Pass A/B 分割、
   Result に `health` / `alerts` 追加、`gates.json` 書き出し (§16 統合 (4))
4. **`pulse` action + cooldown 実装** — 既存 `pulse_relay` を target: relay に retire、
   新規 target: windows 追加 (§16 統合 (1))、Result に `pulses` field、
   executor 側 (stage α) で秒カウントダウン管理 + minimum-dwell 共通機構 (§16 統合 (2))
5. **shadow-run で数週間検証** — score / fault / pulse が期待通り動くか観察、
   RH=100 張り付き問題が fault tier で綺麗に skip されるか確認
6. **evaluate_rules retire → interpreter primary** (§9 step 4 相当)

**追加テスト:** score integrator ×5、fault tier gate ×8、pulse (target: windows/relay) ×4、
substitute 伝播 ×3、recovery_after minimum-dwell ×2 の 22 件くらい (現 44 件 → 66 件)。

**設計原則の再々確認 (second-system syndrome 回避):**
§10-14 は表現力を確実に増やす拡張だが、いずれも **既存の 3 層 (safety/normal/fault) /
score integrator の使い回し** で正当化される、既知パターンの一般化に留まる。
D1-D2 のような paradigm shift (utility / retrieval) は明示的に §14 に隔離、実装しない。
「DSL に何でも詰めない」原則は §10-14 でも維持。

---

## 16. 適用済み統合 (2026-07-13)

§10-14 の初稿に対して schema / 実装の統合機会を洗い出し、以下 4 個を反映済:

### (1) `pulse` action への統合 (旧 pulse_relay + 新 pulse_windows)

「時間限定 override」を単一 action kind に統合、`target: windows | relay` で切り替え。
action vocabulary が 1 個減り、executor 側の pulse 管理 code path も 1 本に。
§3 の `pulse_relay` は §12 以降 `pulse` (target: relay) と等価扱い。

### (2) cooldown と recovery_after を minimum-dwell 共通機構に統合

両方「fire したら N 経過するまで再発火抑止」の同 pattern。field 名は用途で分けるが、
実装は per-id timing state (`last_event_at`) の 1 種類で済む。§11 recovery_after +
§12 cooldown の説明で明示。

### (3) `sensors:` + `health_sources:` を `health:` block に統合

物理 sensor と仮想 source (CommandGate 等) を分ける必要が無い (両方 `healthy`
predicate + `requires_healthy` から同じ dict で参照)。top-level block 1 個に統合、
`stuck_over` / `stale_after` は物理のみ意味を持つ optional field として残す。

### (4) gate file の統合 (`gates.json`)

fault tier 発 `sensor_health.json` と emergency_guard.sh 発 `lockout_state.json` を
統合、単一 `/var/lib/agriha/gates.json` に集約。executor + emergency_guard は
必要な subkey のみ参照。将来 fault tier evaluator を fast loop daemon に切り出しても
同 file format、safety tier を DSL 化するときも gate file 互換で移行可能。

### 検討したが不採用

- **`requires_healthy` と `hard_stop` の統合** — 時系列違う (前 gate vs 発火後副作用)、
  意味が違うので分けたまま
- **`trigger:` と `score:` の統合** — trigger を「score threshold=1」に還元可能だが、
  読みやすさが致命的に落ちる (boolean な意図が伝わらなくなる)、並存維持
- **`on_fire` 副作用への `reset_on_fire` / `cooldown` / `alert` 集約** — cooldown は
  automation config、alert は Result field で、on_fire の「state 変更宣言」とは
  性質が違う。個別 field 維持
