"""The program PyInstaller turns into TikTok Guard.exe (and, as a console program, TikTok Guard Diagnostics.exe)."""
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

if not getattr(sys, "frozen", False):                       # started from source for a quick check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fatal(exc: BaseException) -> int:
    """Something failed before the app's own error handling existed (the settings could not even be loaded). A windowless program
    that fails silently is the worst kind: say it in a message box and leave the details in a file the person can send."""
    crash = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "TikTok Guard crash.txt"
    try:
        crash.write_text(traceback.format_exc(), encoding="utf-8")
    except OSError:
        pass
    text = f"TikTok Guard could not start.\n\n{type(exc).__name__}: {str(exc)[:400]}\n\nDetails were saved in:\n{crash}"
    if os.environ.get("TIKTOK_GUARD_NO_DIALOG") or sys.platform != "win32":
        stream = sys.stderr or sys.stdout
        if stream is not None:
            print(text, file=stream)
    else:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "TikTok Guard", 0x10 | 0x10000 | 0x40000)
        except Exception:  # noqa: BLE001
            pass
    return 1


def entry() -> int:
    try:
        if getattr(sys, "frozen", False) and "diagnostics" in Path(sys.executable).stem.lower():
            from app.launcher import diagnostics
            return diagnostics()
        from app.launcher import main
        return main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        return fatal(exc)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(entry())
