"""Judging the comments of a post: asked once per different text, in few requests, with progress - and a plain demand is not 'hostile'."""
import unittest

from _helpers import comment, make_ai, tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import analysis, links
from app.ai import CLASSIFY_SYSTEM, parse_classification
from app.analyzer import AnalysisJob, PostAnalyzer

VIDEO = "7100000000000000001"
URL = f"https://www.tiktok.com/@shop/video/{VIDEO}"


def answers(verdict="NEG|en|1|other|demands the feature back"):
    """A fake AI that answers every numbered line with `verdict`, and remembers what it was asked."""
    seen = []

    def respond(model, system, user):
        seen.append(user)
        lines = [f"{row.split('.', 1)[0]}|{verdict}" for row in user.splitlines()]
        return {"content": "\n".join(lines)}
    respond.seen = seen
    return respond


def post_with(db, texts):
    db.upsert_post({"video_id": VIDEO, "url": URL, "description": "d", "is_pinned": 0, "comment_count": len(texts)})
    analysis.ingest_comments(db, VIDEO, [comment(f"c{i}", t, video_id=VIDEO) for i, t in enumerate(texts)], first_scan=False)


class TestAskedOncePerDifferentText(unittest.TestCase):
    def test_identical_comments_get_one_verdict_and_one_question(self):
        db = tmp_db()
        respond = answers()
        ai = make_ai(db, respond)
        post_with(db, ["BRING BACK THE BUNNY CHAT BUBBLE"] * 5 + ["bring back the bunny chat bubble"])
        analysis.classify_pending(db, ai, limit=1000, video_id=VIDEO)
        asked = sum(len(u.splitlines()) for u in respond.seen)
        self.assertEqual(asked, 1, "six comments with the same words are ONE question to the AI")
        self.assertEqual({r["status"] for r in db.post_comments(VIDEO)}, {"negative"})

    def test_a_text_judged_in_an_earlier_round_is_not_asked_again_and_gets_the_same_verdict(self):
        """Seen live: round 1 judged the first 120 comments, round 2 asked about the same texts again and answered differently."""
        db = tmp_db()
        respond = answers("Q|en|0|other|asks something")
        ai = make_ai(db, respond)
        post_with(db, ["same words"] + [f"unique text {i}" for i in range(4)] + ["same words"] * 2)
        analysis.classify_pending(db, ai, limit=5, video_id=VIDEO)            # round 1: only some of the rows
        asked_before = sum(len(u.splitlines()) for u in respond.seen)
        respond.verdict = "NEG|en|3|other|insult"                              # the AI is in a different mood in round 2
        analysis.classify_pending(db, ai, limit=5, video_id=VIDEO)            # round 2: the rest
        same = [r for r in db.post_comments(VIDEO) if r["text"] == "same words"]
        self.assertEqual(len(same), 3)
        self.assertEqual(len({(r["status"], r["severity"]) for r in same}), 1, "identical words, identical verdict")
        self.assertEqual(sum(len(u.splitlines()) for u in respond.seen), asked_before, "round 2 asked about nothing it knew already")

    def test_a_text_judged_on_another_post_is_not_asked_again(self):
        db = tmp_db()
        respond = answers("OK|en|0|other|")
        ai = make_ai(db, respond)
        post_with(db, ["lindo demais"])
        analysis.classify_pending(db, ai, limit=50, video_id=VIDEO)
        calls = len(respond.seen)
        db.upsert_post({"video_id": "222", "url": "https://www.tiktok.com/@shop/video/222", "description": "d", "is_pinned": 0, "comment_count": 1})
        analysis.ingest_comments(db, "222", [comment("z1", "lindo demais", video_id="222")], first_scan=False)
        analysis.classify_pending(db, ai, limit=50, video_id="222")
        self.assertEqual(len(respond.seen), calls, "the same words were judged before: no new request")


class TestFewerBiggerRequests(unittest.TestCase):
    def test_the_batch_size_is_chosen_by_the_caller(self):
        db = tmp_db()
        respond = answers()
        ai = make_ai(db, respond)
        items = [{"cid": f"c{i}", "text": f"text {i}"} for i in range(50)]
        ai.classify_batch(items, "B", batch_size=24)
        self.assertEqual([len(u.splitlines()) for u in respond.seen], [24, 24, 2])
        respond.seen.clear()
        ai.classify_batch(items, "B")
        self.assertEqual(len(respond.seen), -(-50 // config.AI_BATCH_SIZE), "the default is still AI_BATCH_SIZE")

    def test_a_bigger_batch_is_allowed_a_bigger_answer(self):
        db = tmp_db()
        ai = make_ai(db, answers("OK|en|0|other|"))
        ai.classify_batch([{"cid": f"c{i}", "text": "x"} for i in range(24)], "B", batch_size=24)
        ai.classify_batch([{"cid": f"c{i}", "text": "x"} for i in range(5)], "B")
        self.assertEqual([c["max_tokens"] for c in ai.calls], [1800, 900], "24 lines need more room than the 900 that was enough for 12")

    def test_progress_is_reported_after_every_request_and_a_broken_screen_does_not_stop_the_judging(self):
        db = tmp_db()
        ai = make_ai(db, answers())
        seen = []
        ai.classify_batch([{"cid": f"c{i}", "text": f"t{i}"} for i in range(30)], "B", batch_size=10, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(10, 30), (20, 30), (30, 30)])
        res, err = ai.classify_batch([{"cid": "a", "text": "t"}], "B", progress=lambda d, t: 1 / 0)
        self.assertEqual((len(res), err), (1, None))

    def test_pending_comments_report_how_many_different_texts_really_need_the_ai(self):
        db = tmp_db()
        ai = make_ai(db, answers())
        post_with(db, ["quero saber o preco"] * 4 + ["onde fica a loja", "ate que horas abre"])
        seen = []
        analysis.classify_pending(db, ai, limit=100, video_id=VIDEO, batch_size=2, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[0], (0, 3), "six comments, three different texts")
        self.assertEqual(seen[-1], (3, 3))


class TestWhatTheAiIsToldAboutSeverity(unittest.TestCase):
    def test_the_rules_that_the_real_data_asked_for_are_in_the_prompt(self):
        text = CLASSIFY_SYSTEM.format(brand="Shop")
        self.assertIn("SEV 3 is ONLY for insults, threats or accusations", text)
        self.assertIn("plain demand, wish or complaint", text)
        self.assertIn("'bring X back'", text)
        self.assertIn("EMPTY for OK", text)

    def test_an_answer_without_a_reason_for_an_ok_comment_still_parses(self):
        got = parse_classification("1|OK|pt|0|other|\n2|NEG|en|1|other|wants it back\n3|OK|pt|0|other", {1, 2, 3})
        self.assertEqual({n: v["sentiment"] for n, v in got.items()}, {1: "OK", 2: "NEG", 3: "OK"})
        self.assertEqual(got[1]["reason"], "")


class TestTheAnalysisSaysHowFarTheJudgingIs(unittest.TestCase):
    def test_the_job_message_and_bar_follow_the_ai(self):
        db = tmp_db()
        ai = make_ai(db, answers())
        analyzer = PostAnalyzer(db, ai, run=lambda fn, timeout: fn())
        post_with(db, [f"different text {i}" for i in range(50)])
        job = AnalysisJob(links.resolve(URL), URL, None)
        analyzer._classify(job)
        self.assertEqual((job.read, job.total), (50, 50))
        self.assertEqual(job.message, "Checking which comments are negative... 50 of 50")
        self.assertEqual(len([r for r in db.post_comments(VIDEO) if r["status"] == "negative"]), 50)
        self.assertEqual(len(ai.calls), 3, "50 comments need 3 requests of 24, not 5 of 12")


if __name__ == "__main__":
    unittest.main()
