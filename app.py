"""TikTok guard - desktop application.   python app.py [--no-window | --check | --self-test | --version]

This file is only the doorway: the launcher itself is app/launcher.py (so the packaged exe and `python app.py` run the very same
code). (It is named app.py on purpose; the `app/` folder next to it is the package with the code - `import app` finds that.)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
