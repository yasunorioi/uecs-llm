#!/usr/bin/env python3
"""
import_logs.py — LINE Bot テキストログ → SQLite インポートスクリプト

Docker logs 形式のテキストログをパースし、conversations テーブルにINSERTする。

想定ログ形式（uvicorn + FastAPI のログ）:
    INFO:app:Received: <user_message>       # ユーザーメッセージ（80文字で打ち切り）
    INFO:app:Quiz mode: random scenario selected  # クイズモード時のみ
    INFO:app:Replied: <bot_response>        # Bot応答（80文字で打ち切り）
    INFO:     172.29.0.1:xxx - "POST ..."   # uvicornアクセスログ（無視）

制約:
- ログにタイムスタンプなし → base_ts から 1秒ずつ増加する連番を使用
- ログにuser_idなし → "log_import_unknown" を使用
- メッセージは80文字で打ち切られている
- ユーザーメッセージが改行を含む場合、複数行にまたがる
"""

import sys
import sqlite3
import re
from datetime import datetime, timezone, timedelta

DB_PATH = "/app/data/conversations.db"
DEFAULT_MODEL = "qwen3:8b"
IMPORT_USER_ID = "log_import_unknown"
# ログのベースタイムスタンプ（ファイル作成日 2026-02-19 を使用）
BASE_TS = datetime(2026, 2, 19, 0, 0, 0, tzinfo=timezone.utc)

# INFO: から始まる行を識別するパターン（メッセージ継続行との区別に使用）
LOG_LINE_PATTERN = re.compile(r'^(INFO|WARNING|ERROR|DEBUG|CRITICAL):')


def parse_log(filepath: str) -> list[tuple[str, str, str]]:
    """
    ログファイルをパースして (role, message, model) のリストを返す。

    Returns:
        list of (role, message, model)
        role: 'user' または 'assistant'
        model: 'quiz' または 'qwen3:8b'
    """
    entries = []
    current_role = None
    current_message_lines = []
    current_model = DEFAULT_MODEL
    quiz_next = False

    def flush_entry():
        """現在のエントリを entries に追加する。"""
        if current_role and current_message_lines:
            msg = '\n'.join(current_message_lines)
            entries.append((current_role, msg, current_model))

    with open(filepath, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.rstrip('\n')

            if line.startswith('INFO:app:Received: '):
                flush_entry()
                current_role = 'user'
                current_message_lines = [line[len('INFO:app:Received: '):]]
                current_model = DEFAULT_MODEL
                quiz_next = False

            elif line.startswith('INFO:app:Quiz mode:'):
                # クイズモード検出 → assistant の model を "quiz" に切り替え
                quiz_next = True

            elif line.startswith('INFO:app:Replied: '):
                # user エントリを確定
                if current_role == 'user' and current_message_lines:
                    entries.append(('user', '\n'.join(current_message_lines), DEFAULT_MODEL))
                # assistant エントリ開始
                current_role = 'assistant'
                current_message_lines = [line[len('INFO:app:Replied: '):]]
                current_model = 'quiz' if quiz_next else DEFAULT_MODEL

            elif LOG_LINE_PATTERN.match(line) or not line:
                # 他のINFO/WARNING等、または空行 → メッセージ継続ではない
                pass

            else:
                # メッセージ継続行（改行を含むユーザー/Bot発言）
                if current_role and current_message_lines:
                    current_message_lines.append(line)

    flush_entry()
    return entries


def import_entries(entries: list[tuple[str, str, str]], db_path: str) -> tuple[int, int]:
    """
    パース結果をSQLiteにINSERT する。重複（role + message の一致）はスキップ。

    Returns:
        (inserted_count, skipped_count)
    """
    inserted = 0
    skipped = 0

    with sqlite3.connect(db_path) as conn:
        for i, (role, message, model) in enumerate(entries):
            # 重複チェック: role + message が同じ行が既にあればスキップ
            cur = conn.execute(
                "SELECT id FROM conversations WHERE role = ? AND message = ?",
                (role, message),
            )
            if cur.fetchone():
                skipped += 1
                print(f"  [SKIP] {role}: {message[:50]!r}")
                continue

            # タイムスタンプ: BASE_TS + i秒（ペア順に連番）
            ts = (BASE_TS + timedelta(seconds=i)).isoformat()

            conn.execute(
                "INSERT INTO conversations (timestamp, user_id, role, message, model, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, IMPORT_USER_ID, role, message, model, "log_import"),
            )
            inserted += 1
            print(f"  [INSERT] {role} ({model}): {message[:50]!r}")

        conn.commit()

    return inserted, skipped


def show_summary(db_path: str) -> None:
    """インポート後の統計を表示する。"""
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(timestamp) FROM conversations WHERE session_id='log_import'"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(timestamp) FROM conversations WHERE session_id='log_import'"
        ).fetchone()[0]
        users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM conversations WHERE session_id='log_import'"
        ).fetchone()[0]

    print(f"\n=== インポート後の統計 ===")
    print(f"  DB総レコード数  : {total}")
    print(f"  インポート最古TS: {oldest}")
    print(f"  インポート最新TS: {newest}")
    print(f"  ユニークユーザー: {users}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <log_file>")
        sys.exit(1)

    log_file = sys.argv[1]
    print(f"=== ログインポート開始 ===")
    print(f"  ログファイル: {log_file}")
    print(f"  DB: {DB_PATH}")

    entries = parse_log(log_file)
    print(f"\n  パース結果: {len(entries)} エントリ")
    for i, (role, msg, model) in enumerate(entries):
        print(f"  [{i+1}] {role} ({model}): {msg[:60]!r}")

    print(f"\n=== DBインポート開始 ===")
    inserted, skipped = import_entries(entries, DB_PATH)

    print(f"\n=== 完了 ===")
    print(f"  INSERT: {inserted}, SKIP: {skipped}")

    show_summary(DB_PATH)


if __name__ == "__main__":
    main()
