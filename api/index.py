import os
import sys
from pathlib import Path

os.environ.setdefault("VERCEL", "1")

ROOT_DIR = Path(__file__).parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server import app  # noqa: E402, F401
