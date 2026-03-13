"""pytest 設定: linebot パッケージをパスに追加する。"""
import os
import sys
from pathlib import Path

# linebot/ ディレクトリを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

# テスト用ダミー環境変数（モジュールレベルの os.environ[] アクセスに対応）
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test_dummy_token")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test_dummy_secret")
