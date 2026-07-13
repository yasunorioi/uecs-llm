"""pytest conftest.py — src ディレクトリを sys.path に追加する。

ogms-dsl は pyproject.toml で git+ssh 依存として宣言済だが、pip install 前の
local dev 環境 (venv 無し) では import できない。次善策として `~/ogms-DSL/src`
が存在すれば sys.path に足す (editable install 相当)。
"""
import sys
from pathlib import Path

# src/ ディレクトリを sys.path の先頭に追加（v2_control 等のパッケージ解決用）
_src = str(Path(__file__).parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# ogms-DSL local fallback: pip install なしでも import 可能にする
_ogms_local = Path.home() / "ogms-DSL" / "src"
if _ogms_local.is_dir():
    _ogms_str = str(_ogms_local)
    if _ogms_str not in sys.path:
        sys.path.insert(0, _ogms_str)
