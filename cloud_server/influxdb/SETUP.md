# InfluxDB 2.x セットアップガイド

> **農業施設向けクラウド基盤 - InfluxDB 2.x OSS インストール・設定手順書**
>
> 対象OS: Ubuntu 22.04 / 24.04 LTS
> ターゲットホスト: 192.168.15.14 (Ubuntu PC)
> 更新日: 2026-02-07

## 概要

InfluxDB 2.xは時系列データベースで、農業施設のセンサーデータを長期保存・可視化するために使用します。

**システム構成:**
- Organization: `agri-ha`
- Bucket: `sensor_data` (保存期間: 365日)
- Measurement: `greenhouse_data`
- Tags: `source`, `sensor_type`, `house_id`, `location`
- Fields: `value` (float)
- ポート: 8086 (HTTP API)

## 前提条件

- Ubuntu 22.04 または 24.04 LTS
- root権限（sudo）
- インターネット接続

## インストール手順

### 1. InfluxDB公式リポジトリの追加

```bash
# GPGキーをダウンロード・追加
wget -q https://repos.influxdata.com/influxdata-archive_compat.key
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c && cat influxdata-archive_compat.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null

# リポジトリを追加
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | sudo tee /etc/apt/sources.list.d/influxdata.list
```

### 2. InfluxDBのインストール

```bash
# パッケージリストを更新
sudo apt-get update

# InfluxDB 2.x をインストール
sudo apt-get install -y influxdb2 influxdb2-cli
```

### 3. InfluxDBサービスの起動

```bash
# サービスを有効化・起動
sudo systemctl enable influxdb
sudo systemctl start influxdb

# ステータス確認
sudo systemctl status influxdb
```

**期待される出力:**
```
● influxdb.service - InfluxDB is an open-source, distributed, time series database
     Loaded: loaded (/lib/systemd/system/influxdb.service; enabled; vendor preset: enabled)
     Active: active (running) since ...
```

### 4. 初期セットアップ

ブラウザまたはCLIで初期設定を行います。

#### 4.1 CLI経由のセットアップ（推奨）

```bash
influx setup
```

**対話的プロンプトで以下を入力:**

| 項目 | 入力値 |
|------|--------|
| Username | `admin` |
| Password | `<強固なパスワード>` (12文字以上、英数字記号混在) |
| Confirm Password | (同じパスワード) |
| Primary Organization Name | `agri-ha` |
| Primary Bucket Name | `sensor_data` |
| Retention Period | `365d` |

**セットアップ完了後、以下が表示されます:**

```
User            Organization    Bucket
admin           agri-ha         sensor_data
```

**重要:** 表示されるAPIトークンを必ず安全に保存してください。

#### 4.2 ブラウザ経由のセットアップ

ブラウザで `http://192.168.15.14:8086` にアクセスし、上記と同じ値を入力します。

### 5. APIトークンの生成（Telegraf用）

Telegrafからデータを書き込むためのAPIトークンを作成します。

```bash
# Telegraf用の読み書き権限トークンを生成
influx auth create \
  --org agri-ha \
  --read-buckets \
  --write-buckets \
  --description "Telegraf MQTT to InfluxDB token"
```

**出力例:**
```
ID                      Description                             Token                           User Name       User ID                 Permissions
0abc123def456           Telegraf MQTT to InfluxDB token         xyz789...                       admin           0abc123def456           [read:orgs/... write:orgs/...]
```

生成されたトークンをTelegraf設定ファイル（`telegraf.conf`）に記載します。

### 6. データ確認コマンド

#### 6.1 Fluxクエリで最新10件取得

```bash
influx query 'from(bucket: "sensor_data")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "greenhouse_data")
  |> limit(n: 10)'
```

#### 6.2 特定センサータイプのデータ取得（温度）

```bash
influx query 'from(bucket: "sensor_data")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "greenhouse_data")
  |> filter(fn: (r) => r.sensor_type == "temperature")
  |> limit(n: 100)'
```

#### 6.3 Webインターフェースでデータ確認

ブラウザで `http://192.168.15.14:8086` にアクセスし、左メニューの「Data Explorer」から視覚的にデータを確認できます。

### 7. ファイアウォール設定

外部ホスト（Picoノード等）からInfluxDBにアクセスする場合、ポート8086を開放します。

#### UFWの場合（Ubuntu標準）

```bash
# ポート8086を開放
sudo ufw allow 8086/tcp

# ルール確認
sudo ufw status
```

#### iptablesの場合

```bash
# ポート8086を開放
sudo iptables -A INPUT -p tcp --dport 8086 -j ACCEPT

# 設定を永続化
sudo netfilter-persistent save
```

**セキュリティ注意:**
- 本番環境では、特定IPアドレスのみ許可することを推奨します。
- 例: `sudo ufw allow from 192.168.15.0/24 to any port 8086`

### 8. systemdサービス管理

```bash
# サービス起動
sudo systemctl start influxdb

# サービス停止
sudo systemctl stop influxdb

# サービス再起動
sudo systemctl restart influxdb

# ステータス確認
sudo systemctl status influxdb

# 自動起動設定
sudo systemctl enable influxdb

# 自動起動解除
sudo systemctl disable influxdb
```

### 9. ログ確認

```bash
# InfluxDBログをリアルタイム表示
sudo journalctl -u influxdb -f

# 最新100行を表示
sudo journalctl -u influxdb -n 100
```

## データスキーマ

農業施設向けシステムで使用するInfluxDBデータスキーマ:

### Measurement: `greenhouse_data`

| フィールド/タグ | 種類 | 説明 | 例 |
|----------------|------|------|-----|
| `source` | Tag | データソース | `pico_node_01`, `weather_station` |
| `sensor_type` | Tag | センサータイプ | `temperature`, `humidity`, `co2` |
| `house_id` | Tag | ハウスID | `house_01` |
| `location` | Tag | 設置場所 | `indoor`, `outdoor` |
| `value` | Field (float) | センサー値 | `25.0`, `75.0` |

### MQTTトピック構造

```
greenhouse/{house_id}/sensor/{sensor_type}
```

**例:**
- `greenhouse/h1/sensor/temperature` → 気温
- `greenhouse/h1/sensor/humidity` → 湿度
- `greenhouse/h1/sensor/co2` → CO2濃度

TelegrafのMQTT Consumerプラグインがこのトピックを購読し、InfluxDBに書き込みます。

## トラブルシューティング

### 1. サービスが起動しない

```bash
# ログを確認
sudo journalctl -u influxdb -n 100 --no-pager

# ポート8086が他のプロセスに使われていないか確認
sudo lsof -i :8086
```

### 2. `influx` コマンドが見つからない

```bash
# influxdb2-cliがインストールされているか確認
dpkg -l | grep influxdb2-cli

# 未インストールの場合
sudo apt-get install -y influxdb2-cli
```

### 3. APIトークンを忘れた

```bash
# 既存のトークン一覧を表示
influx auth list
```

新しいトークンを作成する場合は「5. APIトークンの生成」を参照。

### 4. データが書き込まれない

```bash
# Bucketが存在するか確認
influx bucket list

# 権限を確認
influx auth list
```

Telegraf側のログも確認:
```bash
sudo journalctl -u telegraf -f
```

## セキュリティベストプラクティス

1. **APIトークンの管理**
   - トークンをGitにコミットしない
   - 環境変数または別ファイル（`.env`等）で管理
   - 最小権限の原則（必要な権限のみ付与）

2. **ファイアウォール設定**
   - 必要最小限のIPアドレスのみ許可
   - VPN経由でのアクセスを推奨

3. **定期バックアップ**
   - InfluxDBデータディレクトリ: `/var/lib/influxdb2/`
   - 設定ファイル: `/etc/influxdb/`

4. **アップデート**
   ```bash
   sudo apt-get update
   sudo apt-get upgrade influxdb2 influxdb2-cli
   ```

## 参考リンク

- [InfluxDB 2.x 公式ドキュメント](https://docs.influxdata.com/influxdb/v2/)
- [Flux クエリ言語リファレンス](https://docs.influxdata.com/flux/v0/)
- [Telegraf MQTT Consumer Plugin](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/mqtt_consumer)

## 次のステップ

1. Telegrafのインストール・設定（`../telegraf/SETUP.md` 参照）
2. Grafanaのインストール・設定（`../grafana/SETUP.md` 参照）
3. Grafanaダッシュボードの構築

---

**作成者:** agri-ha DevOps Team
**更新日:** 2026-02-07
**バージョン:** 1.0
