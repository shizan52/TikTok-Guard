"""Comment analysis pipeline. A comment costs AI budget at most ONCE, and usually zero:

    scraped comment
      -> already in the database?            skip (never analysed twice)
      -> too old on the first scan of a post? stored as 'legacy' (never analysed, never announced)
      -> identical text analysed before?     analysis cache hit (free)
      -> obviously harmless (emoji, laughter, short praise)?   heuristics (free)
      -> everything else: ONE batched AI request per AI_BATCH_SIZE comments
      -> AI unavailable and strongly negative wording?   keyword fallback so a human still hears about it
"""
import json
import time

import config
from app import heuristics
from app.lang import resolve_language
from app.ai import AI
from app.db import DB, text_hash, utcnow

_STATUS = {"NEG": "negative", "Q": "question", "OK": "ok"}


def apply_result(db: DB, cid: str, res: dict, *, source: str) -> None:
    sentiment = res["sentiment"]
    db.update_comment(
        cid, status=_STATUS.get(sentiment, "ok"), sentiment=sentiment, lang=res.get("lang"),
        severity=res.get("severity"), category=res.get("category"), reason=(res.get("reason") or "")[:200],
        source=source, analyzed_at=utcnow())


def own_usernames(db: DB | None = None) -> frozenset:
    """TikTok handles that are ours (the watched brand + every posting account): never alerted on."""
    names = {config.TARGET_PROFILE_URL.rstrip("/").split("@")[-1].lower()}
    names |= {str(a.get("username") or "").lower() for a in config.load_accounts() if a.get("username")}
    if db is not None:
        try:
            master = json.loads(config.ACCOUNTS_FILE.read_text(encoding="utf-8")).get("master_username")
            if master:
                names.add(str(master).lower())
        except Exception:  # noqa: BLE001
            pass
    return frozenset(n for n in names if n)


def ingest_comments(db: DB, video_id: str, comments: list, *, first_scan: bool) -> dict:
    """Store the comments not seen before and settle what can be settled for free."""
    legacy_before = None
    if first_scan and config.INITIAL_LOOKBACK_DAYS > 0:
        legacy_before = int(time.time()) - config.INITIAL_LOOKBACK_DAYS * 86400
    fresh = db.insert_new_comments(video_id, comments, legacy_before_ts=legacy_before,
                                   skip_users=own_usernames(db))
    stats = {"seen": len(comments), "new": len(fresh), "legacy": 0, "cache": 0, "heuristic": 0, "pending": 0}
    for c in fresh:
        if c["status"] == "legacy":
            stats["legacy"] += 1
            continue
        if c["status"] != "new":
            continue
        cached = db.cache_get(text_hash(c["text"]))
        if cached:
            apply_result(db, c["cid"], cached, source="cache")
            stats["cache"] += 1
            continue
        screened = heuristics.screen(c["text"])
        if screened:
            apply_result(db, c["cid"], screened, source="heuristic")
            stats["heuristic"] += 1
            continue
        stats["pending"] += 1
    return stats


def classify_pending(db: DB, ai: AI, *, limit: int = 120, video_id: str | None = None, batch_size: int | None = None,
                     progress=None) -> dict:
    """Send the comments still 'new' to the AI in batches, newest first. Never raises.
    `video_id`: only that post's comments (an on-demand analysis must not spend budget on other posts).
    A text that was already judged - in this run, in an earlier round, on another post - is NOT asked again: it gets the
    same verdict (asking twice gave identical comments different verdicts, and cost requests). `batch_size`: comments per
    request; `progress(done, total)` after every request (total = the different texts that really need the AI)."""
    rows = db.comments_to_analyze(limit, video_id)
    stats = {"candidates": len(rows), "classified": 0, "fallback": 0, "left": 0, "error": None}
    if not rows:
        return stats
    # the same text may appear several times in one run: ask once
    unique: dict = {}
    for r in rows:
        unique.setdefault(r["text_hash"], r)
    by_hash: dict = {}
    to_ask: dict = {}
    for h, rep in unique.items():
        cached = db.cache_get(h)
        if cached and cached.get("sentiment"):
            by_hash[h] = {k: cached.get(k) for k in ("sentiment", "lang", "severity", "category", "reason")}
        else:
            to_ask[h] = rep
    if progress and to_ask:
        try:
            progress(0, len(to_ask))
        except Exception:  # noqa: BLE001
            pass
    results, error = ai.classify_batch([{"cid": r["cid"], "text": r["text"]} for r in to_ask.values()],
                                       config.BRAND_NAME, batch_size=batch_size, progress=progress) if to_ask else ({}, None)
    stats["error"] = error
    for h, rep in to_ask.items():
        res = results.get(rep["cid"])
        if res:
            res["lang"] = resolve_language(rep["text"], res.get("lang")) or res.get("lang")
            by_hash[h] = res
            db.cache_put(h, sentiment=res["sentiment"], lang=res["lang"], severity=res["severity"],
                         category=res["category"], reason=res["reason"], source="ai")
    for r in rows:
        res = by_hash.get(r["text_hash"])
        if res:
            apply_result(db, r["cid"], res, source="ai")
            stats["classified"] += 1
            continue
        if error:                                    # the AI could not be asked: don't punish the comment
            fb = heuristics.fallback_classify(r["text"])
            if fb:
                apply_result(db, r["cid"], fb, source="fallback")
                stats["fallback"] += 1
                continue
        else:                                        # asked, but this line came back unusable
            attempts = (r.get("attempts") or 0) + 1
            if attempts >= 3:
                db.update_comment(r["cid"], status="skipped", attempts=attempts, source="unclassified",
                                  analyzed_at=utcnow())
                continue
            db.update_comment(r["cid"], attempts=attempts)
        stats["left"] += 1
    return stats
