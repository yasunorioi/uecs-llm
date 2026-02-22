# uecs-ccm-llama アーキテクチャ設計書

> **Version**: 1.0
> **Date**: 2026-02-20
> **Status**: Draft（殿レビュー待ち）
> **Model**: LFM2.5-1.2B-Instruct (Liquid AI)
> **HW**: vx2.local / nipogi.local / **Pi5 (8GB)** ← 新規追加

---

## 概要

uecs-ccm-llamaは、**LLM推論 + UECS-CCM受信 + MQTT制御**を統合したOSSである。
温室環境データをリアルタイム受信し、LLMがtool callingで制御判断を下す。

### モデル選定: LFM2.5-1.2B-Instruct

| 項目 | 旧候補: Nemotron 9B | **採用: LFM2.5-1.2B-Instruct** |
|------|---------------------|-------------------------------|
| パラメータ | 9B | **1.2B** |
| GGUF Q4_K_M | 6.1 GB | **698 MB**（1/9） |
| RAM消費 | +5.1 GiB | **+1.5 GiB**（1/3） |
| ロード時間 | 数十秒 | **0.5秒** |
| 日本語 tok/s | 5.2（thinking強制） | **46.9**（9倍） |
| Tool calling tok/s | 4.8 | **31.5**（7倍） |
| 1クエリ所要時間 | 200-360秒 | **3-4秒** |
| Ollama対応 | ❌（nemotron_hクラッシュ） | **✅**（Modelfile Template設定でtool calling可） |
| llama-cpp-python v0.3.16 | ❌（未対応） | **✅**（lfm2アーキ直接対応） |
| Pi5(8GB)搭載 | ❌（容量不足） | **✅**（余裕） |

> **JP版ではなくInstruct版を採用する理由**: JP版はtool calling未サポート。
> 温室制御にはtool callingが必須のため、Instruct版一択。
> Instruct版でも日本語JMMLU 47.7（Qwen3-1.7Bと同等）で実用的。

### ライセンス

**LFM Open License v1.0**（Apache 2.0ベース + 収益制限）
- 年商$10M未満: ロイヤリティフリーで商用利用・組み込み・再配布可
- 年商$10M超: 別途有償ライセンス（sales@liquid.ai）
- 殿の事業規模では問題なし

---

## 目次

1. [推論エンジン選定](#1-推論エンジン選定)
2. [アーキテクチャ](#2-アーキテクチャ)
3. [既存設計書との差分整理](#3-既存設計書との差分整理)
4. [リポジトリ構成案](#4-リポジトリ構成案)
5. [uecs-ccm-mcp との関係整理](#5-uecs-ccm-mcp-との関係整理)

---

## §1. 推論エンジン選定

### 1.1 3つの選択肢と実機PoC結果

vx2.local (Ryzen 5 7430U, 30GB RAM) での実測値:

| エンジン | 日本語 tok/s | Tool calling | ロード時間 | 備考 |
|---------|-------------|-------------|-----------|------|
| **Ollama (Modelfile)** | **49.4** | ❌→△（Template設定で可能見込み） | <1秒 | セットアップ最簡。農家配布に最適 |
| **llama-cpp-python v0.3.16** | **45.2** | ❌（jinja非対応でdecodeエラー） | **0.5秒** | 1プロセス統合可。tool callingは別経路 |
| **llama-server --jinja** | **46.9** | **✅ 完全動作** | <1秒 | tool calling + OpenAI互換API。HTTP経由 |

### 1.2 推奨構成: llama-server --jinja（構成A）

**3つの構成案**:

| 構成 | 方式 | Tool calling | 農家配布 | 開発容易性 |
|------|------|-------------|---------|-----------|
| **(A) llama-server内蔵** | uecs-ccm-llama起動時にllama-serverを子プロセス起動。HTTP localhost経由で推論 | **✅ 完全動作** | ○（llama.cppバイナリ同梱） | ◎ |
| (B) llama-cpp-python直接 | Pythonプロセス内でLLMロード | ❌（jinja未対応。tool callingには別途llama-server併用 or 独自jinja処理が必要） | ○ | △ |
| (C) Ollama 3プロセス | Ollama(LLM) + uecs-ccm-receiver(CCM) + controller(MQTT) | △（Modelfile Template設定で可能見込みだが未検証） | ◎（ollama pull一発） | ○ |

**構成A採用理由**:
1. **tool callingが実機で完全動作した唯一の方式**（`--jinja` フラグでGGUF内蔵テンプレート使用）
2. OpenAI互換APIが自動提供される（LINE Bot・Chat窓・Claude Codeから統一的にアクセス）
3. llama-serverはllama.cppのビルド済みバイナリを同梱するだけでよい
4. llama-cpp-pythonのjinja非対応問題（`llama_decode returned -1`）を回避

**構成Cが将来の本命候補**:
- Ollama自体はLFM2.5に対応済み（`lfm2`アーキテクチャ、49.4 tok/s実測）
- tool callingはModelfileの`TEMPLATE`セクションにJinjaテンプレートを記述すれば動作する見込み
- 対応確認後は構成Cに移行することで、セットアップが`ollama pull`一発に簡素化
- 農家配布には構成Cが最適（Ollamaはワンバイナリ、モデル管理も自動）

### 1.3 Tool Calling仕様

**特殊トークン**: `<|tool_call_start|>` / `<|tool_call_end|>`

**llama-serverでの使用**:
```bash
llama-server -m lfm2.5-1.2b-instruct-q4_k_m.gguf \
  -c 4096 --port 8081 --jinja
```
- `--jinja` がGGUF内蔵のJinjaテンプレートを有効化
- OpenAI互換 `tool_calls` フォーマットで返却
- `chat_format` 指定不要

**実測結果（tool calling）**:
```json
{
  "tool_calls": [{
    "function": {
      "name": "set_side_window",
      "arguments": "{\"opening_pct\": 50, \"reason\": \"気温35℃、湿度90%...\"}"
    }
  }]
}
```

**⚠️ 言語混交対策**: 1.2Bモデルの制約として、tool callのreason等に中国語が混入することがある
（実測: "侧窗调整以保持室内舒适"）。system promptに以下を追加:
```
重要: 全ての応答・判断理由は必ず日本語で記述してください。
他の言語（英語・中国語等）を混ぜないでください。
```

### 1.4 LFM2.5-1.2B-Instruct GGUF ロード方法

**モデル入手先**: [LiquidAI/LFM2.5-1.2B-Instruct-GGUF](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF)

| 量子化 | サイズ | 用途 |
|--------|--------|------|
| Q4_0 | 696 MB | 最小サイズ（Pi5向け） |
| **Q4_K_M** | **731 MB** | **推奨（品質/サイズバランス）** |
| Q5_K_M | 843 MB | 品質重視 |
| Q8_0 | 1.25 GB | 高品質（RAM余裕があれば） |

```bash
huggingface-cli download LiquidAI/LFM2.5-1.2B-Instruct-GGUF \
  --include "*q4_k_m*" --local-dir ./models/
```

**アーキテクチャ**: `lfm2`（ハイブリッド畳み込み+注意機構）
- 16層: 10 LIV畳み込みブロック + 6 GQA注意ブロック
- コンテキスト長: 32,768トークン
- llama.cpp PR #14620（2025年7月マージ）で公式サポート

**メモリ見積もり（CPU推論、n_ctx=4096）**:

| 用途 | サイズ |
|------|--------|
| モデル重み | ~0.7 GB |
| KVキャッシュ | ~0.1-0.2 GB |
| ランタイム | ~0.3-0.5 GB |
| **合計** | **~1.0-1.5 GB** |

全ターゲットHW（vx2/nipogi/Pi5）で余裕。

### 1.5 非同期対応

構成Aではllama-serverがHTTPサーバとして動作するため、
Pythonから`httpx.AsyncClient`で非同期呼び出し:

```python
import httpx

async def llm_chat(messages, tools=None):
    async with httpx.AsyncClient(timeout=60.0) as client:
        payload = {
            "model": "LFM2.5-1.2B-Instruct",
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        resp = await client.post(
            "http://localhost:8081/v1/chat/completions",
            json=payload
        )
        return resp.json()
```

### 1.6 Ollamaとの関係（構成A→C移行パス）

| 項目 | 構成A（llama-server） | 構成C（Ollama） |
|------|---------------------|-----------------|
| LLMプロセス | llama-server子プロセス | ollamaデーモン |
| Tool calling | **✅ --jinja で実証済み** | △ Modelfile Template未検証 |
| セットアップ | llama.cppバイナリ同梱 | `ollama pull` 一発 |
| 農家配布 | ○ | **◎** |
| メモリ効率 | ○（バイナリのみ） | △（Goランタイム分+） |
| モデル管理 | 手動（GGUFパス指定） | `ollama list`で自動管理 |

**移行条件**: OllamaでLFM2.5のtool callingが動作確認できたら構成Cに移行。
確認方法: Modelfileに`TEMPLATE`セクションを追加し、`/api/chat`にtoolsパラメータを渡す。

---

## §2. アーキテクチャ

### 2.1 全体構成図（構成A: llama-server内蔵）

```
┌─────────────────────────────────────────────────────────┐
│  uecs-ccm-llama (メインプロセス)                         │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  asyncio イベントループ                            │   │
│  │                                                    │   │
│  │  ┌───────────┐  ┌───────────┐  ┌───────────────┐ │   │
│  │  │CCM受信    │  │制御ループ │  │OpenAI互換API  │ │   │
│  │  │(UDP mcast)│→│(5分cron)  │  │(FastAPI :8080)│ │   │
│  │  │→ Cache    │  │           │  │→ Chat/Tools   │ │   │
│  │  └───────────┘  │     │     │  └───────────────┘ │   │
│  │                  │     │HTTP │                     │   │
│  │  ┌───────────┐  │     ▼     │  ┌───────────────┐ │   │
│  │  │CCM送信    │←│ localhost │  │MCP stdio      │ │   │
│  │  │(UDP mcast)│  │  :8081    │  │(オプション)   │ │   │
│  │  └───────────┘  └───────────┘  └───────────────┘ │   │
│  │                                                    │   │
│  │  ┌────────────────────────────────────────────┐   │   │
│  │  │  SensorCache (インメモリ、スレッドセーフ)     │   │   │
│  │  │  + 判断履歴DB (SQLite)                       │   │   │
│  │  └────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  llama-server (子プロセス, :8081)                  │   │
│  │  LFM2.5-1.2B-Instruct Q4_K_M --jinja             │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
     │              │                │              │
     │ UDP          │ UDP            │ HTTP         │ stdio
     ▼              ▼                ▼              ▼
 ArSprout       ArSprout          LINE Bot       Claude
 センサー       リレー             VPS           Desktop
 ノード群       (UniPi 1.1)                      /Code
```

### 2.2 コンポーネント一覧

| コンポーネント | 役割 | 実行方式 |
|---------------|------|---------|
| **CCM受信** | UDPマルチキャスト受信→SensorCache更新 | asyncio task (常駐) |
| **CCM送信** | 制御パケット送信（安全ガードレール付き） | 同期（呼び出し時） |
| **SensorCache** | 最新センサーデータのインメモリキャッシュ | threading.Lock保護 |
| **llama-server** | LFM2.5-1.2B-Instruct Q4による推論 | 子プロセス (:8081) |
| **制御ループ** | 5分周期でLLM判断→制御実行 | asyncio timer |
| **判断履歴DB** | LLM判断ログのSQLite永続化 | aiosqlite |
| **OpenAI互換API** | HTTP :8080 でChat/Tool呼び出し | FastAPI (uvicorn) |
| **MCP stdio** | Claude Desktop/Code連携 | オプション |

### 2.3 起動モード比較

| モード | トリガー | LLMロード | CCM受信 | API | 用途 |
|--------|---------|----------|---------|-----|------|
| **常駐デーモン** | systemd | **起動時にロード（常駐）** | 常駐 | :8080 | **採用** |
| 5分cron | crontab | 毎回ロード（~0.5秒） | 5分間だけ | なし | 代替案 |

**採用理由: 常駐デーモン**
- CCM受信は常時稼働が必要（データ欠損防止）
- Chat窓（殿のシステムプロンプト育成）にも常時接続が必要
- LINE Bot経由の随時指示にも即応できる
- メモリ常駐コスト（~1.5GB）は全HWで許容範囲

> **Note**: LFM2.5のロード時間は0.5秒のため、cron方式でも十分実用的。
> ただしCCM常時受信とChat窓の理由から常駐デーモンを採用。

### 2.4 制御ループの詳細フロー

```
[5分タイマー発火]
    │
    ├─ Step 1: SensorCacheから最新データ取得（0ms、インメモリ）
    │
    ├─ Step 2: 判断履歴DB（SQLite）から直近3件読み込み（<10ms）
    │
    ├─ Step 3: メッセージ組み立て
    │   └─ system_prompt + 履歴 + センサーデータ + 指示
    │
    ├─ Step 4: LLM推論（HTTP → llama-server :8081）
    │   ├─ Round 1: センサーデータ確認 → tool_call(get_sensor_data)
    │   │   └─ SensorCacheから即時返却
    │   ├─ Round 2: 判断 → tool_call(set_actuator) or 「現状維持」
    │   │   └─ CCM送信またはMQTT publish
    │   └─ Round 3: 最終報告（判断理由の自然言語）
    │
    ├─ Step 5: 判断ログをSQLiteに保存
    │
    └─ Step 6: 次のタイマーまで待機
```

**実測値ベースの速度見積もり（vx2.local）**:

| ステップ | 所要時間 |
|---------|---------|
| センサーデータ取得 | <1ms |
| 履歴読み込み | <10ms |
| LLM推論 Round 1 | ~3-4秒（46.9 tok/s） |
| Tool実行 | <1ms（インメモリ） |
| LLM推論 Round 2 | ~2-3秒（31.5 tok/s） |
| 制御実行 | <100ms |
| LLM推論 Round 3 | ~3-4秒 |
| **合計（2-3ラウンド）** | **~10-15秒** |

> Nemotron 9Bでの旧見積もり ~60-65秒 → **1/4以下に短縮**。
> 5分間隔に対して大幅余裕。1-2分間隔も可能。

### 2.5 安全装置

#### LLM無応答時のフォールバック

```
LLM推論タイムアウト（60秒）  ← 旧180秒から短縮（3-4秒/クエリの実測に基づく）
    │
    ├─ タイムアウト発生
    │   └─ ログに記録 + LINEアラート送信
    │
    ├─ 制御: 何もしない（現状維持）
    │   └─ ArSprout本体の自律制御（Level 5）が継続
    │
    └─ 次のタイマーで再試行
```

#### プロセスクラッシュ時

```
systemd Restart=always, RestartSec=10
    │
    ├─ 再起動時: llama-server子プロセス再起動（モデル再ロード ~0.5秒）
    │
    ├─ CCM受信再開（即時）
    │
    └─ ArSprout本体の自律制御が中断期間をカバー
```

#### 制御コマンドの安全ガードレール

既存uecs-ccm-mcpのCcmSender.SafetyLimitsをそのまま活用:
- アクチュエータホワイトリスト（12種のみ許可）
- レート制限（最小送信間隔1秒）
- 灌水最大時間（3600秒）
- 自動OFFタイマー

### 2.6 ターゲットHWスペックと性能見積もり

| 項目 | vx2.local | nipogi.local | **Pi5 (8GB)** |
|------|----------|-------------|---------------|
| CPU | Ryzen 5 7430U (6C/12T) | Intel N150 (4C/4T) | ARM Cortex-A76 (4C) |
| RAM | 30 GB | 16 GB | **8 GB** |
| モデルロード | **~0.5秒** | ~1-2秒 | ~2-3秒 |
| 推論速度 | **46.9 tok/s（実測）** | ~25-35 tok/s（推定） | **~10-20 tok/s（推定）** |
| 制御ループ所要時間 | **~10-15秒** | ~15-25秒 | ~30-60秒 |
| 5分間隔に対して | **大幅余裕** | **余裕** | **十分** |
| RAM残（モデル+API後） | **~28 GB** | **~14 GB** | **~6.5 GB** |

> **Pi5が新ターゲットとして追加**。698MB Q4_K_M + 1.5GiB RAM消費なら
> 8GB Pi5でも6.5GB残。「各ハウスにPi5ローカルLLM」構想が復活。

### 2.7 データフロー図

```
[ArSprout センサーノード群]
    │ UDP 224.0.0.1:16520
    ▼
[CCM受信スレッド]─────→[SensorCache]←────[CCM型分類]
    │                     │ ↑                 (sensor/actuator/weather)
    │                     │ │
    │                     ▼ │
    │              [OpenAI互換API :8080]──→ LINE Bot (VPS)
    │                     │                  Claude Desktop/Code
    │                     │
    │              [制御ループ (5分)]
    │                     │
    │                     ├─ system_prompt.txt 読み込み
    │                     ├─ 判断履歴3件 読み込み
    │                     ├─ SensorCache参照（tool call応答）
    │                     ▼
    │              [llama-server :8081 --jinja]
    │              [LFM2.5-1.2B-Instruct Q4 推論]
    │                     │ tool_calls
    │                     ▼
    │              [ツール実行]
    │                     ├─ get_sensor_data → SensorCache直参照
    │                     ├─ get_weather_summary → SensorCache直参照
    │                     ├─ get_actuator_status → SensorCache直参照
    │                     └─ set_actuator → CCM送信
    │                                        │
    ▼                                        ▼
[判断履歴DB]                    [ArSprout リレー制御]
(SQLite)                        (UniPi 1.1 MCP23008)
```

---

## §3. 既存設計書との差分整理

### 3.1 llm_control_loop_design.md への変更

| セクション | 変更内容 | 理由 |
|-----------|---------|------|
| **§1.1 シンプル構成** | データフロー図を書き換え。Ollama→llama-server子プロセスに変更 | 構成A採用 |
| **§1.2 定期制御ループ** | Step 1: MCP接続→SensorCache直参照。Step 4: Ollama HTTP→llama-server :8081 | インプロセス統合 |
| **§2.1 方式選定** | Ollama→llama-server。MODEL変数をLFM2.5-1.2B-Instructに変更 | モデル変更 |
| **§2.2 Ollamaネイティブtool calling** | llama-server --jinja方式に書き換え | ツール呼び出し方式変更 |
| **§5.1 推論速度** | ~17tok/s → **46.9 tok/s（実測）**。thinking mode なし | 実測値反映 |
| **§5.2 所要時間** | 通常70秒→**~10-15秒**、最悪110秒→**~30秒** | 大幅短縮 |
| **§5.4 モデル選択の代替案** | LFM2.5-1.2B-Instructを主候補に。Nemotron 9Bは品質重視の代替案に | モデル変更 |
| **§6.2 Ollamaセットアップ** | llama-serverのセットアップに変更。pip install不要、バイナリ同梱 | エンジン変更 |
| **§11 Chat窓** | uecs-ccm-llamaの内蔵API（:8080）で統合 | 別プロセス不要 |

### 3.2 mqtt_remote_arch.md への変更

| セクション | 変更内容 | 理由 |
|-----------|---------|------|
| **§4 ジャンクPC頭脳化** | Docker Ollama構成→uecs-ccm-llama単体インストール | Ollama必須でなくなった |
| **§4.3 Docker構成** | docker-compose.ollama.yaml→不要。pip installのみ | 簡素化 |
| **§4.5 VPSからの接続** | OLLAMA_URL→http://{ip}:8080/v1 | OpenAI互換API |
| **§4.6 Node-REDからのLLM呼び出し** | /api/generate→/v1/chat/completions | エンドポイント変更 |
| **§7 uecs-ccm-mcp** | uecs-ccm-llamaとの関係を追記（§5参照） | 共存方針明記 |

### 3.3 変更が不要なセクション

以下は変更不要（モデル非依存の設計）:

- llm_control_loop_design.md §3（システムプロンプト設計）
- llm_control_loop_design.md §4（ステート管理）
- llm_control_loop_design.md §7（5階層優先度モデル）
- llm_control_loop_design.md §8（アクチュエータ秒数制御）
- llm_control_loop_design.md §9（実機調査TASK A-D）
- llm_control_loop_design.md §12（栽培マニュアル読み取り）
- mqtt_remote_arch.md §1-3（ネットワーク/CCM乗っ取り/UniPi）
- mqtt_remote_arch.md §5（移行パス）
- mqtt_remote_arch.md §6（CCM実装詳細）

---

## §4. リポジトリ構成案

### 4.1 ディレクトリ構成

```
uecs-ccm-llama/
├── src/
│   └── uecs_ccm_llama/
│       ├── __init__.py
│       ├── main.py              # エントリーポイント（デーモン起動）
│       ├── config.py            # 設定管理（YAML + 環境変数）
│       │
│       ├── # --- LLM推論 ---
│       ├── llm_engine.py        # llama-server子プロセス管理 + HTTP呼び出し
│       ├── tool_registry.py     # ツール定義（get_sensor_data等5個）
│       ├── control_loop.py      # 5分周期の制御ループ
│       │
│       ├── # --- CCM通信（uecs-ccm-mcpから移植） ---
│       ├── ccm_protocol.py      # CCM XMLパース/構築
│       ├── ccm_receiver.py      # UDPマルチキャスト受信（asyncio）
│       ├── ccm_sender.py        # 制御パケット送信（安全ガードレール付き）
│       ├── cache.py             # SensorCache（インメモリ、スレッドセーフ）
│       │
│       ├── # --- API ---
│       ├── api.py               # OpenAI互換API（FastAPI）
│       │
│       └── # --- ステート管理 ---
│       └── state.py             # 判断履歴DB（SQLite）+ state.json
│
├── config/
│   ├── config.example.yaml      # 設定テンプレート
│   ├── system_prompt.example.txt # システムプロンプトテンプレート
│   └── actuator_config.example.yaml # アクチュエータ設定テンプレート
│
├── bin/                         # llama-serverバイナリ（プラットフォーム別）
│   └── .gitkeep
│
├── models/                      # GGUFモデル格納ディレクトリ（.gitignore）
│   └── .gitkeep
│
├── tests/
│   ├── test_llm_engine.py
│   ├── test_control_loop.py
│   ├── test_ccm_protocol.py     # uecs-ccm-mcpから移植
│   ├── test_cache.py            # uecs-ccm-mcpから移植
│   ├── test_ccm_sender.py       # uecs-ccm-mcpから移植
│   └── test_api.py
│
├── scripts/
│   ├── download_model.sh        # GGUFモデルダウンロード
│   ├── build_llama_server.sh    # llama-serverビルド（プラットフォーム別）
│   └── install_service.sh       # systemdサービス登録
│
├── systemd/
│   └── uecs-ccm-llama.service   # systemdユニットファイル
│
├── pyproject.toml
├── README.md
├── LICENSE                      # LFM Open License v1.0 準拠の注記 + MIT(コード部分)
└── .gitignore
```

### 4.2 依存関係

```toml
# pyproject.toml
[project]
name = "uecs-ccm-llama"
version = "0.1.0"
description = "UECS-CCM温室制御 + LFM2.5 LLM推論統合デーモン"
requires-python = ">=3.11"

dependencies = [
    "httpx>=0.24.0",              # llama-server HTTP呼び出し
    "fastapi>=0.100.0",           # OpenAI互換API（外部公開用）
    "uvicorn[standard]>=0.20.0",  # ASGIサーバ
    "pyyaml>=6.0",                # 設定ファイル
    "aiosqlite>=0.19.0",          # 非同期SQLite（判断履歴DB）
]

[project.optional-dependencies]
mcp = [
    "mcp>=1.0.0",                 # MCP stdio サーバ（Claude連携時のみ）
]

[project.scripts]
uecs-ccm-llama = "uecs_ccm_llama.main:main"
```

**依存関係の変更点（Nemotron時代との差分）**:
- `llama-cpp-python` → **不要**（llama-serverを子プロセスとして使用）
- `httpx` → **新規追加**（llama-server :8081 への非同期HTTP呼び出し）
- `paho-mqtt` は引き続き不要（CCM直接送信）
- `mcp` はオプション依存（Claude Desktop/Code連携時のみ）

### 4.3 ビルド・セットアップ手順

```bash
# === Step 1: リポジトリクローン ===
git clone https://github.com/yasunorioi/uecs-ccm-llama.git
cd uecs-ccm-llama

# === Step 2: Python環境 ===
python3 -m venv .venv
source .venv/bin/activate

# === Step 3: インストール ===
pip install -e .

# === Step 4: llama-serverビルドまたはダウンロード ===
./scripts/build_llama_server.sh
# → bin/llama-server が生成される
# ビルド要件: cmake, g++。Pi5/x86自動検出

# === Step 5: モデルダウンロード（~731 MB、数十秒で完了） ===
./scripts/download_model.sh
# → models/lfm2.5-1.2b-instruct-q4_k_m.gguf

# === Step 6: 設定ファイル ===
cp config/config.example.yaml config/config.yaml
cp config/system_prompt.example.txt config/system_prompt.txt
# config.yamlを環境に合わせて編集

# === Step 7: 動作テスト ===
uecs-ccm-llama --config config/config.yaml --dry-run
# → llama-server起動 + CCM受信テスト + API起動テスト

# === Step 8: systemdサービス登録（本番用） ===
sudo ./scripts/install_service.sh
sudo systemctl enable --now uecs-ccm-llama
```

### 4.4 設定ファイル構造

```yaml
# config/config.yaml
llm:
  llama_server_bin: ./bin/llama-server
  model_path: ./models/lfm2.5-1.2b-instruct-q4_k_m.gguf
  port: 8081                  # llama-server内部ポート
  n_ctx: 4096
  extra_args: ["--jinja"]     # tool calling有効化
  temperature: 0.1            # 制御用途は低温度
  max_tool_rounds: 5
  inference_timeout_sec: 60   # 旧180秒→短縮（3-4秒/クエリの実測に基づく）

ccm:
  multicast_addr: "224.0.0.1"
  multicast_port: 16520

control:
  interval_sec: 300           # 5分
  system_prompt_path: ./config/system_prompt.txt
  actuator_config_path: ./config/actuator_config.yaml

state:
  db_path: /var/lib/uecs-ccm-llama/control_log.db
  state_json_path: /var/lib/uecs-ccm-llama/state.json
  history_count: 3            # 直近N件の判断履歴をプロンプトに注入

api:
  host: "0.0.0.0"
  port: 8080                  # 外部公開用API
  # api_key: null             # 設定時はBearer認証を有効化
```

### 4.5 systemdサービスファイル

```ini
# systemd/uecs-ccm-llama.service
[Unit]
Description=UECS-CCM LLM温室制御デーモン
After=network.target

[Service]
Type=simple
User=agriha
ExecStart=/opt/uecs-ccm-llama/.venv/bin/uecs-ccm-llama \
  --config /etc/uecs-ccm-llama/config.yaml
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
# メモリ制限（OOM防止）— LFM2.5は~1.5GB。余裕を持って4GB
MemoryMax=4G

[Install]
WantedBy=multi-user.target
```

---

## §5. uecs-ccm-mcp との関係整理

### 5.1 関係図

```
┌─────────────────────────────────────────────────────────┐
│  uecs-ccm-llama (新リポジトリ, public)                   │
│  ├─ llama-server (子プロセス, LFM2.5推論)                │
│  ├─ CCM受信/送信/キャッシュ  ←── uecs-ccm-mcpから移植    │
│  ├─ 制御ループ               ←── llm_control_loop_design │
│  ├─ OpenAI互換API            (新規)                      │
│  └─ MCP stdio (オプション)   ←── uecs-ccm-mcp互換       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  uecs-ccm-mcp (既存リポジトリ, public)                   │
│  ├─ CCM受信/送信/キャッシュ  （独立して維持）             │
│  └─ MCP stdio サーバー       （Claude Desktop/Code向け）  │
└─────────────────────────────────────────────────────────┘
```

### 5.2 共存方針

**uecs-ccm-mcpは吸収しない。両リポジトリを独立して維持する。**

| 項目 | uecs-ccm-llama | uecs-ccm-mcp |
|------|---------------|--------------|
| 目的 | **LLMが温室を自律制御** | **LLMが温室を見る・操作する** |
| LLM | llama-server子プロセス（LFM2.5） | なし（外部LLMがMCP経由でアクセス） |
| ユーザー | 農家（自動制御） | 開発者・Claude ユーザー |
| CCMコード | uecs-ccm-mcpから**コピー移植** | 独立して維持 |
| 公開方針 | public | public（MIT） |
| 依存関係 | httpx, fastapi等 | mcp のみ |

> **Ollama対応時の変更**: 構成Cに移行した場合、uecs-ccm-llamaのllm_engine.pyを
> Ollama HTTP呼び出し（`http://localhost:11434/api/chat`）に差し替えるだけ。
> 他のコンポーネント（CCM、制御ループ、API）は変更不要。

### 5.3 MCP機能の位置づけ

```
(A) uecs-ccm-mcp 単独利用:
    Claude Desktop/Code → stdio → uecs-ccm-mcp → CCM
    （LLMなし。人間がClaudeに「温室の温度見て」と頼む）

(B) uecs-ccm-llama 単独利用:
    uecs-ccm-llama（内蔵LLM via llama-server）→ CCM
    （自律制御。5分ごとにLLMが判断）

(C) uecs-ccm-llama + 外部LLM:
    Claude Desktop/Code → HTTP → uecs-ccm-llama /v1/chat/completions
    （外部LLMがOpenAI互換APIで温室にアクセス。MCP不要）

(D) uecs-ccm-llama MCP対応（オプション）:
    Claude Desktop/Code → stdio → uecs-ccm-llama（MCPモード）
    （uecs-ccm-mcpと同等のMCPツールを提供。ただし内蔵LLMも使える）
```

---

## 付録: 主要な設計判断まとめ

| # | 判断 | 選択肢 | 採用 | 理由 |
|---|------|--------|------|------|
| 1 | LLMモデル | Nemotron 9B / LFM2.5-1.2B-Instruct / Qwen2.5-1.5B | **LFM2.5-1.2B-Instruct** | 46.9 tok/sでリアルタイム制御可。Pi5搭載可。Ollamaも対応 |
| 2 | 推論エンジン | llama-server / llama-cpp-python / Ollama | **llama-server --jinja** | tool calling完全動作の唯一の方式（実証済み）。構成C(Ollama)は将来の本命 |
| 3 | Tool calling方式 | chatml-function-calling / --jinja(GGUF内蔵) / Ollama Template | **--jinja(GGUF内蔵)** | LFM2.5の`<\|tool_call_start\|>`トークンをGGUFテンプレートが処理 |
| 4 | 起動モード | cron / 常駐デーモン | **常駐デーモン** | CCM常時受信、Chat窓常時応答。ロード0.5秒だがCCM/Chat理由で常駐 |
| 5 | CCMコード共有 | import依存 / コピー移植 | **コピー移植** | 依存最小化、独立リリース |
| 6 | uecs-ccm-mcp関係 | 吸収 / 共存 | **共存** | 用途が異なる（自律制御 vs MCP対話） |
| 7 | API方式 | OpenAI互換 / MCP / 独自 | **OpenAI互換 + MCPオプション** | LINE Bot/Claude/ブラウザから統一的にアクセス |
| 8 | ライセンス | MIT / LFM Open License v1.0 | **コード=MIT, モデル=LFM v1.0** | LFMは年商$10M未満ロイヤリティフリー |

---

## 付録B: 品質リスクと対策

### 1.2Bモデルの品質制約

| リスク | 深刻度 | 対策 |
|--------|--------|------|
| ハルシネーション（未測定） | 中 | ArSprout本体の自律制御（Level 5）が安全装置。LLM誤判断は作物被害だがHW制約で物理破壊は防止 |
| Tool calling精度低下 | 中 | 安全ガードレール（ホワイトリスト、レート制限）で誤制御を防止 |
| 言語混交（中国語混入） | 低 | system promptに「必ず日本語で」を明記 |
| 複雑な条件判断の質 | 中 | system promptの品質が決定的に重要。普及員の暗黙知を具体的に記述 |

### Nemotron 9Bとの品質比較（参考）

| 指標 | Nemotron 9B | LFM2.5-1.2B-Instruct |
|------|-------------|---------------------|
| Nejumi LB4 TOTAL | 0.711 | 未提出 |
| Hallulens | 0.960 | 未測定 |
| BFCL v3（tool calling） | 0.649 | 49.12（英語BFCLv3） |
| JMMLU | ~0.73（推定） | 47.7 |

> パラメータ7.5倍差による品質ギャップは確実に存在する。
> ただし安全装置（ArSprout本体HWウォッチドッグ）が独立しているため致命的ではない。
> **system promptの品質で1.2Bの限界を補う**設計思想。

---

## 参照ドキュメント

| ドキュメント | パス | 関連 |
|------------|------|------|
| LLM制御ループ設計書 | ~/unipi-agri-ha/docs/llm_control_loop_design.md | §3 差分整理元 |
| 新アーキテクチャ設計書 | ~/unipi-agri-ha/docs/mqtt_remote_arch.md | §3 差分整理元 |
| uecs-ccm-mcp実装 | ~/uecs-ccm-mcp/ | §5 移植元 |
| LFM2.5スペック調査 | — | §1 モデル情報 |
| LFM2.5実機PoC | — | §1/§2 実測値 |
| 影響分析22件 | — | §3 変更箇所リスト |
| LFM2.5 HuggingFace | https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF | §1 モデル配布 |
| LFM2.5 公式ブログ | https://www.liquid.ai/blog/introducing-lfm2-5-the-next-generation-of-on-device-ai | §1 技術情報 |
| llama.cpp function calling | https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md | §1 tool calling仕様 |
| Liquid AI ライセンス | https://www.liquid.ai/lfm-license | 付録 ライセンス情報 |
