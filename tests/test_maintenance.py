"""Retention, size cap, vacuum and file pruning - the 'don't fill the server's disk' guarantees."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers import comment, tmp_db  # noqa: F401

import config
from app import maintenance
from app.db import utc_ago


def backdate(db, table, where_sql, days, column="first_seen_at", params=()):
    db.conn().execute(f"UPDATE {table} SET {column}=? WHERE {where_sql}", (utc_ago(days=days), *params))


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k) for k in ("DB_MAX_MB", "RETENTION_COMMENT_DAYS", "RETENTION_NEGATIVE_DAYS",
                                                        "RETENTION_POST_DAYS", "RETENTION_APPROVAL_DAYS",
                                                        "APPROVAL_EXPIRE_DAYS", "CACHE_MAX_ROWS")}
        self.db = tmp_db()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)


class TestDbRetention(RetentionCase):
    def test_comment_retention_by_kind(self):
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "comment_count": 1})
        self.db.set_watch_set(["v1"])
        self.db.insert_new_comments("v1", [comment("ok-old"), comment("ok-new"), comment("neg-mid"),
                                           comment("neg-ancient"), comment("neg-open")])
        for cid, status in (("ok-old", "ok"), ("ok-new", "ok"), ("neg-mid", "negative"),
                            ("neg-ancient", "negative"), ("neg-open", "negative")):
            self.db.update_comment(cid, status=status)
        backdate(self.db, "comments", "cid=?", 45, params=("ok-old",))
        backdate(self.db, "comments", "cid=?", 2, params=("ok-new",))
        backdate(self.db, "comments", "cid=?", 100, params=("neg-mid",))
        backdate(self.db, "comments", "cid=?", 400, params=("neg-ancient",))
        backdate(self.db, "comments", "cid=?", 400, params=("neg-open",))
        self.db.create_approval({"id": "a1", "cid": "neg-open", "video_id": "v1", "status": "pending"})
        maintenance.maintain_db(self.db)
        left = {r["cid"] for r in self.db.conn().execute("SELECT cid FROM comments")}
        self.assertEqual(left, {"ok-new", "neg-mid", "neg-open"})

    def test_posts_leave_after_the_watch_set_and_a_grace_period(self):
        for vid in ("watched", "gone-recent", "gone-old"):
            self.db.upsert_post({"video_id": vid, "url": "u", "description": "d", "comment_count": 1})
        self.db.set_watch_set(["watched"])
        self.db.insert_new_comments("gone-old", [comment("x", video_id="gone-old")])
        self.db.update_comment("x", status="ok")
        backdate(self.db, "posts", "video_id=?", 30, column="last_seen_at", params=("gone-old",))
        backdate(self.db, "comments", "cid=?", 1, params=("x",))
        maintenance.maintain_db(self.db)
        ids = {r["video_id"] for r in self.db.conn().execute("SELECT video_id FROM posts")}
        self.assertEqual(ids, {"watched", "gone-recent"})
        self.assertEqual(self.db.conn().execute("SELECT COUNT(*) n FROM comments").fetchone()["n"], 0)

    def test_stale_pending_approvals_expire_and_old_finished_ones_go(self):
        base = {"video_id": "v1", "status": "pending"}
        self.db.create_approval({**base, "id": "stale", "cid": "c1"})
        self.db.create_approval({**base, "id": "fresh", "cid": "c2"})
        self.db.create_approval({**base, "id": "done-old", "cid": "c3", "status": "posted"})
        self.db.create_approval({**base, "id": "done-new", "cid": "c4", "status": "posted"})
        backdate(self.db, "approvals", "id=?", 30, column="created_at", params=("stale",))
        backdate(self.db, "approvals", "id=?", 400, column="updated_at", params=("done-old",))
        maintenance.maintain_db(self.db)
        self.assertEqual(self.db.get_approval("stale")["status"], "expired")
        self.assertEqual(self.db.get_approval("fresh")["status"], "pending")
        self.assertIsNone(self.db.get_approval("done-old"))
        self.assertIsNotNone(self.db.get_approval("done-new"))

    def test_cache_is_capped_and_old_entries_dropped(self):
        config.CACHE_MAX_ROWS = 100
        for i in range(150):
            self.db.cache_put(f"h{i:03d}", sentiment="OK")
        self.db.cache_put("ancient", sentiment="OK")
        self.db.conn().execute("UPDATE analysis_cache SET last_hit_at=? WHERE text_hash='ancient'", (utc_ago(days=100),))
        maintenance.maintain_db(self.db)
        n = self.db.conn().execute("SELECT COUNT(*) n FROM analysis_cache").fetchone()["n"]
        self.assertLessEqual(n, 100)
        self.assertIsNone(self.db.cache_get("ancient"))

    def test_events_and_usage_are_trimmed(self):
        self.db.log_event("old", "x")
        self.db.conn().execute("UPDATE events SET ts=?", (utc_ago(days=30),))
        self.db.add_usage("m", "classify", ok=True, day=utc_ago(days=200)[:10])
        self.db.add_usage("m", "classify", ok=True)
        maintenance.maintain_db(self.db)
        self.assertEqual([e["kind"] for e in self.db.recent_events(50) if e["kind"] == "old"], [])
        days = [r["day"] for r in self.db.conn().execute("SELECT day FROM ai_usage")]
        self.assertEqual(len(days), 1)

    def test_size_cap_tightens_retention_and_shrinks_the_file(self):
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "comment_count": 1})
        self.db.set_watch_set(["v1"])
        big = "x" * 1500
        rows = [comment(f"c{i}", big + str(i)) for i in range(2500)]
        self.db.insert_new_comments("v1", rows)
        self.db.conn().execute("UPDATE comments SET status='ok'")
        # spread ages over 0..29 days: nothing is old enough for the normal 30-day rule
        self.db.conn().execute("UPDATE comments SET first_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ',"
                               " 'now', '-' || (CAST(substr(cid, 2) AS INTEGER) % 30) || ' days')")
        before = self.db.size_bytes()
        config.DB_MAX_MB = 1
        report = maintenance.maintain_db(self.db)
        self.assertGreater(before, 2_000_000)
        self.assertGreaterEqual(report["tightened_steps"], 1)
        self.assertLess(self.db.size_bytes(), before / 2, "the file really shrank (pages returned to the OS)")
        self.assertLessEqual(self.db.size_bytes(), 1.2 * 1048576)

    def test_run_all_reports_and_is_idempotent(self):
        r1 = maintenance.run_all(self.db)
        r2 = maintenance.run_all(self.db)
        for key in ("db", "files", "browser_caches", "freed_mb_total"):
            self.assertIn(key, r1)
        self.assertGreaterEqual(r2["freed_mb_total"], 0)


class TestFilePruning(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="maint-"))
        self._saved = {k: getattr(config, k) for k in ("ROOT", "SCREENSHOTS_DIR", "BFP_RUNS_DIR", "DEMO_OUTPUT_DIR",
                                                        "SCREENSHOT_RETENTION_DAYS", "SCREENSHOT_MAX_MB",
                                                        "RUNS_RETENTION_DAYS")}
        config.ROOT = self.root
        config.DEMO_OUTPUT_DIR = self.root / "demo_output"
        config.SCREENSHOTS_DIR = config.DEMO_OUTPUT_DIR / "screenshots"
        config.BFP_RUNS_DIR = self.root / "network" / "bfp_runs"
        for d in (config.SCREENSHOTS_DIR, config.BFP_RUNS_DIR, self.root / "bfp_runs"):
            d.mkdir(parents=True)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    @staticmethod
    def make(path: Path, size: int, age_days: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0" * size)
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))

    def test_screenshots_by_age_then_size(self):
        config.SCREENSHOT_RETENTION_DAYS, config.SCREENSHOT_MAX_MB = 7, 1
        for i in range(5):
            self.make(config.SCREENSHOTS_DIR / f"new{i}.png", 400_000, age_days=i)
        self.make(config.SCREENSHOTS_DIR / "old.png", 10, age_days=20)
        out = maintenance.prune_files()["screenshots"]
        left = sorted(p.name for p in config.SCREENSHOTS_DIR.iterdir())
        self.assertNotIn("old.png", left)
        self.assertLessEqual(sum(p.stat().st_size for p in config.SCREENSHOTS_DIR.iterdir()), 1_048_576)
        self.assertIn("new0.png", left, "the newest files survive the size cap")
        self.assertGreater(out["removed"], 1)

    def test_run_folders_and_leftover_browser_profiles(self):
        config.RUNS_RETENTION_DAYS = 3
        self.make(config.BFP_RUNS_DIR / "old-session" / "timeline.jsonl", 100, age_days=10)
        ten = time.time() - 10 * 86400
        os.utime(config.BFP_RUNS_DIR / "old-session", (ten, ten))     # a folder's age = its own mtime
        self.make(config.BFP_RUNS_DIR / "new-session" / "timeline.jsonl", 100, age_days=0.1)
        old_t = time.time() - 5 * 86400
        self.make(self.root / "bfp_runs" / "cdp-abc123" / "Default" / "Cache" / "f", 5000, age_days=5)
        os.utime(self.root / "bfp_runs" / "cdp-abc123", (old_t, old_t))
        self.make(self.root / "bfp_runs" / "keep-me" / "f", 5000, age_days=5)
        os.utime(self.root / "bfp_runs" / "keep-me", (old_t, old_t))
        maintenance.prune_files()
        self.assertFalse((config.BFP_RUNS_DIR / "old-session").exists())
        self.assertTrue((config.BFP_RUNS_DIR / "new-session").exists())
        self.assertFalse((self.root / "bfp_runs" / "cdp-abc123").exists())
        self.assertTrue((self.root / "bfp_runs" / "keep-me").exists(), "only cdp-* leftovers are touched")


class TestBrowserCachePruning(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="prof-")) / "User Data"
        d = self.folder / "Default"
        TestFilePruning.make(d / "Cache" / "data_0", 300_000, 1)
        TestFilePruning.make(d / "Code Cache" / "js" / "x", 200_000, 1)
        TestFilePruning.make(d / "Service Worker" / "CacheStorage" / "y", 100_000, 1)
        TestFilePruning.make(d / "Cookies", 4096, 0)
        TestFilePruning.make(d / "Local Storage" / "leveldb" / "000003.log", 4096, 0)
        TestFilePruning.make(d / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb" / "000003.log", 4096, 0)
        TestFilePruning.make(self.folder / "GrShaderCache" / "z", 50_000, 1)
        self._saved = (config.BFP_MASTER_USER_DATA_DIR, config.load_accounts, config.BROWSER_CACHE_MAX_MB)
        config.BFP_MASTER_USER_DATA_DIR = str(self.folder)
        config.load_accounts = lambda: []

    def tearDown(self):
        config.BFP_MASTER_USER_DATA_DIR, config.load_accounts, config.BROWSER_CACHE_MAX_MB = self._saved

    def test_small_caches_are_left_alone(self):
        config.BROWSER_CACHE_MAX_MB = 50
        out = maintenance.prune_browser_caches()
        self.assertEqual(list(out.values())[0]["freed_mb"], 0.0)
        self.assertTrue((self.folder / "Default" / "Cache").exists())

    def test_big_caches_go_but_the_login_never_does(self):
        config.BROWSER_CACHE_MAX_MB = 0.2
        out = maintenance.prune_browser_caches()
        self.assertGreater(list(out.values())[0]["freed_mb"], 0.5)
        d = self.folder / "Default"
        self.assertFalse((d / "Cache").exists())
        self.assertFalse((d / "Code Cache").exists())
        self.assertFalse((d / "Service Worker" / "CacheStorage").exists())
        self.assertFalse((self.folder / "GrShaderCache").exists())
        self.assertTrue((d / "Cookies").exists())
        self.assertTrue((d / "Local Storage" / "leveldb" / "000003.log").exists())
        self.assertTrue((d / "IndexedDB" / "https_www.tiktok.com_0.indexeddb.leveldb" / "000003.log").exists())

    def test_an_everyday_browser_profile_is_never_cleaned_even_if_misconfigured(self):
        import tempfile
        fake_local = Path(tempfile.mkdtemp(prefix="fakelocal-"))
        everyday = fake_local / "BraveSoftware" / "Brave-Browser" / "User Data"
        TestFilePruning.make(everyday / "Default" / "Cache" / "data_0", 400_000, 1)
        TestFilePruning.make(everyday / "Default" / "Cookies", 4096, 0)
        saved = (os.environ.get("LOCALAPPDATA"), config.BFP_MASTER_USER_DATA_DIR)
        os.environ["LOCALAPPDATA"] = str(fake_local)
        config.BFP_MASTER_USER_DATA_DIR = str(everyday)
        config.BROWSER_CACHE_MAX_MB = 0.01
        try:
            out = maintenance.prune_browser_caches(force=True)
        finally:
            if saved[0] is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = saved[0]
            config.BFP_MASTER_USER_DATA_DIR = saved[1]
        self.assertIn("never touched", list(out.values())[0]["skipped"])
        self.assertTrue((everyday / "Default" / "Cache" / "data_0").exists())
        self.assertTrue((everyday / "Default" / "Cookies").exists())

    def test_a_profile_in_use_is_skipped(self):
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        import bfp
        config.BROWSER_CACHE_MAX_MB = 0.2
        lock = bfp._ProfileLock(self.folder)
        lock.acquire(timeout=1)
        try:
            out = maintenance.prune_browser_caches()
        finally:
            lock.release()
        self.assertEqual(list(out.values())[0].get("skipped"), "profile in use")
        self.assertTrue((self.folder / "Default" / "Cache").exists())


if __name__ == "__main__":
    unittest.main()
