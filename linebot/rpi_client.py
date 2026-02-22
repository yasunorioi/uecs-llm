"""RPi REST API クライアント (同期版)

unipi-daemon が動作する RPi への REST API アクセスを提供する。
WireGuard VPN 経由で RPi に接続する（デフォルト: 10.10.0.10:8080）。

環境変数:
  RPI_API_URL     RPi の REST API ベース URL (例: http://10.10.0.10:8080)
  RPI_API_KEY     API キー (空文字で認証スキップ)
  RPI_API_TIMEOUT タイムアウト秒数 (デフォルト: 10)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

RPI_API_URL = os.getenv("RPI_API_URL", "http://10.10.0.10:8080")
RPI_API_KEY = os.getenv("RPI_API_KEY", "")
TIMEOUT_SEC = float(os.getenv("RPI_API_TIMEOUT", "10"))


def _headers() -> dict[str, str]:
    """API キーヘッダーを返す。空文字の場合は空 dict。"""
    if RPI_API_KEY:
        return {"X-API-Key": RPI_API_KEY}
    return {}


def get_sensors() -> dict:
    """GET /api/sensors — ハウスの最新センサーデータを取得する。

    Returns:
        {"sensors": {...}, "updated_at": float, "age_sec": float}
        エラー時: {"error": "...", "message": "..."}
    """
    try:
        with httpx.Client(timeout=TIMEOUT_SEC) as client:
            resp = client.get(f"{RPI_API_URL}/api/sensors", headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        logger.warning("RPi API 接続エラー: %s", RPI_API_URL)
        return {
            "error": "connection_failed",
            "message": f"RPi ({RPI_API_URL}) に接続できませんでした。VPN接続を確認してください。",
        }
    except httpx.TimeoutException:
        logger.warning("RPi API タイムアウト")
        return {"error": "timeout", "message": "RPi API がタイムアウトしました。"}
    except httpx.HTTPStatusError as exc:
        logger.error("RPi API HTTP エラー %d", exc.response.status_code)
        return {"error": f"http_{exc.response.status_code}", "message": str(exc)}
    except Exception as exc:
        logger.error("RPi API エラー: %s", exc)
        return {"error": "api_error", "message": str(exc)}


def set_relay(
    ch: int,
    value: int,
    duration_sec: float = 0.0,
    reason: str = "",
) -> dict:
    """POST /api/relay/{ch} — リレーを制御する。

    Args:
        ch:           チャンネル番号 (1-8)
        value:        0=OFF, 1=ON
        duration_sec: 自動 OFF までの秒数 (0=タイマーなし)
        reason:       制御理由 (ログ用)

    Returns:
        {"ch": int, "value": int, "queued": bool} or {"error": "...", "message": "..."}
    """
    try:
        with httpx.Client(timeout=TIMEOUT_SEC) as client:
            resp = client.post(
                f"{RPI_API_URL}/api/relay/{ch}",
                json={"value": value, "duration_sec": duration_sec, "reason": reason},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        logger.warning("RPi API 接続エラー: %s", RPI_API_URL)
        return {
            "error": "connection_failed",
            "message": f"RPi ({RPI_API_URL}) に接続できませんでした。VPN接続を確認してください。",
        }
    except httpx.TimeoutException:
        logger.warning("RPi API タイムアウト")
        return {"error": "timeout", "message": "RPi API がタイムアウトしました。"}
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 423:
            try:
                body = exc.response.json()
                remaining = body.get("remaining_sec", "?")
                return {
                    "error": "locked_out",
                    "message": f"緊急スイッチによりロックアウト中（残り {remaining} 秒）。物理スイッチを戻してください。",
                }
            except Exception:
                pass
        logger.error("RPi API HTTP エラー %d", status)
        return {"error": f"http_{status}", "message": str(exc)}
    except Exception as exc:
        logger.error("RPi API エラー: %s", exc)
        return {"error": "api_error", "message": str(exc)}
