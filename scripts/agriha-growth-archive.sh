#!/bin/bash
# agriha-growth-archive.sh — growth_log アーカイブ昇格スクリプト
# 毎時00分のペアをarchiveにコピー（1時間間隔・シーズン保持）
# cron: 5 * * * * agriha /usr/local/bin/agriha-growth-archive.sh >> /var/log/agriha/growth_archive.log 2>&1
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

echo "$(date -Iseconds) archive昇格完了: ${ARCHIVE}"
