# Grafana セットアップ手順

> Agricultural Facility 自前クラウド基盤 - Grafana OSS インストール・設定手順書
> Target: Ubuntu 22.04/24.04 LTS
> Host: 192.168.15.14
> Updated: 2026-02-07

## 概要

Grafana OSS をインストールし、InfluxDB をデータソースとして設定する。
プロビジョニング機能を使用してデータソースとダッシュボードを自動登録する。

## システム要件

- Ubuntu 22.04 LTS または 24.04 LTS
- InfluxDB 2.x がインストール済み（http://localhost:8086）
- 最小メモリ: 256MB（推奨: 512MB以上）

## 1. Grafana OSS インストール

### 1.1 公式リポジトリを追加

```bash
# GPGキーを追加
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null

# リポジトリを追加
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
```

### 1.2 Grafana をインストール

```bash
sudo apt-get update
sudo apt-get install -y grafana
```

### 1.3 systemd サービスを有効化・起動

```bash
sudo systemctl daemon-reload
sudo systemctl enable grafana-server
sudo systemctl start grafana-server
```

### 1.4 起動確認

```bash
sudo systemctl status grafana-server
```

出力例:
```
● grafana-server.service - Grafana instance
     Loaded: loaded (/lib/systemd/system/grafana-server.service; enabled; vendor preset: enabled)
     Active: active (running) since ...
```

## 2. 初期アクセス

### 2.1 Webブラウザでアクセス

```
http://192.168.15.14:3000
```

### 2.2 初回ログイン

- **ユーザー名**: `admin`
- **パスワード**: `admin`

初回ログイン後、新しいパスワードへの変更を求められる。

## 3. InfluxDB データソース設定

### 3.1 手動設定（GUI）

1. 左サイドバー: Configuration（歯車アイコン） → Data sources
2. "Add data source" をクリック
3. "InfluxDB" を選択
4. 以下のパラメータを設定:

| 項目 | 値 |
|------|-----|
| Name | InfluxDB-Agricultural Facility |
| Query Language | **Flux** |
| URL | `http://localhost:8086` |
| Access | Server (default) |
| Organization | `agri-ha` |
| Default Bucket | `sensor_data` |
| Token | （InfluxDB APIトークン） |

5. "Save & Test" をクリックして接続を確認

### 3.2 自動設定（プロビジョニング）

プロビジョニング設定ファイルを配置することで、データソースを自動登録できる。

#### 3.2.1 プロビジョニングディレクトリ

Grafana のプロビジョニング設定は以下のディレクトリに配置:

```
/etc/grafana/provisioning/
├── datasources/   # データソース定義
└── dashboards/    # ダッシュボード定義
```

#### 3.2.2 InfluxDB データソース YAML

`/etc/grafana/provisioning/datasources/influxdb.yaml` を作成:

```yaml
apiVersion: 1

datasources:
  - name: InfluxDB-Agricultural Facility
    type: influxdb
    access: proxy
    url: http://localhost:8086
    jsonData:
      version: Flux
      organization: agri-ha
      defaultBucket: sensor_data
      tlsSkipVerify: true
    secureJsonData:
      token: <INFLUXDB_TOKEN>
    editable: true
```

**注意**: `<INFLUXDB_TOKEN>` を実際の InfluxDB API トークンに置き換える。

#### 3.2.3 InfluxDB トークンの取得

InfluxDB の Web UI（http://192.168.15.14:8086）から取得:

1. Load Data → API Tokens
2. "Generate API Token" → "Read/Write Token"
3. Organization: `agri-ha`, Buckets: `sensor_data` を選択
4. 生成されたトークンをコピー

または CLI から取得:

```bash
influx auth list --json | jq -r '.[] | select(.description == "agri-ha-token") | .token'
```

#### 3.2.4 プロビジョニングファイルを配置

```bash
sudo cp influxdb.yaml /etc/grafana/provisioning/datasources/
sudo chown root:grafana /etc/grafana/provisioning/datasources/influxdb.yaml
sudo chmod 640 /etc/grafana/provisioning/datasources/influxdb.yaml
```

Grafana を再起動:

```bash
sudo systemctl restart grafana-server
```

## 4. ダッシュボードプロビジョニング

### 4.1 ダッシュボード定義 YAML

`/etc/grafana/provisioning/dashboards/agri-ha.yaml` を作成:

```yaml
apiVersion: 1

providers:
  - name: 'Agricultural Facility'
    orgId: 1
    folder: 'Agricultural Facility'
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards/agri-ha
```

### 4.2 ダッシュボード JSON ファイル配置

ダッシュボード JSON ファイルは以下のディレクトリに配置:

```bash
sudo mkdir -p /var/lib/grafana/dashboards/agri-ha
```

ダッシュボード JSON は足軽5が `cloud_server/grafana/dashboards/` に作成する。
作成されたファイルを以下のようにコピー:

```bash
sudo cp cloud_server/grafana/dashboards/*.json /var/lib/grafana/dashboards/agri-ha/
sudo chown -R grafana:grafana /var/lib/grafana/dashboards/agri-ha/
```

Grafana を再起動:

```bash
sudo systemctl restart grafana-server
```

## 5. ファイアウォール設定

### 5.1 ufw（Ubuntu Firewall）を使用している場合

```bash
sudo ufw allow 3000/tcp
sudo ufw reload
```

### 5.2 iptables を使用している場合

```bash
sudo iptables -A INPUT -p tcp --dport 3000 -j ACCEPT
sudo netfilter-persistent save
```

## 6. 動作確認

### 6.1 Grafana アクセス確認

```bash
curl -I http://192.168.15.14:3000/login
```

出力例:
```
HTTP/1.1 200 OK
...
```

### 6.2 データソース確認

Grafana Web UI にログイン後、以下を確認:

1. Configuration → Data sources
2. "InfluxDB-Agricultural Facility" が表示されることを確認
3. データソース詳細画面で "Save & Test" → "Data source is working" が表示されることを確認

### 6.3 ダッシュボード確認

1. Dashboards → Browse
2. "Agricultural Facility" フォルダにダッシュボードが表示されることを確認

## 7. トラブルシューティング

### 7.1 Grafana が起動しない

```bash
# ログを確認
sudo journalctl -u grafana-server -n 100 --no-pager

# 設定ファイルの構文確認
sudo grafana-cli admin reset-admin-password temp123
```

### 7.2 データソース接続エラー

```bash
# InfluxDB が起動しているか確認
sudo systemctl status influxdb

# ポート確認
ss -tlnp | grep 8086

# トークンの有効性確認
influx auth list
```

### 7.3 プロビジョニングが反映されない

```bash
# プロビジョニング設定ファイルのパーミッション確認
ls -la /etc/grafana/provisioning/datasources/
ls -la /etc/grafana/provisioning/dashboards/

# Grafana ログで "provisioning" を検索
sudo journalctl -u grafana-server | grep provisioning
```

## 8. 設定ファイル

### 8.1 Grafana メイン設定

- パス: `/etc/grafana/grafana.ini`
- デフォルト値はコメントアウトされている

主要な設定項目:

```ini
[server]
http_port = 3000

[security]
admin_user = admin
admin_password = admin

[paths]
data = /var/lib/grafana
logs = /var/log/grafana
plugins = /var/lib/grafana/plugins
provisioning = /etc/grafana/provisioning
```

## 9. 参考リンク

- [Grafana公式ドキュメント](https://grafana.com/docs/grafana/latest/)
- [Grafana Provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
- [InfluxDB データソース設定](https://grafana.com/docs/grafana/latest/datasources/influxdb/)
- [Flux クエリ言語](https://docs.influxdata.com/flux/v0.x/)

## 付録: 共通規約

| 項目 | 値 |
|------|-----|
| Organization | agri-ha |
| Bucket | sensor_data |
| InfluxDB URL | http://localhost:8086 |
| Grafana ポート | 3000 |
| ターゲットホスト | 192.168.15.14 |
