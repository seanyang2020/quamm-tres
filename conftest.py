"""确保项目根目录在 sys.path 上，使 `pytest tests/ -q` 与 `python -m pytest` 都能导入 app/。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
