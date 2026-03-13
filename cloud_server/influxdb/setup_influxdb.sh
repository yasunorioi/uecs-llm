#!/bin/bash
#===============================================================================
# InfluxDB 2.x セットアップスクリプト
#
# 使用方法:
#   sudo bash setup_influxdb.sh
#
# 実行内容:
#   1. InfluxDB公式リポジトリの追加
#   2. InfluxDB 2.x のインストール
#   3. systemdサービスの有効化・起動
#   4. 初期セットアップ手順の表示（対話的に実行）
#   5. ヘルスチェック
#
# 対象OS: Ubuntu 22.04 / 24.04 LTS
# ターゲットホスト: 192.168.15.14 (Ubuntu PC)
#
# 農業施設向けクラウド基盤構築の一部
# 作成日: 2026-02-07
#===============================================================================

set -euo pipefail

# カラー出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 設定値
INFLUXDB_PORT=8086
ORGANIZATION="agri-ha"
BUCKET="sensor_data"
RETENTION="365d"

#-------------------------------------------------------------------------------
# ユーティリティ関数
#-------------------------------------------------------------------------------

print_header() {
    echo -e "${GREEN}"
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║         InfluxDB 2.x セットアップスクリプト                ║"
    echo "║         農業施設向けクラウド基盤                           ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_step() {
    echo -e "\n${BLUE}[Step $1] $2${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

#-------------------------------------------------------------------------------
# メイン処理
#-------------------------------------------------------------------------------

print_header

# 1. 前提条件チェック
print_step "1/6" "前提条件チェック"

# root権限チェック
if [ "$EUID" -ne 0 ]; then
    print_error "このスクリプトはroot権限で実行してください。"
    echo "使用方法: sudo bash setup_influxdb.sh"
    exit 1
fi

# OS検出
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID=$ID
    OS_VERSION=$VERSION_ID
    echo "  検出OS: $OS_ID $OS_VERSION"
else
    print_error "OSを検出できませんでした。"
    exit 1
fi

# Ubuntu 22.04 / 24.04 チェック
if [ "$OS_ID" != "ubuntu" ]; then
    print_warning "このスクリプトはUbuntu向けに設計されています。"
    print_warning "他のディストリビューションでは動作しない可能性があります。"
    read -p "続行しますか？ (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "中止しました。"
        exit 1
    fi
fi

print_success "前提条件チェック完了"

# 2. InfluxDB公式リポジトリの追加
print_step "2/6" "InfluxDB公式リポジトリの追加"

# GPGキーのダウンロード
if [ ! -f influxdata-archive_compat.key ]; then
    echo "  GPGキーをダウンロード中..."
    wget -q https://repos.influxdata.com/influxdata-archive_compat.key
fi

# GPGキーの検証
echo "  GPGキーを検証中..."
echo '393e8779c89ac8d958f81f942f9ad7fb82a25e133faddaf92e15b16e6ac9ce4c influxdata-archive_compat.key' | sha256sum -c

# GPGキーを追加
cat influxdata-archive_compat.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg > /dev/null
rm influxdata-archive_compat.key

# リポジトリを追加
echo 'deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive_compat.gpg] https://repos.influxdata.com/debian stable main' | tee /etc/apt/sources.list.d/influxdata.list > /dev/null

print_success "InfluxDB公式リポジトリを追加しました"

# 3. InfluxDBのインストール
print_step "3/6" "InfluxDBのインストール"

echo "  パッケージリストを更新中..."
apt-get update -qq

echo "  InfluxDB 2.x をインストール中..."
apt-get install -y influxdb2 influxdb2-cli > /dev/null 2>&1

# インストール確認
if command -v influxd &> /dev/null && command -v influx &> /dev/null; then
    INFLUXDB_VERSION=$(influx version | head -n 1 | awk '{print $2}')
    print_success "InfluxDB ${INFLUXDB_VERSION} をインストールしました"
else
    print_error "InfluxDBのインストールに失敗しました"
    exit 1
fi

# 4. systemdサービスの有効化・起動
print_step "4/6" "systemdサービスの起動"

echo "  サービスを有効化・起動中..."
systemctl enable influxdb > /dev/null 2>&1
systemctl start influxdb

# 起動確認（最大30秒待機）
echo "  起動を待機中..."
for i in {1..30}; do
    if systemctl is-active --quiet influxdb; then
        print_success "InfluxDBサービスが起動しました"
        break
    fi
    sleep 1
done

if ! systemctl is-active --quiet influxdb; then
    print_error "InfluxDBサービスの起動に失敗しました"
    echo "ログを確認してください: sudo journalctl -u influxdb -n 50"
    exit 1
fi

# 5. 初期セットアップ手順の表示
print_step "5/6" "初期セットアップ"

echo -e "${YELLOW}"
cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                     初期セットアップ手順                          ║
╚═══════════════════════════════════════════════════════════════════╝

InfluxDBのインストールが完了しました。
次に、初期セットアップを対話的に実行してください。

【方法1】CLI経由（推奨）:

  influx setup

  以下の値を入力してください:

  - Username:             admin
  - Password:             <強固なパスワード（12文字以上）>
  - Confirm Password:     <同じパスワード>
  - Primary Organization: agri-ha
  - Primary Bucket:       sensor_data
  - Retention Period:     365d

  ※ 表示されるAPIトークンを必ず安全に保存してください！

【方法2】ブラウザ経由:

  http://192.168.15.14:8086 にアクセスし、上記と同じ値を入力

【重要なセキュリティ注意事項】

1. パスワードは強固なものを使用してください（12文字以上、英数字記号混在）
2. APIトークンは安全に保管してください（Gitにコミットしない）
3. ファイアウォールでポート8086を適切に制限してください:

   sudo ufw allow from 192.168.15.0/24 to any port 8086

4. 定期的にバックアップを取得してください:

   データディレクトリ: /var/lib/influxdb2/
   設定ファイル:       /etc/influxdb/

【Telegraf用APIトークンの生成（セットアップ後に実行）】

  influx auth create \
    --org agri-ha \
    --read-buckets \
    --write-buckets \
    --description "Telegraf MQTT to InfluxDB token"

  生成されたトークンをTelegraf設定ファイルに記載してください。

EOF
echo -e "${NC}"

# セットアップ実行の確認
read -p "今すぐ初期セットアップを実行しますか？ (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "\n${BLUE}初期セットアップを開始します...${NC}\n"
    influx setup
else
    print_warning "初期セットアップをスキップしました。"
    echo "後で以下のコマンドで実行してください: influx setup"
fi

# 6. ヘルスチェック
print_step "6/6" "ヘルスチェック"

# ポート確認
if netstat -tuln | grep -q ":${INFLUXDB_PORT}"; then
    print_success "InfluxDBはポート${INFLUXDB_PORT}でリスニングしています"
else
    print_warning "ポート${INFLUXDB_PORT}でリスニングしていません"
fi

# influx ping 確認（初期セットアップ未実行の場合はスキップ）
if [ -f ~/.influxdbv2/configs ]; then
    echo "  influx ping でヘルスチェック中..."
    if influx ping &> /dev/null; then
        print_success "InfluxDB APIが応答しています"
    else
        print_warning "InfluxDB APIが応答していません（初期セットアップ未完了の可能性）"
    fi
else
    print_warning "初期セットアップが未完了のため、APIヘルスチェックをスキップしました"
fi

# 完了メッセージ
echo -e "\n${GREEN}"
cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                  セットアップ完了                                 ║
╚═══════════════════════════════════════════════════════════════════╝

InfluxDB 2.x のセットアップが完了しました！

【次のステップ】

1. 初期セットアップ（未実施の場合）:
   influx setup

2. データ確認:
   influx query 'from(bucket: "sensor_data") |> range(start: -1h) |> limit(n: 10)'

3. Telegrafのインストール・設定:
   ../telegraf/SETUP.md を参照

4. Grafanaのインストール・設定:
   ../grafana/SETUP.md を参照

【サービス管理コマンド】

- ステータス確認: sudo systemctl status influxdb
- サービス再起動: sudo systemctl restart influxdb
- ログ確認:       sudo journalctl -u influxdb -f

【詳細手順書】

  cloud_server/influxdb/SETUP.md を参照してください。

EOF
echo -e "${NC}"

exit 0
