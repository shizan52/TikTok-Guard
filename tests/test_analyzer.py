"""The desktop app's on-demand analysis, without a browser: what it tells the person about a partial read."""
import unittest

from _helpers import comment, make_ai, tmp_db  # noqa: F401  (also sets sys.path)

from app import links
from app.analyzer import AnalysisJob, PostAnalyzer

URL = "https://www.tiktok.com/@shop/video/7100000000000000001"
VIDEO = "7100000000000000001"


class TestWhatAPartialReadSays(unittest.TestCase):
    def store(self, data: dict, *, limit: int = 240) -> AnalysisJob:
        db = tmp_db()
        analyzer = PostAnalyzer(db, make_ai(db, []), run=lambda fn, timeout: fn(), read_limit=lambda: limit)
        job = AnalysisJob(links.resolve(URL), URL, None)
        analyzer._store(job, {"ok": True, "description": "a post", "author": "shop", **data})
        return job

    def comments(self, n: int) -> list:
        return [comment(f"c{i}", f"text {i}", video_id=VIDEO) for i in range(n)]

    def test_a_complete_read_has_no_warning_even_when_tiktoks_total_counts_replies(self):
        # seen live: 14 top-level comments read, TikTok's `total` said 26 because it counts the replies under them
        job = self.store({"comments": self.comments(14), "total": 26, "truncated": False})
        self.assertIsNone(job.note)

    def test_reaching_the_limit_says_so_and_where_to_change_it(self):
        job = self.store({"comments": self.comments(40), "total": 500, "truncated": True}, limit=40)
        self.assertIn("first 40", job.note)
        self.assertIn("limit is 40", job.note)
        self.assertIn("Controls", job.note)

    def test_tiktok_giving_up_early_is_not_blamed_on_the_limit(self):
        job = self.store({"comments": self.comments(14), "total": 500, "truncated": True}, limit=240)
        self.assertIn("stopped loading more comments after 14", job.note)
        self.assertNotIn("limit", job.note)

    def test_the_post_is_kept_with_the_number_of_comments_that_were_read(self):
        db = tmp_db()
        analyzer = PostAnalyzer(db, make_ai(db, []), run=lambda fn, timeout: fn())
        job = AnalysisJob(links.resolve(URL), URL, None)
        analyzer._store(job, {"ok": True, "comments": self.comments(3), "total": None, "truncated": False})
        self.assertEqual(job.read, 3)
        self.assertEqual(len(analyzer.db.post_comments(VIDEO)), 3)


if __name__ == "__main__":
    unittest.main()
