#!/bin/bash
#===============================================================================
# ArSprout CSV 一括インポートスクリプト
#
# data/arsprout_*.csv を全てInfluxDBにインポートする。
# ファイル名からmeasurement名を自動抽出。
#
# 使用例:
#   bash import_all_arsprout.sh                    # 実行
#   bash import_all_arsprout.sh --dry-run          # ドライラン
#===============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="/home/yasu/unipi-agri-ha/data"
IMPORT_SCRIPT="$SCRIPT_DIR/import_arsprout_csv.py"

# InfluxDB接続情報
INFLUX_URL="http://localhost:8086"
INFLUX_ORG="agriha"
INFLUX_BUCKET="sensors"
INFLUX_TOKEN="kH2MEND9UltLDk2Hn1Gy_qVglN3WajHwHi2ZmvIUierDLX7w5IpWRaBkaiahSPIX32Wnt3fB7dMr2rppOLA-Qw=="

# 引数
DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo "=== DRY RUN モード ==="
fi

echo "========================================"
echo " ArSprout CSV 一括インポート"
echo "========================================"
echo "データディレクトリ: $DATA_DIR"
echo ""

SUCCESS=0
FAIL=0
SKIP=0

for csv_file in "$DATA_DIR"/arsprout_*.csv; do
    [ -f "$csv_file" ] || continue

    filename=$(basename "$csv_file")

    # logic_config_dump は除外
    if [[ "$filename" == *"logic_config"* ]]; then
        echo "SKIP: $filename (設定ダンプ)"
        SKIP=$((SKIP + 1))
        continue
    fi

    # ファイル名からmeasurement名を抽出
    # パターン1: arsprout_InAirTemp_20250501_20250930.csv → InAirTemp
    # パターン2: arsprout_2038523_20250501_20250930.csv → node_2038523
    measurement=$(echo "$filename" | sed -E 's/^arsprout_([^_]+(_[^_]+)?)_[0-9]{8}_[0-9]{8}\.csv$/\1/')

    if [[ "$measurement" == "$filename" ]]; then
        echo "SKIP: $filename (measurement名抽出失敗)"
        SKIP=$((SKIP + 1))
        continue
    fi

    # 数字のみの場合はnode_プレフィクス
    if [[ "$measurement" =~ ^[0-9]+$ ]]; then
        measurement="node_$measurement"
    fi

    echo "----------------------------------------"
    echo "インポート: $filename → arsprout_$measurement"

    if python3 "$IMPORT_SCRIPT" \
        --file "$csv_file" \
        --measurement "$measurement" \
        --bucket "$INFLUX_BUCKET" \
        --org "$INFLUX_ORG" \
        --url "$INFLUX_URL" \
        --token "$INFLUX_TOKEN" \
        $DRY_RUN; then
        SUCCESS=$((SUCCESS + 1))
    else
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "========================================"
echo " 完了: 成功=$SUCCESS 失敗=$FAIL スキップ=$SKIP"
echo "========================================"
