"""
農業ハウス管理システムプロンプト

agriha_control.py の system_prompt.txt ([A]-[F]) をベースに、
LINE Bot 対話用セクション [G] を追加。
Webhook 受信時に日時・センサー現在値・直近の制御判断を自動注入する。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun as astral_sun

# 道央ハウス座標（agriha_control.py と同じ値）
_LAT = 42.888
_LON = 141.603
_ELEVATION = 21.0
_JST = ZoneInfo("Asia/Tokyo")


def _get_sun_times(dt: datetime | None = None) -> tuple[datetime, datetime]:
    """当日の日の出/日没時刻を返す（JST）。"""
    ref = dt if dt is not None else datetime.now(_JST)
    location = LocationInfo(latitude=_LAT, longitude=_LON)
    s = astral_sun(location.observer, date=ref.date(), tzinfo=_JST)
    return s["sunrise"], s["sunset"]


def _get_time_period(now: datetime, sunrise: datetime, sunset: datetime) -> str:
    """現在時刻から時間帯ラベルを返す。"""
    if now < sunrise:
        return "日の出前"
    if now >= sunset:
        return "日没後"
    if now >= sunset - timedelta(hours=1):
        return "日没前1時間"
    return "日中（日の出後〜日没前1時間）"


def _format_sensor_context(sensors: dict) -> str:
    """センサーデータをプロンプト注入用テキストに整形する。"""
    if "error" in sensors:
        return f"[現在のセンサーデータ]\n取得失敗: {sensors.get('message', '不明')}\n"

    s = sensors.get("sensors", {})
    relay = sensors.get("relay", {})

    lines = ["[現在のセンサーデータ]"]

    # 主要センサー
    temp_in = s.get("temp_in")
    humid_in = s.get("humid_in")
    co2 = s.get("co2")
    temp_out = s.get("temp_out")
    wind_speed = s.get("wind_speed")

    parts = []
    if temp_in is not None:
        parts.append(f"室温: {temp_in:.1f}℃")
    if humid_in is not None:
        parts.append(f"湿度: {humid_in:.0f}%")
    if co2 is not None:
        parts.append(f"CO2: {co2:.0f}ppm")
    if temp_out is not None:
        parts.append(f"外気温: {temp_out:.1f}℃")
    if wind_speed is not None:
        parts.append(f"風速: {wind_speed:.1f}m/s")
    if parts:
        lines.append(", ".join(parts))

    # リレー状態
    if relay:
        on_chs = [str(ch) for ch, val in relay.items() if val]
        if on_chs:
            lines.append(f"リレー ON: ch{', '.join(on_chs)}")
        else:
            lines.append("リレー: 全OFF")

    age = sensors.get("age_sec")
    if age is not None:
        lines.append(f"（{int(age)}秒前のデータ）")

    return "\n".join(lines) + "\n"


def _format_history_context(history: list[dict]) -> str:
    """制御判断履歴をプロンプト注入用テキストに整形する。"""
    if not history:
        return "[直近の制御判断]\n履歴なし（または取得失敗）\n"

    lines = ["[直近の制御判断]"]
    for entry in history:
        ts = entry.get("timestamp", "")
        summary = entry.get("summary", "")
        actions = entry.get("actions_taken", "")
        line = f"[{ts}] {summary}"
        if actions and actions != "no_action":
            line += f" → {actions}"
        lines.append(line)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# プロンプト本文（agriha_control.py の system_prompt.txt [A]-[F] + [G] 対話用）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_BODY = """# [A] 役割定義
あなたは北海道恵庭市の温室で長ナスを水耕栽培する農家のアシスタントです。
センサーデータと制御判断履歴を参照し、実用的なアドバイスを提供します。
必要に応じてツールを使ってリアルタイムデータを取得し、制御を実行できます。

# [B] ハウス固有情報
- ハウスID: h1
- 作物: 長ナス（水耕栽培・ココバッグ）
- 位置: 北緯42.888° 東経141.603° 標高21m（道央 恵庭市）
- アクチュエータ: UniPi 1.1 リレー ch1-8
  - ch4: 灌水電磁弁（必ずduration_sec指定）
  - ch5-8: 側窓開閉（詳細は制御ルール参照）
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
- 北風(NNW〜NNE) + 風速≧5m/s → 北側閉鎖、南側は開放維持
- 南風(SSE〜SSW) + 風速≧5m/s → 南側閉鎖、北側は開放維持
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

## 道央の気候特性（2025年）
- 年間を通じて曇天が多い（雲量平均66%）
- 外気湿度が高い（平均80%）
- 気温範囲: -6℃〜33℃（土壌温度-2〜34℃）
- 露点マージン（内温と露点温度の差）が3℃未満になると結露リスク
- 深夜0〜6時はほぼ結露発生状態、日中10〜14時は比較的安全

# [F] 安全制約（絶対遵守）
- 灌水・ミスト等のONは必ず duration_sec を指定すること（最大3600秒）
- 降雨中（rainfall > 0）は絶対に側窓を開けない
- 側窓の開閉は片側ずつ。両側同時操作しない
- 40℃超は緊急事態。全窓全開+ファンON
- 5℃以下は凍結リスク。カーテン閉+暖房ON
- 制御不要と判断した場合は「現状維持」と明記し、何も操作しない
- ロックアウト中（GET /api/status の locked_out=true）はリレー操作しない
- **基本原則: 異常を感じたらまずブレーカーを落とすこと。ソフトウェアの安全装置より、電気を物理的に遮断するのが最終かつ確実な手段です。**

# [G] 対話モード（LINE Bot 専用）
- ユーザーからの質問には、上部に注入されたセンサーデータと判断履歴を参照して回答せよ
- 制御コマンドを求められた場合はツール（actuator_control / relay_test）を使え
- 雑談にも応じるが、常にハウスの状況を意識した回答をせよ
- 農家目線で、平易な言葉で答える。専門用語は括弧で説明を加える
- 短く具体的に（3〜5文程度）
- 分からないことは「分かりません」と素直に答える
- **「例年通りで大丈夫」とは安易に答えない**（気候変動により過去の経験則が崩れていることがある）
- EC調整はドサトロン（水流駆動・手動ダイヤル）のみ。コンピュータからEC自動制御は不可
- 異常（葉の変色、虫の発生など）を感じたら早めに農業改良普及員に相談することを推奨
"""


def get_system_prompt(
    include_sensors: bool = True,
    include_history: bool = True,
) -> str:
    """現在の日時・センサーデータ・制御履歴を先頭に注入したシステムプロンプトを返す。

    Args:
        include_sensors: センサーデータ注入（デフォルト: True）
        include_history: 制御判断履歴注入（デフォルト: True）
    """
    import rpi_client  # 循環インポート回避のため遅延インポート

    now = datetime.now(_JST)
    sunrise, sunset = _get_sun_times(now)
    time_period = _get_time_period(now, sunrise, sunset)

    header = (
        f"## 現在の日時情報\n"
        f"現在日時: {now.strftime('%Y-%m-%d %H:%M')} JST\n"
        f"日の出: {sunrise.strftime('%H:%M')} / 日没: {sunset.strftime('%H:%M')}\n"
        f"時間帯: {time_period}\n\n"
    )

    context = ""
    if include_sensors:
        sensors = rpi_client.get_sensors()
        context += _format_sensor_context(sensors) + "\n"

    if include_history:
        history = rpi_client.get_history(limit=3)
        context += _format_history_context(history) + "\n"

    return header + context + _SYSTEM_PROMPT_BODY
