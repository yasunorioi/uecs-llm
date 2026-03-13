# AgriHA v4 仕様書（§6-9）

> Part 2 of 2. §1-5は agriha_v4_spec.md を参照
> Version: 4.0-draft
> Generated: 2026-03-04
> Source: Memory MCP tono-preferences + v3-rebuild実コード + cmd_304 research

---

## §6 設計思想

本システムの設計思想は、開発者（殿）の農業現場での実体験と哲学に基づく。
技術的な正しさよりも「農家が使い続けられること」を最優先とする。

### §6.1 マクガイバー精神

> 「どんなに時間と金があってもガムテと爆発がいい」

- **シンプル・ローコスト・手元の道具で解決する**のが設計の根幹
- RPi 1台 + たまにHaiku API呼び出しで、農家の月額コストはほぼゼロ
- ハードウェア10万円（設備投資）は腹が決まるが、月額1,000円（ランニングコスト）は永遠に気になる — これが農家の心理
- 過剰な抽象化・設定項目・エラーハンドリングは不要
- **動いているものを触るな**。完璧な設計よりも、今動いているガムテープの方が価値がある

### §6.2 三層構造（爆発→ガムテ→知恵）

温室制御の全体アーキテクチャは三層で構成される。下層ほど確実で、どの層が欠けても下の層が支える。

```
Layer 3: 知恵（LLM）   — forecast_engine.py
  │  1時間予報+たまに相談。毎時0分にHaiku呼び出し
  │  月数百円。高札検索ヒット時はLLMスキップ
  │
Layer 2: ガムテ（ルールベース）— rule_engine.py
  │  cron 5分毎。ピタゴラスイッチ段階制御+日射比例灌水+降雨/強風ルール
  │  rules.yaml + channel_map.yaml + window_position.py で設定。API代ゼロ
  │
Layer 1: 爆発（緊急停止）— emergency_guard.sh
     cron 毎分。bash+curl のみ（Python不要）
     40℃超→全窓全開+lockout+LINE通知
     5℃以下→全窓全閉+lockout+LINE通知
```

**階層原則（殿裁定 2026-03-01）**: 下層が上層を黙らせる。

| 状態 | Layer 1 | Layer 2 | Layer 3 |
|------|---------|---------|---------|
| lockout中 | 緊急操作のみ | 全操作スキップ | 計画生成スキップ |
| rain_flag中 | — | 全側窓閉鎖維持 | plan_executorが側窓操作スキップ |
| wind_flag中 | — | 片側制御 | plan_executorが側窓操作スキップ |
| 正常時 | 監視のみ | PID+ルール制御 | 1時間予報+PIDオーバーライド |

例外ルートを作ると階層化の意味がなくなる。LLMは制御の主役ではなく「たまに相談する天気予報士」。

### §6.3 怒り駆動開発

> LINE Botへのクレーム → system_prompt.txt に追記 → 制御ロジックになる

- 農家の怒りがそのまま重みになる
- 「なんで窓閉めなかったんだ！」→ system_prompt.txt に閾値条件が追記される
- 農家ごとの経験則が蓄積され、**その畑専用AI**に育つ
- system_prompt.txt が本体であり、実行環境（Claude API）は差し替え可能（殿裁定 2026-02-24）
- アプリ作者が先回りして判断ルールを作り込むのは過剰設計。**器を用意して怒り駆動で育てる**

### §6.4 LLM自然減衰モデル

LLMのAPI呼び出し頻度は運用とともに自然減衰する。

```
時期      LLM呼び出し   API月額     状態
────────  ────────────  ─────────  ──────────────────
初期      毎時予報      月数百円    判断パターン蓄積中
中期      高札ヒット増  月数十円    頻出パターンをルールに蒸留
後期      ほぼ不要      ほぼゼロ    rules.yamlが判断の大半を処理
```

forecast_engine.py の高札検索（`get_kousatsu_search`）が類似結果3件以上を返した場合、
`build_plan_from_search_results` でLLMをスキップする。これが自然減衰の仕組み。

### §6.5 怒り誘発設計

> うまく回りすぎると人間が放置する問題への対策

- LLMがわざとウザい提案をする → 農家が「ちげーよ！」→ **最高品質の教師データ**
- 確信度が低い提案もあえて出す（間違っていれば農家が訂正してくれる）
- 3日無反応なら疑問を投げかける（ナッジ）
- 完璧な制御は農家を無関心にする。**適度な不完全さ**が人間の関与を引き出す

### §6.6 機能優先順位（殿明言 2026-02-28）

| 優先度 | 機能 | 状態 |
|--------|------|------|
| 1位 | 「開けろ/閉めろ」リモート制御 | **稼働済み**（LINE Bot経由） |
| 2位 | 「今どうなってる？」状況確認 | **稼働済み**（LINE Bot + Chat窓） |
| 3位 | 異常値の早期通知 | 設計済み（1時間予報+緊急フラグで実現可能） |
| 4位 | 自動制御 | **設計・実装中**（三層構造） |

LINE Botが一番大事な機能。自動制御は農家が不在時の保険。

### §6.7 温室版shogun最小構成

shogunシステム（7エージェント）の知見が温室制御に転用される。

| shogunエージェント | 温室コンポーネント | 実装 |
|-------------------|-------------------|------|
| 将軍+家老 | 不要 | system_prompt.txt が方針を持つ |
| 足軽 | Haiku 1体 | forecast_engine.py（Claude Haiku） |
| 高札 | SQLite+FTS5 | 高札API（類似判断検索） |
| お針子 | 閾値監視cron | emergency_guard.sh + ルールエンジン |
| 没日録 | 判断ログDB | control_log.db |

LLMすら不要かもしれない。定型SQLチェック（emergency_guard.sh）+ FTS5突合（高札）+ 文脈判断のみLLM。
Googleの「もしかして」精神。

### §6.8 LLMのクリティカル判断場面

LLMの出番は多くて10回/日。真にLLMが必要な場面は2つだけ:

1. **CO2管理**: 換気との相反（窓を開けるとCO2が下がる）、総合判断が必要
2. **露点管理**: 結露=病気。暖房か換気かの二択で、外気湿度・内温・露点温度の総合判断が必要

それ以外:
- 灌水 → タイマー+日射比例（rule_engine.py `_run_irrigation`）
- 側窓 → 温度閾値+PID（rule_engine.py `GradientController`）
- EC → ドサトロン手動（制御対象外、ガイド表示のみ）

---

## §7 蒸留パイプライン仕様

> **ステータス: 未実装（構想段階）**
> 本節は将来実装のための仕様記述であり、v3-rebuildブランチには実装が存在しない。

### §7.1 概要

LLMの判断パターンをルールベースに「蒸留」し、API呼び出しを段階的に削減するパイプライン。
§6.4 LLM自然減衰モデルの実現手段。

### §7.2 データフロー

```
forecast_engine.py
  │ LLM判断 → current_plan.json 保存
  │
  ▼ control_log.db に判断ログ蓄積
  │
  ▼ 高札API (FTS5) で過去の類似判断を検索
  │   └─ build_search_query(): {季節}_{時間帯}_{温度バンド}_{天気}
  │
  ▼ 頻度分析（未実装）
  │   └─ 同一条件で同一判断が N回繰り返された場合、パターン化
  │
  ▼ ルール候補生成（未実装）
  │   └─ rules.yaml に追加可能な形式でルール候補を生成
  │
  ▼ 農家承認（反省会モード経由、§8参照）
  │   └─ 承認されたルール → rules.yaml に追加
  │   └─ 却下されたルール → 削除
  │   └─ 未承認30日 → 自動削除（腐敗）
  │
  ▼ rule_engine.py がルール処理 → LLM呼び出し不要に
```

### §7.3 蒸留の入口と出口

| 項目 | ファイル | 役割 |
|------|---------|------|
| **入口** | `services/agriha-control/forecast_engine.py` | LLM判断の生成元。`call_claude_haiku()` の出力 |
| **蓄積** | `control_log.db` (decisions テーブル) | 判断履歴の永続ストレージ |
| **検索** | 高札API (`/search`) | FTS5で類似判断検索。`build_search_query()` がクエリ構築 |
| **出口** | `config/rules.yaml` | rule_engine.py が参照するルール定義 |
| **フィードバック** | `config/system_prompt.txt` | 蒸留結果をLLMの指示文にも反映 |

### §7.4 既存実装で蒸留に使える部分

v3-rebuildブランチには蒸留パイプラインの「前半」が既に実装されている:

1. **検索クエリ構築**: `forecast_engine.py:build_search_query()` — `{季節}_{時間帯}_{温度バンド}_{天気}` 形式
2. **高札検索**: `forecast_engine.py:get_kousatsu_search()` — FTS5全文検索
3. **LLMスキップ判定**: `forecast_engine.py:is_sufficient_similar_results()` — 類似結果3件以上でスキップ
4. **検索ログ**: `forecast_engine.py:log_search()` — search_log.jsonl にクエリ・ヒット数・スキップ有無を記録

**未実装部分**:
- 頻度分析（search_log.jsonl の集計→パターン抽出）
- ルール候補生成（パターン→rules.yaml形式への変換）
- 農家承認フロー（反省会モード、§8）
- 自動削除（30日腐敗）

### §7.5 検索クエリ設計

`build_search_query()` の構造化クエリ:

```
{季節}_{時間帯}_{温度バンド}_{天気}
例: 夏_午前_25-30℃_Clear
例: 冬_早朝_0-5℃_Snow
```

| フィールド | 値域 | ソース |
|-----------|------|--------|
| 季節 | 春/夏/秋/冬 | `datetime.now().month` |
| 時間帯 | 早朝/午前/午後/夜間 | `datetime.now().hour` (0-5/6-11/12-17/18-23) |
| 温度バンド | 5℃刻み | `sensors["indoor_temp"]` |
| 天気 | Visual Crossing API | forecast条件 |

### §7.6 未承認ルール候補の腐敗設計

- ルール候補は生成時にタイムスタンプを持つ
- 農家が反省会モード（§8）で承認/却下しなかった候補は **30日で自動削除**
- 理由: 古いルール候補は状況が変化している可能性が高い。永遠に溜まり続けるのを防ぐ
- 削除されたルール候補は再度同じパターンが出現すれば再生成される

---

## §8 反省会モード仕様

> **ステータス: 未実装（構想段階）**
> 本節は将来実装のための仕様記述であり、v3-rebuildブランチには実装が存在しない。

### §8.1 概要

LLMの判断を農家に振り返ってもらい、教師データを収集する仕組み。
§7 蒸留パイプラインの「農家承認」フェーズを担う。

**設計の核心**: 農家にとって「だるくない」こと。文章回答は求めず、**タップ1回が教師データ**。

### §8.2 トリガーとフロー

```
毎週月曜朝（config可変）
  │
  ▼ LINE Bot が農家に反省会メッセージ送信
  │   「先週のAI判断で気になったのはこれです。どう思いますか？」
  │
  ▼ 表示件数: 1〜3件に絞り込み（農家の注意力は有限）
  │
  ▼ 各件について選択式 A/B/C を提示
  │   例: 「3/2 08:00 内温27℃で南窓を開けました」
  │       A. 正しい判断だった
  │       B. もっと早く開けるべきだった
  │       C. 開けなくてよかった
  │
  ▼ 農家がタップ1回で回答
  │
  ▼ 回答データ → 蒸留パイプライン（§7）に投入
```

### §8.3 無反応対応

| 経過 | アクション |
|------|-----------|
| 1週間 | 通常通り次の反省会を送信 |
| 2週間無反応 | ウザく聞く（ナッジ）: 「先週も先々週も聞いてるんですけど…」 |
| 4週間無反応 | 頻度を下げる（月1回に自動切替） |

### §8.4 頻度設定

`config/reflection.yaml`（未実装）で設定:

```yaml
reflection:
  frequency: weekly    # weekly / monthly / daily（「変態モード」）
  day_of_week: monday
  time: "07:00"
  max_items: 3
  expiry_days: 30      # 未承認ルール候補の腐敗日数
```

### §8.5 データモデル

```yaml
reflection_memo:
  id: integer               # 自動採番
  created_at: datetime      # 生成日時
  question: string          # LLMが生成した質問文
  options:                  # 選択肢（常に3択）
    - label: "A"
      text: "正しい判断だった"
    - label: "B"
      text: "もっと早く開けるべきだった"
    - label: "C"
      text: "開けなくてよかった"
  context:                  # 判断時のコンテキスト
    timestamp: datetime     # 元の判断日時
    sensors: object         # その時のセンサー値
    action_taken: string    # 実行したアクション
  answer: string | null     # 農家の回答（null=未回答）
  answered_at: datetime | null
  promoted_to_rule: boolean # rules.yaml に昇格済みか
  expired: boolean          # 30日腐敗で自動削除済みか
```

### §8.6 記録の核心

**観察→思考→仮定→行動**の4点セットが記録の価値。

| 要素 | 内容 | 例 |
|------|------|-----|
| 観察 | センサー値+天気 | 内温27℃、外温20℃、日射0.8MJ、風速2m/s |
| 思考 | LLMの判断根拠 | 「内温が目標上限に近い。風は穏やか」 |
| 仮定 | 前提条件 | 「向こう1時間は晴れ続きと予報」 |
| 行動 | 実行アクション | 南側窓開放 |

反省会で農家が訂正する（B/Cを選択する）とき、修正されるのは**仮定**の部分。
「向こう1時間晴れ続きと思ったが、実は急に曇った」→ 予報精度の問題なのか、判断基準の問題なのかを区別できる。

---

## §9 channel_map.yaml 仕様（他農家ポータビリティ）

### §9.1 概要

`config/channel_map.yaml` は、UniPiリレーのチャンネル番号と制御機器のマッピングを定義する外部設定ファイル。
ソースコードにch番号をハードコードしないための設計であり、**他農家への展開時にこのファイルだけを書き換えればよい**。

### §9.2 現在の構造

```yaml
# config/channel_map.yaml（v3-rebuild実コードより転記）

irrigation:
  channel: 4
  label: "灌水ポンプ"

side_window:
  south:
    channels: [5, 6]
    label: "南側窓"
  north:
    channels: [7, 8]
    label: "北側窓"

relay_labels:
  1: "暖房"
  2: "循環扇"
  3: "CO2発生器"
  4: "灌水ポンプ"
  5: "南側窓"
  6: "南側窓"
  7: "北側窓"
  8: "北側窓"

valid_channels:
  min: 1
  max: 8
```

### §9.3 channel_config.py API

`services/agriha-control/channel_config.py` が全Python制御スクリプト共通のローダー。

| 関数 | 戻り値 | 用途 |
|------|--------|------|
| `load_channel_map(path?)` | `dict` | YAML全体を読み込み。優先順: 引数 > `/etc/agriha/` > リポジトリ `config/` |
| `get_window_channels(config?)` | `list[int]` | 全窓ch（南+北）。例: `[5, 6, 7, 8]` |
| `get_south_channels(config?)` | `list[int]` | 南側窓ch。例: `[5, 6]` |
| `get_north_channels(config?)` | `list[int]` | 北側窓ch。例: `[7, 8]` |
| `get_irrigation_channel(config?)` | `int` | 灌水ch。例: `4` |
| `get_relay_labels(config?)` | `dict[int, str]` | ラベル辞書。例: `{1: "暖房", ...}` |
| `get_valid_channel_range(config?)` | `tuple[int, int]` | 有効ch範囲。例: `(1, 8)` |

**fallbackロジック**:
1. 引数 `path` が指定されていればそのパス（テスト用）
2. `/etc/agriha/channel_map.yaml`（デプロイ先）
3. リポジトリの `config/channel_map.yaml`（開発時）

### §9.4 参照箇所

channel_map.yaml は以下のコンポーネントから参照される:

| コンポーネント | 参照方法 | 使用目的 |
|---------------|---------|---------|
| `rule_engine.py` | `channel_config.py` import | 窓ch・灌水chの取得、リレー操作 |
| `emergency_guard.sh` | python3 one-liner で YAML パース | 窓chリスト取得（全窓全開/全閉） |
| `agriha_chat.py` | YAML直接読み込み (`_load_channel_map`) | ダッシュボードのリレーラベル表示 |
| `plan_executor.py` | `channel_config.py` import | WINDOW_CHANNELS, VALID_CH_MIN/MAX |
| `dashboard.js` | `/api/channel_map` エンドポイント経由 | Chart.jsタイムラインのy軸ラベル |

**emergency_guard.sh のYAML読み込み方式**:
```bash
WINDOW_CHANNELS=$(python3 -c "
import yaml
with open('${CHANNEL_MAP}') as f:
    cfg = yaml.safe_load(f)
chs = cfg['side_window']['south']['channels'] + cfg['side_window']['north']['channels']
print(' '.join(str(c) for c in chs))
")
```
bash スクリプトだがYAMLパースのみpython3を使用。この部分はPython不在時のawkフォールバック未実装（TODO）。

### §9.5 他農家でのカスタマイズ

#### ch番号の変更（配線が違う場合）

別農家のハウスでリレー配線が異なる場合、channel_map.yaml のch番号を変更する。

```yaml
# 例: 別農家の配線
irrigation:
  channel: 2           # ch2が灌水
side_window:
  south:
    channels: [3, 4]    # ch3-4が南側窓
  north:
    channels: [5, 6]    # ch5-6が北側窓
```

ソースコードの変更は不要。channel_config.py が動的にchを取得するため。

#### ラベルの変更（作物が違う場合）

```yaml
# 例: トマト農家
relay_labels:
  1: "暖房"
  2: "灌水ポンプ"        # 灌水chが違う
  3: "CO2発生器"
  4: "遮光カーテン"      # ナスにはない設備
  5: "南側窓"
  6: "南側窓"
  7: "天窓"              # 側窓ではなく天窓
  8: "天窓"
```

#### チャンネル数の変更（リレーが8ch以外の場合）

```yaml
# 例: UniPi Neuron（14chリレー）
valid_channels:
  min: 1
  max: 14
```

`valid_channels` はダッシュボードの表示範囲とバリデーションに使用される。
REST API の `/api/relay/{ch}` のパス制約（`ge=1, le=8`）は `rest_api.py` でハードコードされているため、
8ch超の場合はコード変更が必要（TODO: valid_channelsから動的に設定）。

### §9.6 デプロイ手順

```bash
# 1. channel_map.yaml を編集（リポジトリ側）
vim config/channel_map.yaml

# 2. デプロイ先にコピー
sudo cp config/channel_map.yaml /etc/agriha/channel_map.yaml

# 3. サービス再起動（設定を再読み込み）
sudo systemctl restart agriha-chat
```

> **注記**: 制御スクリプト(emergency_guard/rule_engine/forecast_engine/plan_executor)はcronジョブのためsystemd再起動不要。channel_map.yaml変更は次回cron実行時に自動反映。

`load_channel_map()` はプロセス起動時に1回だけ読み込む（rule_engine.pyのモジュールレベル変数 `_ch_config`）。
設定変更後はサービス再起動が必要。ホットリロードは未対応（TODO: ファイル変更検知）。
