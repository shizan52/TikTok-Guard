"""'Check on TikTok': was a reply that failed or was interrupted posted after all?"""
import threading
import time
import unittest
from unittest import mock

from _helpers import comment, make_ai, tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import analysis, tiktok_watch
from app.controller import CHECKED_ABSENT, ControlError
from app.service import Service

VIDEO = "7100000000000000002"
CID = "7693982897917182727"
URL = f"https://www.tiktok.com/@shop/video/{VIDEO}"
MINE = "Obrigado pela pergunta! Vamos confirmar essa informação. Envie-nos uma mensagem direta, por favor."


def failed_card(db, aid="v1", before=1, kind="link_reply", status="post_failed", error="x", **extra):
    db.create_approval({"id": aid, "cid": CID, "video_id": VIDEO, "video_url": URL, "username": "bobildes",
                        "comment_text": "Voces poderiam me falar o valor?", "reply": MINE, "status": "pending", "kind": kind, **extra})
    db.update_approval(aid, status=status, last_error=error, replies_before=before)
    return aid


class TestCountersStayFresh(unittest.TestCase):
    def test_a_second_read_refreshes_the_reply_count_of_a_comment_that_was_seen_before(self):
        db = tmp_db()
        db.upsert_post({"video_id": VIDEO, "url": URL, "description": "d", "is_pinned": 0, "comment_count": 1})
        analysis.ingest_comments(db, VIDEO, [{**comment(CID, "Voces poderiam me falar o valor?", video_id=VIDEO), "reply_count": 1, "digg_count": 1}],
                                 first_scan=False)
        fresh = analysis.ingest_comments(db, VIDEO, [{**comment(CID, "Voces poderiam me falar o valor?", video_id=VIDEO), "reply_count": 3, "digg_count": 9}],
                                         first_scan=False)
        self.assertEqual(fresh["new"], 0, "it is not a NEW comment")
        row = db.get_comment(CID)
        self.assertEqual((row["reply_count"], row["digg_count"]), (3, 9))

    def test_the_readers_normalisation_keeps_tiktoks_preview_of_the_first_replies(self):
        page = {"comments": [{"cid": "1", "text": "q", "user": {"unique_id": "bob"}, "reply_comment_total": 2,
                              "reply_comment": [{"text": "Hello!", "user": {"unique_id": "shop"}}, "junk"]}]}
        got = tiktok_watch.normalize_comments([page])[0]
        self.assertEqual(got["reply_count"], 2)
        self.assertEqual(got["reply_preview"], [{"user": "shop", "text": "Hello!"}])


class TestTheCountIsRememberedWhenSending(unittest.TestCase):
    def test_pressing_send_stores_how_many_replies_the_comment_had(self):
        db = tmp_db()
        svc = Service(db, telegram=False, monitor=False)
        db.upsert_post({"video_id": VIDEO, "url": URL, "description": "d", "is_pinned": 0, "comment_count": 1})
        analysis.ingest_comments(db, VIDEO, [{**comment(CID, "Voces poderiam?", video_id=VIDEO), "reply_count": 4}], first_scan=False)
        db.create_approval({"id": "s1", "cid": CID, "video_id": VIDEO, "video_url": URL, "comment_text": "Voces poderiam?", "reply": MINE,
                            "status": "pending", "kind": "link_reply"})
        with mock.patch.object(config, "POSTING_ENABLED", True), mock.patch.object(svc, "request_post"):
            svc.controller.request_post("s1")
        self.assertEqual(db.get_approval("s1")["replies_before"], 4)


class TestTheVerdict(unittest.TestCase):
    def setUp(self):
        self.db = tmp_db()
        self.ctl = Service(self.db, telegram=False, monitor=False).controller

    def test_our_own_text_in_tiktoks_preview_means_it_was_posted(self):
        failed_card(self.db)
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 2, "preview": [
            {"user": "shop", "text": "Olá! Pode confirmar?"}, {"user": "helper_account_7", "text": MINE}]})
        self.assertEqual(view["status"], "posted")
        self.assertEqual(view["posted_by"], "helper_account_7")
        self.assertIsNone(view["error"])
        self.assertEqual(view["check"]["outcome"], "found")

    def test_the_same_text_with_different_spacing_and_case_is_still_ours(self):
        failed_card(self.db)
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 2, "preview": [{"user": "x", "text": "  obrigado  pela PERGUNTA!  Vamos confirmar essa informação.  "}]})
        self.assertEqual(view["status"], "posted")

    def test_more_replies_than_when_it_was_sent_is_a_maybe_and_stays_open(self):
        failed_card(self.db, before=1)
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 3, "preview": [{"user": "shop", "text": "Olá!"}]})
        self.assertEqual((view["status"], view["checked"]), ("post_failed", "maybe"))
        self.assertIn("3 replies now and had 1", view["error"])
        self.assertFalse(view["interrupted"])

    def test_no_new_reply_means_it_was_not_posted_and_it_is_safe_to_send_again(self):
        failed_card(self.db, before=1, error="The app was closed while this reply was being sent")
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 1, "preview": [{"user": "shop", "text": "Olá!"}]})
        self.assertEqual((view["status"], view["checked"], view["error"]), ("post_failed", "absent", CHECKED_ABSENT))
        self.assertTrue(view["can"]["post"], "it can be sent again")
        self.assertFalse(view["interrupted"], "no longer 'not sure'")

    def test_a_post_that_could_not_be_read_changes_nothing_and_says_so(self):
        failed_card(self.db, error="interrupted")
        view = self.ctl.apply_verification("v1", {"ok": False, "error": "TikTok would not open the page"})
        self.assertEqual((view["status"], view["error"]), ("post_failed", "interrupted"))
        self.assertEqual(view["check"]["outcome"], "error")
        self.assertIn("TikTok would not open the page", view["check"]["message"])
        self.assertIn("post_verify_failed", [e["kind"] for e in self.db.recent_events(5)])

    def test_without_a_remembered_count_only_the_text_can_prove_it(self):
        failed_card(self.db, before=None)
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 5, "preview": []})
        self.assertEqual(view["checked"], "absent")

    def test_a_card_that_is_no_longer_failed_is_left_alone(self):
        failed_card(self.db, status="posted", error=None)
        view = self.ctl.apply_verification("v1", {"ok": True, "count": 1, "preview": []})
        self.assertEqual(view["status"], "posted")
        self.assertIsNone(view["checked"])

    def test_the_decision_is_written_to_the_event_log(self):
        failed_card(self.db)
        self.ctl.apply_verification("v1", {"ok": True, "count": 1, "preview": []})
        self.assertIn("post_verified", [e["kind"] for e in self.db.recent_events(5)])


class TestAskingForACheck(unittest.TestCase):
    def setUp(self):
        self.db = tmp_db()
        self.svc = Service(self.db, telegram=False, monitor=False)
        self.ctl = self.svc.controller

    def test_only_a_failed_reply_can_be_checked(self):
        failed_card(self.db, "a", status="pending", error=None)
        with self.assertRaises(ControlError) as ctx:
            self.ctl.verify("a")
        self.assertEqual(ctx.exception.code, "bad_request")
        self.db.create_approval({"id": "p", "cid": "link-post:1", "video_id": VIDEO, "video_url": URL, "reply": "hi", "status": "pending", "kind": "link_post"})
        self.db.update_approval("p", status="post_failed")
        with self.assertRaises(ControlError):
            self.ctl.verify("p")                                    # a new comment on a post has nothing to look for
        with self.assertRaises(ControlError) as ctx:
            self.ctl.verify("nope")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_the_card_says_whether_it_can_be_checked(self):
        failed_card(self.db, "a")
        self.assertTrue(self.ctl.approval("a")["can_check"])
        self.db.update_approval("a", status="pending")
        self.assertFalse(self.ctl.approval("a")["can_check"])

    def test_the_check_runs_in_the_background_and_the_card_follows_it(self):
        failed_card(self.db, "a", before=1)
        gate = threading.Event()

        def verifier(a):
            a["_stage"]("Looking at the replies...")
            gate.wait(5)
            return {"ok": True, "count": 1, "preview": []}
        svc = Service(self.db, telegram=False, monitor=False, verifier=verifier)
        svc._memory_check = lambda: (True, 9000.0)
        svc.start()
        try:
            view = svc.controller.verify("a")
            self.assertTrue(view["checking"])
            end = time.monotonic() + 5
            while not svc.send_stage("a") and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(svc.controller.activity()["checking"][0]["text"], "Looking at the replies...")
            self.assertTrue(svc.controller.activity()["busy"])
            gate.set()
            end = time.monotonic() + 5
            while svc.is_checking("a") and time.monotonic() < end:
                time.sleep(0.02)
            done = svc.controller.approval("a")
            self.assertFalse(done["checking"])
            self.assertEqual(done["checked"], "absent")
            self.assertFalse(svc.controller.activity()["busy"])
        finally:
            gate.set()
            svc.stop()

    def test_asking_twice_does_not_start_two_checks(self):
        failed_card(self.db, "a")
        calls = []
        with mock.patch.object(self.svc, "request_verify", side_effect=lambda i: (calls.append(i), self.svc._checking.add(i))):
            self.ctl.verify("a")
            self.ctl.verify("a")
        self.assertEqual(calls, ["a"])

    def test_a_verifier_that_crashes_ends_as_a_failed_check_not_a_stuck_card(self):
        failed_card(self.db, "a", error="interrupted")

        def verifier(a):
            raise RuntimeError("browser died")
        svc = Service(self.db, telegram=False, monitor=False, verifier=verifier)
        svc._memory_check = lambda: (True, 9000.0)
        svc.start()
        try:
            svc.controller.verify("a")
            end = time.monotonic() + 5
            while svc.is_checking("a") and time.monotonic() < end:
                time.sleep(0.02)
            view = svc.controller.approval("a")
            self.assertFalse(view["checking"])
            self.assertEqual(view["check"]["outcome"], "error")
            self.assertEqual(view["status"], "post_failed")
        finally:
            svc.stop()


class TestTheDefaultVerifier(unittest.TestCase):
    def verifier(self, reader_result):
        svc = Service(tmp_db(), telegram=False, monitor=False)
        with mock.patch("app.tiktok_monitor.read_comments_of_post", return_value=reader_result) as read:
            out = svc._default_verifier({"video_url": URL, "cid": CID, "account": "acc1", "_stage": None})
        return out, read

    def test_it_reads_the_post_until_the_comment_and_reports_its_counters(self):
        out, read = self.verifier({"ok": True, "comments": [{"cid": "x", "reply_count": 0},
                                                            {"cid": CID, "reply_count": 2, "reply_preview": [{"user": "shop", "text": "Olá!"}]}]})
        self.assertEqual(out, {"ok": True, "count": 2, "preview": [{"user": "shop", "text": "Olá!"}]})
        self.assertEqual(read.call_args.kwargs["until_cid"], CID)
        self.assertEqual(read.call_args.kwargs["account"], "acc1", "the account that was chosen for the reply, when there is one")

    def test_a_comment_that_is_gone_is_explained(self):
        out, _ = self.verifier({"ok": True, "comments": [{"cid": "other", "reply_count": 0}]})
        self.assertFalse(out["ok"])
        self.assertIn("deleted", out["error"])

    def test_a_post_that_could_not_be_read_hands_over_the_reason(self):
        out, _ = self.verifier({"ok": False, "error": "TikTok would not open the page"})
        self.assertEqual(out, {"ok": False, "error": "TikTok would not open the page"})


if __name__ == "__main__":
    unittest.main()
