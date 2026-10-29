"""Locate the bfp stealth-browser module wherever the project is installed.

Search order: $BFP_PATH, <project>/vendor (what a deployment bundle ships), <project>/../bfp. The first folder containing
bfp.py is put on sys.path."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ensure_bfp_on_path() -> Path:
    if getattr(sys, "frozen", False):                      # the packaged app carries its own copy: never look anywhere else
        import bfp
        return Path(bfp.__file__).resolve().parent
    candidates = [os.environ.get("BFP_PATH"), ROOT / "vendor", ROOT.parent / "bfp"]
    for c in candidates:
        if c and (Path(c) / "bfp.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return Path(c)
    raise ImportError("bfp.py not found: set BFP_PATH or put bfp.py in <project>/vendor")
