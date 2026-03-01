# LLM温室制御ループ設計書

> **Version**: 3.2
> **Date**: 2026-03-02
> **Status**: Draft
> **HW**: RPi (ArSprout RPi, 10.10.0.10, Raspbian Lite, WireGuard VPN)

---

## 概要

### 設計思想: 三層構造（下ほど確実、どの層が欠けても下が支える）

本システムは温室制御を**三層構造**で設計する。
各層は独立して動作し、上位層が欠けても下位層だけで安全に稼働する。

```
┌─────────────────────────────────────────────────────────┐
│  Layer 3: 知恵（LLM）                                    │
│    cron 毎時 → Claude Haiku API → 1時間アクション計画    │
│    CO2制御と露点判断のみ。通常24回/日+緊急数回           │
│    system_prompt.txt が本体。月数百円                     │
│    → 欠けた場合: Layer 2のルールベースで95%回る          │
├─────────────────────────────────────────────────────────┤
│  Layer 2: ガムテ（ルールベース）                          │
│    cron + 日射比例灌水 + 温度閾値側窓 + 重み補正          │
│    日常の95%をカバー。LLMなしでも動く                     │
│    → 欠けた場合: Layer 1の緊急停止で最低限の安全確保     │
├─────────────────────────────────────────────────────────┤
│  Layer 1: 爆発（緊急停止）                                │
│    if文 + LINE curl。27℃超で全開、16℃以下で全閉          │
│    LLMもルールベースも関係なし。問答無用で物理的に動く    │
│    → 最終防壁。何があっても動く                          │
└─────────────────────────────────────────────────────────┘
```

### 機能の優先順位（2026-02-28殿明言）

| 優先度 | 機能 | 状態 |
|--------|------|------|
| 1 | 「開けろ/閉めろ」リモート制御 | **稼働済** |
| 2 | 「今どうなってる？」状態確認 | **稼働済** |
| 3 | 異常値の早期通知 | 未実装 |
| 4 | 自動制御（本設計書の主題） | 設計中 |

### システム全体図

```
ArSprout 観測ノード (192.168.1.70)
    │  CCMマルチキャスト (224.0.0.1:16520) — センサーデータのみ
    ▼
┌─────────────────────────────────────────────────────────┐
│ RPi (ArSprout RPi, 10.10.0.10, Raspbian Lite)            │
│                                                           │
│ [センサー入力]                                            │
│   ccm_receiver.py  →  MQTT: agriha/h01/ccm/...          │
│   sensor_loop.py   →  MQTT: agriha/h01/sensor/...       │
│                    →  MQTT: agriha/farm/weather/misol    │
│                                                           │
│ [三層制御]                                                │
│   Layer 1: emergency_guard.sh (if文+LINE curl)           │
│            27℃超/16℃以下で問答無用。LLM不要              │
│   Layer 2: rule_engine (cron+日射比例灌水+温度閾値側窓)  │
│            日常の95%。LLM不要                             │
│            ↑ gradient_controller がパラメータ動的調整     │
│   gradient_controller (forecast_engine出力→勾配計算→     │
│            rule_engineパラメータ調整。§3.6参照)           │
│   Layer 3: agriha_control.py (cron毎時→Claude Haiku API) │
│            1時間予報JSON生成→plan_executor.pyが実行       │
│                                                           │
│ [外部データ入力]                                          │
│   Open-Meteo API (無料・APIキー不要・JMAモデル)           │
│     → weather_fetcher.py → TTL1hキャッシュ               │
│     → forecast_engine user_message に注入（§3.7参照）    │
│                                                           │
│ [アクチュエータ出力]                                      │
│   REST API (:8080)                                       │
│     POST /api/relay/{ch} → MQTT relay/{ch}/set           │
│   MqttRelayBridge → MCP23008 I2C → リレー ch1-8         │
│                                                           │
│ [安全機構]                                                │
│   CommandGate  ← gpio_watch (DI緊急スイッチ)             │
│   ロックアウト中は全リレー操作を拒否 (423)                │
└──────────────┬──────────────────────────────────────────┘
               │  HTTPS (Anthropic API)
               ▼
         Claude Haiku API
         → tool_calls: get_sensors, get_status, set_relay
         → 1時間アクション計画JSON返却

フェイルセーフ: LLM停止時はLayer 2ルールベース+Layer 1緊急停止で稼働継続
安全制御: Layer 1 = if文+curl（LLM非依存）
          unipi-daemon CommandGate（緊急スイッチ→300秒ロックアウト）

データ経路:
  CCM → ccm_receiver → MQTT → REST API /api/sensors
  CCM → ccm_receiver → MQTT → Telegraf → InfluxDB → Grafana
  Misol WH65LP → sensor_loop → MQTT (agriha/farm/weather/misol)
  DS18B20 → sensor_loop → MQTT (agriha/{house_id}/sensor/DS18B20)
```

> **v3.0変更点**: アーキテクチャを三層構造に全面転換。
> LLMは「知恵」層として1時間予報のみ担当。日常制御の95%はルールベース。
> 緊急停止はLLMを一切介さないif文+curlの独立系統。

---

## 目次

1. [三層制御アーキテクチャ](#1-三層制御アーキテクチャ)
2. [LLMの責務範囲](#2-llmの責務範囲)
3. [1時間予報＋緊急フラグ方式](#3-1時間予報緊急フラグ方式)
   - [3.6 gradient_controller（勾配制御層）](#36-gradient_controller勾配制御層)
   - [3.7 天気予報API統合](#37-天気予報api統合)
4. [Claude Haiku API 接着層](#4-claude-haiku-api-接着層)
5. [システムプロンプト設計](#5-システムプロンプト設計)
6. [ステート管理](#6-ステート管理)
7. [LLM自然減衰モデル](#7-llm自然減衰モデル)
8. [安全制御設計](#8-安全制御設計)
9. [アクチュエータ制御（UniPiリレー）](#9-アクチュエータ制御unipiリレー)
10. [リレーチャンネル割当](#10-リレーチャンネル割当)
11. [RPiセットアップ手順](#11-rpiセットアップ手順)
12. [参照ドキュメント](#12-参照ドキュメント)
13. [付録A: v2.0→v3.2 変更履歴](#付録a-v20v32-変更履歴)

---

## 1. 三層制御アーキテクチャ

### 1.1 設計思想

温室制御において最も重要な原則: **下位層は上位層の障害に影響されない**。

```
Layer 3: 知恵（LLM）  ← 最も高度、最も脆い
Layer 2: ガムテ（ルールベース）  ← 日常の95%
Layer 1: 爆発（緊急停止）  ← 絶対に壊れない
```

ブレーカーと非常ベルは、指導員がパニクっても動く。
緊急停止とLLMは**別系統**。これが本設計書の根幹思想である。

### 1.2 Layer 1: 爆発（緊急停止）

**LLMもルールベースも一切関係ない。if文とcurlだけで動く。**

```bash
# emergency_guard.sh（概念コード）
TEMP=$(curl -s http://localhost:8080/api/sensors | python3 -c "
import sys,json; print(json.load(sys.stdin)['ccm']['InAirTemp'])")

if (( $(echo "$TEMP > 27" | bc -l) )); then
    # 全窓全開（ch5-8 ON）
    for ch in 5 6 7 8; do
        curl -s -X POST "http://localhost:8080/api/relay/$ch" \
            -H 'Content-Type: application/json' \
            -d '{"value":1,"duration_sec":0,"reason":"EMERGENCY: overheat"}'
    done
    # LINE通知（LLMを通さない。curlで直接叩く）
    curl -s -X POST "https://api.line.me/v2/bot/message/push" \
        -H "Authorization: Bearer $LINE_TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"to\":\"$GROUP_ID\",\"messages\":[{\"type\":\"text\",\"text\":\"🚨 ${TEMP}℃ 緊急全開\"}]}"
fi

if (( $(echo "$TEMP < 16" | bc -l) )); then
    # 全窓全閉（ch5-8 OFF）
    for ch in 5 6 7 8; do
        curl -s -X POST "http://localhost:8080/api/relay/$ch" \
            -H 'Content-Type: application/json' \
            -d '{"value":0,"duration_sec":0,"reason":"EMERGENCY: freeze risk"}'
    done
    curl -s -X POST "https://api.line.me/v2/bot/message/push" \
        -H "Authorization: Bearer $LINE_TOKEN" \
        -H 'Content-Type: application/json' \
        -d "{\"to\":\"$GROUP_ID\",\"messages\":[{\"type\":\"text\",\"text\":\"🚨 ${TEMP}℃ 緊急全閉\"}]}"
fi
```

**独立動作保証**:
- LLM（Layer 3）が完全停止しても、Layer 1は動作する
- ルールベース（Layer 2）が停止しても、Layer 1は動作する
- Starlink回線が断絶しても、RPiローカルで動作する（LINE通知のみ不達）
- cron間隔: `*/1`（1分毎）。LLMの1時間間隔を待たない

### 1.3 Layer 2: ガムテ（ルールベース）

**日常の95%を担当。LLMなしでも回る。**

```
制御対象と方式:
  灌水     = タイマー + 日射比例。cron + crop_irrigation.yaml の閾値
  側窓     = 温度閾値。気温>目標+2℃で開、気温<目標-1℃で閉
  EC       = ドサトロン手動調整。制御対象外
  換気扇   = 温度連動ON/OFF（閾値ベース）

重み補正:
  crop_irrigation.yaml のステージ別パラメータで灌水量・温度目標を調整
  将来的にLLM自然減衰モデル（§7）でLLM判断パターンをルールに蒸留
```

**独立動作保証**:
- LLM（Layer 3）が停止しても、日射比例灌水と温度閾値制御は継続
- RPi上のcronとPythonスクリプトのみで動作（外部API不要）

### 1.4 Layer 3: 知恵（LLM）

**たまに相談する知恵袋。system_prompt.txt が本体。**

CO2と露点の判断だけがLLMの仕事。詳細は§2（LLMの責務範囲）参照。

- cron 毎時（`0 * * * *`）にClaude Haiku APIを呼び出し
- 向こう1時間のアクション計画JSONを生成
- RPi上のplan_executor.pyが計画通りに実行
- 通常24回/日 + 緊急割り込み数回
- 月コスト: 数百円程度

**独立動作保証**:
- Layer 3が停止してもLayer 1+2で安全に稼働
- Anthropic API障害時はLayer 2にフォールバック
- system_prompt.txtが方針の正データ源。コード変更なしで制御方針を更新可能

---

## 2. LLMの責務範囲

### 2.1 LLMが判断する場面: CO2と露点の2つだけ

LLMの真のクリティカル判断は**2場面のみ**。

| 判断場面 | なぜLLMが必要か | 判断要素 |
|----------|----------------|----------|
| **CO2制御** | 換気との相反（側窓開→CO2逃げる）。単純閾値では判断不可 | 気温・湿度・日射・風速・CO2濃度の総合判断 |
| **露点制御** | 結露=病気。暖房か換気かの二択を状況判断 | 内気温・外気温・湿度・時間帯・天候 |

### 2.2 LLM不要な制御

| 制御対象 | 方式 | LLMの関与 |
|----------|------|-----------|
| 灌水 | タイマー + 日射比例（crop_irrigation.yaml） | なし（Layer 2） |
| 側窓 | 温度閾値（目標温度±ΔT） | なし（Layer 2）※CO2/露点判断時のみ介入 |
| EC | ドサトロン手動調整 | なし（制御対象外） |
| 緊急停止 | if文 + LINE curl（27℃超/16℃以下） | なし（Layer 1） |

### 2.3 1日の呼び出し回数と頻度

```
定時予報:   24回/日（1時間毎）
緊急割込み: 数回/日（閾値超過時、LLMを待たずにLayer 1が先行動作）
合計:       ~30回/日以下

1回のAPI呼び出し:
  入力: ~2,000トークン（システムプロンプト+履歴+センサーデータ）
  出力: ~300トークン（アクション計画JSON+判断理由）
  コスト: Claude Haiku 1回あたり ~$0.001未満

月間コスト: ~30回/日 × 30日 × $0.001 ≈ $1未満（数百円程度）
```

---

## 3. 1時間予報＋緊急フラグ方式

### 3.1 概要

```
定時予報（cron 毎時）                    緊急割り込み（cron 毎分）
┌──────────────────────┐           ┌─────────────────────────┐
│ agriha_control.py    │           │ emergency_guard.sh      │
│ Claude Haiku API     │           │ if文 + curl             │
│ → 1時間計画JSON      │           │ LLMを待たない           │
│ → plan_executor.py   │           │ 27℃超/16℃以下で即動作  │
└──────────────────────┘           └─────────────────────────┘
      ↓ 通常制御                          ↓ 緊急制御（最優先）
      ▼                                   ▼
  unipi-daemon REST API → MCP23008 I2C → リレー ch1-8
```

### 3.2 定時予報ループの流れ

```
cron (0 * * * *) → agriha_control.py 起動
    │
    ├─ Step 1: unipi-daemon REST API への接続確認
    │
    ├─ Step 2: 直近の判断履歴をSQLiteから読み込み（§6参照）
    │
    ├─ Step 3: システムプロンプト + 履歴 + 指示を組み立て
    │          「現在のセンサーデータを確認し、向こう1時間の
    │           アクション計画を JSON で生成せよ」
    │
    ├─ Step 3.5: 天気予報フェッチ（§3.7参照）
    │            → weather_fetcher.py: Open-Meteo API から向こう6時間予報を取得
    │            → キャッシュ確認（/var/lib/agriha/weather_cache.json, TTL 1時間）
    │            → user_message に「## 天気予報（向こう6時間）」セクションを注入
    │            → フェイルセーフ: API失敗時は「天気予報なし」でLLM呼び出し続行
    │
    ├─ Step 4: Claude Haiku API に送信（tools配列付き）
    │          → LLMが自発的にツールを呼ぶ:
    │            (1) get_sensors  → REST API GET /api/sensors
    │                → CCM(内温/湿度/CO2) + DS18B20 + Misol(外気/風/降雨)
    │            (2) get_status   → REST API GET /api/status
    │                → リレー状態(ch1-8) + ロックアウト状態
    │            (3) 判断 → 1時間アクション計画JSON生成
    │                → 必要に応じて即時set_relay実行も
    │
    ├─ Step 5: アクション計画JSONをファイルに保存
    │          /var/lib/agriha/current_plan.json
    │
    ├─ Step 5.5: gradient_controller による rule_engine パラメータ調整（§3.6参照）
    │            → forecast_engineの出力（target/priority/overrides）を受け取る
    │            → 実測センサー値と目標レンジの差分からゲイン計算
    │            → 3軸（気温・湿度・CO2）のpriority重みを配分
    │            → 病害リスクスコアを更新（§3.6.2参照）
    │            → 調整済みパラメータをrule_engineに渡す（次サイクル適用）
    │
    ├─ Step 6: LLMの最終応答（判断理由）をログに記録
    │
    └─ Step 7: プロセス終了（次のcron起動まで待機）
```

### 3.3 アクション計画JSON

LLMが生成するアクション計画の形式:

```json
{
  "generated_at": "2026-02-28T14:00:00+09:00",
  "valid_until": "2026-02-28T15:00:00+09:00",
  "summary": "日射強く気温上昇傾向。CO2は換気で自然値。露点リスクなし。",
  "actions": [
    {
      "execute_at": "+0min",
      "relay_ch": 5,
      "value": 1,
      "duration_sec": 30,
      "reason": "北側窓50%開（気温上昇対応）"
    },
    {
      "execute_at": "+30min",
      "relay_ch": 4,
      "value": 1,
      "duration_sec": 300,
      "reason": "灌水5分（日射比例閾値到達見込み）"
    }
  ],
  "co2_advisory": "換気中のためCO2自然値で推移。密閉判断不要",
  "dewpoint_risk": "low",
  "next_check_note": "15時に日射減衰見込み。側窓調整の可能性あり"
}
```

### 3.4 緊急割り込み

LLMの1時間予報を待たず、Layer 1が即座に動作する場面:

| 条件 | 動作 | LLMの関与 |
|------|------|-----------|
| 内気温 > 27℃ | 全窓全開 + LINE通知 | **なし** |
| 内気温 < 16℃ | 全窓全閉 + LINE通知 | **なし** |
| 降雨検知 (rainfall > 0.5mm/h) | 窓系リレー全OFF | **なし** |
| 緊急スイッチ押下 | CommandGate 300秒ロックアウト | **なし** |

> **重要**: 緊急割り込みが発動した場合でも、次回の定時予報（毎時cron）で
> LLMは現在の状態を get_status で確認し、緊急割り込み後の状態を考慮した
> 新たなアクション計画を生成する。

### 3.5 コスト見積もり

| 項目 | 数量 | 単価 | 月額 |
|------|------|------|------|
| 定時予報 | 24回/日 × 30日 = 720回 | ~$0.001/回 | ~$0.72 |
| 緊急割り込み | LLMを呼ばない | $0 | $0 |
| **合計** | | | **~$1/月（数百円）** |

> v2.0（cron 10分間隔）では月約$9だったが、1時間予報方式で大幅に削減。

### 3.6 gradient_controller（勾配制御層）

forecast_engine（Layer 3）の予報出力を受けて、rule_engine（Layer 2）のパラメータを動的に調整する層。
**「LLMは予報を出すだけ。制御はPythonの勾配計算」**（殿語録）。

**位置づけ:**

```
Layer 3 (forecast_engine) → target/priority/reason JSON
    ↓
gradient_controller → 実測値との差分→ゲイン計算→パラメータ調整
    ↓
Layer 2 (rule_engine) → 調整済みパラメータで制御実行
```

**設計ポイント:**

- forecast_engineの出力（目標レンジ・優先順位・特殊フラグ）を受けて、rule_engineの制御パラメータ（温度閾値・灌水タイミング等）を動的調整する
- 実測値と目標値の差から開度・強度を比例計算する
- **目標付近ではゲインを落としてソフトランディング**（オーバーシュート防止）
- **変化率制限**: 1サイクルで最大N%しかパラメータを動かさない（急変防止）
- LLM予報が外れても実測フィードバックで自己修正する

**設計根拠（殿語録）:**

- 「LLMは予報を出すだけ。制御はPythonの勾配計算」
- 「目標値付近をフラフラ安定が理想。オーバーシュートは病害・生育障害」
- 「天気自体がハルシネーションする。予測が外れる前提の設計」
- 「ガードレールは保険であって日常ではない」

#### 3.6.1 3軸ゲイン（気温・湿度・CO2）

制御対象の3軸は互いに矛盾する。**「攻めるゲインの数値が農家の腕」**（殿語録）。

| 軸 | ゲイン特性 | トレードオフ |
|----|------------|--------------|
| 気温 | **安全寄り**（低ゲイン） | 失敗の代償大（病害・生育障害）。急変禁止 |
| 湿度 | **中間**（状況依存） | 攻めすぎ→病害リスク、守りすぎ→蒸散効率低下 |
| CO2 | **攻めと換気のトレードオフ** | 側窓を開ける＝CO2逃げる。換気と相反 |

**3軸は全部同時には満たせない**。農家の「今日の攻め方」を `system_prompt.txt` で表現し、
forecast_engineが `priority` フィールドに優先順位を出力する。
gradient_controllerはそのpriority順にゲインの重みを配分する。

**概念コード:**

```python
# gradient_controller ゲイン配分（概念コード）
priority = forecast_output["priority"]   # 例: ["humidity", "temp", "co2"]
gains = {"temp": 0.33, "humidity": 0.33, "co2": 0.34}  # デフォルト均等配分

# priority順に重みをシフト
gains[priority[0]] += 0.2   # 最優先軸のゲイン増
gains[priority[-1]] -= 0.2  # 最低優先軸のゲイン減

for axis in ["temp", "humidity", "co2"]:
    delta = actual[axis] - target_mid[axis]

    # 目標付近ではゲインを落とす（ソフトランディング）
    if abs(delta) < tolerance[axis]:
        gains[axis] *= 0.5  # 目標付近: ゲイン半減

    # 変化率制限（1サイクル最大10%）
    param_new = param_old[axis] + gains[axis] * delta
    param_new = clamp(param_new, param_old[axis] * 0.9, param_old[axis] * 1.1)
```

#### 3.6.2 病害リスクスコア

**目的: 科学的実証データを受け入れるための器を先に用意する。今は仮実装、将来データが溜まったら差し替え。**
「データが取れてから再設計しないための保険。器を用意するだけ」（殿語録）。

**仮実装:**

```python
def get_disease_risk() -> float:
    """病害リスクスコア（仮実装。将来は実証データで差し替え）

    Returns:
        float: 0.0 = リスク低、1.0 以上 = 警戒
    """
    # dew_hours: 過去24hで露点付近（temp - dewpoint < 2.0℃）が続いた時間
    # dry_hours: 過去24hで日射が十分（solar > 200 W/m²）だった時間
    risk_score = dew_hours / max(dry_hours, 1)
    return risk_score
```

**インターフェース設計:**

- `get_disease_risk() -> float` を定義し、中身を差し替え可能にする
- 将来: 1年分のログ蓄積後、病害発生実績と突合して実証ベースに移行する

**LLMへの渡し方:**

- プロンプトにrisk_scoreと内訳を載せて判断材料にする
- 例: `「risk=4.0, 露点付近8h, 乾燥機会2h → 灰色かび警戒」`

#### 3.6.3 LLM予報フォーマット改訂（§3.3拡張）

既存の§3.3アクション計画JSON（実行スケジュール中心）に加え、
forecast_engineが出力する**目標パラメータ仕様**を追記する。
gradient_controllerはこのフォーマットの `target/priority` を入力として動作する。

```jsonc
// forecast_engine 出力フォーマット（v3.1追記）
{
  "target": {
    "temp":     [22, 26],    // 目標気温レンジ [min, max] ℃
    "humidity": [65, 75],    // 目標湿度レンジ [min, max] %
    "co2":      [400, 800]   // 目標CO2レンジ [min, max] ppm
  },
  "priority": ["humidity", "temp", "co2"],  // gradient_controllerがゲイン配分に使う優先順位
  "overrides": {
    "ventilation_priority": true  // 換気優先フラグ（true時: 窓閉めを抑制）
  },
  "reason": "昨日の多湿+露点接近8h。灰色かび警戒。CO2犠牲にしてでも換気優先"
}
```

**フィールド仕様:**

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `target` | object | ✓ | 各軸の目標レンジ。gradient_controllerがゲイン計算に使用 |
| `priority` | array[string] | ✓ | 優先順位リスト（先頭が最優先）。3軸ゲイン重みに反映 |
| `overrides` | object | — | 特殊制御フラグ（`ventilation_priority`, `heating_priority` 等） |
| `reason` | string | ✓ | 判断理由（ログ・デバッグ用）。gradient_controllerログに記録 |

> このフォーマットは既存の§3.3 `actions` 配列と**併存**する。
> LLMが出力する1つのJSONに `target/priority/reason` ブロックと `actions` 配列の
> 両方を含める（v3.1以降）。

---

### 3.7 天気予報API統合

外部天気予報APIをforecast_engineに統合し、LLMが翌時間の気象条件を考慮して制御計画を立てられるようにする。
**「予報が外れても勾配制御が吸収する設計。精度で悩むな」**（殿方針）。

#### 3.7.1 API選定結果

**比較表（7候補）:**

| API | 無料枠 | 1時間予報 | 日射量(W/m²) | APIキー | レート制限 | 推奨度 |
|-----|--------|-----------|-------------|---------|-----------|--------|
| **Open-Meteo** | 完全無料 | ✓ | ✓ (JMA MSM) | **不要** | 10,000回/日 | **◎ 第1推奨** |
| Visual Crossing | 1,000レコード/日 | ✓ | ✓ | 必要 | 中 | ○ 第2推奨(バックアップ) |
| 気象庁forecast API | 完全無料 | ✗ (6時間単位) | ✗ | 不要 | なし | △ 補助的参照のみ |
| OpenWeatherMap | 1,000回/日 | ✓ | **有料** | 必要 | 中 | ✗ 不採用 |
| WeatherAPI | 100万回/月 | ✓ | **Enterprise** | 必要 | 中 | ✗ 不採用 |
| AccuWeather | 50回/日 (試用) | ✓ | ✓ | 必要 | 低 | ✗ 不採用(試用のみ) |
| Tomorrow.io | 500回/日 | ✓ | **有料** | 必要 | 中 | ✗ 不採用 |

**選定: Open-Meteo**

選定理由:
- 完全無料・APIキー不要（月額忌避・マクガイバー精神に合致）
- 日射量(W/m²)を無料で取得可能（灌水の日射比例制御に必須）
- VPD(飽差)を無料で提供（病害リスク判断に有用）
- JMAデータ（MSM 5km解析メッシュ）ベースで北海道精度良好
- 10,000回/日の枠（cron毎時=24回/日で余裕十分）

**バックアップ: Visual Crossing**
- Open-Meteoが商用利用不可と判断された場合の代替（APIキー取得必要）
- 日射量込み・15日予報対応

> **⚠️ 商用利用注意事項（殿判断事項）**: Open-MeteoのCC BY 4.0ライセンスは
> 「農家へのサービス提供」が商用利用に該当するか解釈の余地あり。
> 顧客への販売モデルを確立する段階で、Visual Crossingへの移行を検討せよ。

**補助的参照: 気象庁forecast API**
- `https://www.jma.go.jp/bosai/forecast/data/forecast/016000.json`（石狩地方）
- 6時間単位・日射量なし → 1時間制御には不向き。概況把握のみに使用可

**恵庭市エンドポイント（Open-Meteo）:**
```
https://api.open-meteo.com/v1/forecast?latitude=42.888&longitude=141.603
  &hourly=temperature_2m,relative_humidity_2m,precipitation_probability,
  precipitation,rain,wind_speed_10m,wind_direction_10m,
  shortwave_radiation,direct_radiation,diffuse_radiation,
  surface_pressure,vapour_pressure_deficit
  &wind_speed_unit=ms&forecast_days=7&timezone=Asia%2FTokyo
```

#### 3.7.2 データフロー

```
Open-Meteo API (HTTPS)
    │
    ▼
weather_fetcher.py
    ├─ キャッシュ確認: /var/lib/agriha/weather_cache.json（TTL 60分）
    │    ├─ 有効なキャッシュあり → キャッシュから返却
    │    └─ キャッシュ古/なし  → Open-Meteo API fetch → キャッシュ更新
    │
    ├─ フェイルセーフ: try/except → logging.warning → None 返却
    │    → None の場合: forecast_engine は「天気予報なし」でLLM呼び出し続行
    │
    └─ 向こう6時間分（hourlyの6エントリ）を返却
         → 気温(℃), 湿度(%), 降水確率(%), 降水量(mm),
           風速(m/s), 風向(°), 日射量(W/m²), 気圧(hPa), VPD(kPa)
```

**fetchタイミング:** run_forecast() Step3後・Step4前（Step3.5）に配置。
LLM API呼び出しの直前に天気データをuser_messageに注入する。

**取得データ一覧:**

| パラメータ | 単位 | 用途 |
|-----------|------|------|
| temperature_2m | ℃ | 外気温予報（側窓制御参考） |
| relative_humidity_2m | % | 外気湿度（換気効果判断） |
| precipitation_probability | % | 降水確率（灌水判断・窓制御） |
| precipitation / rain | mm | 降水量（窓閉め判断） |
| wind_speed_10m | m/s | 風速（強風時窓制御） |
| wind_direction_10m | ° | 風向（片側制御§9参照） |
| shortwave_radiation | W/m² | 全天日射量（灌水日射比例制御の主入力） |
| surface_pressure | hPa | 気圧（参考） |
| vapour_pressure_deficit | kPa | VPD（病害リスク補助指標） |

**キャッシュ設計:**
- パス: `/var/lib/agriha/weather_cache.json`
- TTL: 60分（cron毎時起動と整合。API呼び出しは最大1回/時間）
- 形式: `{"fetched_at": "ISO8601", "hourly": {...}}` (Open-Meteoレスポンスそのまま)

#### 3.7.3 LLMプロンプト注入フォーマット

forecast_engine.py の user_message 構築部（L422付近、日出日没の直後・指示セクションの前）に
「## 天気予報（向こう6時間）」セクションを挿入する。

**トークン効率重視の1行/時間フォーマット:**
```
## 天気予報（向こう6時間）
14:00 曇 8.2℃ 湿62% 風3.1m/s北 雨0mm 射45W/m²
15:00 曇 7.8℃ 湿65% 風2.9m/s北 雨0mm 射30W/m²
16:00 曇 7.1℃ 湿68% 風2.5m/s北北西 雨0mm 射12W/m²
17:00 雨 6.5℃ 湿78% 風3.8m/s北西 雨0.4mm 射0W/m²
18:00 雨 6.2℃ 湿82% 風4.2m/s北西 雨1.1mm 射0W/m²
19:00 雨 6.0℃ 湿85% 風4.0m/s北西 雨0.8mm 射0W/m²
```

**フォーマット仕様:**
- 天気記号: 射量>200W/m²→「晴」, 50-200→「曇」, 0-50→「薄曇」, 降水確率>50%→「雨」
- 風向: 45°刻みで「北/北東/東/南東/南/南西/西/北西」に変換
- フォーマット例: `{HH:MM} {天気記号} {気温:.1f}℃ 湿{湿度:.0f}% 風{風速:.1f}m/s{風向} 雨{降水量:.1f}mm 射{日射量:.0f}W/m²`

**gradient_controller（§3.6）との連携:**
- LLMが出力する `priority` フィールドには、天気予報（降雨予測・日射見込み）が考慮される
- 例: 「17時から降雨予測 → humidity優先度UP → ventilation_priority=true」
- gradient_controllerは§3.7のweather_fetcherと直接連携しない。LLMの判断結果（§3.6.3 `priority`）を通じて間接的に天気が反映される

**フェイルセーフ時のプロンプト:**
```
## 天気予報
天気予報の取得に失敗しました。Misolの外気センサーデータを参照してください。
```

#### 3.7.4 設定ファイル拡張

`layer3_config.yaml` に `weather` セクションを追加:

```yaml
weather:
  provider: open-meteo          # 主力プロバイダー
  latitude: 42.888              # 恵庭市
  longitude: 141.603
  forecast_hours: 6             # 向こう何時間分を取得・注入するか
  cache_ttl_minutes: 60         # キャッシュ有効期間（分）
  cache_path: /var/lib/agriha/weather_cache.json
  fallback_provider: visual-crossing  # バックアップ（APIキーが必要）
  # visual_crossing_api_key: YOUR_KEY  # 商用移行時に設定
```

#### 3.7.5 実装ロードマップ

天気予報API統合の実装は以下のステップで行う:

| Step | 内容 | 規模 |
|------|------|------|
| **Step1** | `weather_fetcher.py` 新規モジュール作成 | ~80行 |
|           | Open-Meteo fetchクライアント（httpx） | |
|           | TTLキャッシュ（JSON read/write） | |
|           | フェイルセーフ（try/except → None返却） | |
|           | pytest（モックfetch/キャッシュHIT/キャッシュMISS/フェイルセーフ） | |
| **Step2** | `forecast_engine.py` 改修 | ~30行 |
|           | Step3.5: weather_fetcher呼び出しをuser_message構築直前に追加 | |
|           | user_message拡張: 天気予報セクション整形・注入 | |
|           | 既存astral日時注入との連携（同一try/exceptパターン） | |
| **Step3** | `layer3_config.yaml` 拡張 | ~10行 |
|           | weatherセクション追加（§3.7.4の内容） | |
| **Step4** | pytest追加 | ~40行 |
|           | forecast_engine統合テスト（weather注入あり/なし両ケース） | |

> **設計根拠**: 「予報が外れても勾配制御が吸収する設計。精度で悩むな」（殿方針）。
> 天気予報は「LLMへの参考情報」であり、制御の決定権はgradient_controller（§3.6）が持つ。
> Open-Meteo fetch失敗でもMisolの実測外気センサーでフォールバック可能。
> 「壊れても動く設計。天気APIが落ちても温室は制御できる」（マクガイバー精神）。

---

## 4. Claude Haiku API 接着層

### 4.1 方式選定

| 方式 | 依存 | コスト | 評価 |
|------|------|--------|------|
| **(採用) Claude Haiku API + anthropic SDK** | `anthropic`, `httpx` | 月~$1 | **2026-02-23殿裁定** |
| LFM2.5 (llama-server) | `httpx` | 電力のみ | **廃止**: 対話能力致命的不足 |
| Ollama (ローカルLLM) | `ollama` | 電力のみ | **停止済み**: シャドーモード2026-02-24殿判断で停止。vx2ベンチマーク専用 |

**廃止理由（LFM2.5）**:
- 日時読取不可、tool_calls自発生成不可
- 殿曰く「自分の人件費よりは安い」（Claude Haiku月~$1）
- 将来ローカルLLMが実用に耐えれば再検討（§7 LLM自然減衰モデル参照）

> **v3.0変更**: llama-server (localhost:8081) を全面廃止。
> RPiからAnthropic APIに直接HTTPS通信。中間層（nipogi等）不要。

### 4.2 Anthropic tool calling

Claude Haiku APIは `tools` 配列を受け取り、LLMが自発的にtool_useを生成する。

```python
import anthropic

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 環境変数

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    system=system_prompt,
    tools=[
        {
            "name": "get_sensors",
            "description": "全センサーデータ取得（CCM内気象 + DS18B20 + Misol外気象）",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_status",
            "description": "デーモン状態取得（リレー状態ch1-8 + ロックアウト状態）",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "set_relay",
            "description": (
                "UniPiリレー制御。ch=チャンネル(1-8), value=1(ON)/0(OFF), "
                "duration_sec=自動OFF秒数(灌水等は必須指定), reason=理由"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ch": {"type": "integer", "minimum": 1, "maximum": 8},
                    "value": {"type": "integer", "enum": [0, 1]},
                    "duration_sec": {"type": "number", "default": 0},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["ch", "value"],
            },
        },
    ],
    messages=messages,
)
```

### 4.3 接着層スクリプト: agriha_control.py

```python
#!/usr/bin/env python3
"""AgriHA LLM制御ループ — Claude Haiku API + unipi-daemon REST API
1時間予報方式: 毎時cronで起動、向こう1時間のアクション計画を生成"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from astral import LocationInfo
from astral.sun import sun

# 設定
# astral: 日の出/日没計算（LLMは自力で日時把握不可→全経路に注入）
LOCATION = LocationInfo("Greenhouse", "Japan", "Asia/Tokyo", 42.888, 141.603)  # 道央圃場
UNIPI_API = "http://localhost:8080"  # unipi-daemon REST API（RPiローカル）
API_KEY = ""  # config.yaml の rest_api.api_key に合わせる
DB_PATH = Path("/var/lib/agriha/control_log.db")
SYSTEM_PROMPT_PATH = Path("/etc/agriha/system_prompt.txt")
PLAN_PATH = Path("/var/lib/agriha/current_plan.json")
MAX_TOOL_ROUNDS = 5  # ツール呼び出し最大ラウンド数
MODEL = "claude-haiku-4-5-20251001"

logger = logging.getLogger("agriha_control")

# ツール定義（Anthropic tools形式）
TOOLS = [
    {
        "name": "get_sensors",
        "description": "全センサーデータ取得（CCM内気象 + DS18B20 + Misol外気象）",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_status",
        "description": "デーモン状態取得（リレー状態ch1-8 + ロックアウト状態）",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_relay",
        "description": (
            "UniPiリレー制御。ch=チャンネル(1-8), value=1(ON)/0(OFF), "
            "duration_sec=自動OFF秒数(灌水等は必須指定), reason=理由"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ch": {"type": "integer", "minimum": 1, "maximum": 8},
                "value": {"type": "integer", "enum": [0, 1]},
                "duration_sec": {"type": "number", "default": 0},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["ch", "value"],
        },
    },
]


def load_recent_history(db: sqlite3.Connection, n: int = 3) -> str:
    """直近n回の判断履歴をテキストで返す"""
    rows = db.execute(
        "SELECT timestamp, summary, actions_taken "
        "FROM decisions ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    if not rows:
        return "（過去の判断履歴なし — 初回起動）"
    lines = []
    for ts, summary, actions in reversed(rows):
        lines.append(f"[{ts}] {summary} → {actions}")
    return "\n".join(lines)


def save_decision(db: sqlite3.Connection, summary: str, actions: str,
                  raw_response: str, sensor_snapshot: str):
    """判断ログをSQLiteに保存"""
    db.execute(
        "INSERT INTO decisions (timestamp, summary, actions_taken, "
        "raw_response, sensor_snapshot) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), summary, actions,
         raw_response, sensor_snapshot)
    )
    db.commit()


def call_tool(client: httpx.Client, name: str, args: dict) -> str:
    """ツール名に応じてunipi-daemon REST APIを呼ぶ"""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}

    if name == "get_sensors":
        r = client.get(f"{UNIPI_API}/api/sensors", headers=headers)
        return r.text

    if name == "get_status":
        r = client.get(f"{UNIPI_API}/api/status", headers=headers)
        return r.text

    if name == "set_relay":
        ch = args.get("ch", 1)
        payload = {
            "value": args.get("value", 0),
            "duration_sec": args.get("duration_sec", 0),
            "reason": args.get("reason", "LLM auto"),
        }
        r = client.post(
            f"{UNIPI_API}/api/relay/{ch}",
            json=payload,
            headers=headers,
        )
        return r.text

    return json.dumps({"error": f"unknown tool: {name}"})


def run_control_loop():
    """メイン制御ループ（1回実行、毎時cronから呼ばれる）"""

    # DB接続
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        summary TEXT,
        actions_taken TEXT,
        raw_response TEXT,
        sensor_snapshot TEXT
    )""")

    # システムプロンプト読み込み
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    # 直近の判断履歴
    history = load_recent_history(db, n=3)

    # Anthropic クライアント + unipi-daemon HTTPクライアント
    llm_client = anthropic.Anthropic()
    api_client = httpx.Client(timeout=30)

    try:
        # astral: 日の出/日没計算 → 時間帯4区分を注入
        now = datetime.now()
        s = sun(LOCATION.observer, date=now.date())
        sunrise = s["sunrise"].strftime("%H:%M")
        sunset = s["sunset"].strftime("%H:%M")
        hour = now.hour
        sunrise_h = s["sunrise"].hour
        sunset_h = s["sunset"].hour
        if hour < sunrise_h:
            time_period = "日の出前（夜間）"
        elif hour >= sunset_h:
            time_period = "日没後（夜間）"
        elif hour >= sunset_h - 1:
            time_period = "日没前1h（夕方遮光注意）"
        else:
            time_period = "日中"

        # メッセージ組み立て（1時間予報指示）
        messages = [
            {"role": "user", "content": (
                f"## 直近の判断履歴\n{history}\n\n"
                f"## 指示\n"
                f"現在時刻: {now.strftime('%Y-%m-%d %H:%M')}\n"
                f"日の出: {sunrise} / 日没: {sunset} / 時間帯: {time_period}\n"
                f"センサーデータを確認し、向こう1時間のアクション計画を"
                f"JSON形式で生成せよ。\n"
                f"CO2制御と露点リスクに特に注意せよ。\n"
                f"アクションが不要なら「現状維持」と報告せよ。"
            )},
        ]

        # Tool calling ループ
        sensor_snapshot = ""
        actions_taken = []

        for round_num in range(MAX_TOOL_ROUNDS):
            response = llm_client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            # レスポンス解析
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if not tool_uses:
                # ツール呼び出しなし → 最終応答
                break

            # assistantメッセージを追加
            messages.append({"role": "assistant", "content": response.content})

            # 各ツール呼び出しを実行
            tool_results = []
            for tu in tool_uses:
                logger.info("Tool call [%d]: %s(%s)", round_num, tu.name, tu.input)

                result_text = call_tool(api_client, tu.name, tu.input)

                if tu.name in ("get_sensors", "get_status"):
                    sensor_snapshot += f"\n--- {tu.name} ---\n{result_text}"
                if tu.name == "set_relay":
                    actions_taken.append(
                        f"relay ch{tu.input.get('ch')}={tu.input.get('value')}"
                    )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})

        # 最終応答テキストを取得
        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text
        if not final_text:
            final_text = "（応答なし）"

        # アクション計画JSONをファイルに保存（plan_executor用）
        try:
            # LLMの応答からJSONブロックを抽出して保存
            PLAN_PATH.write_text(final_text, encoding="utf-8")
        except Exception as e:
            logger.warning("Plan file save failed: %s", e)

        # 判断ログ保存
        save_decision(
            db,
            summary=final_text[:500],
            actions="; ".join(actions_taken) if actions_taken else "現状維持",
            raw_response=json.dumps({"text": final_text}, ensure_ascii=False),
            sensor_snapshot=sensor_snapshot[:2000],
        )

        logger.info("Decision: %s | Actions: %s", final_text[:200], actions_taken)

    finally:
        api_client.close()
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_control_loop()
```

### 4.4 接続モード

| モード | 仕組み | 用途 |
|--------|--------|------|
| **RPiローカル** | RPi上でagriha_control.pyが動作、localhost:8080でunipi-daemon REST APIに接続 | **標準構成** |
| **VPN経由** | WireGuard VPN越しにRPi上のREST APIにリモートアクセス | 遠隔デバッグ |

> **v3.0変更**: RPiからAnthropic APIに直接HTTPS通信。
> 中間層（nipogi, nuc.local等）は不要。Starlink回線で直結。

---

## 5. システムプロンプト設計

### 5.1 構造

```
/etc/agriha/system_prompt.txt
  │
  ├─ [A] 役割定義（固定）
  ├─ [B] ハウス固有情報（設定ファイルから生成）
  ├─ [C] 作物パラメータ（crop_irrigation.yamlから生成）
  ├─ [D] 制御ルール（ArSproutマニュアルから抽出）
  ├─ [E] 暗黙知（農家フィードバック+怒り駆動で蓄積）
  ├─ [F] 安全制約（絶対遵守）
  └─ [G] 出力形式（1時間アクション計画JSON）
```

> **v3.0変更**: [G]セクションを追加。LLMに1時間アクション計画JSONの出力形式を指示。

### 5.2 プロンプト全文（テンプレート）

```text
# [A] 役割定義
あなたは道央の温室環境制御AIです。
1時間ごとにセンサーデータを確認し、向こう1時間のアクション計画をJSON形式で生成します。
あなたの主な判断領域はCO2制御と露点管理です。灌水・側窓の基本制御はルールベースが担当しています。

# [B] ハウス固有情報
- ハウスID: h1
- 作物: 長ナス（水耕栽培・ココバッグ）
- 位置: 北緯42.888° 東経141.603° 標高21m
- アクチュエータ: UniPi 1.1 リレー ch1-8
  - ch4: 灌水電磁弁（必ずduration_sec指定）
  - ch5-8: 側窓開閉（詳細は§10参照）
  - ch1-3: 未割当（将来拡張用）
- 制御: POST /api/relay/{ch} value=1/0 duration_sec=秒
- 側窓は北側と南側で独立制御。風向を考慮して片側制御すること

# [C] 作物パラメータ（現在ステージ: 収穫盛期）
- 温度目標: 昼間25-28℃、夜間15-18℃
- EC: 1.8-2.0 mS/cm（ドサトロンで手動調整、制御対象外）
- 日射比例灌水閾値: 0.9 MJ/m²
- 灌水量: 270-300 ml/株
- 飽差(VPD)目標: 3-8 hPa
- CO2目標: 換気時は自然値、密閉時700ppm
- 降雨時灌水停止: 0.5mm/h以上で停止、30分後再開

# [D] 制御ルール
## 温度制御の目安
- 目標温度から+5℃以上で窓全開相当
- 1℃刻みで10%程度の出力変化を目安に

## 安全優先順位（上位が下位を上書き）
1. 強風時閉鎖: 風速≧5m/s の風上側を閉鎖
2. 気温急上昇: 20分で3℃以上上昇 → 開放
3. 温度超過: 絶対値で閾値超え → 開放
4. 降雨時閉鎖: 降雨検知 → 閉鎖

## 風向と片側制御
- 北風(NNW～NNE) + 風速≧5m/s → 北側閉鎖、南側は開放維持
- 南風(SSE～SSW) + 風速≧5m/s → 南側閉鎖、北側は開放維持
- 16方位: N=1, NNE=2, NE=3, ... NW=16

## 時間帯制御
- 日の出前: 側窓閉鎖（結露防止のため）
- 日の出後: PID制御開始
- 日没前1時間: 徐々に閉鎖開始
- 日没後: 全閉

# [E] 暗黙知（農家フィードバック）
- 外気湿度99%以上の夜間: 換気しても除湿効果なし。内外温度差を利用した
  循環ファンによる結露軽減のみ可能
- VPD>15hPa: ナスの気孔が閉じ光合成停止。灌水増量+ミストで飽差を下げる
- 雨天後の急な晴れ間: 日射急変で葉焼けリスク。遮光カーテン10-20%推奨
- CO2 218ppm以下は光合成の限界ライン。密閉して400ppm以上に回復を待つ
- 7月の灌水ピーク時: 灌水閾値を下げすぎると水浸しになる。実績データ参照

# [F] 安全制約（絶対遵守）
- 灌水・ミスト等のONは必ず duration_sec を指定すること（最大3600秒）
- 降雨中（rainfall > 0）は絶対に側窓を開けない
- 側窓の開閉は片側ずつ。両側同時操作しない
- 40℃超は緊急事態。全窓全開+ファンON（ただし緊急停止はLayer 1が先行実行済み）
- 5℃以下は凍結リスク。カーテン閉+暖房ON
- 制御不要と判断した場合は「現状維持」と明記し、何も操作しない
- ロックアウト中（GET /api/status の locked_out=true）はリレー操作しない

# [G] 出力形式
向こう1時間のアクション計画を以下のJSON形式で出力せよ:
{
  "summary": "判断の要約",
  "actions": [
    {"execute_at": "+0min", "relay_ch": N, "value": 0or1,
     "duration_sec": N, "reason": "理由"}
  ],
  "co2_advisory": "CO2に関する所見",
  "dewpoint_risk": "low/medium/high",
  "next_check_note": "次回チェック時の注意事項"
}
アクション不要なら actions を空配列にし、summary に「現状維持」と明記せよ。
```

### 5.3 怒り駆動開発: 暗黙知の収集・更新フロー

**農家の怒りが制御ロジックになる。**

```
LINE Botへのクレーム
  │  「朝の灌水が遅すぎる！」「窓開けっぱなしで寒い！」
  ▼
殿（普及員）がレビュー
  │  怒りの内容を制御ルールに翻訳
  ▼
system_prompt.txt [E]セクションに追記
  │  例: 「朝7時前の灌水開始は禁止。根が冷える」
  │  怒りの強さ → そのまま重み（何度も同じ苦情 = 重要度高）
  ▼
agriha_control.py 次回実行で自動反映
  │  コード変更不要。テキストファイル編集のみ
  ▼
農家ごとの経験則が蓄積 → その畑専用AIに育つ
```

**怒り駆動の効果**:
- 農家の暗黙知がsystem_prompt.txtに自然蓄積される
- 怒りの頻度 = 重要度。何度もクレームが来るルールほど上位に記載
- 農家ごとにsystem_prompt.txtが異なる → その畑に最適化されたAI
- 普及員はLLM育成係: クイズ回答で暗黙知を引き出し、全農家に展開

> **v3.0変更**: [E]セクションの収集フローに「怒り駆動開発」を正式導入。
> 暗黙知の重みは怒りの強さ（=苦情回数）で決まる。

### 5.4 トークン数見積もり

| セクション | 推定トークン数 |
|-----------|-------------|
| [A] 役割定義 | ~100 |
| [B] ハウス固有情報 | ~120 |
| [C] 作物パラメータ | ~150 |
| [D] 制御ルール | ~400 |
| [E] 暗黙知 | ~200（初期、蓄積で増加） |
| [F] 安全制約 | ~150 |
| [G] 出力形式 | ~150 |
| **合計** | **~1,270** |

Claude Haikuのコンテキストウィンドウは200Kトークン。余裕は十分。
システムプロンプト1,270 + 履歴300 + ツール定義500 + センサーデータ500
= 合計約2,570トークン。

---

## 6. ステート管理

### 6.1 二層ステート設計

```
┌────────────────────────────────────────────────┐
│  Layer S1: リレー物理状態                       │
│    - ch1-8のON/OFF状態                          │
│    - 管理主体: MqttRelayBridge (MCP23008 I2C)   │
│    - 取得方法: GET /api/status → relay_state    │
│    - MQTT: agriha/{house_id}/relay/state        │
│    - 動作中はラッチ維持（LLM停止でも状態保持）   │
│    - RPi再起動時はPORで全OFF初期化               │
└────────────────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│  Layer S2: 判断履歴 (control_log.db — SQLite)   │
│    - LLMの判断ログ（理由+アクション+センサー値）  │
│    - 直近3回の履歴を次回プロンプトに含める        │
│    - 管理主体: agriha_control.py                 │
│    - 保持場所: /var/lib/agriha/control_log.db    │
└────────────────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│  Layer S3: アクション計画 (current_plan.json)    │
│    - LLMが生成した1時間アクション計画JSON         │
│    - plan_executor.pyが計画を時刻通りに実行       │
│    - 管理主体: agriha_control.py（生成）          │
│    - 保持場所: /var/lib/agriha/current_plan.json │
│    - 毎時更新、次の予報で上書き                   │
└────────────────────────────────────────────────┘
```

> **v3.0変更**: Layer S3（アクション計画）を追加。
> 1時間予報方式に伴い、計画JSONを一時ファイルとして保持。

### 6.2 判断履歴DB (Layer S2) スキーマ

```sql
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,           -- ISO8601 UTC
    summary TEXT,                      -- LLMの最終応答（判断理由、500文字以内）
    actions_taken TEXT,                -- 実行したアクション一覧
    raw_response TEXT,                 -- LLMの生レスポンス（デバッグ用）
    sensor_snapshot TEXT               -- 判断時のセンサー値スナップショット
);

-- 直近3件取得用インデックス
CREATE INDEX idx_decisions_timestamp ON decisions(timestamp DESC);
```

### 6.3 LLMのコンテキスト戦略

| 方式 | メリット | デメリット |
|------|---------|-----------|
| **毎回リセット + 直近3件履歴** | メモリリーク無し、再現性高い | 長期トレンドを見れない |
| コンテキストウィンドウ保持 | 連続的な判断が可能 | cronで毎回起動するため不可能 |
| 全履歴をDBから注入 | 長期トレンドを参照 | トークン消費過大 |

**採用方式**: **毎回リセット + 直近3件の判断履歴をプロンプトに注入**。

cronで毎回新規プロセスを起動するため、コンテキストウィンドウの保持は不可能。
代わりにSQLiteから直近3件の判断サマリを読み込み、短期的な文脈を維持する。

> **将来（LLM自然減衰 中期）**: FTS5で過去の類似判断を検索し、プロンプトに注入する方式に拡張（§7参照）。

### 6.4 ログ保持ポリシー

- **直近30日**: 全レコード保持
- **30日超**: 日次サマリに集約（1日1レコード、主要判断のみ）
- **90日超**: 月次サマリに集約
- InfluxDB/Grafana経由でセンサー生データは別途保持

---

## 7. LLM自然減衰モデル

### 7.1 三段階ロードマップ

LLMへの依存度は時間とともに**自然に減衰**する。
最終的にLLMを呼ばなくても回る状態を目指す。

```
コスト
  ▲
  │ ■■■■■
  │ ■■■■■■■■
  │        ■■■■■■
  │              ■■■■■
  │                   ■■■■
  │                        ■■■
  │                            ■■
  │                               ■■
  │                                  ■→ ほぼゼロ
  └──────────────────────────────────────▶ 時間
    初期        中期          成熟期
    (月数百円)  (月~百円)     (月ほぼゼロ)
```

### 7.2 初期フェーズ（運用開始～）

- LLMに毎時予報させる（月数百円）
- 全ての判断をcontrol_log.dbに蓄積
- 判断パターン: CO2制御、露点対応、換気調整etc.
- この段階ではLLMが100%の判断を担当（Layer 3依存）

### 7.3 中期フェーズ（判断パターン蓄積後）

- control_log.dbに十分な判断履歴が蓄積
- FTS5（全文検索）で過去の類似状況を検索
- 類似判断をプロンプトに注入 → LLMは「過去の自分の判断」を参照して判断

```
今回の状況: 内温32℃、CO2 350ppm、側窓全開
    │
    ▼ FTS5検索
過去の類似判断: 「内温30-34℃ + CO2 300-400ppm + 側窓全開」
    → 過去5回中5回とも「現状維持、換気中のCO2は自然値で可」
    │
    ▼ プロンプトに注入
LLM: 「過去の判断と同様、現状維持」
```

- パターンが確立した判断はLLMを呼ばずにルールベース化の候補に
- LLM呼び出し頻度が徐々に減少（月~百円）

### 7.4 成熟期フェーズ（ルール蒸留完了後）

- 蓄積された判断パターンを**重み付きルール**に蒸留
- ルールベース（Layer 2）に統合
- LLMは「新しい状況」「前例のない組み合わせ」のみ呼び出し

```
蒸留されたルール例:
  IF 内温 > 30℃ AND CO2 < 400ppm AND 側窓 == 全開:
      → 現状維持（換気中のCO2は自然値で可）[weight: 0.95, n=47]
  IF 湿度 > 90% AND 内温 < 外温 + 2℃ AND 時間帯 == 日没後:
      → 露点リスク高。暖房ON推奨 [weight: 0.88, n=23]
```

- LLM呼び出し: 月数回（前例のない状況のみ）
- 月コスト: ほぼゼロ
- ローカルLLM蒸留（Qwen3.5-35B-A3B等）も成熟期の選択肢

> **重要**: LLM自然減衰は「LLMを捨てる」のではなく「LLMの知恵をルールに落とす」プロセス。
> 新しい作物や新しい環境条件ではLLMの出番が復活する。

---

## 8. 安全制御設計

### 8.1 4層安全モデル

> **v3.0変更**: v2.0の3層モデルに「緊急停止のLLM分離」を明示し4層に拡張。
> 緊急停止とLLMは**完全に別系統**。

```
┌─────────────────────────────────────────────────────────┐
│ Layer S-1: 物理層（即時、最優先）                         │
│   - 緊急スイッチ (UniPi DI07-DI14)                       │
│   → gpio_watch → CommandGate → 300秒ロックアウト         │
│   - ロックアウト中: REST API relay操作は全て 423 拒否     │
│   - 手動解除: POST /api/emergency/clear                   │
│   - LLMは一切関与しない                                   │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Layer S-2: 緊急停止（if文 + LINE curl、LLM非依存）       │
│   - emergency_guard.sh（cron 毎分）                       │
│   - 27℃超: 全窓全開 + LINE通知                           │
│   - 16℃以下: 全窓全閉 + LINE通知                         │
│   - LLMの応答を待たずに物理的に動く                       │
│   - LINE通知もLLMを通さない（if文+curlで直接叩く）        │
│   → ブレーカーと非常ベルは指導員がパニクっても動く        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Layer S-3: LLMプロンプト安全制約                          │
│   - system_prompt.txt [F]セクションで定義（§5.2参照）     │
│   - 降雨中の窓開禁止、過熱時全開、凍結防止etc.           │
│   - LLMが1時間ごとにセンサーを確認し安全制御を計画        │
│   - 応答時間: 数秒（API応答）。即時性はLayer S-2が担保    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Layer S-4: フォールバック（リレーラッチ + 自動OFFタイマー）│
│   - RPi停止 → リレーは最後の状態を保持                  │
│   - MqttRelayBridge duration_sec タイマー:                │
│     灌水ONなどは必ず自動OFF時間を指定                     │
│   - 最悪ケース: 灌水ON放置 → duration_sec で自動OFF      │
│   - RPi再起動時: リレーは初期状態(全OFF)に復帰           │
└─────────────────────────────────────────────────────────┘
```

### 8.2 緊急停止とLLMの完全分離

**設計原則: 緊急系統にLLMは一切入れない。**

```
× 旧設計（LLM依存）:
  センサー異常 → LLM判断 → 制御命令 → アクチュエータ
  問題: LLMハレーション時に危険な判断をする可能性

○ 新設計（LLM非依存）:
  センサー異常 → if文（閾値比較）→ 制御命令 → アクチュエータ
  同時に → LINE curl（LLMを通さない通知）→ 農家のスマホ
```

LLMがハレーションしても、Layer S-1とS-2は独立して動作する。
LLMが「大丈夫です、何もしなくていいです」と間違った判断をしても、
閾値超過ならif文が問答無用で動く。

### 8.3 LINE通知のLLM非依存

```
× 危険な設計:
  異常検知 → LLM「通知文を生成して」→ LINE API
  問題: LLMが通知を「不要」と判断する可能性

○ 安全な設計:
  異常検知 → if文 → curl -X POST (LINE API) → 固定テンプレート
  「🚨 内温{TEMP}℃ — 緊急{ACTION}」
```

通知文にLLMの創造性は不要。温度と動作を伝えるだけ。

### 8.4 フォールバック遷移

```
正常運転（LLMが毎時制御計画 + ルールベースが日常制御）
    │
    │ Anthropic API障害 / Starlink回線断
    ▼
Layer 2: ルールベースのみで運転（日常の95%はカバー）
    │ ├─ 日射比例灌水: 継続
    │ ├─ 温度閾値側窓: 継続
    │ └─ CO2/露点のLLM判断: 停止（次善策: 換気で自然値）
    │
    │ RPi自体が停止
    ▼
Layer S-4: リレー現状維持（MCP23008はラッチ型）
    │ ├─ 灌水ON中 → duration_sec タイマーで自動OFF
    │ ├─ 換気扇ON中 → 回しっぱなし（安全上問題なし）
    │ └─ 全OFF中 → そのまま（最も安全な状態）
    │
    │ RPi復旧
    │ → agriha_control.py cron再開
    │ → GET /api/status でリレー現状を確認
    │ → LLMが状況に応じて制御再開
    ▼
正常運転に復帰
```

### 8.5 オフラインフェイルセーフ

Starlink回線断時、RPi上のLayer 1+2が独立稼働:

| 状況 | 動作 | Layer |
|------|------|-------|
| 内温 > 27℃ | 全窓全開 | Layer 1 |
| 内温 < 16℃ | 全窓全閉 | Layer 1 |
| 降雨検知 | 窓系全閉 | Layer 2 |
| 日射比例閾値到達 | 灌水実行 | Layer 2 |
| その他 | 現状維持 | — |

LINE通知は回線断中は不達だが、制御自体は継続する。

---

## 9. アクチュエータ制御（UniPiリレー）

> ArSproutアクチュエータ制御（CCM経由）は全面廃止済み。
> 全アクチュエータをUniPi 1.1 MCP23008 I2Cリレー（ch1-8）経由で制御。

### 9.1 制御方式

UniPi 1.1のリレーは**単純ON/OFF型**。

| 制御方式 | 説明 | 例 |
|----------|------|-----|
| **ON/OFF** | リレーをON/OFFするだけ | 換気扇、暖房、灌水弁 |
| **duration付きON** | ONにして指定秒数後に自動OFF | 灌水（300秒）、ミスト（60秒） |

### 9.2 制御フロー

```
LLM判断（1時間アクション計画）: 「灌水5分実行」
    │
    ▼
agriha_control.py → REST API POST /api/relay/4
    payload: {"value": 1, "duration_sec": 300, "reason": "灌水5分"}
    │
    ├─ CommandGate チェック
    │   └─ ロックアウト中 → 423 拒否
    │
    ├─ MQTT publish: agriha/h01/relay/4/set
    │
    ▼
MqttRelayBridge
    ├─ MCP23008 I2C → リレーch4 ON
    ├─ MQTT publish: agriha/h01/relay/state (全ch状態)
    └─ 300秒タイマー開始 → 自動OFF
```

### 9.3 安全ガード

| ガード | 実装場所 | 動作 |
|--------|---------|------|
| **ロックアウト** | CommandGate | 緊急スイッチ検知で300秒間全操作拒否 |
| **duration_sec必須** | システムプロンプト [F] | 灌水/ミスト等はduration_sec指定を義務付け |
| **自動OFFタイマー** | MqttRelayBridge | duration_sec指定時、タイマーで自動OFF |
| **チャンネル範囲** | REST API Path validation | ch1-8以外は400エラー |
| **API認証** | REST API X-API-Key | 無認証アクセスを拒否（本番設定時） |

### 9.4 モーター付きアクチュエータの秒数制御

側窓・天窓・カーテンなどモーター駆動のアクチュエータは、リレーON時間で開度を制御する。

```
例: 側窓を30%開ける（全開60秒の場合）
    │
    ├─ Step 1: POST /api/relay/{ch} value=1, duration_sec=18
    │          → リレーON（モーター開方向に回転）
    │
    ├─ Step 2: 18秒後に自動OFF（MqttRelayBridgeタイマー）
    │          → モーター停止
    │
    └─ 注意: 位置はソフトウェア推定のみ（フィードバックなし）
             → システムプロンプトで「毎朝全閉リセット」をLLMに指示
```

**制限事項**:
- UniPiリレーは1ch=1方向。開/閉で別チャンネルが必要なアクチュエータは2ch使用
- 位置フィードバックなし（エンコーダ等は未接続）
- LLMが判断履歴から前回の操作を参照し、おおよその位置を推定する

---

## 10. リレーチャンネル割当

### 10.1 割当表

| ch | 用途 | 制御方式 | duration_sec | 備考 |
|----|------|---------|-------------|------|
| 1 | （未割当） | - | - | |
| 2 | （未割当） | - | - | |
| 3 | （未割当） | - | - | |
| 4 | 灌水電磁弁 | ON→duration後OFF | 60-600秒 | 必ずduration_sec指定 |
| 5 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 6 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 7 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 8 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |

> **側窓ch5-8の詳細**: 南北の開/閉でどのchがどの動作に対応するかは5月の実機確認で確定。
> 想定パターン: ch5=北側開, ch6=北側閉, ch7=南側開, ch8=南側閉 等（要実測）

### 10.2 残りchの候補

ch1-3 は未割当。以下の用途に割当可能:

| アクチュエータ候補 | 制御方式 | duration_sec目安 |
|------------------|---------|----------------|
| 換気扇 | ON/OFF | 0（手動OFF） |
| ミスト | ON→duration後OFF | 30-120秒 |
| 暖房 | ON/OFF | 0（手動OFF） |
| CO2バルブ | ON→duration後OFF | 60-300秒 |

### 10.3 確認タスク（5月予定）

1. ch5-8の側窓動作を実機確認（どのchが南/北/開/閉に対応するか）
2. ch4 灌水電磁弁のON/OFF動作確認、適切なduration_sec実測
3. ch1-3 の配線先を確認（未接続の場合は将来割当）
4. duration_secの適切な値を実測（側窓の全開秒数等）
5. システムプロンプト [B]セクションにch割当を記載
6. 本セクションの割当表を確定

---

## 11. RPiセットアップ手順

### 11.1 前提条件

- RPi (ArSprout RPi): Raspbian Lite, WireGuard VPN (10.10.0.10)
- ハウスLAN (192.168.1.0/24) に有線接続済み
- unipi-daemon REST API (http://localhost:8080) がRPi上で稼働
- Starlink回線経由でインターネット接続（Anthropic API用）

### 11.2 Python環境 + Anthropic SDK

```bash
# === Step 1: Python 3.11+ 確認 ===
python3 --version  # 3.11以上

# === Step 2: Anthropic SDK + HTTP クライアント ===
sudo pip install anthropic httpx pyyaml

# === Step 3: API キー設定 ===
# /etc/environment に追記（またはsystemd service Environment=）
echo 'ANTHROPIC_API_KEY=sk-ant-...' | sudo tee -a /etc/environment
```

### 11.3 制御スクリプト配置

```bash
# === Step 1: ディレクトリ作成 ===
sudo mkdir -p /opt/agriha-control
sudo mkdir -p /var/lib/agriha
sudo mkdir -p /etc/agriha
sudo mkdir -p /var/log/agriha

# === Step 2: スクリプト配置 ===
# agriha_control.py を配置（§4.3の内容）
sudo cp agriha_control.py /opt/agriha-control/

# emergency_guard.sh を配置（§1.2の内容）
sudo cp emergency_guard.sh /opt/agriha-control/
sudo chmod +x /opt/agriha-control/emergency_guard.sh

# システムプロンプト配置（§5.2の内容）
sudo cp system_prompt.txt /etc/agriha/

# === Step 3: unipi-daemon REST API 疎通確認 ===
curl http://localhost:8080/api/sensors
# → CCM + DS18B20 + Misol + relay 全センサーデータが返ること

curl http://localhost:8080/api/status
# → relay_state (ch1-8), locked_out, uptime_sec が返ること

# === Step 4: Anthropic API 疎通確認 ===
python3 -c "
import anthropic
c = anthropic.Anthropic()
r = c.messages.create(model='claude-haiku-4-5-20251001', max_tokens=50,
    messages=[{'role':'user','content':'hello'}])
print(r.content[0].text)
"
```

### 11.4 cronスケジュール

```bash
# /etc/cron.d/agriha-control

# Layer 3: LLM 1時間予報（毎時0分）
0 * * * * root flock -n /tmp/agriha_control.lock \
  /usr/bin/python3 /opt/agriha-control/agriha_control.py \
  >> /var/log/agriha/control.log 2>&1

# Layer 1: 緊急停止監視（毎分）
* * * * * root /opt/agriha-control/emergency_guard.sh \
  >> /var/log/agriha/emergency.log 2>&1
```

### 11.5 動作確認手順

```bash
# 1. unipi-daemon REST API確認
curl http://localhost:8080/api/sensors | python3 -m json.tool
# → sensors dict にCCM/DS18B20/Misol/relayデータが存在すること

# 2. Anthropic API確認
python3 -c "import anthropic; print('OK')"

# 3. 制御ループ手動テスト
python3 /opt/agriha-control/agriha_control.py
# → control_log.db に1レコード追加されていること確認
sqlite3 /var/lib/agriha/control_log.db "SELECT * FROM decisions ORDER BY id DESC LIMIT 1;"

# 4. 緊急停止テスト
/opt/agriha-control/emergency_guard.sh
# → 閾値内であれば何も起きないこと確認

# 5. cron実行確認（1時間待つ、または手動でcronをテスト）
tail -f /var/log/agriha/control.log
```

---

## 12. 参照ドキュメント

| ドキュメント | パス | 内容 |
|------------|------|------|
| unipi-daemon | ~/unipi-agri-ha/services/unipi-daemon/ | 5タスクasyncioデーモン（センサー+MQTT+GPIO+REST+CCM） |
| MQTTトピック仕様 | ~/unipi-agri-ha/docs/mqtt_topic_spec.md | agriha/名前空間定義、QoS、ペイロード例 |
| LLM制御ロードマップ | ~/unipi-agri-ha/docs/llm_control_roadmap.md | Phase3.5-6の全体像 |
| 新アーキテクチャ設計書 | ~/unipi-agri-ha/docs/mqtt_remote_arch.md | ネットワーク/UniPi |
| 作物灌水設定 | ~/unipi-agri-ha/config/crop_irrigation.yaml | ステージ別パラメータ |
| LLMベンチマークシナリオ | ~/unipi-agri-ha/docs/llm_benchmark_scenarios.md | LINE Bot判断力テスト |
| uecs-ccm-mcp（参考） | ~/uecs-ccm-mcp/ | CCM受信テストツール（制御には使用しない） |

---

## 付録A: v2.0→v3.2 変更履歴

### A.1 アーキテクチャ変更の背景

2026-02-28の殿との設計議論で、温室LLM制御の根本思想が転換された（v3.0）。
2026-03-02の設計議論で gradient_controller（勾配制御層）が追加された（v3.1）。
2026-03-02の部屋子リサーチ結果を統合し、天気予報API統合設計が追加された（v3.2）。

1. **三層構造の導入**: LLMは「知恵」層として最上位に位置し、下位層（ルールベース、緊急停止）が独立動作する設計に変更
2. **LLM責務の限定**: LLMの判断はCO2制御と露点管理の2場面のみ。灌水・側窓の基本制御はルールベースに委譲
3. **1時間予報方式**: cron 5分/10分間隔のリアルタイム制御から、1時間予報+計画実行方式に変更
4. **緊急系統のLLM分離**: 緊急停止とLINE通知をLLMから完全分離
5. **怒り駆動開発**: 農家フィードバックをsystem_prompt.txtに蓄積する仕組みを正式導入
6. **LLM自然減衰モデル**: LLM依存度が時間とともに自然減衰するロードマップを策定
7. **緊急停止閾値の見直し**: Layer 1の緊急停止閾値を40℃/5℃→27℃/16℃に変更（農家の実運用に即した現実的閾値）
8. **astral日時注入**: LLMは自力で日時・日照条件を把握不可のため、全API経路にastral（日の出/日没）+時間帯4区分を注入

### A.2 廃止された設計要素

| v2.0 設計要素 | 廃止理由 |
|--------------|---------|
| LFM2.5 (llama-server) | 対話能力不足。Claude Haiku APIに全面移行（2026-02-23殿裁定） |
| nuc.local (Intel N150) | RPi (10.10.0.10)に制御を一本化。中間層不要 |
| cron 5分間隔 | 1時間予報方式に変更（コスト削減+設計簡素化） |
| nipogi中間層 | RPi→Claude API直結。中間サーバー不要 |
| llama-server (localhost:8081) | Anthropic API (HTTPS)に全面移行 |
| N150ベンチマーク（§5旧） | ローカルLLM推論不要。クラウドAPI応答は数秒 |
| Ollamaシャドーモード | 2026-02-24殿判断で停止。vx2はベンチマーク専用に転用 |
| 緊急停止閾値 40℃/5℃ | 27℃/16℃に変更。農家の実運用に即した現実的閾値 |

### A.3 新規追加された設計要素

| バージョン | 設計要素 | 説明 |
|-----------|---------|------|
| v3.0 | 三層制御アーキテクチャ（§1） | 爆発/ガムテ/知恵の3層、各層独立動作保証 |
| v3.0 | LLM責務範囲（§2） | CO2制御と露点管理の2場面に限定 |
| v3.0 | 1時間予報+緊急フラグ方式（§3） | cron毎時予報+計画実行+緊急割り込み |
| v3.0 | Claude Haiku API接着層（§4） | Anthropic SDK、RPiから直接HTTPS |
| v3.0 | LLM自然減衰モデル（§7） | 初期→中期→成熟期の三段階、最終的にLLM不要化 |
| v3.0 | 怒り駆動開発（§5.3） | 農家クレーム→system_prompt.txt蓄積→制御ロジック |
| v3.0 | 機能優先順位（概要） | リモート制御→状態確認→異常通知→自動制御 |
| v3.0 | emergency_guard.sh（§1.2, §8） | LLM非依存の緊急停止+LINE通知 |
| v3.0 | Layer S3: アクション計画（§6.1） | current_plan.json による1時間計画管理 |
| v3.0 | 4層安全モデル（§8.1） | 物理層+緊急停止(if文)+LLMプロンプト+フォールバック |
| v3.0 | astral日時注入（§4.3） | 日の出/日没+時間帯4区分をLLMプロンプトに自動注入 |
| v3.0 | 緊急停止閾値27℃/16℃（§1.2, §8） | 旧閾値40℃/5℃から農家実運用に即した値に変更 |
| **v3.1** | **gradient_controller（§3.6）** | **forecast_engineとrule_engineの間の勾配制御層。3軸ゲイン+病害リスクスコア+LLM予報フォーマット改訂** |
| **v3.1** | **3軸ゲイン設計（§3.6.1）** | **気温・湿度・CO2の相矛盾するゲイン。priority順に重み配分。農家の腕をゲインで表現** |
| **v3.1** | **病害リスクスコア（§3.6.2）** | **dew_hours/dry_hoursの仮実装。インターフェース定義済み、将来データで差し替え** |
| **v3.1** | **LLM予報フォーマット改訂（§3.6.3）** | **target/priority/overrides/reasonを§3.3 actionsと併存。gradient_controllerへの入力仕様** |
| **v3.2** | **天気予報API統合設計（§3.7）** | **Open-Meteo選定（完全無料・APIキー不要・JMAモデル・日射量VPD付き）。weather_fetcher.py設計+TTL1hキャッシュ+フェイルセーフ** |
| **v3.2** | **API選定比較表（§3.7.1）** | **7候補比較。Open-Meteo◎/Visual Crossing○/気象庁△。商用利用問題は殿判断事項として明記** |
| **v3.2** | **天気予報フォーマット（§3.7.3）** | **1行/時間のトークン効率重視フォーマット。射量/降水/風向を簡潔に表現。gradient_controller間接連携** |
| **v3.2** | **実装ロードマップ（§3.7.5）** | **Step1-4: weather_fetcher.py新規+forecast_engine改修+layer3_config拡張+pytest追加** |

### A.4 継続利用される設計要素

| 設計要素 | 変更有無 |
|---------|---------|
| agriha_control.py | API呼び出し先をllama-server→Claude Haiku APIに変更、cron間隔を5分→1時間に変更 |
| システムプロンプト設計（§5） | [G]セクション追加（1時間予報出力形式）、[E]に怒り駆動開発を正式導入 |
| 判断履歴DB control_log.db（§6） | Layer S3（current_plan.json）を追加 |
| UniPi I2Cリレーch1-8（§9, §10） | 変更なし |
| unipi-daemon REST API | 変更なし |
| CommandGate 安全機構 | 変更なし（Layer S-1として位置づけ明確化） |
| MqttRelayBridge duration_sec | 変更なし（Layer S-4として位置づけ明確化） |
