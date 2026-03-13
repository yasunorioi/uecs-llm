#!/usr/bin/env python3
"""
ArSprout CSV → InfluxDB インポートスクリプト
2/19提案資料準備用（2025年5-9月実データ）
"""
import csv
import sys
import re
from datetime import datetime
import requests

# InfluxDB接続情報
INFLUX_URL = "http://localhost:8086/api/v2/write"
ORG = "agriha"
BUCKET = "sensors"
TOKEN = "kH2MEND9UltLDk2Hn1Gy_qVglN3WajHwHi2ZmvIUierDLX7w5IpWRaBkaiahSPIX32Wnt3fB7dMr2rppOLA-Qw=="
BATCH_SIZE = 5000

def extract_field_name(filename):
    """ファイル名からfield名を抽出
    例: arsprout_InAirTemp_*.csv → InAirTemp
    """
    match = re.search(r'arsprout_([^_]+)_', filename)
    if match:
        return match.group(1)
    return None

def datetime_to_nanoseconds(dt_str):
    """ISO8601 datetime → ナノ秒timestamp変換
    例: 2025-06-06T13:15:00.000+0000 → 1717679700000000000
    """
    # +0000 を +00:00 に変換（strptime用）
    dt_str_fixed = dt_str[:-2] + ':' + dt_str[-2:] if dt_str[-5] in ['+', '-'] else dt_str
    dt = datetime.strptime(dt_str_fixed, "%Y-%m-%dT%H:%M:%S.%f%z")
    return int(dt.timestamp() * 1e9)

def parse_csv_to_line_protocol(csv_file, field_name):
    """CSV → InfluxDB Line Protocol変換

    入力CSV形式: datetime,value
    出力Line Protocol: environment,source=arsprout field_name=value timestamp_ns
    """
    lines = []
    skipped = 0

    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row_num, row in enumerate(reader, start=2):  # ヘッダ行は1行目
            dt = row.get('datetime', '').strip()
            value = row.get('value', '').strip()

            # 空行・異常値スキップ
            if not dt or not value:
                skipped += 1
                continue

            try:
                # valueが数値でない場合スキップ
                float(value)
            except ValueError:
                skipped += 1
                continue

            try:
                # datetimeをナノ秒に変換
                ts_ns = datetime_to_nanoseconds(dt)
            except Exception as e:
                print(f"Warning: {csv_file}:{row_num} datetime parse error: {dt}", file=sys.stderr)
                skipped += 1
                continue

            # Line Protocol: measurement,tag=value field=value timestamp
            line = f"environment,source=arsprout {field_name}={value} {ts_ns}"
            lines.append(line)

    return lines, skipped

def send_batch(lines):
    """InfluxDB APIにバッチ送信"""
    payload = "\n".join(lines)
    headers = {
        "Authorization": f"Token {TOKEN}",
        "Content-Type": "text/plain; charset=utf-8"
    }
    params = {
        "org": ORG,
        "bucket": BUCKET,
        "precision": "ns"
    }

    try:
        response = requests.post(INFLUX_URL, headers=headers, params=params, data=payload.encode('utf-8'), timeout=30)
        if response.status_code == 204:
            return True
        else:
            print(f"InfluxDB API Error: {response.status_code} {response.text}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Request Error: {e}", file=sys.stderr)
        return False

def import_csv(csv_file):
    """1つのCSVファイルをインポート"""
    field_name = extract_field_name(csv_file)
    if not field_name:
        print(f"Error: ファイル名からfield名を抽出できません: {csv_file}", file=sys.stderr)
        return None

    print(f"\n[{field_name}] 変換開始: {csv_file}")
    lines, skipped = parse_csv_to_line_protocol(csv_file, field_name)
    total = len(lines)

    if skipped > 0:
        print(f"  スキップ: {skipped}行")

    print(f"  変換完了: {total}行 → InfluxDBにインポート中...")

    # バッチ送信
    sent = 0
    failed = 0
    for i in range(0, total, BATCH_SIZE):
        batch = lines[i:i+BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        batch_total = (total + BATCH_SIZE - 1) // BATCH_SIZE

        if send_batch(batch):
            sent += len(batch)
            print(f"  バッチ {batch_num}/{batch_total}: {len(batch)}行送信完了")
        else:
            failed += len(batch)
            print(f"  バッチ {batch_num}/{batch_total}: 送信失敗", file=sys.stderr)

    print(f"[{field_name}] インポート完了: {sent}/{total}行 (失敗: {failed}行)")
    return {
        'field': field_name,
        'total': total,
        'sent': sent,
        'failed': failed,
        'skipped': skipped
    }

def main():
    """メイン処理"""
    csv_files = [
        "/home/yasu/unipi-agri-ha/data/arsprout_InAirTemp_20250501_20250930.csv",
        "/home/yasu/unipi-agri-ha/data/arsprout_InAirCO2_20250501_20250930.csv",
        "/home/yasu/unipi-agri-ha/data/arsprout_InRadiation_20250501_20250930.csv",
        "/home/yasu/unipi-agri-ha/data/arsprout_WRadiation_20250501_20250930.csv",
        "/home/yasu/unipi-agri-ha/data/arsprout_WRadInteg_20250501_20250930.csv"
    ]

    print("=" * 60)
    print("ArSprout CSV → InfluxDB インポート")
    print("=" * 60)
    print(f"InfluxDB: {INFLUX_URL}")
    print(f"Org: {ORG}, Bucket: {BUCKET}")
    print(f"CSVファイル数: {len(csv_files)}")

    results = []
    for csv_file in csv_files:
        result = import_csv(csv_file)
        if result:
            results.append(result)

    # サマリ表示
    print("\n" + "=" * 60)
    print("インポート結果サマリ")
    print("=" * 60)
    for r in results:
        print(f"{r['field']:15s}: {r['sent']:6d} / {r['total']:6d} records (スキップ: {r['skipped']:3d}, 失敗: {r['failed']:3d})")

    total_sent = sum(r['sent'] for r in results)
    total_records = sum(r['total'] for r in results)
    print("-" * 60)
    print(f"{'合計':15s}: {total_sent:6d} / {total_records:6d} records")
    print("=" * 60)

    return 0 if all(r['failed'] == 0 for r in results) else 1

if __name__ == "__main__":
    sys.exit(main())
