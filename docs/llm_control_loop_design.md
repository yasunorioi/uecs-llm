# LLM温室制御ループ設計書

> **Version**: 2.0
> **Date**: 2026-02-21
> **Status**: Draft
> **HW**: nipogi.local (Intel N150, 16GB RAM, Ubuntu, USB-SSD起動)

---

## 概要

Node-RED全面撤去後、**LLMが直接温室を制御する**アーキテクチャ。
外側は単純なcronスクリプト、内側はLFM2.5 (llama-server) のtool calling機能を通じて
REST APIでunipi-daemonのリレーをLLMが自発的に操作する。

> **v2.0変更点**: ArSproutのコントローラソフトウェアはRaspbian Liteに入れ替え済み。
> ArSprout観測ノード（192.168.1.70）はCCMセンサーデータ送信のみ。
> アクチュエータ制御は**全てUniPi 1.1 I2Cリレー（ch1-8）**経由に一本化。

```
ArSprout 観測ノード (192.168.1.70)
    │  CCMマルチキャスト (224.0.0.1:16520) — センサーデータのみ
    ▼
┌─────────────────────────────────────────────────────┐
│ unipi-daemon (Pi Lite, 10.10.0.10)                  │
│                                                       │
│ [センサー入力]                                        │
│   ccm_receiver.py  →  MQTT: agriha/h01/ccm/...      │
│   sensor_loop.py   →  MQTT: agriha/h01/sensor/...   │
│                    →  MQTT: agriha/farm/weather/misol│
│                                                       │
│ [アクチュエータ出力]                                  │
│   REST API (:8080)                                   │
│     POST /api/relay/{ch} → MQTT relay/{ch}/set       │
│   MqttRelayBridge → MCP23008 I2C → リレー ch1-8     │
│                                                       │
│ [安全機構]                                            │
│   CommandGate  ← gpio_watch (DI緊急スイッチ)         │
│   ロックアウト中は全リレー操作を拒否 (423)            │
└──────────────┬──────────────────────────────────────┘
               │  REST API (HTTP)
               ▼
agriha_control.py (接着層 — LFM2.5 tool calling ループ)
    │  OpenAI互換API (localhost:8081)
    ▼
llama-server (LFM2.5 1.2B Q4, nipogi.local)
    │  tool_calls → REST API POST /api/relay/{ch}
    ▼
unipi-daemon REST API → MQTT → MqttRelayBridge → I2C リレー

フェイルセーフ: LLM停止時はリレー現状維持（物理的にラッチ）
安全制御: unipi-daemon CommandGate（緊急スイッチ→300秒ロックアウト）
          + システムプロンプトの安全制約（LLM側ガードレール）

データ経路:
  CCM → unipi-daemon ccm_receiver → MQTT → REST API /api/sensors
  CCM → unipi-daemon ccm_receiver → MQTT → Telegraf → InfluxDB → Grafana
  Misol WH65LP → unipi-daemon sensor_loop → MQTT (agriha/farm/weather/misol)
  DS18B20 → unipi-daemon sensor_loop → MQTT (agriha/{house_id}/sensor/DS18B20)
```

---

## 目次

1. [制御ループの仕組み](#1-制御ループの仕組み)
2. [LFM2.5→REST APIツール呼び出しの接着層](#2-lfm25rest-apiツール呼び出しの接着層)
3. [システムプロンプト設計](#3-システムプロンプト設計)
4. [ステート管理](#4-ステート管理)
5. [応答速度見積もり](#5-応答速度見積もり)
6. [nipogi.localセットアップ手順](#6-nipogilocalセットアップ手順)
7. [安全制御設計](#7-安全制御設計)
8. [アクチュエータ制御（UniPiリレー）](#8-アクチュエータ制御unipiリレー)
9. [リレーチャンネル割当](#9-リレーチャンネル割当)
10. [参照ドキュメント](#10-参照ドキュメント)
11. [Chat窓（LLM育成用UI）](#11-chat窓llm育成用ui)
12. [栽培マニュアルPDF/JPG読み取り](#12-栽培マニュアルpdfjpg読み取り)
13. [付録A: v1.x→v2.0 変更履歴](#付録a-v1xv20-変更履歴)

---

## 1. 制御ループの仕組み

### 1.1 シンプル構成: cron + LLM + UniPiリレー

```
┌─────────────────────────────────────────────────────┐
│  定期制御ループ（cron 5分間隔）                      │
│    - REST APIでセンサー読み取り → LLM判断            │
│    → REST APIでリレー制御                            │
│    - 通常運転の主制御パス                             │
│    - LFM2.5のtool callingで自律的にツール選択          │
│                                                       │
│  安全制御:                                            │
│    - 緊急スイッチ → CommandGate → 300秒ロックアウト  │
│    - LLM停止時: リレーは現状維持（MCP23008はラッチ型）│
│    - システムプロンプトに安全制約を記述（LLM側ガード）│
└─────────────────────────────────────────────────────┘
```

### 1.2 定期制御ループの流れ

```
cron (*/5 * * * *) → agriha_control.py 起動
    │
    ├─ Step 1: unipi-daemon REST APIへの接続確認
    │
    ├─ Step 2: 直近の判断履歴をSQLiteから読み込み（§4参照）
    │
    ├─ Step 3: システムプロンプト + 履歴 + 指示を組み立て
    │          「現在のセンサーデータを確認し、5分後の気象を予測し、
    │           目標値に近づける制御アクションを実行せよ」
    │
    ├─ Step 4: llama-server /v1/chat/completions に送信（tools配列付き）
    │          → LLMが自発的にツールを呼ぶ:
    │            (1) get_sensors  → REST API GET /api/sensors
    │                → CCM(内温/湿度/CO2) + DS18B20 + Misol(外気/風/降雨)
    │            (2) get_status   → REST API GET /api/status
    │                → リレー状態(ch1-8) + ロックアウト状態
    │            (3) 判断 → set_relay（必要な場合のみ）
    │                → REST API POST /api/relay/{ch}
    │
    ├─ Step 5: LLMの最終応答（判断理由の自然言語）をログに記録
    │
    └─ Step 6: プロセス終了（次のcron起動まで待機）
```

**ポイント**: 外側のスクリプト(agriha_control.py)はcronで起動される単純なスクリプト。
LLMがどのツールをどの順番で呼ぶかはLLM自身が判断する。
スクリプトは「ツール呼び出しがあったら実行して結果を返す」だけのループ。

### 1.3 安全制御の設計方針

> **v2.0変更**: ArSproutコントローラはRaspbian Liteに入れ替え済み。
> ArSproutの安全制御（警報駆動制御）は**利用不可**。
> 安全制御はunipi-daemon側で完結する。

安全制御の3層:

1. **物理層: 緊急スイッチ → CommandGate**
   - UniPi 1.1 DIピン(DI07-DI14)に接続された緊急スイッチ
   - gpio_watchが検知 → CommandGateが300秒ロックアウト
   - ロックアウト中はREST API relay操作を全て拒否(423)
   - REST API POST /api/emergency/clear で手動解除可能

2. **LLM層: システムプロンプトの安全制約**
   - 降雨時の側窓閉鎖、強風時の制御、温度上下限etc.（§3.2 [F]セクション）
   - LLMが自律判断で安全制御を実行

3. **フォールバック: リレーラッチ**
   - LLM/nipogi.localが停止しても、MCP23008リレーは最後の状態を保持
   - 灌水ON放置を防ぐため、duration_secの指定を必須とする
   - MqttRelayBridgeの自動OFFタイマーが最終防壁

---

## 2. LFM2.5→REST APIツール呼び出しの接着層

### 2.1 方式選定

| 方式 | 依存パッケージ | RAM消費 | 起動時間 | 評価 |
|------|-------------|---------|---------|------|
| **(b) 自前Python + llama-server** | `httpx` | ~50MB (+ llama-server) | <1秒 | **採用** |
| (c) Ollama | `ollama` | ~100MB | 数秒 | 不採用（管理デーモン不要なllama-server直接起動を優先） |
| (a) LangChain | langchain全体 | ~300MB+ | 5秒+ | 不採用 |

**採用理由**: llama-server (llama.cpp) はOpenAI互換の `/v1/chat/completions` APIを提供し、
`tools` 配列を渡すだけでLLMが構造化されたtool_callsを返す。
LFM2.5 (Liquid Foundation Model) はGGUF形式で直接llama-serverに読み込める。
N150のリソース制約上、最小の依存で最大の効果を得られる自前スクリプトが最適。

> **v2.0変更**: uecs-ccm-mcp (MCP) を介さず、unipi-daemon REST API に直接HTTP通信。
> センサーデータもアクチュエータ制御もREST API経由に統一。

### 2.2 llama-server OpenAI互換tool calling

llama-server `/v1/chat/completions` に `tools` 配列を渡すと、LLMが自発的にtool callを生成する。

```json
{
  "messages": [...],
  "stream": false,
  "temperature": 0.1,
  "max_tokens": 512,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_sensors",
        "description": "全センサーデータ取得（CCM内気象 + DS18B20 + Misol外気象 + リレー状態）",
        "parameters": {
          "type": "object",
          "properties": {}
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "set_relay",
        "description": "UniPiリレー制御。ch=チャンネル(1-8), value=0/1, duration_sec=自動OFF秒数",
        "parameters": {
          "type": "object",
          "properties": {
            "ch": {"type": "integer", "minimum": 1, "maximum": 8},
            "value": {"type": "integer", "enum": [0, 1]},
            "duration_sec": {"type": "number", "default": 0},
            "reason": {"type": "string"}
          },
          "required": ["ch", "value"]
        }
      }
    }
  ]
}
```

レスポンス例:
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "tool_calls": [
        {
          "id": "call_1",
          "type": "function",
          "function": {
            "name": "get_sensors",
            "arguments": "{}"
          }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

### 2.3 接着層スクリプト: agriha_control.py

```python
#!/usr/bin/env python3
"""AgriHA LLM制御ループ — LFM2.5 (llama-server) + unipi-daemon REST API"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

# 設定
LLAMA_SERVER_URL = "http://localhost:8081"  # llama-server OpenAI互換API
UNIPI_API = "http://10.10.0.10:8080"  # unipi-daemon REST API
API_KEY = ""  # config.yaml の rest_api.api_key に合わせる
DB_PATH = Path("/var/lib/agriha/control_log.db")
SYSTEM_PROMPT_PATH = Path("/etc/agriha/system_prompt.txt")
MAX_TOOL_ROUNDS = 5  # ツール呼び出し最大ラウンド数
INFERENCE_TIMEOUT = 60  # 推論タイムアウト（秒）

logger = logging.getLogger("agriha_control")

# ツール定義（OpenAI互換 tools形式）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_sensors",
            "description": "全センサーデータ取得（CCM内気象 + DS18B20 + Misol外気象）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "デーモン状態取得（リレー状態ch1-8 + ロックアウト状態）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_relay",
            "description": (
                "UniPiリレー制御。ch=チャンネル(1-8), value=1(ON)/0(OFF), "
                "duration_sec=自動OFF秒数(灌水等は必須指定), reason=理由"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ch": {"type": "integer", "minimum": 1, "maximum": 8},
                    "value": {"type": "integer", "enum": [0, 1]},
                    "duration_sec": {"type": "number", "default": 0},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["ch", "value"],
            },
        },
    },
]


def load_recent_history(db: sqlite3.Connection, n: int = 3) -> str:
    """直近n回の判断履歴をテキストで返す"""
    rows = db.execute(
        "SELECT timestamp, summary, actions_taken "
        "FROM decisions ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    if not rows:
        return "（過去の判断履歴なし — 初回起動）"
    lines = []
    for ts, summary, actions in reversed(rows):
        lines.append(f"[{ts}] {summary} → {actions}")
    return "\n".join(lines)


def save_decision(db: sqlite3.Connection, summary: str, actions: str,
                  raw_response: str, sensor_snapshot: str):
    """判断ログをSQLiteに保存"""
    db.execute(
        "INSERT INTO decisions (timestamp, summary, actions_taken, "
        "raw_response, sensor_snapshot) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), summary, actions,
         raw_response, sensor_snapshot)
    )
    db.commit()


def call_tool(client: httpx.Client, name: str, args: dict) -> str:
    """ツール名に応じてunipi-daemon REST APIを呼ぶ"""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}

    if name == "get_sensors":
        r = client.get(f"{UNIPI_API}/api/sensors", headers=headers)
        return r.text

    if name == "get_status":
        r = client.get(f"{UNIPI_API}/api/status", headers=headers)
        return r.text

    if name == "set_relay":
        ch = args.get("ch", 1)
        payload = {
            "value": args.get("value", 0),
            "duration_sec": args.get("duration_sec", 0),
            "reason": args.get("reason", "LLM auto"),
        }
        r = client.post(
            f"{UNIPI_API}/api/relay/{ch}",
            json=payload,
            headers=headers,
        )
        return r.text

    return json.dumps({"error": f"unknown tool: {name}"})


def llm_chat(llm_client, llm_url, messages, tools, temperature=0.1, max_tokens=512):
    """llama-server の /v1/chat/completions を呼び出す"""
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    resp = llm_client.post(f"{llm_url}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    message = choice["message"]

    # tool_calls の arguments パース
    raw_tool_calls = message.get("tool_calls") or []
    parsed = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        parsed.append({"id": tc.get("id", ""), "function": {"name": fn["name"], "arguments": args}})

    return {"content": message.get("content"), "tool_calls": parsed or None}


def run_control_loop():
    """メイン制御ループ（1回実行、cronから呼ばれる）"""

    # DB接続
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        summary TEXT,
        actions_taken TEXT,
        raw_response TEXT,
        sensor_snapshot TEXT
    )""")

    # システムプロンプト読み込み
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    # 直近の判断履歴
    history = load_recent_history(db, n=3)

    # HTTP クライアント（unipi-daemon用、llama-server用）
    api_client = httpx.Client(timeout=30)
    llm_client = httpx.Client(timeout=INFERENCE_TIMEOUT)

    try:
        # メッセージ組み立て
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"## 直近の判断履歴\n{history}\n\n"
                f"## 指示\n"
                f"現在時刻: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"センサーデータを確認し、5分後の気象変化を予測し、"
                f"目標値に近づける制御アクションを実行せよ。\n"
                f"アクションが不要なら「現状維持」と報告せよ。"
            )},
        ]

        # Tool calling ループ
        sensor_snapshot = ""
        actions_taken = []

        for round_num in range(MAX_TOOL_ROUNDS):
            msg = llm_chat(llm_client, LLAMA_SERVER_URL, messages, TOOLS)

            # ツール呼び出しがなければ最終応答
            if not msg.get("tool_calls"):
                break

            # assistant メッセージを追加（tool_calls 付き）
            messages.append({"role": "assistant", "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"],
                              "arguments": json.dumps(tc["function"]["arguments"])}}
                for tc in msg["tool_calls"]
            ]})

            for tc in msg["tool_calls"]:
                fn = tc["function"]
                tool_name = fn["name"]
                tool_args = fn["arguments"]

                logger.info("Tool call [%d]: %s(%s)",
                            round_num, tool_name, tool_args)

                result_text = call_tool(api_client, tool_name, tool_args)

                if tool_name in ("get_sensors", "get_status"):
                    sensor_snapshot += f"\n--- {tool_name} ---\n{result_text}"
                if tool_name == "set_relay":
                    actions_taken.append(f"relay ch{tool_args.get('ch')}={tool_args.get('value')}")

                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})

        # 最終応答を取得
        final_text = msg.get("content", "（応答なし）") or "（応答なし）"

        # 判断ログ保存
        save_decision(
            db,
            summary=final_text[:500],
            actions="; ".join(actions_taken) if actions_taken else "現状維持",
            raw_response=json.dumps(msg, ensure_ascii=False),
            sensor_snapshot=sensor_snapshot[:2000],
        )

        logger.info("Decision: %s | Actions: %s",
                    final_text[:200], actions_taken)

    finally:
        api_client.close()
        llm_client.close()
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_control_loop()
```

### 2.4 接続モード

> **v2.0変更**: uecs-ccm-mcp (MCP stdio) を廃止。unipi-daemon REST API に直接HTTP通信。

| モード | 仕組み | 用途 |
|--------|--------|------|
| **LAN直接** | nipogi.localからunipi-daemon REST API (http://10.10.0.10:8080) にHTTP | nipogi.localがハウスLAN内にある場合 |
| **VPN経由** | WireGuard VPN越しに同一REST APIにアクセス | nipogi.localが遠隔の場合 |

**推奨**: ハウスLAN内にnipogi.localを設置し、LAN直接モードで運用。

---

## 3. システムプロンプト設計

### 3.1 構造

```
/etc/agriha/system_prompt.txt
  │
  ├─ [A] 役割定義（固定）
  ├─ [B] ハウス固有情報（設定ファイルから生成）
  ├─ [C] 作物パラメータ（crop_irrigation.yamlから生成）
  ├─ [D] 制御ルール（ArSproutマニュアルから抽出）
  ├─ [E] 暗黙知（普及員フィードバックから蓄積）
  └─ [F] 安全制約（絶対遵守）
```

### 3.2 プロンプト全文（テンプレート）

```text
# [A] 役割定義
あなたは北海道恵庭市の温室環境制御AIです。
センサーデータを読み取り、アクチュエータを操作して温室環境を最適に維持します。
利用可能なツールを使ってデータを取得し、必要に応じて制御を実行してください。

# [B] ハウス固有情報
- ハウスID: h1
- 作物: 長ナス（水耕栽培・ココバッグ）
- 位置: 北緯42.888° 東経141.603° 標高21m
- アクチュエータ: UniPi 1.1 リレー ch1-8
  - ch4: 灌水電磁弁（必ずduration_sec指定）
  - ch5-8: 側窓開閉（詳細は§9参照）
  - ch1-3: 未割当（将来拡張用）
- 制御: POST /api/relay/{ch} value=1/0 duration_sec=秒
- 側窓は北側と南側で独立制御。風向を考慮して片側制御すること

# [C] 作物パラメータ（現在ステージ: 収穫盛期）
- 温度目標: 昼間25-28℃、夜間15-18℃
- EC: 1.8-2.0 mS/cm（ドサトロンで手動調整、制御対象外）
- 日射比例灌水閾値: 0.9 MJ/m²
- 灌水量: 270-300 ml/株
- 飽差(VPD)目標: 3-8 hPa
- CO2目標: 換気時は自然値、密閉時700ppm
- 降雨時灌水停止: 0.5mm/h以上で停止、30分後再開

# [D] 制御ルール
## 温度制御の目安
- 目標温度から+5℃以上で窓全開相当
- 1℃刻みで10%程度の出力変化を目安に

## 安全優先順位（上位が下位を上書き）
1. 強風時閉鎖: 風速≧5m/s の風上側を閉鎖
2. 気温急上昇: 20分で3℃以上上昇 → 開放
3. 温度超過: 絶対値で閾値超え → 開放
4. 降雨時閉鎖: 降雨検知 → 閉鎖

## 風向と片側制御
- 北風(NNW～NNE) + 風速≧5m/s → 北側閉鎖、南側は開放維持
- 南風(SSE～SSW) + 風速≧5m/s → 南側閉鎖、北側は開放維持
- 16方位: N=1, NNE=2, NE=3, ... NW=16

## 時間帯制御
- 日の出前: 側窓閉鎖（結露防止のため）
- 日の出後: PID制御開始
- 日没前1時間: 徐々に閉鎖開始
- 日没後: 全閉

# [E] 暗黙知（普及員フィードバック）
- 外気湿度99%以上の夜間: 換気しても除湿効果なし。内外温度差を利用した
  循環ファンによる結露軽減のみ可能
- VPD>15hPa: ナスの気孔が閉じ光合成停止。灌水増量+ミストで飽差を下げる
- 雨天後の急な晴れ間: 日射急変で葉焼けリスク。遮光カーテン10-20%推奨
- CO2 218ppm以下は光合成の限界ライン。密閉して400ppm以上に回復を待つ
- 7月の灌水ピーク時: 灌水閾値を下げすぎると水浸しになる。実績データ参照

# [F] 安全制約（絶対遵守）
- 灌水・ミスト等のONは必ず duration_sec を指定すること（最大3600秒）
- 降雨中（rainfall > 0）は絶対に側窓を開けない
- 側窓の開閉は片側ずつ。両側同時操作しない
- 40℃超は緊急事態。全窓全開+ファンON
- 5℃以下は凍結リスク。カーテン閉+暖房ON
- 制御不要と判断した場合は「現状維持」と明記し、何も操作しない
- ロックアウト中（GET /api/status の locked_out=true）はリレー操作しない
```

### 3.3 暗黙知の収集・更新フロー

```
LINE Botクイズ回答 → 殿がレビュー → system_prompt.txt [E]セクションに追記
                                       ↓
                                  agriha_control.py 次回実行で反映
                                       ↓
                                  Chat窓（§11）で「この場合どうする？」と質問して確認
```

- プロンプトはテキストファイルで管理（コード変更不要で更新可能）
- 暗黙知は人間がレビューしてから追記（LLMが自動追記しない）
- crop_irrigation.yamlのステージ変更時はプロンプト[C]セクションも更新
- 栽培マニュアル読み取り（§12）の結果は[D][E]セクションに反映

### 3.4 トークン数見積もり（システムプロンプト）

| セクション | 推定トークン数 |
|-----------|-------------|
| [A] 役割定義 | ~80 |
| [B] ハウス固有情報 | ~120 |
| [C] 作物パラメータ | ~150 |
| [D] 制御ルール | ~400 |
| [E] 暗黙知 | ~200（初期、蓄積で増加） |
| [F] 安全制約 | ~150 |
| **合計** | **~1,100** |

LFM2.5 1.2Bのコンテキストウィンドウは4Kトークン（llama-server `-c 4096`）。
システムプロンプト1,100 + 履歴300 + ツール定義500 + センサーデータ500
= 合計約2,400トークンで、4Kコンテキスト内に収まる。

---

## 4. ステート管理

### 4.1 二層ステート設計

```
┌────────────────────────────────────────────────┐
│  Layer S1: リレー物理状態                       │
│    - ch1-8のON/OFF状態                          │
│    - 管理主体: MqttRelayBridge (MCP23008 I2C)   │
│    - 取得方法: GET /api/status → relay_state    │
│    - MQTT: agriha/{house_id}/relay/state        │
│    - 動作中はラッチ維持（LLM停止でも状態保持）   │
│    - Pi Lite再起動時はPORで全OFF初期化            │
└────────────────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│  Layer S2: 判断履歴 (control_log.db — SQLite)   │
│    - LLMの判断ログ（理由+アクション+センサー値）  │
│    - 直近3回の履歴を次回プロンプトに含める        │
│    - 管理主体: agriha_control.py                 │
│    - 保持場所: /var/lib/agriha/control_log.db    │
└────────────────────────────────────────────────┘
```

> **v2.0変更**: Layer S1はstate.json（ソフトウェア状態推定）から、
> MCP23008 I2Cレジスタ直読み（物理状態）に変更。位置推定やキャリブレーションは不要。
> UniPiリレーはON/OFF型のみのため、開度(position_pct)管理は不要。

### 4.2 判断履歴DB (Layer S2) スキーマ

```sql
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,           -- ISO8601 UTC
    summary TEXT,                      -- LLMの最終応答（判断理由、500文字以内）
    actions_taken TEXT,                -- 実行したアクション一覧
    raw_response TEXT,                 -- LLMの生レスポンス（デバッグ用）
    sensor_snapshot TEXT               -- 判断時のセンサー値スナップショット
);

-- 直近3件取得用インデックス
CREATE INDEX idx_decisions_timestamp ON decisions(timestamp DESC);
```

### 4.3 LLMのコンテキスト戦略

| 方式 | メリット | デメリット |
|------|---------|-----------|
| **毎回リセット + 直近3件履歴** | メモリリーク無し、再現性高い | 長期トレンドを見れない |
| コンテキストウィンドウ保持 | 連続的な判断が可能 | cronで毎回起動するため不可能 |
| 全履歴をDBから注入 | 長期トレンドを参照 | トークン消費過大 |

**採用方式**: **毎回リセット + 直近3件の判断履歴をプロンプトに注入**。

cronで毎回新規プロセスを起動するため、コンテキストウィンドウの保持は不可能。
代わりにSQLiteから直近3件の判断サマリを読み込み、短期的な文脈を維持する。

### 4.4 ログ保持ポリシー

- **直近30日**: 全レコード保持
- **30日超**: 日次サマリに集約（1日1レコード、主要判断のみ）
- **90日超**: 月次サマリに集約
- 生データは別途InfluxDB（将来）に流す想定

---

## 5. 応答速度見積もり

### 5.1 N150ベンチマーク（実測データ参照）

Intel N150（4 Eコア、バースト3.6GHz）でのllama-server推論性能:

| モデル | 生成速度 | 備考 |
|--------|---------|------|
| **LFM2.5 1.2B Q4** | **~15-20 tok/s** | N150推定値（1.2Bパラメータ、Q4_K_M量子化） |
| Qwen2.5 1.5B Q4 | ~17 tok/s | 代替候補（N150実測値） |
| Qwen2.5 3B Q4 | ~9 tok/s | 代替候補（tool calling精度向上） |

### 5.2 1回の制御ループの所要時間

```
Step 1: REST API疎通確認                       ~0.05秒
Step 2: DB履歴読み込み                         ~0.01秒
Step 3: プロンプト組み立て                     ~0.01秒
Step 4: llama-server推論 Round 1（ツール選択）
  - 入力: ~2,400トークン → プリフィル: ~24秒
  - 出力: ~50トークン（tool_call JSON）→ ~3秒
Step 5: REST API GET /api/sensors              ~0.05秒
Step 6: REST API GET /api/status               ~0.05秒
Step 7: llama-server推論 Round 2（判断+制御）
  - 入力: ~3,500トークン（Round 1結果含む）→ ~35秒
  - 出力: ~100トークン（判断理由+set_relay呼び出し）→ ~6秒
Step 8: REST API POST /api/relay/{ch}          ~0.05秒
Step 9: llama-server推論 Round 3（最終報告）
  - 入力: ~4,000トークン → ~40秒
  - 出力: ~50トークン → ~3秒
Step 10: DB保存 + プロセス終了                 ~0.1秒
────────────────────────────────────────────────
合計見積もり（最悪ケース3ラウンド）:          ~110秒 ≈ 約2分
合計見積もり（通常ケース2ラウンド）:          ~70秒  ≈ 約1分10秒
合計見積もり（現状維持1ラウンド）:            ~30秒
```

> **v2.0注**: REST API呼び出し（~50ms）はMCP stdio（~100ms）より高速。
> ボトルネックはllama-server推論時間で変わらず。

### 5.3 5分間隔に間に合うか

| ケース | 所要時間 | 5分間隔に対して |
|--------|---------|---------------|
| 現状維持（制御不要） | ~30秒 | 十分余裕 |
| 通常制御（2ラウンド） | ~70秒 | 余裕あり |
| 複雑判断（3ラウンド） | ~110秒 | ギリギリ可 |
| 異常（4ラウンド以上） | ~150秒+ | MAX_TOOL_ROUNDS=5で打ち切り |

**結論**: 5分間隔で十分間に合う。ただし安全マージンを考慮し、
cron実行時に前回プロセスが生存している場合はスキップする（flock使用）。

### 5.4 モデル選択の代替案

LFM2.5のtool calling精度が不十分な場合の代替（全てllama-server GGUF形式で動作）:

| モデル | 速度(N150) | tool calling精度 | RAM |
|--------|-----------|----------------|-----|
| **LFM2.5 1.2B Q4** | ~15-20 tok/s | 中（Liquid AI） | ~1.0GB |
| Qwen2.5 1.5B Q4 | 17 tok/s | 中 | ~1.2GB |
| Qwen2.5 3B Q4 | 9 tok/s | 高 | ~2.0GB |
| Qwen2.5 7B Q4 | 4 tok/s | 最高 | ~4.5GB |

16GB RAMのN150なら7Bまで動作可能だが、応答時間が5分を超える可能性あり。
まずLFM2.5 1.2Bで試し、tool calling精度が不足ならQwen2.5 1.5Bまたは3Bに切替。
llama-serverはGGUF形式のモデルを `-m` オプションで指定するだけで切替可能。

---

## 6. nipogi.localセットアップ手順

### 6.1 前提条件

- nipogi.local: Intel N150, 16GB RAM, USB-SSDからUbuntu 24.04起動
- ハウスLAN (192.168.1.0/24) に有線/WiFi接続済み
- unipi-daemon REST API (http://10.10.0.10:8080) に到達可能

### 6.2 llama-server + LFM2.5 セットアップ

```bash
# === Step 1: llama.cpp (llama-server) ビルド or バイナリ取得 ===
# Option A: リリースバイナリ（推奨）
wget https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-server-linux-x86_64.tar.gz
tar xzf llama-server-linux-x86_64.tar.gz
sudo mkdir -p /opt/llama-server/bin
sudo cp llama-server /opt/llama-server/bin/

# === Step 2: LFM2.5 GGUFモデルダウンロード ===
sudo mkdir -p /opt/llama-server/models
cd /opt/llama-server/models
wget https://huggingface.co/liquid/LFM2-1.2B-Instruct-GGUF/resolve/main/lfm2.5-1.2b-instruct-q4_k_m.gguf

# === Step 3: 動作テスト ===
/opt/llama-server/bin/llama-server \
  -m /opt/llama-server/models/lfm2.5-1.2b-instruct-q4_k_m.gguf \
  --port 8081 -c 4096 -t 4 --mlock --jinja &

# ヘルスチェック
curl http://localhost:8081/health

# 推論テスト
curl -s http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"温室の内温が35℃です。どうしますか？"}],"max_tokens":200}' | python3 -m json.tool
```

### 6.3 制御スクリプト配置

```bash
# === Step 1: Python 3.11+ 確認 ===
python3 --version  # 3.11以上

# === Step 2: HTTP クライアント ===
sudo pip install httpx pyyaml

# === Step 3: 制御スクリプト配置 ===
sudo mkdir -p /opt/agriha-control
sudo mkdir -p /var/lib/agriha
sudo mkdir -p /etc/agriha

# agriha_control.py を配置（§2.3の内容）
sudo cp agriha_control.py /opt/agriha-control/

# システムプロンプト配置（§3.2の内容）
sudo cp system_prompt.txt /etc/agriha/

# === Step 4: unipi-daemon REST API 疎通確認 ===
curl http://10.10.0.10:8080/api/sensors
# → CCM + DS18B20 + Misol + relay 全センサーデータが返ること

curl http://10.10.0.10:8080/api/status
# → relay_state (ch1-8), locked_out, uptime_sec が返ること
```

### 6.4 cronスクリプト

```bash
# === 定期制御ループ ===
# /etc/cron.d/agriha-control
*/5 * * * * root flock -n /tmp/agriha_control.lock \
  /usr/bin/python3 /opt/agriha-control/agriha_control.py \
  >> /var/log/agriha/control.log 2>&1
```

```bash
# ログディレクトリ作成
sudo mkdir -p /var/log/agriha
```

### 6.5 動作確認手順

```bash
# 1. llama-serverサービス確認
curl http://localhost:8081/health
# → {"status":"ok"} が返ること

# 2. unipi-daemon REST API確認
curl http://10.10.0.10:8080/api/sensors | python3 -m json.tool
# → sensors dict にCCM/DS18B20/Misol/relayデータが存在すること

# 3. 制御ループ手動テスト
python3 /opt/agriha-control/agriha_control.py
# → control_log.db に1レコード追加されていること確認
sqlite3 /var/lib/agriha/control_log.db "SELECT * FROM decisions ORDER BY id DESC LIMIT 1;"

# 4. cron実行確認（5分待つ）
tail -f /var/log/agriha/control.log
```

---

## 7. 安全制御設計

> **v2.0変更**: ArSproutコントローラソフトウェアがRaspbian Liteに入れ替えられたため、
> ArSproutの5階層優先度モデル（CCM priority）は**全面廃止**。
> 安全制御はunipi-daemon側で完結する3層モデルに変更。

### 7.1 3層安全モデル

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: 物理層（即時、最優先）                          │
│   - 緊急スイッチ (UniPi DI07-DI14)                      │
│   → gpio_watch → CommandGate → 300秒ロックアウト        │
│   - ロックアウト中: REST API relay操作は全て 423 拒否    │
│   - 手動解除: POST /api/emergency/clear                  │
│   - リレーは現状維持（明示的OFF操作は行わない）          │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Layer 2: LLMプロンプト安全制約                           │
│   - system_prompt.txt [F]セクションで定義（§3.2参照）    │
│   - 降雨中の窓開禁止、過熱時全開、凍結防止etc.          │
│   - LLMが5分ごとにセンサーを確認し安全制御を実行        │
│   - 応答時間: 30-110秒（即時性はLayer 1が担保）         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Layer 3: フォールバック（リレーラッチ + 自動OFFタイマー）│
│   - LLM/nipogi.local停止 → リレーは最後の状態を保持     │
│   - MqttRelayBridge duration_sec タイマー:               │
│     灌水ONなどは必ず自動OFF時間を指定                    │
│   - 最悪ケース: 灌水ON放置 → duration_sec で自動OFF     │
│   - Pi Lite再起動時: リレーは初期状態(全OFF)に復帰      │
└─────────────────────────────────────────────────────────┘
```

### 7.2 LLMの位置づけ

LLMはセンサーデータを読み取り、環境制御の判断を行う唯一のインテリジェント層。

- **判断者**: 温湿度・日射・風速・降雨を総合的に判断し、リレーを制御
- **安全担当**: システムプロンプトの安全制約に従い、危険な操作を自主回避
- **応答速度**: 30-110秒。即時安全はLayer 1（緊急スイッチ）が担保
- **停止時**: リレーは最後の状態を保持。duration_secで灌水等は自動OFF

### 7.3 フォールバック遷移

```
正常運転（LLMが5分ごとに制御判断）
    │
    │ nipogi.local停止 / ネットワーク断
    ▼
リレー現状維持（MCP23008はラッチ型）
    │ ├─ 灌水ON中 → duration_sec タイマーで自動OFF
    │ ├─ 換気扇ON中 → 回しっぱなし（電力消費のみ、安全上問題なし）
    │ └─ 全OFF中 → そのまま（最も安全な状態）
    │
    │ nipogi.local復旧
    │ → agriha_control.py cron再開
    │ → GET /api/status でリレー現状を確認
    │ → LLMが状況に応じて制御再開
    ▼
正常運転に復帰
```

### 7.4 将来拡張: イベント駆動安全層

LLMの5分間隔制御では間に合わない緊急事態（突然の豪雨等）に対応するため、
将来的にunipi-daemon内にルールベースの安全層を追加する選択肢がある。

```python
# 構想: unipi-daemon内にセンサー監視ループを追加
# CCM/MisolデータをMQTTで受信し、閾値超過時に即座にリレー操作
SAFETY_RULES = {
    "rain_close": {"trigger": "Misol rainfall > 0.5mm/h", "action": "窓系リレー全OFF"},
    "overheat":   {"trigger": "CCM InAirTemp > 40℃", "action": "換気扇ON + 窓リレーON"},
    "freeze":     {"trigger": "CCM InAirTemp < 5℃", "action": "暖房リレーON"},
}
```

実装はLLM制御ループの実運用開始後、必要性が確認されてから。

---

## 8. アクチュエータ制御（UniPiリレー）

> **v2.0変更**: CCM経由のArSproutアクチュエータ制御を全面廃止。
> 全アクチュエータをUniPi 1.1 MCP23008 I2Cリレー（ch1-8）経由で制御。

### 8.1 制御方式

UniPi 1.1のリレーは**単純ON/OFF型**。CCM時代の秒数制御（position_pct）は不要。

| 制御方式 | 説明 | 例 |
|----------|------|-----|
| **ON/OFF** | リレーをON/OFFするだけ | 換気扇、暖房、灌水弁 |
| **duration付きON** | ONにして指定秒数後に自動OFF | 灌水（300秒）、ミスト（60秒） |

### 8.2 制御フロー

```
LLM判断: 「灌水5分実行」
    │
    ▼
agriha_control.py → REST API POST /api/relay/4
    payload: {"value": 1, "duration_sec": 300, "reason": "灌水5分"}
    │
    ├─ CommandGate チェック
    │   └─ ロックアウト中 → 423 拒否
    │
    ├─ MQTT publish: agriha/h01/relay/4/set
    │
    ▼
MqttRelayBridge
    ├─ MCP23008 I2C → リレーch4 ON
    ├─ MQTT publish: agriha/h01/relay/state (全ch状態)
    └─ 300秒タイマー開始 → 自動OFF
```

### 8.3 安全ガード

| ガード | 実装場所 | 動作 |
|--------|---------|------|
| **ロックアウト** | CommandGate | 緊急スイッチ検知で300秒間全操作拒否 |
| **duration_sec必須** | システムプロンプト [F] | 灌水/ミスト等はduration_sec指定を義務付け |
| **自動OFFタイマー** | MqttRelayBridge | duration_sec指定時、タイマーで自動OFF |
| **チャンネル範囲** | REST API Path validation | ch1-8以外は400エラー |
| **API認証** | REST API X-API-Key | 無認証アクセスを拒否（本番設定時） |

### 8.4 モーター付きアクチュエータの秒数制御

側窓・天窓・カーテンなどモーター駆動のアクチュエータは、リレーON時間で開度を制御する。

```
例: 側窓を30%開ける（全開60秒の場合）
    │
    ├─ Step 1: POST /api/relay/{ch} value=1, duration_sec=18
    │          → リレーON（モーター開方向に回転）
    │
    ├─ Step 2: 18秒後に自動OFF（MqttRelayBridgeタイマー）
    │          → モーター停止
    │
    └─ 注意: 位置はソフトウェア推定のみ（フィードバックなし）
             → システムプロンプトで「毎朝全閉リセット」をLLMに指示
```

**制限事項**:
- UniPiリレーは1ch=1方向。開/閉で別チャンネルが必要なアクチュエータは2ch使用
- 位置フィードバックなし（エンコーダ等は未接続）
- LLMが判断履歴から前回の操作を参照し、おおよその位置を推定する

---

## 9. リレーチャンネル割当

### 9.1 割当表

| ch | 用途 | 制御方式 | duration_sec | 備考 |
|----|------|---------|-------------|------|
| 1 | （未割当） | - | - | |
| 2 | （未割当） | - | - | |
| 3 | （未割当） | - | - | |
| 4 | 灌水電磁弁 | ON→duration後OFF | 60-600秒 | 必ずduration_sec指定 |
| 5 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 6 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 7 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |
| 8 | 側窓（開閉） | ON→duration後OFF | 0-60秒 | 4ch(5-8)で側窓制御 |

> **側窓ch5-8の詳細**: 南北の開/閉でどのchがどの動作に対応するかは5月の実機確認で確定。
> 想定パターン: ch5=北側開, ch6=北側閉, ch7=南側開, ch8=南側閉 等（要実測）

### 9.2 残りchの候補

ch1-3 は未割当。以下の用途に割当可能:

| アクチュエータ候補 | 制御方式 | duration_sec目安 |
|------------------|---------|----------------|
| 換気扇 | ON/OFF | 0（手動OFF） |
| ミスト | ON→duration後OFF | 30-120秒 |
| 暖房 | ON/OFF | 0（手動OFF） |
| CO2バルブ | ON→duration後OFF | 60-300秒 |

### 9.3 確認タスク（5月予定）

1. ch5-8の側窓動作を実機確認（どのchが南/北/開/閉に対応するか）
2. ch4 灌水電磁弁のON/OFF動作確認、適切なduration_sec実測
3. ch1-3 の配線先を確認（未接続の場合は将来割当）
4. duration_secの適切な値を実測（側窓の全開秒数等）
5. システムプロンプト [B]セクションにch割当を記載
6. 本セクションの割当表を確定

---

## 10. 参照ドキュメント

| ドキュメント | パス | 内容 |
|------------|------|------|
| unipi-daemon | ~/unipi-agri-ha/services/unipi-daemon/ | 5タスクasyncioデーモン（センサー+MQTT+GPIO+REST+CCM） |
| MQTTトピック仕様 | ~/unipi-agri-ha/docs/mqtt_topic_spec.md | agriha/名前空間定義、QoS、ペイロード例 |
| LLM制御ロードマップ | ~/unipi-agri-ha/docs/llm_control_roadmap.md | Phase3.5-6の全体像 |
| 新アーキテクチャ設計書 | ~/unipi-agri-ha/docs/mqtt_remote_arch.md | ネットワーク/UniPi |
| 作物灌水設定 | ~/unipi-agri-ha/config/crop_irrigation.yaml | ステージ別パラメータ |
| LLMベンチマークシナリオ | ~/unipi-agri-ha/docs/llm_benchmark_scenarios.md | LINE Bot判断力テスト |
| uecs-ccm-mcp（参考） | ~/uecs-ccm-mcp/ | CCM受信テストツール（制御には使用しない） |

---

## 11. Chat窓（LLM育成用UI）

### 11.1 目的

nipogi.local上にブラウザからアクセスできるChat UIを設置する。
殿がLLM（llama-server / LFM2.5）と対話してシステムプロンプトを育てるためのツール。

```
育成サイクル:
  殿がsystem_prompt.txt [E]セクションを編集
    → Chat窓で「内温38℃、どうする？」と質問
    → LLMの回答を確認（適切か？安全か？）
    → 不十分なら [E]セクションに知識を追記
    → 再度Chat窓で確認
    → 満足したらcron制御ループに反映（自動的に次回起動で読み込まれる）
```

### 11.2 技術選定

| 方式 | RAM消費 | セットアップ | system_prompt.txt読み込み | 評価 |
|------|---------|------------|--------------------------|------|
| **(c) 自前FastAPI + HTML** | ~20-40MB | `pip install fastapi uvicorn` | ファイルから直接読み込み | **採用** |
| (a) Open WebUI | 500MB-1.5GB | Docker or pip（重量級） | UI上で手動設定 | 不採用 |
| (b) Hollama | ~30-50MB | npm build + nginx | セッションごとに手動貼り付け | 不採用 |
| (d) 単一HTMLファイル | ~10-20MB | ファイル1つ | Modelfile経由（手間） | 代替案 |

**採用理由**:

- **Open WebUI不採用**: RAM 500MB-1.5GBをアイドルで消費。N150でllama-server（1-5GB）と同居すると
  メモリが逼迫する。マルチユーザー管理・RAG・モデル管理等の不要機能が多すぎる
- **Hollama不採用**: 軽量で良いが、system_prompt.txtからの自動読み込みに対応していない。
  毎回手動でシステムプロンプトを貼り付ける必要がある
- **自前FastAPI + HTML採用**: 最軽量（~20-40MB）。system_prompt.txtからの直接読み込みが
  自然に実装できる。殿がテキストファイルを編集→ブラウザリロードで即反映。
  全体で200行程度のコード量

### 11.3 アーキテクチャ

```
ブラウザ (殿のPC/スマホ)
  │  http://nipogi.local:8501
  ▼
FastAPI (:8501, nipogi.local)
  │  ├─ GET /       → チャットUI (HTML)
  │  ├─ POST /chat  → llama-server /v1/chat/completions をプロキシ
  │  │               system_prompt.txt を自動注入
  │  └─ GET /prompt → 現在のシステムプロンプト表示
  ▼
llama-server (:8081, nipogi.local)
  │  LFM2.5 1.2B Q4
  ▼
レスポンス（ストリーミング）
```

### 11.4 実装概要

```python
#!/usr/bin/env python3
"""AgriHA Chat窓 — システムプロンプト育成用UI"""

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import httpx
import json

app = FastAPI()
LLAMA_SERVER_URL = "http://localhost:8081/v1/chat/completions"
SYSTEM_PROMPT_PATH = Path("/etc/agriha/system_prompt.txt")


def load_system_prompt() -> str:
    """毎リクエストでファイルから読み込み（編集即反映）"""
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return "あなたは温室環境制御AIです。"


@app.get("/", response_class=HTMLResponse)
async def chat_ui():
    """チャットUI HTML（インライン）"""
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AgriHA Chat</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
  #messages { height: 500px; overflow-y: auto; border: 1px solid #ccc; padding: 10px; }
  .user { color: blue; } .assistant { color: green; }
  #input { width: 80%; padding: 8px; } #send { padding: 8px 16px; }
</style></head><body>
<h2>AgriHA Chat — システムプロンプト育成</h2>
<p><small>system_prompt.txt を編集後、ページリロードで反映</small></p>
<div id="messages"></div>
<input id="input" placeholder="質問を入力..." />
<button id="send" onclick="sendMsg()">送信</button>
<script>
let history = [];
async function sendMsg() {
  const input = document.getElementById('input');
  const msg = input.value.trim(); if (!msg) return;
  input.value = '';
  addMsg('user', msg);
  history.push({role: 'user', content: msg});
  const res = await fetch('/chat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({messages: history})
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let full = '';
  addMsg('assistant', '');
  const last = document.querySelector('#messages .assistant:last-child');
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value);
    for (const line of chunk.split('\\n')) {
      if (!line.trim()) continue;
      try { const j = JSON.parse(line); full += j.message?.content || ''; }
      catch(e) {}
    }
    last.textContent = full;
  }
  history.push({role: 'assistant', content: full});
}
function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = role; d.textContent = (role==='user'?'殿: ':'AI: ') + text;
  document.getElementById('messages').appendChild(d);
  d.scrollIntoView();
}
document.getElementById('input').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendMsg();
});
</script></body></html>"""


@app.post("/chat")
async def chat(request: Request):
    """llama-serverにsystem_prompt付きでプロキシ"""
    body = await request.json()
    messages = body.get("messages", [])
    system_prompt = load_system_prompt()

    payload = {
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "stream": True,
        "max_tokens": 1024,
    }

    async def generate():
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", LLAMA_SERVER_URL, json=payload) as resp:
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield line + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.get("/prompt")
async def show_prompt():
    """現在のシステムプロンプトを表示（デバッグ用）"""
    return {"prompt": load_system_prompt()}
```

### 11.5 セットアップ手順

```bash
# === Step 1: 依存インストール ===
pip install fastapi uvicorn httpx

# === Step 2: スクリプト配置 ===
sudo cp agriha_chat.py /opt/agriha-control/

# === Step 3: systemdサービス登録 ===
sudo tee /etc/systemd/system/agriha-chat.service << 'EOF'
[Unit]
Description=AgriHA Chat UI (system prompt development)
After=network.target agriha-llm.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m uvicorn agriha_chat:app --host 0.0.0.0 --port 8501
WorkingDirectory=/opt/agriha-control
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now agriha-chat

# === Step 4: 動作確認 ===
# ブラウザで http://nipogi.local:8501 にアクセス
# 「内温が38℃です。どうしますか？」と質問してLLMの回答を確認
```

### 11.6 使い方（殿向け）

1. ブラウザで `http://nipogi.local:8501` を開く
2. 「内温38℃、外気温32℃、風速3m/s。どう制御する？」と質問
3. LLMの回答を確認。不十分なら `/etc/agriha/system_prompt.txt` を編集
4. ブラウザをリロード（新しいプロンプトが自動反映）
5. 同じ質問を再度投げて改善を確認
6. `/prompt` エンドポイントで現在読み込まれているプロンプトを確認可能

---

## 12. 栽培マニュアルPDF/JPG読み取り

### 12.1 目的

殿が長ナス栽培マニュアル（PDF or スキャンJPG）を投入し、
その内容をシステムプロンプトの[D]制御ルール・[E]暗黙知セクションに反映する。

### 12.2 マルチモーダルモデル調査

llama-serverで利用可能なビジョンモデル（GGUF形式）のうち、N150(16GB RAM, CPU推論)で動作可能なもの:

| モデル | パラメータ | Q4サイズ | RAM必要量 | N150速度(推定) | 日本語OCR品質 |
|--------|-----------|---------|-----------|---------------|-------------|
| **Qwen2.5-VL 3B** | 3B | 3.2GB | ~5-6GB | 2-4 tok/s, 画像30-50秒 | **非常に良い（MTVQA1位）** |
| Qwen3-VL 2B | 2B | 1.9GB | ~3-4GB | 3-5 tok/s, 画像30-45秒 | 良い |
| Moondream 1.8B | 1.8B | 0.8GB | ~2GB | 5-8 tok/s, 画像20-30秒 | 悪い（英語中心） |
| GLM-OCR 0.9B | 0.9B | 1.6GB | ~2-3GB | 6-10 tok/s, 画像15-25秒 | 中（中国語中心） |
| Gemma 3 4B | 4B | 3.3GB | ~5-6GB | 1-3 tok/s, 画像40-60秒 | 良い |
| MiniCPM-V 8B | 8B | 5.5GB | ~8-10GB | 0.3-0.8 tok/s | 非常に良いが遅すぎ |

**N150での実用上の制約**:
- 画像エンコードだけで30-60秒かかる（ビジョンエンコーダのオーバーヘッド）
- テキスト生成はさらに低速（2-4 tok/s）
- 1ページの読み取りに合計1-3分かかる見込み
- マニュアルが数十ページある場合、バッチ処理に数時間

### 12.3 Tesseract OCR（代替方式）

| 比較項目 | Tesseract OCR (jpn) | Qwen2.5-VL 3B |
|----------|---------------------|---------------|
| **速度** | **1-3秒/ページ** | 60-180秒/ページ |
| **RAM** | ~200-500MB | ~5-6GB |
| **清刷り日本語** | 89-94%精度 | 70-85%精度 |
| **劣化スキャン** | 65-80%精度 | 40-60%（幻覚リスク） |
| **表・レイアウト理解** | 低い（テキスト抽出のみ） | 高い（構造を理解） |
| **専門用語理解** | なし（文字認識のみ） | 文脈から推測可能 |
| **インストール** | `apt install tesseract-ocr tesseract-ocr-jpn` | llama-server + 3GB GGUF |

### 12.4 推奨方式: ハイブリッドアプローチ

**Tesseract OCRを主体とし、Qwen2.5-VL 3Bを補助に使うハイブリッド方式を推奨。**

```
栽培マニュアル PDF/JPG
    │
    ├─[一括処理] Tesseract OCR（高速、全ページ）
    │   → テキスト抽出（1-3秒/ページ）
    │   → raw_text/*.txt に保存
    │
    ├─[選択処理] Qwen2.5-VL 3B（低速、難読ページのみ）
    │   → 表・図・レイアウトが複雑なページ
    │   → Tesseractで精度不足のページ
    │   → 1-3分/ページ
    │
    ▼
殿がテキストをレビュー + 編集
    │
    ▼
system_prompt.txt [D][E]セクションに反映
    │
    ▼
Chat窓（§11）で「この場合どう制御する？」と確認
```

**理由**:
1. N150のCPU推論では、VLMのみで数十ページを処理するのは非実用的（数時間）
2. 清刷りの日本語印刷物ならTesseractの精度で十分（89-94%）
3. VLMは表やグラフの構造理解が必要な場面のみに限定
4. 最終的に人間（殿）がレビューするため、完璧なOCRは不要

### 12.5 セットアップ手順

```bash
# === Tesseract OCR ===
sudo apt install tesseract-ocr tesseract-ocr-jpn tesseract-ocr-jpn-vert

# PDFをページ画像に変換
sudo apt install poppler-utils
# pdftoppm manual.pdf pages/page -png

# === Qwen2.5-VL 3B（補助用） ===
# GGUF形式でダウンロード（HuggingFace）
# RAM消費: LFM2.5 1.2B（制御用）とQwen2.5-VL 3Bは同時にロードしない
# → 使い分け: 制御中はVLモデルのllama-serverを停止
```

### 12.6 読み取りスクリプト

```bash
#!/bin/bash
# ocr_manual.sh — 栽培マニュアルバッチOCR
# Usage: ./ocr_manual.sh input.pdf output_dir/

INPUT="$1"
OUTPUT_DIR="$2"
mkdir -p "$OUTPUT_DIR"

# PDFを画像に変換
if [[ "$INPUT" == *.pdf ]]; then
    pdftoppm "$INPUT" "$OUTPUT_DIR/page" -png -r 300
    IMAGES="$OUTPUT_DIR"/page-*.png
else
    IMAGES="$INPUT"
fi

# Tesseract OCR実行
for img in $IMAGES; do
    base=$(basename "$img" .png)
    echo "Processing: $base"
    tesseract "$img" "$OUTPUT_DIR/$base" -l jpn --psm 6
done

echo "Done. Output in $OUTPUT_DIR/"
echo "Review .txt files and incorporate into /etc/agriha/system_prompt.txt"
```

### 12.7 VLMを使った補助読み取り

```python
# llama-server マルチモーダルで難読ページを処理
# llama-server --model <VLMモデル>.gguf --port 8082 で別ポート起動
import httpx
import base64

LLAMA_VLM_URL = "http://localhost:8082"  # VLM専用llama-serverポート

def read_page_with_vlm(image_path: str) -> str:
    """VLM (マルチモーダルGGUFモデル) で画像からテキスト抽出"""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    resp = httpx.post(
        f"{LLAMA_VLM_URL}/v1/chat/completions",
        json={
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "この画像は農業栽培マニュアルのページです。"
                                              "日本語テキストを全て読み取り、表があれば構造を保持して出力してください。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 2048,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
```

### 12.8 システムプロンプトへの反映フロー

```
1. OCR結果のテキストファイルを殿がレビュー
2. 制御に関連する知識を抽出:
   - 温度管理ルール → [D]セクション
   - 灌水タイミング → [C]セクション（crop_irrigation.yaml更新）
   - 栽培のコツ・注意点 → [E]セクション
3. system_prompt.txt に追記
4. Chat窓（§11）で「長ナスの定植直後、内温30℃。どうする？」と質問
5. LLMの回答が栽培マニュアルの内容と整合するか確認
6. 不十分なら[D][E]を修正して再テスト
```

---

## 付録A: v1.x→v2.0 変更履歴

### A.1 アーキテクチャ変更の背景

ArSproutコントローラ（192.168.1.65）のソフトウェアをRaspbian Liteに入れ替えたため、
ArSproutのCCM制御パケット受信・アクチュエータ駆動機能が**全て利用不可**になった。

残存する機能:
- ArSprout観測ノード（192.168.1.70）: CCMセンサーデータ送信のみ（InAirTemp, InAirHumid, CO2等）

### A.2 廃止された設計要素

| v1.x 設計要素 | 廃止理由 |
|--------------|---------|
| 5階層優先度モデル（CCM priority） | ArSproutがCCM制御パケットを受信不可 |
| uecs-ccm-mcp set_actuator | CCM送信先のArSproutリレーが動作しない |
| state.json（position_pct推定） | UniPiリレーはON/OFFのみ、位置管理不要 |
| actuator_config.yaml（秒数制御設定） | CCM秒数制御が不要 |
| TASK A-D（実機調査） | ArSproutアクチュエータ制御が前提のタスク |
| ccm_watchdog.py（イベント駆動安全層） | CCM経由で制御しないため不要 |
| ArSprout警報駆動制御（安全制御委任） | ArSproutコントローラが存在しない |
| MCP stdio接続（uecs-ccm-mcp経由） | REST API直接通信に変更 |

### A.3 新規追加された設計要素

| v2.0 設計要素 | 説明 |
|--------------|------|
| UniPi I2Cリレーch1-8による全アクチュエータ制御 | MCP23008経由 |
| REST API直接通信（agriha_control.py → unipi-daemon） | MCP廃止 |
| CommandGate 3層安全モデル | 緊急スイッチ + LLMプロンプト + リレーラッチ |
| unipi-daemon ccm_receiver | CCMセンサーデータ受信→MQTT publish |
| MqttRelayBridge duration_sec 自動OFF | 灌水等の安全タイマー |

### A.4 継続利用される設計要素

| 設計要素 | 変更有無 |
|---------|---------|
| agriha_control.py（cron 5分間隔） | ツール呼び出しをMCP→REST APIに変更 |
| LFM2.5 (llama-server) tool calling | ツール定義を変更（set_actuator→set_relay）、OpenAI互換API使用 |
| システムプロンプト設計（§3） | [B]にリレーch割当追加、[D]からArSprout依存部分削除 |
| 判断履歴DB control_log.db（§4） | 変更なし |
| Chat窓（§11） | 変更なし |
| 栽培マニュアルOCR（§12） | 変更なし |
