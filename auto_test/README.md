# Pico 2 W 無人自動テスト環境

Pico 2 WをUSB接続すると自動でテストが実行され、結果がLINEに通知される。

## ファイル構成

```
auto_test/
├── auto_test_runner.py    # メインテストスクリプト
├── notify.py              # 通知モジュール
├── test_config.json       # 設定ファイル
├── requirements.txt       # Python依存パッケージ
├── README.md              # このファイル
├── udev/
│   └── 99-pico-test.rules # USB検知ルール
├── systemd/
│   └── pico-test.service  # テストサービス
└── results/               # テスト結果（自動生成）
```

## セットアップ手順

### 1. Python依存パッケージのインストール

```bash
pip3 install -r requirements.txt
```

### 2. ユーザーをdialoutグループに追加

```bash
sudo usermod -a -G dialout $USER
# ログアウト・ログインが必要
```

### 3. ログディレクトリ作成

```bash
sudo mkdir -p /var/log/arsprout
sudo chown $USER:$USER /var/log/arsprout
```

### 4. LINE Notifyトークン設定

1. https://notify-bot.line.me/ja/ にアクセス
2. ログイン -> マイページ -> トークン発行
3. トークン名: "Pico Test" など
4. 通知先: 自分のLINEアカウント
5. `test_config.json` の `notification.token` に設定

```json
{
    "notification": {
        "type": "line",
        "token": "発行されたトークンをここに貼り付け"
    }
}
```

### 5. udevルール配置

```bash
sudo cp udev/99-pico-test.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### 6. systemdサービス配置

```bash
sudo cp systemd/pico-test.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## 使用方法

### 自動実行

Pico 2 W（CircuitPythonインストール済み）をUSB接続すると、
自動的にテストが実行されLINEに結果が通知される。

### 手動実行

```bash
# 通知なし
python3 auto_test_runner.py

# LINE通知あり
python3 auto_test_runner.py --notify

# 設定ファイル指定
python3 auto_test_runner.py --config /path/to/config.json --notify
```

### サービスとして手動起動

```bash
sudo systemctl start pico-test.service
```

## テスト項目

| ID | テスト項目 | 内容 | タイムアウト |
|----|-----------|------|-------------|
| USB-001 | デバイス認識 | Picoポートの検出 | 10秒 |
| USB-002 | シリアル接続 | 115200bpsで接続 | 5秒 |
| USB-003 | REPL応答 | Ctrl+Cでプロンプト表示 | 5秒 |
| USB-004 | CPU温度読み取り | microcontroller.cpu.temperature | 5秒 |
| USB-005 | エコーテスト | print()の応答確認 | 5秒 |

## ログ確認

```bash
# テストログ
tail -f /var/log/arsprout/pico_test.log

# udevイベントログ
journalctl -t pico-udev -f

# サービスログ
journalctl -u pico-test.service -f
```

## トラブルシューティング

### デバイスが認識されない

```bash
# 接続されているUSBデバイス確認
lsusb | grep -i pico

# シリアルポート確認
ls -la /dev/ttyACM*

# udevルール確認
udevadm test /sys/class/tty/ttyACM0
```

### 権限エラー

```bash
# dialoutグループ確認
groups $USER

# 手動で権限変更（一時的）
sudo chmod 666 /dev/ttyACM0
```

### CircuitPythonがインストールされていない

Pico 2 W用のCircuitPython UF2をインストール:
1. BOOTSELボタンを押しながらUSB接続
2. RPI-RP2ドライブが表示される
3. UF2ファイルをドラッグ&ドロップ

ダウンロード: https://circuitpython.org/board/raspberry_pi_pico2_w/

## 安全ガイドライン

- テスト中にPicoを抜かないこと
- 異常発生時はCtrl+Cで中断可能
- ログファイルのディスク使用量に注意

## 関連ドキュメント

- [CircuitPython Pico 2 W](https://circuitpython.org/board/raspberry_pi_pico2_w/)
- [LINE Notify API](https://notify-bot.line.me/doc/ja/)
- [udev man page](https://www.freedesktop.org/software/systemd/man/udev.html)
