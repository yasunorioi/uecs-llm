"""pytest conftest.py — src ディレクトリを sys.path に追加する。"""
import sys
from pathlib import Path

# src/ ディレクトリを sys.path の先頭に追加（v2_control 等のパッケージ解決用）
_src = str(Path(__file__).parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
