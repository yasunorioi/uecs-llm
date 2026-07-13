# shadow-run report — 配置メモ (2026-07-11 revised)

`tools/replay_driver.py` + `tools/shadow_report.py` を **yasu-hp**
(`yasu-hp.tail883646.ts.net`) に置き、Pi4 で agriha-logger.service が MQTT→SQLite
に流し込む `/var/lib/agriha/sensor_log.db` を rsync pull → interpreter-only
replay → HTML 再生成 の flow で LAN 内に配信する。

## 実際に稼働している状態

```
[agriha Pi4]                                [yasu-hp]
 agriha-ccm-bridge  CCM MQTT                  cron: */5 tick.sh
  → agriha-logger  → sensor_log.db  ──rsync─┐   ├─ rsync pull /etc/agriha/*.yaml
                                            └──▶ /srv/shadow/sensor_log.db
                                                 ├─ replay_driver
                                                 │    → /srv/shadow/log.jsonl (mode=replay 追記)
                                                 ├─ shadow_report html
                                                 │    → /srv/shadow/report.html
                                                 └─ nginx :80 /shadow/ で serve
```

Pi4 の rule_engine は本流制御に参加していない (実制御は arsprout-ccm) ため、
interpreter vs evaluate_rules の A/B 比較は **保留**。実データでの interpreter
trace のみを取り、日中/夜間/湿度スパイクでの発火傾向を眺める用途。

## 配置状態 (2026-07-11 現在)

- `~yasu/shadow-report/` に `shadow_report.py`, `replay_driver.py`,
  `tick.sh`, `config/*.yaml` (interpreter は ogms-DSL を rsync deploy か pip install)
- `/srv/shadow/` に `sensor_log.db`, `log.jsonl`, `report.html`, `active_state.json`
- nginx `/etc/nginx/sites-enabled/docs` に `location /shadow/`
- user crontab (yasu@yasu-hp): `*/5 * * * * /home/yasu/shadow-report/tick.sh`
- SSH key: `yasu@yasu-hp` → `pi@100.102.95.37` (ed25519, no passphrase)

参照 URL: `http://yasu-hp.local/shadow/report.html`

## 運用上の注意

- 現在 CCM の publish が数時間空くパターンあり (2026-07-11 は 12:00 で止まった)。
  `--max-age-min 1440` (24h) で許容中。CCM が publish 再開すれば自動で新値が乗る
- 同一 snapshot_ts で毎 tick 追記されるので、5 分 × 288/日 のペースで log は
  伸びる。100 日で ~30k 行、shadow_report `--limit 2000` で HTML 埋め込みは頭切り
- config は Pi4 の `/etc/agriha/` から毎 tick 上書き pull されるので、Pi4 で
  編集すればすぐ replay 側に反映される
- shadow-run (A/B) を将来復活させたい場合: Pi4 の rule_engine が sensor fetch
  できる経路 (unipi-daemon の /api/sensors 実装 or SQLite 直読み) を用意した上で、
  `.env` に `export AGRIHA_SHADOW_RUN=1` を追加。同じ jsonl フォーマットで
  mode="shadow" のエントリが混ざる → shadow_report は自動で両モードを分けて集計
