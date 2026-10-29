"""One-time import of what the earlier JSON-file version of the bot had already processed, so the
first run on the database does not re-announce comments that were handled long ago."""
import json

import config
from app.db import DB, utcnow


def import_legacy(db: DB) -> dict:
    if db.meta_get("legacy_imported"):
        return {"skipped": True}
    imported = 0
    try:
        seen = json.loads(config.SEEN_COMMENTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        seen = {}
    now = utcnow()
    with db.tx() as c:
        for video_id, cids in (seen or {}).items():
            for cid in cids or []:
                cur = c.execute("INSERT OR IGNORE INTO comments(cid, video_id, first_seen_at, status, source)"
                                " VALUES(?,?,?,?,?)", (str(cid), str(video_id), now, "legacy", "legacy"))
                imported += cur.rowcount
    db.meta_set("legacy_imported", now)
    db.log_event("legacy_import", f"{imported} previously seen comments marked as handled")
    return {"imported": imported}
