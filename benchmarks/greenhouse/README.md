# 温室制御LLMベンチマーク

長ナス農家専属AIの判断品質を測定するベンチマークスイート。
system_prompt.txtに記載された農家知恵ルール(4ルール)への対応を7シナリオで評価する。

## シナリオ一覧

| ID | カテゴリ | シナリオ名 | 対応ルール |
|----|---------|-----------|-----------|
| S01 | 殿指定 | 外部湿度100%正しい無視 | ルール1 |
| S02 | 殿指定 | 気温急上昇先読み開放 | ルール2 |
| S03 | 殿指定 | 朝の湿度優先開放 | ルール3 |
| S04 | 追加 | CO2低下パルス換気（雨天） | ルール4 |
| S05 | 追加 | 高温緊急全窓全開 | 基本ルール |
| S06 | 追加 | 夜間低温・暖房ON+全窓閉 | 基本ルール |
| S07 | 追加 | 北風強風・方角別制御 | 基本ルール |

## 評価軸

1. **論理整合性** (0-10点/シナリオ): set_relay の呼び出しが期待通りか
2. **JSON構文検証**: LLM応答テキストにJSONが含まれる場合にパース可能か
3. **時間軸リニア性**: 複数ステップのプランがタイムスタンプ順か

合格ライン: 各シナリオ6点以上 (60%)

## 使い方

```bash
# NullClaw (デフォルト、agriha-nullclaw-proxy が起動していること)
cd /home/yasu/uecs-llm
python benchmarks/greenhouse/runner.py

# Claude Haiku
python benchmarks/greenhouse/runner.py \
  --base-url https://api.anthropic.com/v1 \
  --model claude-haiku-4-5-20251001 \
  --api-key $ANTHROPIC_API_KEY

# 特定シナリオのみ + 詳細ログ
python benchmarks/greenhouse/runner.py --scenarios S01,S03 --verbose

# 結果をJSONに保存
python benchmarks/greenhouse/runner.py \
  --output results/nullclaw_$(date +%Y%m%d).json
```

## 依存パッケージ

- `openai` (既にpyproject.tomlに含まれる)

## ファイル構成

```
benchmarks/greenhouse/
  scenarios/
    S01_outside_humidity_ignore.json    # 殿指定: 外部湿度無視
    S02_temp_spike_preemptive.json      # 殿指定: 気温急上昇先読み
    S03_morning_humidity_priority.json  # 殿指定: 朝の湿度優先
    S04_co2_pulse_ventilation.json      # CO2パルス換気（雨天）
    S05_heat_emergency.json             # 高温緊急全窓全開
    S06_night_heating.json              # 夜間低温暖房ON
    S07_wind_direction.json             # 強風方角別制御
  runner.py     # ベンチマーク実行スクリプト (CLI)
  evaluate.py   # 評価ロジック (スコアリング)
  README.md     # このファイル
```

## スコアリング詳細

各シナリオは `grading.criteria` に複数の評価基準を持つ。
set_relay の呼び出し結果（チャンネル・値・duration_sec）と比較し、
最も高い点数の基準に一致したものをスコアとする。

`grading.forbidden_conditions` に該当する場合はペナルティが適用される。

### S04 パルス換気の評価について

CO2パルス換気シナリオ(S04)では `duration_sec > 0` の set_relay を
呼んだ場合に満点(10点)、duration_secなしの場合は7点となる。
これにより「パルス換気の概念を理解しているか」を測定できる。
