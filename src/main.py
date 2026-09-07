"""Shim so `python src/main.py` works the same as `python main.py`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from main import main
    sys.exit(main())
