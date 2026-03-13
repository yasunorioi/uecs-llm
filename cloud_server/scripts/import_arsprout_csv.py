#!/usr/bin/env python3
"""
ArSprout CSVデータ → InfluxDB インポートスクリプト

CSV形式: datetime,value（1行目ヘッダー）
datetime: ISO 8601 (例: 2025-06-06T09:15:00.000+0000)

使用例:
  python3 import_arsprout_csv.py \
    --file /home/yasu/unipi-agri-ha/data/arsprout_InAirTemp_20250501_20250930.csv \
    --measurement InAirTemp \
    --bucket sensors \
    --org agriha \
    --url http://localhost:8086 \
    --token <token>
"""

import argparse
import csv
import sys
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


def parse_datetime(dt_str: str) -> datetime:
    """ISO 8601文字列をdatetimeに変換"""
    # "2025-06-06T09:15:00.000+0000" → Python datetime
    # +0000 を +00:00 形式に変換
    if dt_str.endswith("+0000"):
        dt_str = dt_str[:-5] + "+00:00"
    elif len(dt_str) > 5 and dt_str[-5] in "+-" and dt_str[-4:].isdigit():
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]
    return datetime.fromisoformat(dt_str)


def import_csv(
    file_path: str,
    measurement: str,
    bucket: str,
    org: str,
    url: str,
    token: str,
    house: str = "h1",
    source: str = "arsprout_cloud",
    batch_size: int = 5000,
    dry_run: bool = False,
):
    total_lines = 0
    with open(file_path, "r") as f:
        total_lines = sum(1 for _ in f) - 1  # ヘッダー除外

    if total_lines <= 0:
        print(f"ERROR: ファイルが空です: {file_path}")
        return 0, 0

    print(f"ファイル: {file_path}")
    print(f"measurement: arsprout_{measurement}")
    print(f"データ行数: {total_lines}")
    print(f"バッチサイズ: {batch_size}")
    if dry_run:
        print("--- DRY RUN モード（書き込みなし） ---")
    print()

    if not dry_run:
        client = InfluxDBClient(url=url, token=token, org=org)
        write_api = client.write_api(write_options=SYNCHRONOUS)

    imported = 0
    skipped = 0
    batch = []
    progress_step = max(1, total_lines // 10)

    with open(file_path, "r") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            try:
                # datetime と dateTime の両方に対応
                dt_str = row.get("datetime") or row.get("dateTime")
                if not dt_str:
                    raise KeyError("datetime or dateTime column not found")
                dt = parse_datetime(dt_str)
                value = float(row["value"])

                point = (
                    Point(f"arsprout_{measurement}")
                    .tag("house", house)
                    .tag("source", source)
                    .field("value", value)
                    .time(dt, WritePrecision.S)
                )
                batch.append(point)
                imported += 1

                if len(batch) >= batch_size:
                    if not dry_run:
                        write_api.write(bucket=bucket, org=org, record=batch)
                    batch = []

            except (ValueError, KeyError) as e:
                skipped += 1
                if skipped <= 10:
                    print(f"  SKIP 行{i}: {e} → {row}")

            if i % progress_step == 0:
                pct = int(i / total_lines * 100)
                print(f"  進捗: {pct}% ({i}/{total_lines})")

    # 残りのバッチを書き込み
    if batch and not dry_run:
        write_api.write(bucket=bucket, org=org, record=batch)

    if not dry_run:
        client.close()

    print(f"\n完了: インポート {imported} 件, スキップ {skipped} 件")
    return imported, skipped


def main():
    parser = argparse.ArgumentParser(description="ArSprout CSV → InfluxDB インポート")
    parser.add_argument("--file", required=True, help="CSVファイルパス")
    parser.add_argument("--measurement", required=True, help="measurement名（センサー名）")
    parser.add_argument("--bucket", default="sensors", help="InfluxDB bucket名")
    parser.add_argument("--org", default="agriha", help="InfluxDB organization名")
    parser.add_argument("--url", default="http://localhost:8086", help="InfluxDB URL")
    parser.add_argument("--token", required=True, help="InfluxDB APIトークン")
    parser.add_argument("--house", default="h1", help="ハウスタグ")
    parser.add_argument("--source", default="arsprout_cloud", help="ソースタグ")
    parser.add_argument("--batch-size", type=int, default=5000, help="バッチサイズ")
    parser.add_argument("--dry-run", action="store_true", help="ドライラン（書き込みなし）")
    args = parser.parse_args()

    imported, skipped = import_csv(
        file_path=args.file,
        measurement=args.measurement,
        bucket=args.bucket,
        org=args.org,
        url=args.url,
        token=args.token,
        house=args.house,
        source=args.source,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    sys.exit(0 if skipped == 0 else 1)


if __name__ == "__main__":
    main()
