# RPi OS ベース「一発稼働SDカードイメージ」設計書

> Version: 2.0
> Date: 2026-02-17
> Author: Yasunori Oi
> Status: Draft (事前調査 + 設計)
> Changelog: v2.0 - デュアルNIC構成・chrony/dnsmasq必須化・ビルド知見追記

---

## 1. 概要

農業ハウスIoT監視・制御システム用の、Raspberry Pi OS Lite ベースのカスタムSDカードイメージ設計書。
**「SDカードを焼いてRPiに挿すだけで、ブラウザからNode-RED UIにアクセスできる」** 状態を目指す。

### 1.1 なぜHA OSから切り替えるか

| 比較項目 | Home Assistant OS | RPi OS Lite |
|---------|------------------|-------------|
| パッケージ管理 | apt不可（Add-on経由のみ） | **apt使用可** |
| CSIカメラ | コンテナ経由、制限あり | **直接アクセス可能** |
| I2C/GPIO | コンテナ隔離で設定困難 | **dtoverlayで有効化→直接アクセス** |
| SSH | Add-on経由、機能制限 | **標準SSH** |
| カスタムサービス追加 | Dockerコンテナ必須 | **systemdで自由に追加** |
| RAM消費(idle) | RPi4で65-70%超 | **60-80 MiB** |
| CPU消費(idle) | 約3% | **1%未満** |
| ストレージ | Docker層で重い | **Lite版 約2.2 GiB** |
| OSSとして配布 | Buildroot系で敷居高い | **pi-gen/CustomPiOS等ツール豊富** |

**結論**: 農業IoT用途ではI2Cデバイスアクセス・CSIカメラ・apt自由度・リソース効率の全てでRPi OSが優位。HA OSの箱庭環境は不要（Mosquitto + Node-REDで十分）。

---

## 2. イメージ構成図

```
┌──────────────────────────────────────────────────────────┐
│                agriha-os v2.0 (.img.xz)                    │
│           RPi OS Lite 64bit (Bookworm)                     │
├──────────────────────────────────────────────────────────┤
│                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │  Mosquitto   │  │   Node-RED   │  │     avahi      │  │
│  │  MQTT Broker │  │  + Dashboard │  │    (mDNS)      │  │
│  │  port 1883   │  │  port 1880   │  │   .local       │  │
│  └──────┬───────┘  └──────┬───────┘  └────────────────┘  │
│         │                  │                               │
│         │   MQTT subscribe │                               │
│         └──────────────────┘                               │
│                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │   chrony     │  │   dnsmasq    │  │     SSH        │  │
│  │  NTP Str.2   │  │  DHCPD       │  │   port 22      │  │
│  │  (Starlink)  │  │  (eth0)      │  │                │  │
│  └──────────────┘  └──────────────┘  └────────────────┘  │
│                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │     ufw      │  │  fail2ban    │  │  unattended    │  │
│  │  firewall    │  │  SSH保護     │  │  -upgrades     │  │
│  └──────────────┘  └──────────────┘  └────────────────┘  │
│                                                            │
│  ─ ─ ─ ─ ─ ─ ─ ─ オプション（デフォルトOff） ─ ─ ─ ─ ─  │
│  ┌──────────────┐  ┌──────────────┐                       │
│  │ CSIカメラ    │  │ InfluxDB     │                       │
│  │ (motion/     │  │ + Grafana    │                       │
│  │  libcamera)  │  │              │                       │
│  └──────────────┘  └──────────────┘                       │
└──────────────────────────────────────────────────────────┘
```

### ネットワーク構成図（デュアルNIC）

```
                 Starlink ルーター
                 192.168.1.1
                      │
           ┌──────────┴──────────┐
           │                     │
     スマホ/PC              RPi (agriha)
     ブラウザ          ┌────────────────────┐
     :1880             │  wlan0             │
                       │  192.168.1.x       │←─ Starlink上流（DHCP）
                       │  用途: UI / SSH    │    NTP上流: 192.168.100.1
                       │                    │
                       │  eth0              │
                       │  192.168.100.1     │←─ 固定IP（PoEセグメント）
                       │  用途:             │
                       │   ├─ DHCPD(dnsmasq)│ option 42でNTP配布
                       │   ├─ MQTT Broker   │
                       │   └─ NTP Str.2     │ chrony
                       └─────────┬──────────┘
                                 │ eth0 (PoE HUB)
                    ┌────────────┼────────────┐
                    │            │            │
              ┌─────┴────┐ ┌────┴─────┐ ┌────┴─────┐
              │ W5500    │ │ W5500    │ │ Pico 2W  │
              │ Pico PoE │ │ Pico PoE │ │ (WiFi)   │
              │ DHCP IP  │ │ DHCP IP  │ │ mDNS     │
              │ NTP=opt42│ │ NTP=opt42│ │ agriha   │
              └──────────┘ └──────────┘ │ .local   │
                                        └──────────┘
```

### データフロー

```
W5500-EVB-Pico (PoE)               RPi (agriha-os)
┌─────────────────┐               ┌───────────────────────────┐
│ SHT40/SCD41     │── eth0 ─────▶│ Mosquitto (port 1883)     │
│ BMP280          │  192.168.100.x│     │ MQTT subscribe       │
│ PVSS-03(日射)   │   PoE給電     │     ▼                     │
└─────────────────┘               │ Node-RED (port 1880)      │
                                  │     ├─ Dashboard 2.0 UI   │
Pico 2W (WiFi)                    │     ├─ アラート判定       │
┌─────────────────┐               │     ├─ 制御コマンド       │
│ PVSS-03(日射)   │── WiFi ─────▶│     └─ 日射比例灌水       │
│ mDNS発見        │  192.168.1.x  │                           │
└─────────────────┘               │ chrony (NTP Stratum 2)    │
                                  │     上流: Starlink(Str.1) │
  スマホ/PC                       │                           │
  ┌──────────┐                    │ dnsmasq (DHCPD)           │
  │ ブラウザ │◀── http ──────────│     eth0: 192.168.100.0/24│
  │          │  agriha.local:1880 │     option 42: NTP配布    │
  └──────────┘                    └───────────────────────────┘
```

---

## 3. パッケージ一覧

### 3.1 必須パッケージ

| パッケージ | バージョン | インストール方法 | 用途 |
|-----------|-----------|---------------|------|
| Mosquitto | 2.x (apt) | `apt install mosquitto mosquitto-clients` | MQTTブローカー |
| Node-RED | 5.x (npm) | `npm install -g node-red` | フロー制御・UI |
| Node.js | 20 LTS (apt) | `apt install nodejs npm` | Node-RED実行環境 |
| @flowfuse/node-red-dashboard | latest | npm (package.json) | モニタリングUI (Vue.js/Vuetify3) |
| avahi-daemon | apt | プリインストール済み | mDNS (.local解決) |
| chrony | apt | `apt install chrony` | NTP Stratum 2サーバー（上流: Starlink） |
| dnsmasq | apt | `apt install dnsmasq` | DHCPD（eth0 PoEセグメント） + option 42 NTP配布 |
| ufw | apt | `apt install ufw` | ファイアウォール |
| fail2ban | apt | `apt install fail2ban` | SSH ブルートフォース対策 |
| unattended-upgrades | apt | `apt install unattended-upgrades` | 自動セキュリティアップデート |

### 3.2 オプションパッケージ（デフォルトOff）

| パッケージ | 用途 | 有効化方法 |
|-----------|------|-----------|
| motion / libcamera-apps | CSIカメラ映像配信 | `sudo apt install motion` + 設定ファイル編集 |
| InfluxDB | 時系列データ保存 | 公式リポジトリから apt install |
| Grafana | 高度なダッシュボード | 公式リポジトリから apt install |

---

## 4. ビルド手順

### 4.1 ビルドツール選定

| ツール | 方式 | メリット | デメリット | 推奨度 |
|--------|------|---------|----------|--------|
| **pi-gen** | ゼロからステージ制ビルド | 公式ツール、最も柔軟 | ビルド時間長い(20-30分) | **推奨** |
| CustomPiOS | 既存イメージ改変 | モジュール制、再利用性高い | pi-genより知名度低い | 次点 |
| rpi-image-gen | YAML宣言式(公式新) | SBOM/CVE対応、最新 | 2025年3月発表で実績少ない | 将来候補 |
| CustoPiZer | Docker内イメージ改変 | シンプル、OctoPrint実績 | 機能限定的 | 小規模向け |

**選定: pi-gen（公式ツール）** — ビルド実績あり（747MB、Pi 4動作確認済み）

### 4.2 pi-gen ステージ構成

```
pi-gen-agriha/
├── stage0/           # Bootstrap (debootstrap)
├── stage1/           # Minimal System
├── stage2/           # RPi OS Lite 相当
│   └── EXPORT_IMAGE  # ← ここでLiteイメージ出力（オリジナル）
├── stage3/           # SKIP（Desktop不要）
├── stage4/           # SKIP（Desktop不要）
├── stage5/           # SKIP（Full不要）
└── stage-agriha/     # ★ カスタムステージ
    ├── EXPORT_IMAGE
    ├── prerun.sh
    ├── 00-install-deps/
    │   ├── 00-packages           # apt: mosquitto, chrony, dnsmasq, ufw等
    │   └── 00-run-chroot.sh      # Node.js/Node-REDインストール
    ├── 01-configure-mosquitto/
    │   ├── files/
    │   │   ├── agri-iot.conf     # Mosquitto設定
    │   │   └── acl               # ACLファイル
    │   └── 00-run.sh             # ファイル配置 + systemd有効化
    ├── 02-configure-nodered/
    │   ├── files/
    │   │   ├── settings.js       # Node-RED設定
    │   │   ├── package.json      # プリインストールパッケージ
    │   │   └── flows.json        # 初期フロー（テンプレート）
    │   └── 00-run.sh             # ファイル配置 + npm install + systemd
    ├── 03-configure-network/
    │   ├── files/
    │   │   ├── avahi-daemon.conf # mDNS設定
    │   │   ├── chrony.conf       # NTP設定（Starlink上流 + Stratum 2サーバー）
    │   │   ├── dnsmasq.conf      # DHCPD設定（eth0セグメント + option 42）
    │   │   └── eth0-static.nmconnection  # eth0固定IP設定
    │   └── 00-run.sh             # ファイル配置 + systemd有効化
    └── 04-configure-security/
        ├── 00-run.sh             # ufw, fail2ban, SSH硬化, unattended-upgrades
        └── SKIP                  # ※現在SKIPあり（後述: 既知の問題）
```

### 4.3 pi-gen config ファイル

```bash
# 実運用中の config（pi-gen-agriha/config）
IMG_NAME=agriha-os
RELEASE=bookworm
TARGET_HOSTNAME=agriha
FIRST_USER_NAME=agriha
FIRST_USER_PASSWORD=
ENABLE_SSH=1
LOCALE_DEFAULT=en_US.UTF-8
KEYBOARD_KEYMAP=us
KEYBOARD_LAYOUT="English (US)"
TIMEZONE_DEFAULT=Asia/Tokyo
STAGE_LIST="stage0 stage1 stage2 stage-agriha"
```

> **注**: LOCALE/KEYBOARDは一般配布向けにen_US/usを採用。rpi-imagerで農家が個別設定可能。

### 4.4 GitHub Actions 自動ビルド

```yaml
name: Build AgriHA OS Image
on:
  push:
    tags: ['v*']
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: usimd/pi-gen-action@v1
        id: build
        with:
          image-name: agriha-os
          stage-list: stage0 stage1 stage2 ./stage-agriha
          release: bookworm
          compression: xz
          compression-level: 6
          hostname: agriha
          username: agriha
          enable-ssh: 1
          locale: en_US.UTF-8
          keyboard-keymap: us
          keyboard-layout: "English (US)"
          timezone: Asia/Tokyo
          increase-runner-disk-size: true
          export-last-stage-only: true

      - uses: actions/upload-artifact@v4
        with:
          name: agriha-os-image
          path: ${{ steps.build.outputs.image-path }}

      - name: Create Release
        if: startsWith(github.ref, 'refs/tags/')
        uses: softprops/action-gh-release@v2
        with:
          files: ${{ steps.build.outputs.image-path }}
```

**ビルド時間見積もり**: ubuntu-latestランナーで30-45分（Lite + カスタムステージ）

---

## 5. Mosquitto 設定詳細

### 5.1 /etc/mosquitto/conf.d/agri-iot.conf

```conf
# リスナー（LAN内全IF）
listener 1883 0.0.0.0

# 認証（allow_anonymous false はMosquitto 2.xデフォルト）
allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl

# 永続化（SDカード磨耗対策: 1時間間隔）
persistence true
persistence_location /var/lib/mosquitto/
autosave_interval 3600
autosave_on_changes false

# 制限
max_connections 50
message_size_limit 1048576
```

### 5.2 初期ユーザー

| ユーザー | 用途 | トピック権限 |
|---------|------|-------------|
| `nodered` | Node-REDからの接続 | 全トピック readwrite |
| `sensor` | センサーノードからの接続 | `sensor/+/data` write, `command/+` read |
| `admin` | 管理用 | 全トピック readwrite |

初期パスワードはイメージビルド時に設定し、READMEで変更を推奨。

### 5.3 TLS不要の根拠

- 通信はPoE有線LAN内に閉じている（物理的にアクセス制限）
- TLS証明書の管理コストがIoTノード側の実装複雑さに見合わない
- パスワード認証 + ufw ファイアウォールで十分なセキュリティ

---

## 6. Node-RED 設定詳細

### 6.1 インストール方法

pi-gen カスタムステージ内で apt + npm でインストール:

```bash
#!/bin/bash -e
# 00-run-chroot.sh（実装済み）

# Node.js (Debian standard)
apt-get install -y nodejs npm

# Node-RED
npm install -g --unsafe-perm node-red

# ユーザーディレクトリ作成
mkdir -p /home/${FIRST_USER_NAME}/.node-red
chown -R 1000:1000 /home/${FIRST_USER_NAME}/.node-red
```

### 6.2 プリインストールパッケージ (package.json)

```json
{
  "name": "agriha-nodered",
  "description": "AgriHA IoT Node-RED configuration",
  "version": "1.0.0",
  "dependencies": {
    "@flowfuse/node-red-dashboard": "~1.x"
  }
}
```

`node-red-dashboard`（旧）は2024年6月に非推奨化。`@flowfuse/node-red-dashboard`（Dashboard 2.0、Vue.js/Vuetify3ベース）を採用。

### 6.3 settings.js のポイント

```javascript
module.exports = {
    uiPort: 1880,
    flowFile: "flows.json",       // ホスト名に依存しない固定名
    credentialSecret: false,       // 初期は暗号化なし（初回起動で生成）
    externalModules: {
        autoInstall: true,         // 不足モジュール自動インストール
        palette: { allowInstall: true }
    }
}
```

### 6.4 systemdサービス（実装済み）

```ini
[Unit]
Description=Node-RED
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=agriha
Group=agriha
WorkingDirectory=/home/agriha/.node-red
ExecStart=/usr/local/bin/node-red --userDir /home/agriha/.node-red
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
SyslogIdentifier=node-red
Environment="NODE_OPTIONS=--max_old_space_size=256"

[Install]
WantedBy=multi-user.target
```

### 6.5 フローファイル配置

- `flows.json` をテンプレートとしてイメージに焼き込み
- 初期フロー: MQTT subscribe → Dashboard表示の基本構成
- ユーザーがフローを変更したら上書きされない（Node-REDが管理）

---

## 7. ネットワーク設定（デュアルNIC構成）

### 7.1 設計方針（2026-02-17殿裁定）

RPiの物理NIC 2つを活用し、用途でセグメント分離する。USB-NIC不要。

| インターフェース | IPアドレス | 用途 | 方式 |
|----------------|----------|------|------|
| **wlan0** | 192.168.1.x (DHCP) | Starlink上流、UI/SSHアクセス、NTP上流取得 | DHCP（Starlinkルーターから） |
| **eth0** | 192.168.100.1 (固定) | PoE HUB専用セグメント、DHCPD/MQTT/NTP | 固定IP + dnsmasq DHCPD |

### 7.2 eth0 固定IP設定（NetworkManager）

Bookworm以降は **NetworkManager** がデフォルト。

```bash
# NetworkManager接続プロファイル: eth0-static.nmconnection
[connection]
id=eth0-poe
type=ethernet
interface-name=eth0
autoconnect=true

[ipv4]
method=manual
addresses=192.168.100.1/24

[ipv6]
method=disabled
```

wlan0はDHCP（デフォルト動作、設定不要）。

### 7.3 dnsmasq（DHCPDサーバー on eth0）

```conf
# /etc/dnsmasq.d/agriha-eth0.conf

# eth0のみでDHCPサービス
interface=eth0
bind-interfaces

# DHCPレンジ
dhcp-range=192.168.100.10,192.168.100.126,255.255.255.128,12h

# デフォルトゲートウェイ（RPi自身）
dhcp-option=3,192.168.100.1

# NTP配布（option 42）— Picoノードへ自動配布
dhcp-option=42,192.168.100.1

# DNS（RPi自身 → 上流はStarlinkルーター）
dhcp-option=6,192.168.100.1

# MQTTブローカー通知（非標準、FWにハードコード推奨）
# W5500: ブローカー=デフォルトゲートウェイ=NTP=同一IP(192.168.100.1)
```

> **設計思想**: W5500ノードから見て、ブローカー=NTP=ゲートウェイ=192.168.100.1。全て同一IPで設定統一。DHCPDの競合ゼロ（物理セグメント分離のため、Starlinkルーターと干渉しない）。

### 7.4 chrony（NTP Stratum 2サーバー）

```conf
# /etc/chrony/chrony.conf

# 上流: Starlink ディッシュ NTPサーバー（Stratum 1, GPS由来）
# 実測: offset -1ms, delay 30ms（2026-02-17実機確認済み）
server 192.168.100.1 iburst prefer

# Stratum 2サーバーとしてeth0セグメントにNTP提供
allow 192.168.100.0/24

# ローカルクロックをフォールバック（Starlink不通時）
local stratum 10

# RTC同期
rtcsync
```

> **Starlink NTP実績**: 192.168.1.0/24ネットワークからStarlinkディッシュ(192.168.100.1)へのNTP到達確認済み。stratum 1, offset -1ms, delay 30ms。GPSモジュール自作は不要。

> **注意**: Starlink NTPの192.168.100.1はStarlinkディッシュのIPであり、RPi eth0の192.168.100.1とは別物。Starlinkディッシュへの到達性はwlan0側（192.168.1.0/24）を経由する。chronyの上流設定はwlan0経由で192.168.100.1(Starlink)に接続し、eth0の192.168.100.0/24にStratum 2として再配布する構成。

### 7.5 ホスト名とmDNS

- デフォルトホスト名: `agriha`（rpi-imagerで変更可能）
- mDNS: avahi-daemon プリインストール済み → `agriha.local` でアクセス可能
- avahi: eth0, wlan0 両方でmDNS応答
- 複数RPi: rpi-imagerでホスト名を `agriha-01`, `agriha-02` 等に変更
- Windows: Win10 1903以降はmDNSネイティブ対応（Bonjour不要）
- **Pico 2W**: WiFi + mDNS(`agriha.local`)でブローカーを自動発見
- **W5500**: eth0セグメント、DHCP取得IP + ブローカー=192.168.100.1（ゲートウェイと同一）

---

## 8. 初回セットアップフロー

### 8.1 一般農家向け最短手順

```
Step 1: rpi-imager をPCにインストール
         https://www.raspberrypi.com/software/

Step 2: SDカードをPCに挿入

Step 3: rpi-imager 起動
         ├─ デバイス: Raspberry Pi 4/5 を選択
         ├─ OS: 「カスタムイメージを使う」→ agriha-os.img.xz を選択
         └─ ストレージ: SDカードを選択

Step 4: 歯車アイコン「設定を編集する」
         ├─ ホスト名: agriha（複数台なら agriha-01 等）
         ├─ ユーザー名: agriha
         ├─ パスワード: （自分で決める）
         ├─ WiFi: SSID/パスワード入力（Starlinkルーター等）
         └─ SSH: 有効、パスワード認証

Step 5: 「書き込む」→ 3-5分で完了

Step 6: RPiにPoE HAT装着 + LANケーブルでPoEスイッチに接続

Step 7: SDカードをRPiに挿して電源ON（PoE給電）

Step 8: 1-2分待つ

Step 9: ブラウザで http://agriha.local:1880
         → Node-RED UI が表示される → 完了!
```

### 8.2 デュアルNIC構成（メイン想定）

```
Starlinkルーター (192.168.1.1)
  │
  ├── WiFi ──── RPi wlan0 (192.168.1.x DHCP)
  │                 └─ UI/SSHアクセス、NTP上流
  │
  └── (別セグメント)
          RPi eth0 (192.168.100.1 固定)
           │    └─ DHCPD + MQTT Broker + NTP Stratum 2
           │
          PoE HUB
           ├── W5500 Pico PoE ノード1 (192.168.100.10~ DHCP)
           ├── W5500 Pico PoE ノード2
           └── ...

  WiFi ──── Pico 2W ノード (192.168.1.x DHCP, mDNS発見)
```

- W5500ノード: PoE給電 + eth0セグメントでDHCP自動取得。NTPもoption 42で自動配布
- Pico 2Wノード: WiFi接続 + mDNS(`agriha.local`)でブローカー自動発見
- スマホ/PC: WiFiからagriha.local:1880でNode-RED UIアクセス

---

## 9. セキュリティ設定

### 9.1 層別セキュリティ

| 層 | 対策 | 状態 |
|----|------|------|
| ネットワーク | ufw（22, 123, 1880, 1883, 5353のみ開放） | **初回起動後に手動設定**（※1） |
| SSH | PermitRootLogin no, MaxAuthTries 3 | **イメージに焼き込み** |
| SSH保護 | fail2ban（3回失敗→1時間BAN） | **初回起動後に手動設定**（※1） |
| MQTT | allow_anonymous false + パスワード認証 | **イメージに焼き込み** |
| OS更新 | unattended-upgrades（セキュリティパッチ自動） | **イメージに焼き込み** |
| SSH鍵認証 | READMEで推奨 | **手動（READMEで案内）** |
| Node-RED adminAuth | READMEで推奨 | **手動（READMEで案内）** |

> **※1 既知の問題**: pi-gen chroot環境内でiptables操作（ufw enable, fail2ban）が失敗する制約あり。stage-agriha/04-configure-security/SKIPファイルにより現在スキップ中。初回起動後のセットアップスクリプト（`/usr/local/bin/agriha-security-setup.sh`）で対応予定。

### 9.2 ufwルール

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 123/udp   # NTP（chrony Stratum 2サーバー）
ufw allow 1880/tcp  # Node-RED
ufw allow 1883/tcp  # MQTT
ufw allow 5353/udp  # mDNS
```

### 9.3 自動アップデート対象

| パッケージ | 更新方式 | 理由 |
|-----------|---------|------|
| Raspbian/Debianセキュリティパッチ | **自動** | セキュリティ最優先 |
| RPi Foundationパッケージ | **自動** | カーネル・FW安定性 |
| Node-RED | **手動** | バージョンアップでフロー互換性の問題あり |
| Mosquitto | **手動** | 設定互換性の確認必要 |

---

## 10. イメージ配布方法

### 10.1 配布形式

| 項目 | 値 |
|------|-----|
| ファイル名 | `agriha-os-v2.0.0.img.xz` |
| 圧縮形式 | xz（rpi-imager直接書き込み対応） |
| 実測サイズ | **747 MB**（圧縮後、v1.0ビルド実績） |
| 配布先 | GitHub Releases |
| 対応機種 | Raspberry Pi 4/5 (64bit) |

### 10.2 rpi-imager カスタムリポジトリ

```json
{
  "os_list": [
    {
      "name": "AgriHA OS",
      "description": "Agriculture IoT monitoring system (Mosquitto + Node-RED)",
      "url": "https://github.com/yasunorioi/unipi-agri-ha/releases/download/v2.0.0/agriha-os-v2.0.0.img.xz",
      "release_date": "2026-02-17",
      "devices": ["pi4-64bit", "pi5-64bit"],
      "init_format": "systemd"
    }
  ]
}
```

---

## 11. 先行事例まとめ

### 11.1 参考にすべきプロジェクト

| プロジェクト | 参考ポイント |
|------------|-------------|
| **OctoPi** (★2,600) | CustomPiOSによるカスタムイメージ構築の実績。boot設定ファイル方式 |
| **FullPageOS** (★4,300) | CustomPiOSモジュール構成の参考 |
| **MtigOS** | Mosquitto+Telegraf+InfluxDB+Grafanaスタックのpi-gen構築 |
| **DietPi** | 軽量化の手法（RAM 118MiB, ブート6.2秒） |
| **FarmBot** | Nervesフレームワーク(Elixir)による組み込みOS。方向性は異なるが参考 |
| **Farm-Data-Relay-System** | ESP-NOW+LoRaセンサーネットワーク。無線補助の参考 |
| **OpenMinder** | RPi HAT基板+API。ハードウェア設計の参考 |

### 11.2 農業IoT向けNode-RED+MQTTの構成パターン

MING (Mosquitto + InfluxDB + Node-RED + Grafana) スタックが業界標準構成。
balena MINGプロジェクトがDocker Composeでの構成例を提供している。
AgriHA OSはInfluxDB/Grafanaをオプションとし、初期は軽量構成で配布。

---

## 12. オプション機能の有効化手順（README記載用）

### 12.1 CSIカメラ

```bash
sudo apt install motion
sudo nano /etc/motion/motion.conf
# stream_port 8081
# stream_localhost off
sudo systemctl enable motion
sudo systemctl start motion
# ブラウザで http://agriha.local:8081 でライブ映像
```

### 12.2 InfluxDB + Grafana（高度なデータ可視化）

```bash
# InfluxDB 2.x
curl -s https://repos.influxdata.com/influxdata-archive_compat.key | gpg --dearmor > /etc/apt/trusted.gpg.d/influxdata-archive.gpg
echo 'deb https://repos.influxdata.com/debian stable main' > /etc/apt/sources.list.d/influxdata.list
sudo apt update && sudo apt install influxdb2

# Grafana
sudo apt install grafana
sudo systemctl enable grafana-server
# http://agriha.local:3000
```

---

## 13. 既知の問題・ビルド知見

### 13.1 pi-genビルド実績からの知見

| 問題 | 状況 | 対処 |
|------|------|------|
| **kernel8.img gzip圧縮** | 64bit kernelの起動失敗（HDMIケーブル問題と同時発生、真因未確定） | `config.txt`で`kernel=kernel7.img`(32bit)を強制指定で回避中。要検証 |
| **ufw/fail2ban chroot制約** | pi-gen chroot環境内でiptables操作が失敗する | `04-configure-security/SKIP`ファイルで当該ステージをスキップ。初回起動スクリプトで対応予定 |
| **systemd-networkd eth0** | pi-genイメージにeth0 DHCP設定が含まれていなかった | 手動追加で解決済み。デュアルNIC構成ではNetworkManagerプロファイルで明示設定 |

### 13.2 初回起動スクリプト（セキュリティ設定用）

ufw/fail2banのchroot制約を回避するため、初回起動時に実行するスクリプトを検討:

```bash
#!/bin/bash
# /usr/local/bin/agriha-security-setup.sh
# 初回起動時にsystemdのoneshot serviceで実行

# ufw設定
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 123/udp
ufw allow 1880/tcp
ufw allow 1883/tcp
ufw allow 5353/udp
ufw --force enable

# fail2ban設定
cat > /etc/fail2ban/jail.d/sshd.conf << 'JAIL'
[sshd]
enabled = true
port = ssh
filter = sshd
maxretry = 3
bantime = 3600
findtime = 600
JAIL
systemctl enable fail2ban
systemctl start fail2ban

# 自身を無効化（1回だけ実行）
systemctl disable agriha-security-setup.service
```

---

## 14. 今後の課題・判断事項

| 項目 | 選択肢 | 推奨 | 状態 |
|------|--------|------|------|
| ベースOS | Bookworm vs Trixie | Bookworm（安定版） | **確定: Bookworm** |
| ビルドツール | pi-gen | pi-gen（実績あり） | **確定** |
| Node-RED Dashboard | Dashboard 2.0 | Dashboard 2.0 | **確定** |
| InfluxDB/Grafana | オプション | オプション（初期は軽量に） | **確定** |
| ネットワーク構成 | デュアルNIC | wlan0+eth0分離 | **確定（2026-02-17殿裁定）** |
| NTP | chrony + Starlink | Stratum 2サーバー | **確定（実機確認済み）** |
| DHCPD | dnsmasq on eth0 | option 42でNTP配布 | **確定** |
| kernel8.img問題 | 32bit回避 or 要検証 | デバッガー到着後に検証 | **保留** |
| ufw chroot問題 | 初回起動スクリプト | oneshot service | **設計済み、実装待ち** |
| イメージ名 | agriha-os | — | **確定** |
| 配布リポジトリ | yasunorioi/unipi-agri-ha | GitHub Releases | **確定** |
| W5500 DHCP option 42 | lwIPで対応確認 | 非対応ならFWハードコード | **要実機検証** |
