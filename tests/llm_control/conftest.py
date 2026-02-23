import sys
from pathlib import Path

# src/uecs_llm/ をsys.pathに追加 → "from agriha_control import" を解決
sys.path.insert(0, str(Path(__file__).parents[2] / "src" / "uecs_llm"))
