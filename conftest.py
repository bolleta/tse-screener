"""pytest 共通設定。リポジトリルートを import パスに通し、`import src.*` を解決する。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
