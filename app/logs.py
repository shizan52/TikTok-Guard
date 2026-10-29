"""One log file for the desktop app, so that "what went wrong yesterday?" has an answer.

    data/logs/desktop.log     2 MB x 5 files (never grows without bound); every line says when, how bad, which thread, who.
                              Every uncaught error - in any thread - ends up here with its traceback.

Secrets never reach the file: the API token, an OpenRouter key and a Telegram bot token are masked on the way in.
`tail()` gives the Help page the last lines (masked again - an old file may have been written by an older version).
"""
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler

import config

FILE_NAME = "desktop.log"
FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_MASKS = (
    (re.compile(r"(token=)[^&\s\"']+", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (re.compile(r"sk-or-v1-[A-Za-z0-9]+"), "sk-or-***"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"), "***:telegram-token***"),
)


def mask(text: str) -> str:
    """`text` without anything that must stay secret."""
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text


class _Mask(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = mask(record.getMessage()), ()
        except Exception:  # noqa: BLE001 - a log line must never be the thing that breaks
            pass
        return True


def path():
    return config.LOG_DIR / FILE_NAME


def setup(level: int = logging.INFO) -> None:
    """Start logging to the file (once per process) and make uncaught errors - in any thread - land there too."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(h, "_guard_file", False) for h in root.handlers):
        handler = RotatingFileHandler(path(), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(_Mask())
        handler._guard_file = True
        root.addHandler(handler)
    root.setLevel(level)

    def uncaught(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger("uncaught").critical("uncaught error", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def uncaught_in_thread(args):
        if not issubclass(args.exc_type, SystemExit):
            logging.getLogger("uncaught").critical("uncaught error in thread %s", getattr(args.thread, "name", "?"),
                                                   exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = uncaught_in_thread


def tail(lines: int = 80) -> list:
    """The last `lines` lines of the log, masked. [] when there is no log yet."""
    try:
        with open(path(), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [mask(line) for line in data.splitlines()[-max(1, int(lines)):]]
