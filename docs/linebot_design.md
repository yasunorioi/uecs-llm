# LINE Bot + Cloudflare Tunnel 設計書

> Version: 1.0
> Date: 2026-03-05

## 1. 背景と優先度

殿の明言（2026-02-28）:
> 「LINE Botが一番大事な機能」

機能優先順位:
1. 「開けろ」「閉めろ」— リモート制御（旧VPS版で稼働実績あり）
2. 「今どうなってる？」— 状況確認（同上）
3. 異常値の早期通知 — 緊急時LINE push
4. 自動制御 — 三層制御（稼働中）

旧LINE BotはVPS上でDocker運用していたが、VPS依存を廃止し
RPi上で直接動かす設計に変更する。Webhookの到達にはCloudflare Tunnelを使う。

## 2. アーキテクチャ

```
LINE Platform
  │ Webhook POST
  ▼
Cloudflare Tunnel (cloudflared)
  │ localhost:8501/callback にトンネル
  ▼
agriha-ui (FastAPI, ポート8501)
  │ /callback エンドポイント
  │   1. LINE署名検証
  │   2. 全テキスト → LLM API (OpenAI SDK互換) → tool calling
  │   3. LLM が system_prompt.txt に従い判断・REST API 呼び出し
  │   4. LINE Reply API で応答
  ▼
unipi-daemon (REST API, ポート8080)
  │ リレー制御・センサー取得
  ▼
UniPi 1.1 ハードウェア
```

### 設計判断

- **agriha-uiに統合**: 別サービスにせず agriha-ui (FastAPI) に `/callback` を追加
  - 理由: system_prompt, forecast.yaml, .env を共有。systemdサービスを増やさない
  - LLM呼び出しは forecast_engine と同じ OpenAI SDK 互換クライアント
- **VPS不要**: Cloudflare Tunnel で LINE Webhook を RPi に直接到達させる
- **LINE SDK最小限**: `line-bot-sdk` を使用。Webhook受信 + Reply/Push のみ

## 3. メッセージ処理

### 3a. 全テキスト → LLM（ショートコマンドなし）

全てのテキストメッセージをLLM APIに転送する。ショートコマンドのハードコードはしない。

- system_prompt.txt に「開けろ→全側窓開」等のコマンド定義を記述
- LLMがsystem_promptに従い、tool callingでリレー制御・センサー取得を実行
- ツール定義: get_sensors, get_status, set_relay（forecast_engineと共通 + set_relay追加）
- set_relay はLINE Bot経由のみ許可（forecast_engineでは除外済み）
- channel_map.yaml のgroups情報もsystem_promptに注入（開/閉チャンネル対応）

利点:
- コマンド追加・変更がsystem_prompt.txt編集だけで完結（コード変更不要）
- 農家の方言・言い回しにLLMが対応（「窓あけといて」「ちょっと閉めて」等）
- 怒り駆動: 「それ違う！」→ system_prompt追記 → 即反映

### 3b. Push通知（RPi→LINE）

- 緊急制御発動時（emergency_guard.sh から curl）
- 異常値検出時（rule_engine.py から呼び出し）
- 通知先: LINE_USER_ID（.env設定）

## 4. Cloudflare Tunnel

### 4a. 仕組み

cloudflared がRPiからCloudflareにアウトバウンド接続を張り、
Cloudflare側で公開URLを提供。固定IP・ポート開放・VPN不要。

### 4b. セットアップ手順

```bash
# 1. cloudflaredインストール
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# 2. トンネル作成（Cloudflareダッシュボードで実施、トークン取得）
# → CLOUDFLARE_TUNNEL_TOKEN を .env に記入

# 3. systemdサービス
cloudflared service install <TOKEN>
# → /etc/systemd/system/cloudflared.service が作成される
```

### 4c. 設定

```yaml
# /etc/cloudflared/config.yml (cloudflaredが自動生成)
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: agriha.example.com
    service: http://localhost:8501
  - service: http_status:404
```

LINE Developers Console の Webhook URL に `https://agriha.example.com/callback` を設定。

### 4d. AgriHA設定画面での管理

settings画面に「ネットワーク設定」セクション追加:
- Cloudflare Tunnel トークン入力 → .env に保存
- cloudflared サービス状態表示（running/stopped）
- Webhook URL 表示（設定済みなら）

## 5. ファイル構成（新規・変更）

```
src/agriha/chat/
  app.py              # /callback エンドポイント追加, /settings にLINE Bot設定追加
  linebot_handler.py  # LINE Webhook処理（署名検証・コマンド解析・LLM呼び出し・Reply）

config/
  .env.example        # LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID,
                      # CLOUDFLARE_TUNNEL_TOKEN 追加

pyproject.toml        # line-bot-sdk 追加（daemon extras）
```

### linebot_handler.py の責務

- `verify_signature(body, signature, secret)` — LINE署名検証
- `handle_message(text, llm_client, llm_cfg, system_prompt, http_client)` — テキスト→LLM tool calling→応答テキスト生成
- `call_tool(http_client, base_url, api_key, tool_name, tool_input)` — REST API呼び出し（forecast_engineと共通ロジック）
- `send_push(user_id, message, token)` — Push通知送信

## 6. .env 追加項目

```bash
# === LINE Bot ===
export LINE_CHANNEL_SECRET=        # Webhook署名検証用
export LINE_CHANNEL_ACCESS_TOKEN=  # Reply/Push API用
export LINE_USER_ID=               # Push通知先ユーザーID

# === Cloudflare Tunnel ===
export CLOUDFLARE_TUNNEL_TOKEN=    # cloudflared service install で使用
```

## 7. settings画面 追加セクション

### 7a. LINE Bot設定

```
┌─────────────────────────────────┐
│ LINE Bot設定                     │
│                                  │
│ Channel Secret:  [********cret] │
│ Access Token:    [********oken] │
│ User ID:         [********efgh] │
│                                  │
│ [保存]                           │
│                                  │
│ 状態: 設定済み / 未設定          │
└─────────────────────────────────┘
```

### 7b. ネットワーク設定（Cloudflare Tunnel）

```
┌─────────────────────────────────┐
│ ネットワーク設定                 │
│                                  │
│ Cloudflare Tunnel Token:         │
│ [________________________________] │
│                                  │
│ [保存してトンネル開始]            │
│                                  │
│ 状態: running / stopped          │
│ Webhook URL: https://xxx.trycloudflare.com/callback │
└─────────────────────────────────┘
```

## 8. 実装フェーズ

### Phase 1: LINE Bot バックエンド + LLM tool calling
- linebot_handler.py 新規作成
- 全テキスト → LLM API → tool calling（get_sensors, get_status, set_relay）
- app.py に /callback 追加
- system_prompt.txt にLINE Botコマンド定義を追記
- テスト（署名検証・LLM tool calling・Reply）
- line-bot-sdk を pyproject.toml に追加

### Phase 2: Push通知
- emergency_guard.sh からの緊急通知
- rule_engine.py からの異常値通知
- config/emergency.conf の LINE設定を .env に統合

### Phase 3: Cloudflare Tunnel + settings UI
- cloudflared インストールスクリプト
- settings画面にLINE Bot設定・ネットワーク設定セクション追加
- setup.sh に cloudflared セットアップ追加（オプション）

### Phase 4: RPiデプロイ + 動作確認
- RPiに全成果物デプロイ
- LINE Developers Console で Webhook URL 設定
- 「開けろ」「閉めろ」「状況」の動作確認

## 9. 旧LINE Bot との差分

| 項目 | 旧（VPS版） | 新（RPi直接版） |
|------|------------|----------------|
| 実行場所 | VPS (Docker) | RPi (agriha-ui内) |
| LLM | Anthropic SDK直接 | OpenAI SDK互換（マルチプロバイダー） |
| センサー取得 | SSH経由 or MQTT | localhost REST API (直接) |
| Webhook到達 | VPS固定IP | Cloudflare Tunnel |
| 設定管理 | VPS上.env | RPi /opt/agriha/.env + settings UI |
| system_prompt | linebot/system_prompt.py | /etc/agriha/system_prompt.txt (共用) |
| 依存サービス | Docker, Nginx, Certbot | cloudflared のみ |

## 10. セキュリティ考慮

- LINE Webhook署名検証は必須（HMAC-SHA256）
- Cloudflare Tunnel はアウトバウンド接続のみ（ポート開放不要）
- .env のAPIキー/トークンはsettings画面でマスク表示
- agriha-ui の Basic認証はLAN内アクセス用。LINE Webhookは署名検証で保護
- set_relay はLINE Bot経由のみ許可（LINE_USER_ID一致確認）
