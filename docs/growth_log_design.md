# カメラ+センサー紐づけログ（growth_log）設計書

> **Version**: 1.0.0-draft
> **策定日**: 2026-03-16
> **subtask**: subtask_909 / cmd_416
> **目的**: 定点カメラ画像とセンサーデータをペアで長期保存し、灌水量×生育ステージ相関分析の基盤を構築する

---

## 1. 概要

ナスの生育ステージを画像+センサーで記録し、シーズン終了後にバッチ分析する仕組み。

**解決する課題**:
- 現状の定点撮影（agriha-capture.sh）は24時間で削除され、長期記録が残らない
- センサーデータはMQTTの揮発キャッシュのみで、画像との時刻紐づけがない
- 灌水量×蒸散量×画像の相関データがないと生育ステージ推定仮説を検証できない

**スコープ外**: Vision AI分析の実装（本設計はデータ収集基盤のみ）

---

## 2. 現状分析

### 2-1. agriha-capture.sh の動作

| 項目 | 値 |
|------|-----|
| 実行間隔 | cron 5分毎 |
| 撮影コマンド | `rpicam-still --quality 85` |
| 保存先 | `/var/lib/agriha/photos/` |
| latest | `latest.jpg`（常時上書き） |
| アーカイブ | `YYYYMMDD_HHMMSS.jpg` |
| 削除ポリシー | 24時間超の `.jpg` を `find -mmin +1440 -delete` |
| 1日の枚数 | 288枚（24h × 12枚/h） |

### 2-2. nginx 画像公開パスの不一致

| 場所 | パス |
|------|------|
| capture.sh 保存先 | `/var/lib/agriha/photos/` |
| nginx.conf alias | `/var/lib/agriha/pictures/` |

**注記**: 実環境でどちらが正か要確認。シンボリックリンクで繋がっている可能性あり。growth_logは別ディレクトリのため影響なし。

### 2-3. センサーデータ取得方法

daemon REST API `GET /api/sensors` で全センサーの最新値を一括取得可能。

レスポンス構造:
```json
{
  "sensors": {
    "agriha/h01/sensor/DS18B20": {"device_id": "...", "temperature_c": 23.5, "timestamp": ...},
    "agriha/farm/weather/misol": {"temperature_c": 3.1, "humidity_pct": 88, ...},
    "agriha/h01/ccm/sensor/InAirTemp": {"value": 23.5, ...},
    "agriha/h01/ccm/sensor/InAirHumid": {"value": 65.0, ...},
    ...
  },
  "updated_at": 1771680000.0,
  "age_sec": 2.3
}
```

取得可能なセンサー種別（生育ログに関連するもの）:

| センサー | ccm_type / トピック | 単位 |
|---------|-------------------|------|
| ハウス内温度 | `InAirTemp` | ℃ |
| ハウス内湿度 | `InAirHumid` | % |
| ハウス内CO2 | `InAirCO2` | ppm |
| 日射量 | `InRadiation` | W/m² |
| 積算日射量 | `IntgRadiation` | MJ/m² |
| 土壌温度 | `SoilTemp` | ℃ |
| 土壌EC | `SoilEC` | dS/m |
| 土壌水分 | `SoilWC` | % |
| 灌水パルス | `Pulse` | 回 |
| 外気温 | Misol `temperature_c` | ℃ |
| 外湿度 | Misol `humidity_pct` | % |
| 日照(lux) | Misol `light_lux` | lux |
| 雨量 | Misol `rainfall_mm` | mm |

---

## 3. growth_log 仕様

### 3-1. ディレクトリ構成

```
/var/lib/agriha/growth_log/
├── realtime/              # リアルタイム用（5分間隔・7日保持）
│   ├── 20260401_080000.jpg
│   ├── 20260401_080000.json
│   ├── 20260401_080500.jpg
│   ├── 20260401_080500.json
│   └── ...
└── archive/               # 長期保存用（1時間間隔・シーズン保持）
    ├── 2026-04/
    │   ├── 20260401_0800.jpg
    │   ├── 20260401_0800.json
    │   ├── 20260401_0900.jpg
    │   ├── 20260401_0900.json
    │   └── ...
    └── 2026-05/
        └── ...
```

### 3-2. ファイル命名規則

| 用途 | 画像 | JSON |
|------|------|------|
| リアルタイム | `YYYYMMDD_HHMMSS.jpg` | `YYYYMMDD_HHMMSS.json` |
| アーカイブ | `YYYYMMDD_HHMM.jpg` | `YYYYMMDD_HHMM.json` |

ペアであることがファイル名で自明になる。

### 3-3. JSON スキーマ

```json
{
  "timestamp": "2026-04-01T08:00:00+09:00",
  "unix_ts": 1774854000,
  "camera": {
    "resolution": "640x480",
    "quality": 85
  },
  "sensors": {
    "in_air_temp": 23.5,
    "in_air_humid": 65.0,
    "in_air_co2": 450,
    "in_radiation": 320.0,
    "intg_radiation": 2.1,
    "soil_temp": 18.2,
    "soil_ec": 1.8,
    "soil_wc": 42.0,
    "pulse": 12
  },
  "weather": {
    "temperature_c": 15.2,
    "humidity_pct": 72,
    "light_lux": 45000.0,
    "rainfall_mm": 0.0,
    "wind_speed_ms": 2.1
  },
  "relay": {
    "ch4_irrigation": 1,
    "ch1_heater": 0,
    "ch5_north_win_open": 0,
    "ch8_south_win_open": 1
  }
}
```

| フィールド | 型 | 備考 |
|-----------|-----|------|
| `timestamp` | string | ISO 8601 JST |
| `unix_ts` | int | UNIX秒（ソート・検索用） |
| `camera.resolution` | string | 撮影解像度 |
| `camera.quality` | int | JPEG品質 |
| `sensors.*` | float\|int\|null | GET /api/sensors から取得。欠損時はnull |
| `weather.*` | float\|int\|null | Misol外気象。欠損時はnull |
| `relay.*` | int | 0=OFF, 1=ON。灌水・側窓等の状態 |

### 3-4. 間引きロジック

| 層 | 間隔 | 保持期間 | 用途 |
|----|------|---------|------|
| リアルタイム | 5分（capture.shと同期） | 7日 | ダッシュボード表示・直近確認 |
| アーカイブ | 1時間（毎時00分のペアをコピー） | シーズン終了まで（~180日） | 生育ステージ分析・Vision AI |

**間引き処理**:
- リアルタイム → アーカイブ昇格: cronで毎時、`*_??0000.{jpg,json}` をarchiveにコピー
- リアルタイム削除: cronで日次、7日超のファイルを削除

---

## 4. ストレージ試算

### 前提
- 解像度: 640x480
- JPEG品質: 85
- 1枚あたり: ~60KB（実測ベース推定）
- JSON: ~0.5KB/件
- シーズン: 180日（4月〜9月）

### リアルタイム層（最大7日分）

| 項目 | 計算 | 容量 |
|------|------|------|
| 画像 | 60KB × 288枚/日 × 7日 | ~118MB |
| JSON | 0.5KB × 288件/日 × 7日 | ~1MB |
| **小計** | | **~119MB** |

### アーカイブ層（シーズン全体）

| 項目 | 計算 | 容量 |
|------|------|------|
| 画像 | 60KB × 24枚/日 × 180日 | ~247MB |
| JSON | 0.5KB × 24件/日 × 180日 | ~2MB |
| **小計** | | **~249MB** |

### 合計

| 項目 | 容量 | 32GB SD比率 |
|------|------|------------|
| リアルタイム+アーカイブ | ~368MB | **1.1%** |
| 既存photos（24h分） | ~17MB | 0.05% |

**結論**: 32GB SDカードに対し1%強。容量問題なし。

---

## 5. 既存動作との共存

### 変更方針: capture.sh への追記を最小化

```diff
 #!/bin/bash
 # agriha-capture.sh — 定点カメラ撮影スクリプト
 set -euo pipefail

 PHOTO_DIR="/var/lib/agriha/photos"
+GROWTH_RT="/var/lib/agriha/growth_log/realtime"
 LATEST="${PHOTO_DIR}/latest.jpg"
-ARCHIVE="${PHOTO_DIR}/$(date +%Y%m%d_%H%M%S).jpg"
+TS=$(date +%Y%m%d_%H%M%S)
+ARCHIVE="${PHOTO_DIR}/${TS}.jpg"

 mkdir -p "$PHOTO_DIR"
+mkdir -p "$GROWTH_RT"

 rpicam-still --nopreview --output "$LATEST" --timeout 2000 --quality 85
 cp "$LATEST" "$ARCHIVE"

+# growth_log: 画像コピー + センサーJSON取得
+cp "$LATEST" "${GROWTH_RT}/${TS}.jpg"
+curl -sf http://localhost:8080/api/sensors \
+  | python3 -c "
+import sys, json
+from datetime import datetime
+raw = json.load(sys.stdin)
+s = raw.get('sensors', {})
+def ccm(t): v = s.get(f'agriha/h01/ccm/sensor/{t}', {}); return v.get('value')
+misol = s.get('agriha/farm/weather/misol', {})
+relay = s.get('agriha/h01/relay/state', {})
+out = {
+  'timestamp': datetime.now().astimezone().isoformat(),
+  'unix_ts': int(datetime.now().timestamp()),
+  'camera': {'resolution': '640x480', 'quality': 85},
+  'sensors': {
+    'in_air_temp': ccm('InAirTemp'), 'in_air_humid': ccm('InAirHumid'),
+    'in_air_co2': ccm('InAirCO2'), 'in_radiation': ccm('InRadiation'),
+    'intg_radiation': ccm('IntgRadiation'), 'soil_temp': ccm('SoilTemp'),
+    'soil_ec': ccm('SoilEC'), 'soil_wc': ccm('SoilWC'), 'pulse': ccm('Pulse')
+  },
+  'weather': {
+    'temperature_c': misol.get('temperature_c'), 'humidity_pct': misol.get('humidity_pct'),
+    'light_lux': misol.get('light_lux'), 'rainfall_mm': misol.get('rainfall_mm'),
+    'wind_speed_ms': misol.get('wind_speed_ms')
+  },
+  'relay': {
+    'ch4_irrigation': relay.get('ch4', 0), 'ch1_heater': relay.get('ch1', 0),
+    'ch5_north_win_open': relay.get('ch5', 0), 'ch8_south_win_open': relay.get('ch8', 0)
+  }
+}
+json.dump(out, open('${GROWTH_RT}/${TS}.json', 'w'), ensure_ascii=False)
+" 2>/dev/null || echo "$(date -Iseconds) WARN: sensor取得失敗（画像のみ保存）"

 # 古いアーカイブを削除（24時間超）
 find "$PHOTO_DIR" -name "*.jpg" ! -name "latest.jpg" -mmin +1440 -delete

 echo "$(date -Iseconds) 撮影完了: $LATEST"
```

### 追加cronジョブ

```cron
# growth_log アーカイブ昇格（毎時5分）
5 * * * * agriha /usr/local/bin/agriha-growth-archive.sh >> /var/log/agriha/growth_archive.log 2>&1

# growth_log リアルタイム削除（毎日3:00）
0 3 * * * agriha find /var/lib/agriha/growth_log/realtime -name "*.jpg" -o -name "*.json" -mtime +7 -delete
```

### agriha-growth-archive.sh（新規・小スクリプト）

```bash
#!/bin/bash
# 毎時00分のペアをarchiveにコピー
set -euo pipefail
RT="/var/lib/agriha/growth_log/realtime"
MONTH=$(date +%Y-%m)
ARCHIVE="/var/lib/agriha/growth_log/archive/${MONTH}"
mkdir -p "$ARCHIVE"
# 直近1時間以内の毎時00分ファイルを探す
for f in "${RT}/"*_??0000.jpg; do
  [ -f "$f" ] || continue
  BASE=$(basename "$f" .jpg)
  SHORT="${BASE:0:13}"  # YYYYMMDD_HHMM
  cp -n "$f" "${ARCHIVE}/${SHORT}.jpg"
  [ -f "${RT}/${BASE}.json" ] && cp -n "${RT}/${BASE}.json" "${ARCHIVE}/${SHORT}.json"
done
```

### 既存機能への影響

| 既存機能 | 影響 |
|---------|------|
| latest.jpg（ダッシュボード表示） | 変更なし |
| photos/ 24時間アーカイブ | 変更なし（削除ポリシーそのまま） |
| nginx /picture/ 公開 | 変更なし（growth_logは別パス） |
| daemon REST API | 読み取りのみ（curlで1回GETするだけ） |

---

## 6. 将来のVision AI分析

growth_log/archive/ のJPG+JSONペアは以下のバッチ分析に利用可能:

| 分析 | 入力 | 目的 |
|------|------|------|
| 生育ステージ推定 | 画像（葉面積変化） + sensors.pulse + weather.light_lux | 蒸散係数の推定 |
| 灌水効率分析 | sensors.soil_wc + relay.ch4_irrigation + 画像 | 灌水量と土壌水分の相関 |
| 異常検知 | 画像差分 + sensors全体 | 病害・萎れの早期発見 |

**形式の利点**: 1ファイルペア（.jpg + .json）で完結。DBやAPIへの依存なし。`jq` + `find` でフィルタリング可能。

---

## 7. 実装計画（Wave2で必要な変更）

| # | 対象ファイル | 変更内容 | 新規/既存 |
|---|------------|---------|----------|
| 1 | `scripts/agriha-capture.sh` | growth_log書き込み追加（§5のdiff） | 既存変更 |
| 2 | `scripts/agriha-growth-archive.sh` | アーカイブ昇格スクリプト | 新規 |
| 3 | `config/cron.d/agriha-growth` | cron定義（archive昇格+realtime削除） | 新規 |

**変更しないもの**:
- daemon（REST APIは読み取りのみ利用）
- agriha-chat（UI変更なし）
- nginx.conf（growth_logの公開は不要）

---

## 改版履歴

| バージョン | 日付 | 変更内容 |
|-----------|------|---------|
| 1.0.0-draft | 2026-03-16 | 初版策定（subtask_909/cmd_416） |
