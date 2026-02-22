# UniPi Agri HA カスタムイメージ

AgriHA用カスタムRaspbian イメージ

## 構成

```
Raspbian Lite 64-bit (bookworm)
├── Docker + Docker Compose
│   ├── Home Assistant（制御・ローカルUI）
│   ├── Node-RED（農業ロジック）
│   └── Mosquitto（MQTTブローカー）
├── EVOK（UniPi 1.1制御）
├── WireGuard（VPN）
├── RS485気象センサーサービス
├── 設定バックアップcron
└── カメラ対応（オプション）
```

## データ保存方針

- **ローカル（Pi）**: 制御・一時データのみ
- **クラウド（さくら）**: 長期保存（InfluxDB, Grafana, カメラ画像）
- **設定バックアップ**: 毎日クラウドへ同期

## ファイル構成

```
custom_image/
├── scripts/
│   ├── build_image.sh      # イメージビルドスクリプト
│   ├── first_boot.sh       # 初回起動時セットアップ
│   ├── install_evok.sh     # EVOK インストール
│   ├── install_docker.sh   # Docker インストール
│   └── backup_config.sh    # 設定バックアップ
├── config/
│   ├── wg0.conf.template   # WireGuard設定テンプレート
│   ├── evok.conf           # EVOK設定
│   └── settings.yaml       # デバイス固有設定
├── docker/
│   ├── docker-compose.yaml # HA + Node-RED + Mosquitto
│   └── ha-config/          # Home Assistant設定
└── overlays/
    └── config.txt          # /boot/config.txt 追記内容
```

## ビルド手順

1. Raspbian Lite 64-bit イメージをダウンロード
2. `build_image.sh` でイメージをカスタマイズ
3. SDカードに書き込み
4. 初回起動時に `first_boot.sh` が実行される

## 対応ハードウェア

- Raspberry Pi 3B/3B+/4B/Zero 2 W
- UniPi 1.1（I2C接続）
- RS485 USB-Serialアダプタ（CH341等）
- PoE IPカメラ（オプション）
