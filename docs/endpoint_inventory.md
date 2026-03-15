# uecs-llm FastAPI エンドポイント棚卸し

> **作成日**: 2026-03-16 | **作成者**: ashigaru2 (subtask_907/cmd_413)
> **対象リビジョン**: main ブランチ現時点

---

## 1. unipi-daemon REST API（`src/agriha/daemon/rest_api.py`）

**ポート**: 8080（`config/unipi_daemon.example.yaml` の `rest_api.port`）
**認証**: `X-API-Key` ヘッダー（`rest_api.api_key` が空文字の場合はスキップ）
**実装クラス**: `RestApi._setup_routes()`

| # | メソッド | パス | リクエスト | レスポンス | ステータスコード | 用途 | ファイル:行 |
|---|---------|------|-----------|-----------|----------------|------|-----------|
| 1 | POST | `/api/relay/{ch}` | Path: `ch` (int, 1-8)<br>Body: `{"value": 0\|1, "duration_sec": float, "reason": str}` | `{"ch": N, "value": 0\|1, "queued": true}` | 202 成功<br>423 ロックアウト中<br>503 MQTT未接続<br>403 認証エラー | リレー ch ON/OFF → MQTT publish | rest_api.py:254 |
| 2 | GET | `/api/sensors` | なし | `{"sensors": {...}, "updated_at": float, "age_sec": float\|null}` | 200<br>403 認証エラー | 最新センサーキャッシュ取得（MQTT subscribe済みデータ） | rest_api.py:302 |
| 3 | GET | `/api/status` | なし | `{"house_id": str, "uptime_sec": int, "locked_out": bool, "lockout_remaining_sec": float, "relay_state": {"ch1"..ch8: bool}\|null, "ts": float}` | 200<br>403 認証エラー | デーモン状態 + リレー状態 + ロックアウト状態 | rest_api.py:324 |
| 4 | POST | `/api/emergency/clear` | なし | `{"cleared": true, "was_locked_out": bool}` | 200<br>403 認証エラー | 緊急スイッチによるロックアウト手動解除 | rest_api.py:354 |

### MQTT サブスクリプション（センサーキャッシュ用）

| トピック | 内容 |
|---------|------|
| `agriha/{house_id}/sensor/#` | ハウス固有センサーデータ |
| `agriha/farm/weather/misol` | 農場気象データ（Misol） |
| `agriha/{house_id}/relay/state` | リレー状態 |
| `agriha/{house_id}/ccm/#` | UECS-CCM 内気象/アクチュエータ |

---

## 2. Web UI + LINE Bot FastAPI（`src/agriha/chat/app.py`）

**ポート**: 8502（ファイル冒頭ドキュメント）
**認証**: HTTP Basic 認証（環境変数 `UI_AUTH_USER` / `UI_AUTH_PASS`、デフォルト `admin/agriha`）
**対外URL**: nginx（port 80）が `/` → `127.0.0.1:8501` にプロキシ（※nginx.confは8501、ファイル冒頭は8502 — 要確認）

### 2-1. HTML 画面エンドポイント

| # | メソッド | パス | リクエスト | レスポンス | ステータスコード | 用途 | ファイル:行 |
|---|---------|------|-----------|-----------|----------------|------|-----------|
| 1 | GET | `/` | なし | HTML（ダッシュボード） | 200<br>401 認証失敗 | ダッシュボード画面（センサー・計画・リレー状態） | app.py:535 |
| 2 | GET | `/api/dashboard-partial` | なし | HTML（`<main>` 内部部分） | 200<br>401 | htmx ポーリング用部分テンプレート | app.py:545 |
| 3 | GET | `/settings` | Query: `saved`, `error`（省略可） | HTML（設定画面） | 200<br>401 | 設定画面（閾値・systemPrompt・rules.yaml等） | app.py:557 |
| 4 | GET | `/history` | なし | HTML（制御履歴） | 200<br>401 | 制御履歴画面（直近24時間の decisions） | app.py:828 |

### 2-2. 設定保存 POST エンドポイント（全て `303 リダイレクト` 返却）

| # | メソッド | パス | フォームパラメータ | 成功時リダイレクト | 用途 | ファイル:行 |
|---|---------|------|-----------------|-----------------|------|-----------|
| 5 | POST | `/settings/prompt` | `prompt_text: str` | `/settings?saved=1` | system_prompt.txt 保存 | app.py:596 |
| 6 | POST | `/settings/thresholds` | `high_temp: float`, `low_temp: float`, `co2_target: int` | `/settings?saved=1` | thresholds.yaml 保存（バリデーション: high>low, 0<co2<5000） | app.py:609 |
| 7 | POST | `/settings/rules` | `rules_text: str` | `/settings?saved=1` | rules.yaml 保存（yaml.safe_load 構文チェック） | app.py:632 |
| 8 | POST | `/settings/channel_map` | `channel_map_text: str` | `/settings?saved=1` | channel_map.yaml 保存（yaml.safe_load 構文チェック） | app.py:649 |
| 9 | POST | `/settings/forecast` | `forecast_config_text: str` | `/settings?saved=1` | forecast.yaml 保存（yaml.safe_load 構文チェック） | app.py:666 |
| 10 | POST | `/settings/llm_provider` | `provider: str`, `model: str`（省略可）, `api_key: str`（省略可） | `/settings?saved=1` | LLMプロバイダー選択 + APIキー保存（forecast.yaml + .env 更新） | app.py:683 |

### 2-3. LINE Bot Webhook

| # | メソッド | パス | リクエスト | レスポンス | ステータスコード | 用途 | ファイル:行 |
|---|---------|------|-----------|-----------|----------------|------|-----------|
| 11 | POST | `/callback` | Header: `X-Line-Signature`<br>Body: LINE Webhook JSON | `{"status": "ok"}` | 200 成功<br>403 署名不正<br>400 JSON不正<br>503 LINE未設定 | LINE Webhook 受信 → LLM tool calling → Reply | app.py:733 |

### 2-4. JSON API エンドポイント

| # | メソッド | パス | リクエスト | レスポンス | ステータスコード | 用途 | ファイル:行 |
|---|---------|------|-----------|-----------|----------------|------|-----------|
| 12 | GET | `/api/flags` | なし | `{"lockout": bool, "rain": bool, "wind": bool}` | 200<br>401 | フラグファイル状態（lockout/rain_flag/wind_flag）取得 | app.py:848 |
| 13 | GET | `/api/plan` | なし | current_plan.json の内容 or `{"plan": null}` | 200<br>401 | 現在の計画（current_plan.json）取得 | app.py:854 |
| 14 | GET | `/api/dashboard` | なし | センサー・計画・relay・フラグ・ログを集約したJSON | 200<br>401 | 集約エンドポイント（§3.5） | app.py:860 |
| 15 | GET | `/api/logs` | Query: `lines: int`（デフォルト50, max200） | `{"control_log": [...], "search_log": [...], "forecast_log": [...], "emergency_log": [...]}` | 200<br>401 | 4ログファイルの末尾N行取得 | app.py:879 |

---

## 3. LINE Bot ハンドラ（`src/agriha/chat/linebot_handler.py`）

**ルート定義なし**（`app.py` の `/callback` から呼び出されるモジュール）
LLM tool calling で unipi-daemon REST API を**内部呼び出し**する。

### LLMツール定義（`LINEBOT_TOOLS`）

| ツール名 | 内部呼び出し先 | 引数 | 説明 |
|---------|--------------|------|------|
| `get_sensors` | GET `/api/sensors` | なし | 全センサーデータ取得 |
| `get_status` | GET `/api/status` | なし | デーモン状態取得 |
| `set_relay` | POST `/api/relay/{ch}` | `channel: int`, `value: 0\|1`, `duration_sec: int`（省略可） | リレー制御（NullClawクライアント時は無効化） |

---

## 4. 設定サマリ

| 項目 | 値 | 出典 |
|-----|---|------|
| unipi-daemon ポート | 8080 | config/unipi_daemon.example.yaml |
| Web UI ポート（ファイル冒頭） | 8502 | src/agriha/chat/app.py:6 |
| nginx proxy先（UI） | 127.0.0.1:**8501** | config/nginx.conf:7 ⚠️ポート不一致 |
| nginx proxy先（/api/sensors, /api/relay） | 127.0.0.1:8080 | config/nginx.conf:13,16 |
| 認証方式（daemon） | X-API-Key ヘッダー | rest_api.py:237 |
| 認証方式（Web UI） | HTTP Basic | app.py:56 |
| CORS設定 | **未設定**（FastAPIデフォルト） | — |
| ロックアウト期間 | 300秒（自動解除） | rest_api.py:274 |

### ⚠️ 要確認事項

1. **nginx.conf のポート不一致**: UI は `8502`（app.py ドキュメント）だが nginx は `8501` にプロキシ。実際の起動コマンドを確認要。
2. **CORS未設定**: FastAPI デフォルトでは CORS ヘッダーなし。外部ドメインからの JS リクエストは失敗する。
3. **nginx で未公開のエンドポイント**: `/api/status`, `/api/emergency/clear`, `/api/logs` 等はnginx経由では到達不可（直接8080/8502アクセスのみ）。

---

## 5. エンドポイント全体図

```
nginx :80
├── /                       → agriha-chat :8501 (※要確認, app.py冒頭は8502)
│   ├── GET  /
│   ├── GET  /api/dashboard-partial
│   ├── GET  /settings
│   ├── POST /settings/prompt
│   ├── POST /settings/thresholds
│   ├── POST /settings/rules
│   ├── POST /settings/channel_map
│   ├── POST /settings/forecast
│   ├── POST /settings/llm_provider
│   ├── POST /callback          ← LINE Webhook
│   ├── GET  /history
│   ├── GET  /api/flags
│   ├── GET  /api/plan
│   ├── GET  /api/dashboard
│   └── GET  /api/logs
├── /api/sensors            → unipi-daemon :8080
└── /api/relay              → unipi-daemon :8080

unipi-daemon :8080 (直接アクセスも可)
├── POST /api/relay/{ch}
├── GET  /api/sensors
├── GET  /api/status
└── POST /api/emergency/clear
```
