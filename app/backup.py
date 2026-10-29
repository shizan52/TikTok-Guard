"""A daily copy of the database - the one file that holds what cannot be re-read from TikTok: the AI instructions, the company
information the AI answers from, every reply card and the settings.

    data/backups/bot-2026-10-15.db     one per day, the newest KEEP_DAYS are kept, older ones are deleted

The copy is made with SQLite's own backup API (it is consistent even while the app is writing) and is checked before it is
trusted: a copy that fails `PRAGMA integrity_check` is deleted again and reported - never kept as a false comfort.
Browser logins (cookies) are NOT copied: they are secrets of the accounts, and a login can always be done again.
"""
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

import config

LOG = logging.getLogger("backup")
KEEP_DAYS = 7
_NAME = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.db$")


def backup_dir() -> Path:
    return config.DATA_DIR / "backups"


def make_backup(db_path=None, dest_dir=None, *, keep: int = KEEP_DAYS, today: date | None = None) -> Path | None:
    """Today's copy of the database. -> its path, or None when there is nothing to copy or today's copy already exists.
    Never raises: a backup that fails is logged, the app goes on."""
    src, dest_dir = Path(db_path or config.DB_FILE), Path(dest_dir or backup_dir())
    partial = None
    try:
        if not src.exists() or src.stat().st_size == 0:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"bot-{(today or date.today()).isoformat()}.db"
        if target.exists():
            _prune(dest_dir, keep)
            return None
        partial = target.with_suffix(".part")
        partial.unlink(missing_ok=True)
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        verdict = _integrity(partial)
        if verdict != "ok":
            partial.unlink(missing_ok=True)
            LOG.error("the backup of the database failed its integrity check (%s) - it was deleted", str(verdict)[:100])
            return None
        partial.replace(target)
        _prune(dest_dir, keep)
        LOG.info("database backed up to %s (%d KB)", target.name, target.stat().st_size // 1024)
        return target
    except Exception:  # noqa: BLE001
        LOG.exception("the daily backup of the database failed")
        if partial is not None:
            try:
                partial.unlink(missing_ok=True)                 # a half-written copy is never left lying around
            except OSError:
                pass
        return None


def _integrity(path: Path) -> str:
    """SQLite's own verdict on a database file: 'ok' or a description of the damage."""
    con = sqlite3.connect(path)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def _prune(dest_dir: Path, keep: int) -> list:
    """Delete all but the newest `keep` daily copies (only files made by this module are ever touched)."""
    copies = sorted((p for p in dest_dir.glob("bot-*.db") if _NAME.match(p.name)), key=lambda p: p.name)
    gone = copies[:-keep] if keep > 0 else copies
    for p in gone:
        try:
            p.unlink()
        except OSError:
            LOG.warning("could not delete the old backup %s", p.name)
    return [p.name for p in gone]


def list_backups(dest_dir=None) -> list:
    """[{name, day, kb}] newest first - what Help -> Diagnostics shows."""
    d = Path(dest_dir or backup_dir())
    rows = [{"name": p.name, "day": _NAME.match(p.name).group(1), "kb": p.stat().st_size // 1024}
            for p in d.glob("bot-*.db") if _NAME.match(p.name)] if d.exists() else []
    return sorted(rows, key=lambda r: r["name"], reverse=True)
