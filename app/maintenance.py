"""Keeps the server's disk use bounded: database retention + size cap, and the
folders the browser automation fills up (screenshots, session run folders,
leftover throw-away browser profiles, browser cache directories).

Everything here is rule-based and conservative:
  * the DB keeps finished business records far longer than throw-away ones, and
    if the file is still over DB_MAX_MB it tightens the throw-away retention in
    steps instead of deleting blindly;
  * file pruning only touches generated, re-creatable data - never cookies,
    localStorage, IndexedDB or any saved login (cache dirs only, and only while
    the profile is not in use: it takes the profile's own lock first).
"""
import os
import shutil
import sqlite3
import time
from pathlib import Path

import config
from app.db import DB, utc_ago, utc_day

MB = 1024 * 1024

# Chromium/Brave cache directories: safe to delete (re-created on demand), never hold the login.
_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
               "Service Worker/CacheStorage", "Service Worker/ScriptCache", "Media Cache", "optimization_guide_model_store")
_ROOT_CACHE_DIRS = ("GrShaderCache", "ShaderCache", "GraphiteDawnCache", "component_crx_cache", "BrowserMetrics",
                    "Crashpad/reports")


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _rmtree(path: Path) -> int:
    size = _dir_size(path) if path.is_dir() else (path.stat().st_size if path.exists() else 0)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return 0
    return size if not path.exists() else 0


# ----------------------------------------------------------------------------- database
def _purge_db(db: DB, *, comment_days: float, negative_days: float) -> dict:
    c = db.conn()
    out = {}
    cut_c = utc_ago(days=comment_days)
    cut_n = utc_ago(days=negative_days)
    # harmless/finished comments go first; negatives/questions are the audit trail and live longer
    out["comments_harmless"] = c.execute(
        "DELETE FROM comments WHERE status IN ('ok','skipped','legacy') AND first_seen_at < ?", (cut_c,)).rowcount
    out["comments_flagged"] = c.execute(
        "DELETE FROM comments WHERE status IN ('negative','question') AND first_seen_at < ? AND NOT EXISTS"
        " (SELECT 1 FROM approvals a WHERE a.cid=comments.cid AND a.status IN ('pending','approved','posting'))",
        (cut_n,)).rowcount
    # posts that left the watch set long ago, with whatever comments they still have
    cut_p = utc_ago(days=config.RETENTION_POST_DAYS)
    old = [r["video_id"] for r in c.execute(
        "SELECT video_id FROM posts WHERE watch_rank IS NULL AND last_seen_at < ?", (cut_p,))]
    for vid in old:
        c.execute("DELETE FROM comments WHERE video_id=? AND status NOT IN ('negative','question')", (vid,))
    out["posts"] = c.execute(
        "DELETE FROM posts WHERE watch_rank IS NULL AND last_seen_at < ? AND NOT EXISTS"
        " (SELECT 1 FROM comments m WHERE m.video_id=posts.video_id)", (cut_p,)).rowcount
    return out


def _stale_approvals(db: DB) -> int:
    """Pending approvals nobody acted on for APPROVAL_EXPIRE_DAYS become 'expired' (kept, just closed)."""
    cut = utc_ago(days=config.APPROVAL_EXPIRE_DAYS)
    return db.conn().execute(
        "UPDATE approvals SET status='expired', updated_at=? WHERE status='pending' AND created_at < ?",
        (utc_ago(days=0), cut)).rowcount


def maintain_db(db: DB, *, force_vacuum: bool = False) -> dict:
    c = db.conn()
    before = db.size_bytes()
    report: dict = {"db_before_mb": round(before / MB, 2)}
    report["approvals_expired"] = _stale_approvals(db)

    comment_days, negative_days = float(config.RETENTION_COMMENT_DAYS), float(config.RETENTION_NEGATIVE_DAYS)
    report["purged"] = _purge_db(db, comment_days=comment_days, negative_days=negative_days)

    cut = utc_ago(days=config.RETENTION_APPROVAL_DAYS)
    report["approvals_deleted"] = c.execute(
        "DELETE FROM approvals WHERE status IN ('posted','rejected','post_failed','superseded','expired')"
        " AND updated_at < ?", (cut,)).rowcount
    report["cache_deleted"] = c.execute(
        "DELETE FROM analysis_cache WHERE last_hit_at < ?", (utc_ago(days=45),)).rowcount
    over = c.execute("SELECT COUNT(*) n FROM analysis_cache").fetchone()["n"] - config.CACHE_MAX_ROWS
    if over > 0:
        report["cache_deleted"] += c.execute(
            "DELETE FROM analysis_cache WHERE text_hash IN (SELECT text_hash FROM analysis_cache"
            " ORDER BY last_hit_at LIMIT ?)", (over,)).rowcount
    report["usage_deleted"] = c.execute(
        "DELETE FROM ai_usage WHERE day < ?", (utc_ago(days=90)[:10],)).rowcount
    report["events_deleted"] = c.execute("DELETE FROM events WHERE ts < ?", (utc_ago(days=14),)).rowcount
    c.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 5000)")

    # still over the size target? tighten the throw-away retention step by step
    steps = 0
    limit = config.DB_MAX_MB * MB
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    while db.size_bytes() > limit and steps < 5:
        steps += 1
        comment_days = max(1.0, comment_days / 2)
        negative_days = max(7.0, negative_days / 2)
        _purge_db(db, comment_days=comment_days, negative_days=negative_days)
        c.execute("DELETE FROM analysis_cache WHERE text_hash IN (SELECT text_hash FROM analysis_cache"
                  " ORDER BY last_hit_at LIMIT (SELECT COUNT(*)/2 FROM analysis_cache))")
        c.execute("PRAGMA incremental_vacuum")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report["tightened_steps"] = steps

    # give freed pages back to the OS: cheap incremental pass every time, a full VACUUM at most weekly
    c.execute("PRAGMA incremental_vacuum")
    pages = c.execute("PRAGMA page_count").fetchone()[0]
    free = c.execute("PRAGMA freelist_count").fetchone()[0]
    last_vac = float(db.meta_get("last_full_vacuum_ts", 0) or 0)
    if force_vacuum or (pages and free / pages > 0.25) or time.time() - last_vac > 7 * 86400:
        try:
            c.execute("VACUUM")
            db.meta_set("last_full_vacuum_ts", time.time())
            report["vacuum"] = True
        except sqlite3.OperationalError as exc:  # another connection mid-transaction: try next round
            report["vacuum"] = f"skipped ({exc})"
    c.execute("PRAGMA optimize")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report["db_after_mb"] = round(db.size_bytes() / MB, 2)
    return report


# ----------------------------------------------------------------------------- files
def _prune_by_age_and_size(root: Path, *, max_age_days: float, max_mb: float | None,
                           only_files: bool = True, pattern: str = "*") -> dict:
    freed, removed = 0, 0
    if not root.exists():
        return {"removed": 0, "freed_mb": 0.0}
    now = time.time()
    entries = []
    for p in root.glob(pattern):
        try:
            if only_files and not p.is_file():
                continue
            entries.append((p.stat().st_mtime, p))
        except OSError:
            continue
    for mtime, p in list(entries):
        if now - mtime > max_age_days * 86400:
            size = _rmtree(p)
            if size or not p.exists():
                freed += size
                removed += 1
                entries.remove((mtime, p))
    if max_mb is not None:
        entries.sort()
        total = sum(_dir_size(p) if p.is_dir() else p.stat().st_size for _, p in entries if p.exists())
        for mtime, p in entries:
            if total <= max_mb * MB:
                break
            size = _rmtree(p)
            total -= size
            freed += size
            removed += 1
    return {"removed": removed, "freed_mb": round(freed / MB, 1)}


def prune_files() -> dict:
    out = {}
    out["screenshots"] = _prune_by_age_and_size(config.SCREENSHOTS_DIR, max_age_days=config.SCREENSHOT_RETENTION_DAYS,
                                                max_mb=config.SCREENSHOT_MAX_MB)
    # per-session run folders written by bfp (timeline/report/screenshots)
    out["bfp_runs"] = _prune_by_age_and_size(config.BFP_RUNS_DIR, max_age_days=config.RUNS_RETENTION_DAYS,
                                             max_mb=None, only_files=False)
    # throw-away random browser profiles left in <cwd>/bfp_runs by older raw-CDP experiments (cdp-xxxxxx)
    out["leftover_browser_profiles"] = _prune_by_age_and_size(
        config.ROOT / "bfp_runs", max_age_days=1, max_mb=None, only_files=False, pattern="cdp-*")
    out["demo_reports"] = _prune_by_age_and_size(config.DEMO_OUTPUT_DIR, max_age_days=30, max_mb=None,
                                                 pattern="report_*.json")
    return out


def _profile_folders() -> list:
    folders = []
    if config.BFP_MASTER_USER_DATA_DIR:
        folders.append(Path(config.BFP_MASTER_USER_DATA_DIR))
    folders += [Path(a["user_data_dir"]) for a in config.load_accounts()]
    return [f for f in folders if f.exists()]


def prune_browser_caches(*, force: bool = False) -> dict:
    """Delete cache-only directories of the saved browser profiles when they grew past
    BROWSER_CACHE_MAX_MB. Cookies / localStorage / IndexedDB / login are never touched.
    A profile that is in use right now (its lock is held) is skipped."""
    import sys
    from app.bfp_loader import ensure_bfp_on_path
    ensure_bfp_on_path()
    import bfp
    out = {}
    for folder in _profile_folders():
        try:
            bfp._refuse_personal_profile(folder)       # same rule as the launcher: never an everyday browser profile
        except bfp.LaunchError:
            out[folder.parent.name if folder.name == "User Data" else folder.name] = {
                "cache_mb": 0.0, "freed_mb": 0.0, "skipped": "everyday browser profile - never touched"}
            continue
        sizes = {}
        for rel in _CACHE_DIRS:
            p = folder / "Default" / rel
            if p.exists():
                sizes[p] = _dir_size(p)
        for rel in _ROOT_CACHE_DIRS:
            p = folder / rel
            if p.exists():
                sizes[p] = _dir_size(p)
        total = sum(sizes.values())
        entry = {"cache_mb": round(total / MB, 1), "freed_mb": 0.0}
        if total > config.BROWSER_CACHE_MAX_MB * MB or (force and total):
            lock = bfp._ProfileLock(folder)
            try:
                lock.acquire(timeout=1.0, poll=0.2)
            except Exception:  # noqa: BLE001
                entry["skipped"] = "profile in use"
            else:
                try:
                    freed = sum(_rmtree(p) for p in sizes)
                    entry["freed_mb"] = round(freed / MB, 1)
                finally:
                    lock.release()
        out[folder.parent.name if folder.name == "User Data" else folder.name] = entry
    return out


def run_all(db: DB, *, force_vacuum: bool = False, force_caches: bool = False) -> dict:
    report = {"day": utc_day(), "db": maintain_db(db, force_vacuum=force_vacuum), "files": prune_files(),
              "browser_caches": prune_browser_caches(force=force_caches)}
    freed = report["files"]["screenshots"]["freed_mb"] + report["files"]["bfp_runs"]["freed_mb"] \
        + report["files"]["leftover_browser_profiles"]["freed_mb"] \
        + sum(v.get("freed_mb", 0.0) for v in report["browser_caches"].values())
    report["freed_mb_total"] = round(freed + max(0.0, report["db"]["db_before_mb"] - report["db"]["db_after_mb"]), 1)
    db.meta_set("last_maintenance_ts", time.time())
    db.log_event("maintenance", f"freed ~{report['freed_mb_total']} MB; db {report['db']['db_before_mb']}"
                                f"->{report['db']['db_after_mb']} MB")
    return report
