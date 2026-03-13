# Telegraf セットアップ手順書

> **Agricultural Facility  データブリッジ（MQTT → InfluxDB）**
>
> Telegraf を使用して、Mosquitto（MQTT）から InfluxDB v2 へデータを転送します。

---

## 目次

1. [前提条件](#前提条件)
2. [Telegraf インストール](#telegraf-インストール)
3. [設定ファイル配置](#設定ファイル配置)
4. [InfluxDB トークン設定](#influxdb-トークン設定)
5. [起動と確認](#起動と確認)
6. [動作テスト](#動作テスト)
7. [トラブルシューティング](#トラブルシューティング)

---

## 前提条件

以下のサービスが稼働している必要があります：

- **Mosquitto (MQTT Broker)**: localhost:1883
- **InfluxDB v2**: localhost:8086
  - Organization: `agri-ha`
  - Bucket: `sensor_data`

確認コマンド：
```bash
# Mosquitto 確認
systemctl status mosquitto

# InfluxDB 確認
systemctl status influxdb

# MQTT ポート確認
ss -tlnp | grep 1883

# InfluxDB ポート確認
ss -tlnp | grep 8086
```

---

## Telegraf インストール

### 1. InfluxData 公式リポジトリ追加

```bash
# GPG キーをダウンロード
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c && cat influxdata-archive_compat.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null

# リポジトリ追加
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | sudo tee /etc/apt/sources.list.d/influxdata.list
```

### 2. Telegraf インストール

```bash
sudo apt update
sudo apt install -y telegraf
```

### 3. インストール確認

```bash
telegraf --version
# 出力例: Telegraf 1.28.x
```

---

## 設定ファイル配置

### 1. 設定ファイルをコピー

```bash
sudo cp telegraf_agri-ha.conf /etc/telegraf/telegraf.d/agri-ha.conf
sudo chown root:root /etc/telegraf/telegraf.d/agri-ha.conf
sudo chmod 640 /etc/telegraf/telegraf.d/agri-ha.conf
```

### 2. 設定ファイル確認

```bash
ls -l /etc/telegraf/telegraf.d/agri-ha.conf
```

### 3. デフォルト設定の無効化（オプション）

Telegraf はデフォルトで `/etc/telegraf/telegraf.conf` も読み込みます。
Agricultural Facility 専用設定のみを使用する場合は、デフォルト設定を無効化できます。

```bash
# デフォルト設定をリネーム（バックアップ）
sudo mv /etc/telegraf/telegraf.conf /etc/telegraf/telegraf.conf.bak
```

---

## InfluxDB トークン設定

### 1. InfluxDB トークン取得

InfluxDB UI（http://localhost:8086）にログインし、以下の手順でトークンを取得：

1. 左メニュー「API Tokens」をクリック
2. 既存トークンを選択、または「Generate API Token」→「All Access Token」
3. トークン文字列をコピー

### 2. 環境変数として設定（推奨）

Telegraf 用の環境変数ファイルを作成：

```bash
sudo mkdir -p /etc/telegraf/env
echo 'INFLUX_TOKEN="YOUR_INFLUXDB_TOKEN_HERE"' | sudo tee /etc/telegraf/env/influx.env
sudo chmod 600 /etc/telegraf/env/influx.env
```

**重要**: `YOUR_INFLUXDB_TOKEN_HERE` を実際のトークンに置き換えてください。

### 3. systemd 環境変数読み込み設定

```bash
sudo mkdir -p /etc/systemd/system/telegraf.service.d
cat <<EOF | sudo tee /etc/systemd/system/telegraf.service.d/influx-env.conf
[Service]
EnvironmentFile=/etc/telegraf/env/influx.env
EOF

sudo systemctl daemon-reload
```

### 4. 設定ファイル検証（ドライラン）

```bash
# 環境変数を読み込んでテスト
source /etc/telegraf/env/influx.env
telegraf --config /etc/telegraf/telegraf.d/agri-ha.conf --test

# エラーがなければOK（数秒でCtrl+C）
```

---

## 起動と確認

### 1. Telegraf サービス起動

```bash
sudo systemctl enable telegraf
sudo systemctl start telegraf
```

### 2. 起動確認

```bash
sudo systemctl status telegraf
```

**正常な出力例**:
```
● telegraf.service - The plugin-driven server agent for reporting metrics into InfluxDB
   Loaded: loaded (/lib/systemd/system/telegraf.service; enabled)
   Active: active (running) since ...
```

### 3. ログ確認

```bash
# リアルタイムログ監視
sudo journalctl -u telegraf -f

# エラーのみ表示
sudo journalctl -u telegraf -p err
```

---

## 動作テスト

### 1. テストデータ送信（MQTT）

Mosquitto クライアントでテストデータを送信：

```bash
mosquitto_pub -h localhost -t "agri-ha/greenhouse/WAirTemp" -m '{
  "value": -9.2,
  "sensor_type": "WAirTemp",
  "source": "192.168.1.71",
  "room": "1",
  "region": "1",
  "order": "1",
  "unit": "℃",
  "timestamp": "2026-02-07T12:00:00Z"
}'
```

### 2. InfluxDB でデータ確認

#### 方法1: InfluxDB CLI

```bash
influx query 'from(bucket:"sensor_data") |> range(start: -1h) |> filter(fn: (r) => r._measurement == "greenhouse_data")' \
  --org agri-ha
```

#### 方法2: InfluxDB UI

1. http://localhost:8086 にアクセス
2. 左メニュー「Data Explorer」
3. Bucket: `sensor_data` を選択
4. Measurement: `greenhouse_data` を選択
5. 「Submit」をクリック

データが表示されれば成功です。

### 3. 複数データ種別のテスト

```bash
# 湿度データ
mosquitto_pub -h localhost -t "agri-ha/greenhouse/WAirHumid" -m '{
  "value": 75,
  "sensor_type": "WAirHumid",
  "source": "192.168.1.71",
  "room": "1",
  "region": "1",
  "order": "1",
  "unit": "%",
  "timestamp": "2026-02-07T12:01:00Z"
}'

# 風速データ
mosquitto_pub -h localhost -t "agri-ha/greenhouse/WWindSpeed" -m '{
  "value": 2.5,
  "sensor_type": "WWindSpeed",
  "source": "192.168.1.71",
  "room": "1",
  "region": "1",
  "order": "1",
  "unit": "m/s",
  "timestamp": "2026-02-07T12:02:00Z"
}'
```

---

## トラブルシューティング

### 問題: データが InfluxDB に書き込まれない

#### 1. Telegraf 設定テスト

```bash
source /etc/telegraf/env/influx.env
telegraf --config /etc/telegraf/telegraf.d/agri-ha.conf --test --input-filter mqtt_consumer
```

正常なら `greenhouse_data` というメトリクスが表示されます。

#### 2. MQTT 接続確認

```bash
# MQTT サブスクライブテスト
mosquitto_sub -h localhost -t "agri-ha/greenhouse/#" -v
```

別ターミナルで `mosquitto_pub` でデータ送信し、受信できるか確認。

#### 3. InfluxDB トークン確認

```bash
# 環境変数確認
sudo systemctl show telegraf | grep INFLUX_TOKEN

# トークンが表示されない場合、環境変数が読み込まれていない
```

#### 4. InfluxDB 接続テスト

```bash
curl -H "Authorization: Token YOUR_TOKEN" \
  http://localhost:8086/api/v2/buckets?org=agri-ha
```

正常なら bucket 情報が JSON で返ります。

### 問題: Telegraf が起動しない

#### ログ確認

```bash
sudo journalctl -u telegraf -n 50 --no-pager
```

**よくあるエラー**:

1. **設定ファイル構文エラー**
   ```bash
   telegraf --config /etc/telegraf/telegraf.d/agri-ha.conf --test
   ```

2. **MQTT 接続エラー**
   - Mosquitto が起動しているか確認: `systemctl status mosquitto`
   - ファイアウォール確認: `sudo ufw status`

3. **InfluxDB 接続エラー**
   - InfluxDB が起動しているか確認: `systemctl status influxdb`
   - トークンが正しいか確認

### 問題: データが重複して書き込まれる

複数の Telegraf プロセスが起動している可能性があります。

```bash
ps aux | grep telegraf
sudo systemctl restart telegraf
```

### デバッグ用ログ出力

一時的にファイル出力を有効化して、Telegraf が受信しているデータを確認：

```bash
# /etc/telegraf/telegraf.d/agri-ha.conf の末尾に追加
sudo tee -a /etc/telegraf/telegraf.d/agri-ha.conf <<EOF

[[outputs.file]]
  files = ["/tmp/telegraf_debug.log"]
  data_format = "influx"
EOF

sudo systemctl restart telegraf

# ログ確認
tail -f /tmp/telegraf_debug.log
```

テスト後は outputs.file セクションをコメントアウトしてください。

---

## 設定ファイル構造

```
/etc/telegraf/
├── telegraf.conf.bak          # デフォルト設定（無効化済み）
├── telegraf.d/
│   └── agri-ha.conf          # Agricultural Facility 専用設定
└── env/
    └── influx.env             # InfluxDB トークン（環境変数）
```

---

## 次のステップ

1. **Grafana ダッシュボード作成**
   - InfluxDB をデータソースとして追加
   - greenhouse_data メトリクスを可視化

2. **- ブリッジスクリプト設定**
   - Agricultural Facility から UDP マルチキャスト受信
   - MQTT へ変換・送信

3. **アラート設定**
   - Grafana でしきい値アラート設定
   - LINE Messaging API 連携

---

## 参考資料

- [Telegraf 公式ドキュメント](https://docs.influxdata.com/telegraf/)
- [MQTT Consumer Plugin](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/mqtt_consumer)
- [InfluxDB v2 Output Plugin](https://github.com/influxdata/telegraf/tree/master/plugins/outputs/influxdb_v2)
- [Agricultural Facility  仕様](../../context/agri-ha.md)
