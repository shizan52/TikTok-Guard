"""TikTok watch logic (pure parts) and the Service orchestration with fake scanner / poster / Telegram."""
import threading
import time
import unittest
from pathlib import Path

from _helpers import FakeTG, comment, make_ai, tmp_db  # noqa: F401

import config
from app import ai as ai_mod, analysis, notifier, tiktok_watch as tw
from app.service import P_SCAN, Service


def item(vid, ts, comments, pinned=False, desc="d"):
    it = {"id": vid, "desc": f"{desc} {vid}", "createTime": ts, "stats": {"commentCount": str(comments), "diggCount": 1,
                                                                          "playCount": 2}}
    if pinned:
        it["isPinnedItem"] = True
    return it


class TestWatchSelection(unittest.TestCase):
    def sample(self):
        items = [item("p1", 100, 16, True), item("p2", 90, 25, True), item("p3", 80, 152, True)]
        items += [item(f"n{i}", 1000 - i, i) for i in range(10)]        # n0 is the newest
        return tw.parse_item_list([{"itemList": items}], "shop")

    def test_parse_normalises_and_dedupes(self):
        posts = tw.parse_item_list([{"itemList": [item("a", 1, 3)]}, {"itemList": [item("a", 1, 3), item("b", 2, "7")]}], "shop")
        self.assertEqual([p["video_id"] for p in posts], ["a", "b"])
        self.assertEqual(posts[1]["comment_count"], 7)
        self.assertEqual(posts[0]["url"], "https://www.tiktok.com/@shop/video/a")

    def test_watch_set_is_pinned_then_newest(self):
        watch = tw.select_watch(self.sample(), latest_n=5, pinned_n=2)
        self.assertEqual([p["video_id"] for p in watch], ["p1", "p2", "n0", "n1", "n2", "n3", "n4"])

    def test_watch_numbers_are_adjustable(self):
        watch = tw.select_watch(self.sample(), latest_n=2, pinned_n=3)
        self.assertEqual([p["video_id"] for p in watch], ["p1", "p2", "p3", "n0", "n1"])
        self.assertEqual(tw.select_watch(self.sample(), latest_n=0, pinned_n=0), [])

    def test_the_latest_five_slide_forward_when_a_new_post_appears(self):
        """Always the 5 newest non-pinned posts: a new post pushes the oldest of the five out and takes its place."""
        db = tmp_db()

        def scan_round(items):
            posts = tw.parse_item_list([{"itemList": items}], "shop")
            watch = tw.select_watch(posts, latest_n=5, pinned_n=2)
            for p in watch:
                db.upsert_post(p)
            db.set_watch_set([p["video_id"] for p in watch])
            for row in db.watched_posts():                    # exactly what _scan_with does for empty posts
                if row["comment_count"] == 0 and row.get("checked_comment_count") is None:
                    db.mark_post_checked(row["video_id"], 0)
            return [p["video_id"] for p in db.watched_posts()], [p["video_id"] for p in tw.due_posts(
                db.watched_posts(), now=time.time(), recheck_hours=12, cap=10)]

        base = [item("p1", 100, 16, True), item("p2", 90, 25, True)] + [item(f"n{i}", 1000 - i, i % 3) for i in range(8)]
        watched, due = scan_round(base)
        self.assertEqual(watched, ["p1", "p2", "n0", "n1", "n2", "n3", "n4"])
        for vid in due:                                       # the first round reads what has comments
            db.mark_post_checked(vid, db.get_post(vid)["comment_count"])
        self.assertEqual(scan_round(base)[1], [], "nothing changed -> nothing to open")

        fresh = item("brand-new", 2000, 0)                    # a new post goes live with no comments yet
        watched, due = scan_round([fresh] + base)
        self.assertEqual(watched, ["p1", "p2", "brand-new", "n0", "n1", "n2", "n3"], "n4 (the oldest of the five) drops out")
        self.assertIsNone(db.get_post("n4")["watch_rank"])
        self.assertEqual(db.get_post("n4")["comment_count"], 1, "its stored data is kept, it just is not watched any more")
        self.assertEqual(due, [], "an empty new post needs no visit")

        grown = item("brand-new", 2000, 2)                    # ...and the first comments arrive two minutes later
        watched, due = scan_round([grown] + base)
        self.assertEqual(due, ["brand-new"], "the new post is opened as soon as it has comments")

    def test_due_logic(self):
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 20 * 3600))
        posts = [
            {"video_id": "never", "comment_count": 5, "checked_comment_count": None, "last_checked_at": None, "watch_rank": 3},
            {"video_id": "grew", "comment_count": 9, "checked_comment_count": 4, "last_checked_at": stamp, "watch_rank": 1},
            {"video_id": "same-fresh", "comment_count": 3, "checked_comment_count": 3, "last_checked_at": stamp, "watch_rank": 2},
            {"video_id": "same-stale", "comment_count": 3, "checked_comment_count": 3, "last_checked_at": old, "watch_rank": 4},
            {"video_id": "shrank", "comment_count": 1, "checked_comment_count": 6, "last_checked_at": stamp, "watch_rank": 5},
        ]
        order = [p["video_id"] for p in tw.due_posts(posts, now=now, recheck_hours=12, cap=10)]
        self.assertEqual(order, ["never", "grew", "shrank", "same-stale"])
        self.assertNotIn("same-fresh", order)
        self.assertEqual(len(tw.due_posts(posts, now=now, recheck_hours=12, cap=2)), 2)

    def test_recent_failure_backs_off_then_retries(self):
        now = time.time()
        row = {"video_id": "x", "comment_count": 5, "checked_comment_count": None, "watch_rank": 1,
               "last_error": "no comment data", "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))}
        self.assertEqual(tw.due_posts([row], now=now, recheck_hours=12, cap=5), [])
        row["last_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        self.assertEqual(len(tw.due_posts([row], now=now, recheck_hours=12, cap=5)), 1)

    def test_normalize_comments(self):
        pages = [{"comments": [{"cid": "1", "text": "a", "user": {"unique_id": "ana"}, "digg_count": 2,
                                "create_time": 5, "reply_comment_total": 1}]},
                 {"comments": [{"cid": "1", "text": "a"}, {"cid": "2", "text": "b", "user": {"nickname": "Bob"}}]}]
        out = tw.normalize_comments(pages)
        self.assertEqual([c["cid"] for c in out], ["1", "2"])
        self.assertEqual((out[0]["username"], out[0]["reply_count"], out[1]["username"]), ("ana", 1, "Bob"))

    def test_capture_routes_responses(self):
        class R:
            def __init__(self, url, payload):
                self.url, self._p = url, payload

            def json(self):
                return self._p
        cap = tw.Capture()
        cap.on_response(R("https://www.tiktok.com/api/post/item_list/?x=1", {"itemList": []}))
        cap.on_response(R("https://www.tiktok.com/api/comment/list/?aweme_id=77&cursor=0", {"comments": [1]}))
        cap.on_response(R("https://www.tiktok.com/api/comment/list/reply/?aweme_id=77", {"comments": [2]}))
        cap.on_response(R("https://www.tiktok.com/api/other/", {}))
        self.assertEqual(len(cap.item_lists), 1)
        self.assertEqual(cap.pages("77"), [{"comments": [1]}])


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self._saved = (config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED)
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED = "4242", True
        self.db = tmp_db()
        self.tg = FakeTG()
        self.ai = make_ai(self.db, lambda m, s, u: (
            {"content": "\n".join(f"{i}|NEG|pt|3|scam|accuses fraud" for i in range(1, u.count("\n") + 2))}
            if "screen comments" in s else {"content": "Sentimos muito! Fale conosco no direct."}))
        self.scans: list = []
        self.posted: list = []
        self.post_result = {"ok": True, "profile": "real-account-1",
                            "publish_response": {"body": {"comment": {"cid": "999"}}}}

        def scanner(db):
            self.scans.append(1)
            db.upsert_post({"video_id": "v1", "url": "https://www.tiktok.com/@x/video/v1", "description": "iPhone",
                            "is_pinned": 0, "comment_count": 2})
            db.set_watch_set(["v1"])
            analysis.ingest_comments(db, "v1", [comment("c1", "É golpe, nunca mais compro!"),
                                                comment("c2", "Amei 😍")], first_scan=False)
            return {"opened": 1, "new": 2}

        def poster(a):
            self.posted.append(a)
            return self.post_result

        self.svc = Service(self.db, tg=self.tg, ai=self.ai, scanner=scanner, poster=poster)
        self.svc._memory_check = lambda: (True, 8192.0)          # independent of this machine's RAM

    def tearDown(self):
        self.svc.stop()
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED = self._saved


class TestSharedServerGuard(ServiceCase):
    def test_scheduled_scan_is_skipped_while_memory_is_short_and_runs_when_it_is_back(self):
        self.svc._memory_check = lambda: (False, 410.0)
        self.svc.do_scan()
        self.assertEqual(self.scans, [], "no browser while the neighbours are short of memory")
        self.assertIn("low memory", self.db.meta_get("last_scan_skip_reason"))
        self.assertEqual(self.tg.sent, [], "a skipped slot is not an alarm")
        self.svc.do_scan()
        self.assertEqual([e for e in self.db.recent_events(20) if e["kind"] == "scan_skipped_low_memory"].__len__(), 1,
                         "logged once an hour, not once per slot")
        self.svc._memory_check = lambda: (True, 3000.0)
        self.svc.do_scan()
        self.assertEqual(len(self.scans), 1)

    def test_manual_scan_waits_then_tells_the_operator(self):
        self.svc._memory_check = lambda: (False, 410.0)
        self.svc.stop_event.set()                                 # do not really wait 60 s in a test
        self.svc.do_scan(manual=True)
        self.assertEqual(self.scans, [])
        self.assertTrue(any("short of memory" in m["text"] for m in self.tg.sent), self.tg.sent)

    def test_approved_post_is_not_launched_into_a_full_server_and_stays_retryable(self):
        card = TestPostJob.card(self)
        self.svc._memory_check = lambda: (False, 300.0)
        self.svc.stop_event.set()
        self.db.update_approval(card["id"], status="posting")
        self.svc.do_post(card["id"])
        self.assertEqual(self.posted, [])
        a = self.db.get_approval(card["id"])
        self.assertEqual(a["status"], "post_failed")
        self.assertIn("short of memory", a["last_error"])

    def test_sysguard_reads_a_plausible_number_and_unknown_means_go(self):
        from app import sysguard
        free = sysguard.available_mb()
        self.assertTrue(free is None or free > 50, free)
        self.assertEqual(sysguard.enough_memory(0)[0], True)
        orig = sysguard.available_mb
        sysguard.available_mb = lambda: None
        try:
            self.assertEqual(sysguard.enough_memory(10 ** 9), (True, None))
        finally:
            sysguard.available_mb = orig
        readings = iter([100.0, 100.0, 5000.0])
        sysguard.available_mb = lambda: next(readings)
        try:
            slept = []
            ok, got = sysguard.wait_for_memory(900, 60, 1, sleep=slept.append)
        finally:
            sysguard.available_mb = orig
        self.assertEqual((ok, got, len(slept)), (True, 5000.0, 2))


class TestCaptchaCooldown(ServiceCase):
    def test_pauses_grow_5_10_20_40_60_and_a_clean_open_ends_the_episode(self):
        self.assertEqual(tw.captcha_cooldown_left(self.db), 0)
        minutes = [tw.start_captcha_cooldown(self.db, now=1000.0)["minutes"] for _ in range(6)]
        self.assertEqual(minutes, [5, 10, 20, 40, 60, 60])
        self.assertAlmostEqual(tw.captcha_cooldown_left(self.db, now=1000.0), 3600.0)
        self.assertEqual(tw.captcha_cooldown_left(self.db, now=1000.0 + 3601), 0, "it simply runs out")
        tw.clear_captcha_cooldown(self.db)
        self.assertEqual(tw.captcha_cooldown_left(self.db, now=1000.0), 0)
        self.assertEqual(tw.start_captcha_cooldown(self.db, now=1000.0)["streak"], 1, "a clean open resets the streak")

    def test_only_captcha_errors_trigger_it(self):
        self.assertTrue(tw.is_captcha_error(tw.ScanError("captcha wall appeared while opening a post")))
        self.assertFalse(tw.is_captcha_error(tw.ScanError("no comment data arrived for post 1")))

    def test_operator_is_told_once_per_episode_not_every_slot(self):
        def scanner(streak, minutes):
            return lambda db: {"opened": 0, "new": 0, "captcha": {"streak": streak, "minutes": minutes}}
        self.svc._scan_fn = scanner(1, 5)
        self.svc.do_scan()
        told = [m for m in self.tg.sent if "security check" in m["text"]]
        self.assertEqual(len(told), 1)
        self.assertIn("5 min", told[0]["text"])
        for streak, minutes in ((2, 10), (3, 20), (4, 40)):
            self.svc._scan_fn = scanner(streak, minutes)
            self.svc.do_scan()
        self.assertEqual(len([m for m in self.tg.sent if "security check" in m["text"]]), 1, "same episode: no repeats")
        self.db.meta_set("captcha_alerted_at", time.time() - 7 * 3600)
        self.svc._scan_fn = scanner(5, 60)
        self.svc.do_scan()
        self.assertEqual(len([m for m in self.tg.sent if "security check" in m["text"]]), 2, "still walled hours later")
        self.svc._scan_fn = scanner(1, 5)                      # flapping: a new episode within the hour stays quiet
        self.svc.do_scan()
        self.assertEqual(len([m for m in self.tg.sent if "security check" in m["text"]]), 2)
        self.db.meta_set("captcha_alerted_at", time.time() - 2 * 3600)
        self.svc.do_scan()                                     # ...but a genuinely new episode later is announced
        self.assertEqual(len([m for m in self.tg.sent if "security check" in m["text"]]), 3)

    def test_a_scan_without_a_captcha_says_nothing(self):
        self.svc._scan_fn = lambda db: {"opened": 0, "new": 0}
        self.svc.do_scan()
        self.assertFalse([m for m in self.tg.sent if "security check" in m["text"]])


class TestScanJob(ServiceCase):
    def test_scan_analyses_and_alerts(self):
        self.svc.do_scan()
        self.assertEqual(self.db.get_comment("c1")["status"], "negative")
        self.assertEqual(self.db.get_comment("c2")["status"], "ok")
        self.assertEqual(len(self.db.list_approvals(("pending",))), 1)
        self.assertIn("Negative comment", self.tg.sent[-1]["text"])
        self.assertEqual(self.db.usage_for_day()["by_kind"], {"classify": 1, "draft": 1}, "2 requests in total")
        self.assertTrue(self.db.meta_get("last_scan_ts"))

    def test_second_scan_costs_nothing_and_does_not_repeat_the_alert(self):
        self.svc.do_scan()
        calls, cards = self.db.usage_for_day()["calls"], len(self.tg.sent)
        self.svc.do_scan()
        self.assertEqual(self.db.usage_for_day()["calls"], calls)
        self.assertEqual(len(self.tg.sent), cards)

    def test_there_is_no_way_to_pause_monitoring(self):
        for gone in ("set_paused", "is_paused", "request_scan"):
            self.assertFalse(hasattr(self.svc, gone), gone)
        self.db.meta_set("paused", "1")                       # a leftover flag from an older version changes nothing
        self.svc.do_scan()
        self.assertEqual(len(self.scans), 1, "monitoring always runs")

    def test_scan_failure_alerts_once_then_recovers(self):
        def broken(db):
            raise tw.ScanError("captcha wall on the profile page")
        self.svc._scan_fn = broken
        self.svc.do_scan()
        self.svc.do_scan()
        alerts = [m for m in self.tg.sent if "scan problem" in m["text"]]
        self.assertEqual(len(alerts), 1, "one alert, not one per cycle")
        self.assertIn("captcha", self.db.meta_get("last_scan_error"))
        self.assertIn("⚠", self.svc.last_scan_text() + "⚠")
        self.svc._scan_fn = lambda db: {"opened": 0, "new": 0}
        self.svc.do_scan()
        self.assertEqual(self.db.meta_get("last_scan_error"), "")

    def test_failed_scan_still_announces_what_is_already_stored(self):
        self.svc.do_scan()                       # stores + announces c1
        analysis.ingest_comments(self.db, "v1", [comment("c3", "isso é fraude e golpe")], first_scan=False)

        def broken(db):
            raise tw.ScanError("403")
        self.svc._scan_fn = broken
        self.svc.do_scan()
        self.assertEqual(self.db.get_comment("c3")["status"], "negative")
        self.assertEqual(len(self.db.list_approvals(("pending",))), 2)

    def test_budget_exhausted_alert_once_per_day(self):
        self.db.meta_set("ai_exhausted_day", time.strftime("%Y-%m-%d", time.gmtime()))
        self.svc.do_scan()
        self.svc.do_scan()
        alerts = [m for m in self.tg.sent if "quota" in m["text"]]
        self.assertEqual(len(alerts), 1)
        # the strongly negative comment is still announced (keyword fallback), without a drafted reply
        self.assertEqual(self.db.get_comment("c1")["source"], "fallback")
        self.assertTrue(any("No reply drafted yet" in m["text"] for m in self.tg.sent))


class TestQuestionsAreAnswered(unittest.TestCase):
    """A customer question is recognised, announced by default and answered from the company information."""
    CONTEXT = ("Example Store is a duty-free shopping mall on the Examplia border. " + "It sells imported goods. " * 12
               + "\n\n"
               "Locations: the main store is at Avenida Central 1234, Central City, Examplia. "
               + "Parking is available for visitors. " * 12 + "\n\n"
               "Awards: winner of the Frontier Awards several years running. " + "A long and proud history. " * 12)

    def setUp(self):
        self._saved = (config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED, config.NOTIFY_QUESTIONS_DEFAULT)
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED, config.NOTIFY_QUESTIONS_DEFAULT = "4242", True, True
        self.db = tmp_db()
        self.tg = FakeTG()
        self.db.set_setting("company_context", self.CONTEXT)
        self.drafts = []

        def transport(model, system, user):
            if "screen comments" in system:                       # the batch classifier: everything is a question
                n = user.count("\n") + 1
                return {"content": "\n".join(f"{i}|Q|pt|0|other|asks for information" for i in range(1, n + 1))}
            self.drafts.append((system, user))
            return {"content": self.reply}
        self.reply = "Ficamos na Avenida Central 1234, em Central City! \U0001F642"
        self.ai = make_ai(self.db, transport)
        self.comments = [comment("q1", "Onde fica a loja?")]

        def scanner(db):
            db.upsert_post({"video_id": "v1", "url": "https://www.tiktok.com/@x/video/v1", "description": "Tour",
                            "is_pinned": 0, "comment_count": 1})
            db.set_watch_set(["v1"])
            analysis.ingest_comments(db, "v1", self.comments, first_scan=False)
            return {"opened": 1, "new": len(self.comments)}
        self.svc = Service(self.db, tg=self.tg, ai=self.ai, scanner=scanner, poster=lambda a: {"ok": True})
        self.svc._memory_check = lambda: (True, 8192.0)

    def tearDown(self):
        self.svc.stop()
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED, config.NOTIFY_QUESTIONS_DEFAULT = self._saved

    def test_a_question_gets_a_card_with_an_answer_without_any_setting(self):
        self.svc.do_scan()
        self.assertEqual(self.db.get_comment("q1")["status"], "question")
        card = self.db.list_approvals(("pending",))[0]
        self.assertEqual(card["cid"], "q1")
        self.assertIn("Avenida Central 1234", card["reply"])
        text = self.tg.sent[-1]["text"]
        self.assertIn("Question that needs an answer", text)
        self.assertIn("Onde fica a loja?", text)
        self.assertIn("ap:post:" + card["id"], str(self.tg.sent[-1]["markup"]), "the operator can post the answer with one tap")

    def test_the_ai_is_told_to_answer_and_gets_the_relevant_company_information(self):
        self.svc.do_scan()
        system, user = self.drafts[0]
        self.assertIn("asks a QUESTION", system)
        self.assertIn("Do not apologise", system)
        self.assertNotIn("The customer is unhappy", system)
        self.assertIn("Avenida Central 1234", system, "the 'Locations' section reaches the AI for an address question")
        self.assertIn("Type: customer question", user)
        self.assertIn("Speak AS the business", system, "the reply says 'we', not 'they'")

    def test_a_complaint_still_gets_the_apology_style_task(self):
        self.comments[:] = [comment("n1", "É golpe, nunca recebi nada")]
        self.ai = make_ai(self.db, lambda m, s, u: (
            {"content": "1|NEG|pt|3|scam|accuses fraud"} if "screen comments" in s
            else self.drafts.append((s, u)) or {"content": "Sentimos muito, fale conosco no direct."}))
        self.svc.ai = self.ai
        self.svc.do_scan()
        system, user = self.drafts[0]
        self.assertIn("unhappy or critical", system)
        self.assertNotIn("asks a QUESTION", system)
        self.assertIn("Type: complaint", user)

    def test_questions_can_be_switched_off_and_on_again(self):
        self.db.set_setting("notify_questions", "0")
        self.svc.do_scan()
        self.assertEqual(self.db.list_approvals(("pending",)), [], "off: no card")
        self.assertEqual(self.db.get_comment("q1")["status"], "question", "...but it is still recognised and stored")
        self.db.set_setting("notify_questions", "1")
        self.svc.do_scan()
        self.assertEqual(len(self.db.list_approvals(("pending",))), 1, "on: the stored question is announced now")

    def test_a_question_still_reaches_you_when_the_ai_has_no_budget_and_is_answered_later(self):
        self.db.meta_set("ai_exhausted_day", time.strftime("%Y-%m-%d", time.gmtime()))
        self.svc.do_scan()
        self.assertEqual(self.db.get_comment("q1")["source"], "fallback")
        self.assertEqual(self.db.get_comment("q1")["status"], "question")
        self.assertTrue(any("Question that needs an answer" in m["text"] and "No reply drafted yet" in m["text"]
                            for m in self.tg.sent))
        self.db.meta_set("ai_exhausted_day", "")                    # a new day: the budget is back
        self.svc.do_scan()
        card = self.db.list_approvals(("pending",))[0]
        self.assertIn("Avenida Central 1234", card["reply"], "the answer is drafted as soon as the AI is available")
        self.assertIn("Avenida Central 1234", self.tg.edits[-1]["text"], "and the card is updated in place")

    def reply_for(self, text, cid="q9", lang="pt"):
        self.comments[:] = [comment(cid, text)]
        self.svc.do_scan()
        return self.db.get_approval(next(a["id"] for a in self.db.list_approvals(("pending",), 50) if a["cid"] == cid))["reply"]

    def test_when_the_information_does_not_contain_the_answer_the_ai_says_unknown_and_the_standard_reply_goes_out(self):
        self.reply = "UNKNOWN"
        got = self.reply_for("Que horas vocês abrem aos domingos?")
        self.assertEqual(got, ai_mod.CONFIRM_REPLY["pt"])
        self.assertIn("direta", got)
        self.assertTrue([e for e in self.db.recent_events(10) if e["kind"] == "question_unanswerable"])
        system = self.drafts[0][0]
        self.assertIn("UNKNOWN", system)
        self.assertNotIn("apologise briefly and invite them to send a direct", system.split("Task:")[0],
                         "the generic rules no longer push a question towards an apology")

    def test_an_invented_number_never_reaches_the_customer(self):
        self.reply = "Aos domingos abrimos às 9h, venha nos visitar!"            # the real, observed invention
        self.assertEqual(self.reply_for("Que horas vocês abrem aos domingos?"), ai_mod.CONFIRM_REPLY["pt"])
        self.reply = "Existimos desde 2009, com mais de 15 anos de atuação."
        self.assertEqual(self.reply_for("Desde que ano vocês existem?", cid="q10"), ai_mod.CONFIRM_REPLY["pt"])
        ev = [e for e in self.db.recent_events(20) if e["kind"] == "question_unanswerable"]
        self.assertEqual(len(ev), 2)
        self.assertIn("2009", ev[0]["detail"] + ev[1]["detail"])

    def test_a_number_that_is_in_the_company_information_is_fine(self):
        self.db.set_setting("company_context", self.CONTEXT + "\n\nHistory: founded in 1950 by an Italian immigrant. " * 3)
        self.reply = "Desde 1950! Uma história de família. \U0001F642"
        self.assertEqual(self.reply_for("Desde que ano vocês existem?"), self.reply)

    def test_the_standard_reply_follows_the_language_of_the_comment(self):
        self.reply = "UNKNOWN"
        self.ai = make_ai(self.db, lambda m, s, u: (
            {"content": "\n".join(f"{i}|Q|es|0|other|pregunta" for i in range(1, u.count("\n") + 2))} if "screen comments" in s
            else {"content": "UNKNOWN"}))
        self.svc.ai = self.ai
        self.assertEqual(self.reply_for("¿A qué hora abren los domingos?", cid="q11"), ai_mod.CONFIRM_REPLY["es"])
        self.assertEqual(ai_mod.CONFIRM_REPLY.keys(), {"pt", "es", "en", "pl"})
        self.assertTrue(all(len(t) <= ai_mod.TIKTOK_REPLY_LIMIT for t in ai_mod.CONFIRM_REPLY.values()))

    def test_unsupported_numbers_helper(self):
        ctx = "Founded in 1950. Parking for 1,000+ cars. 3 floors. Open 2013, 2014."
        self.assertEqual(ai_mod.unsupported_numbers("Desde 1950, 3 andares, 1.000 vagas", ctx), [])
        self.assertEqual(ai_mod.unsupported_numbers("Abrimos às 9h desde 2009", ctx), ["2009", "9"])
        self.assertEqual(ai_mod.unsupported_numbers("Somos a maior loja", ctx), [])
        self.assertEqual(ai_mod.unsupported_numbers("Em 2009", ctx, "perguntou sobre 2009"), [], "a number the customer wrote is fine")

    def test_a_complaint_reply_is_not_touched_by_the_question_guard(self):
        self.comments[:] = [comment("n7", "É golpe, nunca recebi o pedido 4455")]
        self.ai = make_ai(self.db, lambda m, s, u: (
            {"content": "1|NEG|pt|3|scam|accuses fraud"} if "screen comments" in s
            else {"content": "Sentimos muito pelo pedido 4455, envie uma mensagem direta."}))
        self.svc.ai = self.ai
        self.svc.do_scan()
        self.assertIn("pedido 4455", self.db.list_approvals(("pending",))[0]["reply"])

    def test_the_ai_test_box_treats_a_sample_question_as_a_question(self):
        from app import ai as ai_mod
        self.assertTrue(ai_mod.is_question({"text": "Aceitam cartão?"}))
        self.assertFalse(ai_mod.is_question({"text": "Aceitam cartão? Vocês são ladrões, é golpe"}))
        self.assertFalse(ai_mod.is_question({"text": "Amei o shopping"}))
        self.assertTrue(ai_mod.is_question({"text": "Amei", "status": "question"}), "the stored verdict wins")
        self.assertFalse(ai_mod.is_question({"text": "Onde fica?", "status": "negative"}), "the stored verdict wins")


class TestAdaptiveInterval(ServiceCase):
    def setUp(self):
        super().setUp()
        keep = (config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS, config.SCAN_MIN_GAP_SECONDS)
        self.addCleanup(lambda: (setattr(config, "POLL_INTERVAL_SECONDS", keep[0]),
                                 setattr(config, "POLL_INTERVAL_MAX_SECONDS", keep[1]),
                                 setattr(config, "SCAN_MIN_GAP_SECONDS", keep[2])))

    def test_default_is_a_fixed_120_second_rhythm(self):
        src = Path(config.__file__).read_text(encoding="utf-8")          # the shipped defaults, whatever .env says
        self.assertIn('os.environ.get("POLL_INTERVAL_SECONDS", "120")', src)
        self.assertIn('os.environ.get("POLL_INTERVAL_MAX_SECONDS", str(POLL_INTERVAL_SECONDS))', src,
                      "the ceiling defaults to the interval itself = no stretching")
        config.POLL_INTERVAL_SECONDS = config.POLL_INTERVAL_MAX_SECONDS = 120
        self.svc._scan_fn = lambda db: {"opened": 0, "new": 0}
        for _ in range(8):                                        # quiet scan after quiet scan: still every 120 s
            self.svc.do_scan()
            self.assertEqual(self.svc.next_scan_delay(), 120)

    def test_next_scan_starts_one_interval_after_the_previous_one_started(self):
        config.POLL_INTERVAL_SECONDS = config.POLL_INTERVAL_MAX_SECONDS = 120
        config.SCAN_MIN_GAP_SECONDS = 10
        self.assertEqual(self.svc.next_scan_start(1000.0, 1012.0), 1120.0, "a 12 s scan does not add to the 120 s")
        self.assertEqual(self.svc.next_scan_start(1000.0, 1062.0), 1120.0, "a 62 s scan (3 posts opened) neither")
        self.assertEqual(self.svc.next_scan_start(1000.0, 1150.0), 1160.0, "an overrun gets a short breather")

    def test_quiet_scans_stretch_the_interval_and_news_resets_it(self):
        config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS = 120, 600       # opt-in stretching
        base, mx = config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS
        self.svc._scan_fn = lambda db: {"opened": 0, "new": 0}
        delays = []
        for _ in range(6):
            self.svc.do_scan()
            delays.append(round(self.svc.next_scan_delay()))
        self.assertEqual(delays[0], round(base * 1.5))
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])), delays)
        self.assertEqual(delays[-1], max(base, mx), "capped at POLL_INTERVAL_MAX_SECONDS")
        self.svc._scan_fn = lambda db: {"opened": 1, "new": 2}
        self.svc.do_scan()
        self.assertEqual(round(self.svc.next_scan_delay()), base, "news resets it")

    def test_manual_scan_and_failed_scan_do_not_make_it_longer(self):
        self.svc._scan_fn = lambda db: {"opened": 0, "new": 0}
        self.svc.do_scan(manual=True)
        self.assertEqual(self.svc._idle_scans, 0)
        self.svc.do_scan()
        before = self.svc._idle_scans

        def broken(db):
            raise tw.ScanError("403")
        self.svc._scan_fn = broken
        self.svc.do_scan()
        self.assertEqual(self.svc._idle_scans, before, "a failure is not a sign of a quiet page")

    def test_interval_never_drops_below_the_configured_base(self):
        saved = (config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS)
        config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS = 300, 100       # misconfigured ceiling
        try:
            self.svc._idle_scans = 5
            self.assertEqual(self.svc.next_scan_delay(), 300)
        finally:
            config.POLL_INTERVAL_SECONDS, config.POLL_INTERVAL_MAX_SECONDS = saved


class TestPostJob(ServiceCase):
    def card(self):
        self.svc.do_scan()
        return self.db.list_approvals(("pending",))[0]

    def card_again(self):
        analysis.ingest_comments(self.db, "v1", [comment("c9", "isso é fraude e golpe total")], first_scan=False)
        self.db.update_comment("c9", status="negative", severity=3, category="scam", lang="pt")
        notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)
        return [x for x in self.db.list_approvals(("pending",)) if x["cid"] == "c9"][0]

    def test_successful_post_updates_the_card(self):
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        done = self.db.get_approval(a["id"])
        self.assertEqual((done["status"], done["posted_by"], done["posted_reply_cid"]), ("posted", "real-account-1", "999"))
        self.assertIn("Posted on TikTok", self.tg.edits[-1]["text"])
        self.assertIn("real-account-1", self.tg.edits[-1]["text"])
        self.assertIsNone(self.tg.edits[-1]["markup"])
        self.assertEqual(self.posted[0]["cid"], "c1")

    def test_a_reply_that_needed_several_goes_keeps_the_reasons(self):
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.post_result = {**self.post_result, "attempts": [
            {"account": "m", "attempt": 1, "ok": False, "captcha": True, "error": "unexpected error (treated as a possible wall): Timeout"},
            {"account": "m", "attempt": 2, "ok": False, "captcha": True, "error": ""},
            {"account": "m", "attempt": 3, "ok": True, "captcha": False, "error": ""}]}
        self.svc.do_post(a["id"])
        ev = [e for e in self.db.recent_events(10) if e["kind"] == "post_attempts"]
        self.assertEqual(len(ev), 1)
        self.assertIn("#1 m: unexpected error", ev[0]["detail"])
        self.assertIn("#2 m: captcha", ev[0]["detail"])
        self.assertIn("#3 m: ok", ev[0]["detail"])

    def test_post_reply_records_why_each_try_ended(self):
        import contextlib
        from app import tiktok_monitor as tm
        outcomes = iter([{"ok": False, "captcha": True, "error": ""}, TimeoutError("input box never attached"),
                         {"ok": True, "profile": "m"}])
        saved = (tm._post_reply_once, tm._pace_posts, tm._mark_post_attempt, tm._use_account, tm.time.sleep, config.account_chain)

        def once(*a, **k):
            r = next(outcomes)
            if isinstance(r, Exception):
                raise r
            return r
        tm._post_reply_once, tm._pace_posts, tm._mark_post_attempt = once, lambda: None, lambda: None
        tm._use_account = lambda name: contextlib.nullcontext()
        tm.time.sleep = lambda s: None
        try:
            result = tm.post_reply("https://x/video/1", "c1", "hi", profile_names=["m"])
        finally:
            tm._post_reply_once, tm._pace_posts, tm._mark_post_attempt, tm._use_account, tm.time.sleep, config.account_chain = saved
        self.assertTrue(result["ok"])
        self.assertEqual([t["attempt"] for t in result["attempts"]], [1, 2, 3])
        self.assertEqual([t["ok"] for t in result["attempts"]], [False, False, True])
        self.assertIn("input box never attached", result["attempts"][1]["error"])

    def test_a_reply_tiktok_publishes_as_author_only_is_flagged_not_celebrated(self):
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.post_result = {**self.post_result, "visibility": "limited", "comment_status": 2}
        self.svc.do_post(a["id"])
        done = self.db.get_approval(a["id"])
        self.assertEqual((done["status"], done["posted_visibility"]), ("posted", "limited"))
        self.assertIn("ONLY to the account that posted it", self.tg.edits[-1]["text"])
        warn = [m for m in self.tg.sent if "visible only to" in m["text"]]
        self.assertEqual(len(warn), 1, "the operator is told right away, once")
        self.assertIn("REPLY_ACCOUNT_FIRST", warn[0]["text"])
        self.assertTrue([e for e in self.db.recent_events(10) if e["kind"] == "reply_limited"])

    def test_a_public_reply_gets_no_warning(self):
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.post_result = {**self.post_result, "visibility": "public", "comment_status": 1}
        self.svc.do_post(a["id"])
        self.assertEqual(self.db.get_approval(a["id"])["posted_visibility"], "public")
        self.assertNotIn("ONLY to the account", self.tg.edits[-1]["text"])
        self.assertFalse([m for m in self.tg.sent if "visible only to" in m["text"]])

    def test_comment_visibility_from_tiktoks_status_field(self):
        from app import tiktok_monitor as tm
        self.assertEqual([tm.comment_visibility(x) for x in (1, 4, "1", 2, "2", 0, None, 7)],
                         ["public", "public", "public", "limited", "limited", None, None, None])

    def test_the_preferred_account_answers_first_everywhere(self):
        import json, tempfile, pathlib
        saved = (config.ACCOUNTS_FILE, config.REPLY_ACCOUNT_FIRST)
        tmp = pathlib.Path(tempfile.mkdtemp()) / "accounts.json"
        tmp.write_text(json.dumps({"accounts": [{"name": "real-account-1", "user_data_dir": "x"},
                                                {"name": "owner", "user_data_dir": "y"},
                                                {"name": "off", "user_data_dir": "z", "enabled": False}]}), encoding="utf-8")
        config.ACCOUNTS_FILE = tmp
        try:
            config.REPLY_ACCOUNT_FIRST = ""
            self.assertEqual(config.account_chain(), [config.BFP_PROFILE_NAME, "real-account-1", "owner"])
            config.REPLY_ACCOUNT_FIRST = "owner"
            self.assertEqual(config.account_chain(), ["owner", config.BFP_PROFILE_NAME, "real-account-1"])
            config.REPLY_ACCOUNT_FIRST = "does-not-exist"
            self.assertEqual(config.account_chain(), [config.BFP_PROFILE_NAME, "real-account-1", "owner"], "unknown name: ignored")
        finally:
            config.ACCOUNTS_FILE, config.REPLY_ACCOUNT_FIRST = saved

    def test_an_older_database_is_upgraded_in_place(self):
        import sqlite3, tempfile, pathlib
        from app.db import DB
        path = pathlib.Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""CREATE TABLE approvals (id TEXT PRIMARY KEY, cid TEXT NOT NULL, video_id TEXT NOT NULL, video_url TEXT,
            username TEXT, comment_text TEXT, lang TEXT, category TEXT, severity INTEGER, reason TEXT, reply TEXT, status TEXT NOT NULL,
            chat_id TEXT, message_id INTEGER, simulated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            posted_at TEXT, post_attempts INTEGER NOT NULL DEFAULT 0, regen_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            posted_by TEXT, posted_reply_cid TEXT);
            INSERT INTO approvals(id,cid,video_id,status,created_at,updated_at) VALUES('a','c','v','posted','t','t');""")
        con.commit()
        con.close()
        db = DB(path)
        self.assertIn("posted_visibility", [r[1] for r in db.conn().execute("PRAGMA table_info(approvals)")])
        self.assertEqual(db.get_approval("a")["status"], "posted", "existing rows untouched")
        db.update_approval("a", posted_visibility="limited")
        self.assertEqual(db.get_approval("a")["posted_visibility"], "limited")

    def test_successful_post_sends_a_screenshot_of_the_reply(self):
        import tempfile, pathlib
        shot = pathlib.Path(tempfile.mkdtemp()) / "proof.png"
        shot.write_bytes(b"\x89PNG fake")
        self.post_result = {**self.post_result, "proof_screenshot": str(shot)}
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        self.assertEqual(len(self.tg.photos), 1)
        photo = self.tg.photos[0]
        self.assertEqual(photo["path"], str(shot))
        self.assertEqual(photo["reply_to"], self.db.get_approval(a["id"])["message_id"], "attached to the alert card")
        for needle in ("Reply posted on TikTok", "real-account-1", "@user", "golpe", "Sentimos muito", "Screenshot"):
            self.assertIn(needle, photo["caption"])
        self.assertLessEqual(len(photo["caption"]), 1024)

    def test_proof_falls_back_to_the_after_post_screenshot_and_survives_a_missing_file(self):
        import tempfile, pathlib
        shot = pathlib.Path(tempfile.mkdtemp()) / "after.png"
        shot.write_bytes(b"x")
        self.post_result = {**self.post_result, "proof_screenshot": None, "after_screenshot": str(shot)}
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        self.assertEqual(self.tg.photos[0]["path"], str(shot))
        self.tg.photos.clear()
        self.post_result = {**self.post_result, "proof_screenshot": "C:/does/not/exist.png", "after_screenshot": None}
        b = self.card_again()
        self.db.update_approval(b["id"], status="posting")
        self.svc.do_post(b["id"])
        self.assertEqual(self.tg.photos, [], "no file, no photo, and no crash")
        self.assertEqual(self.db.get_approval(b["id"])["status"], "posted")

    def test_failed_post_sends_no_screenshot(self):
        self.post_result = {"ok": False, "error": "refused to act: the Reply button is covered"}
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        self.assertFalse(self.tg.photos)

    def test_failed_post_offers_retry(self):
        self.post_result = {"ok": False, "error": "TikTok's slider-puzzle wall appeared"}
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        self.assertEqual(self.db.get_approval(a["id"])["status"], "post_failed")
        self.assertIn("slider", self.tg.edits[-1]["text"])
        data = [b["callback_data"] for row in self.tg.edits[-1]["markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"ap:post:{a['id']}", data)

    def test_poster_exception_is_a_failure_not_a_crash(self):
        def boom(a):
            raise RuntimeError("browser died")
        self.svc._post_fn = boom
        a = self.card()
        self.db.update_approval(a["id"], status="posting")
        self.svc.do_post(a["id"])
        self.assertIn("browser died", self.db.get_approval(a["id"])["last_error"])

    def test_a_card_is_only_posted_when_it_is_in_posting_state(self):
        a = self.card()
        self.svc.do_post(a["id"])                # still 'pending': nothing happens
        self.assertEqual(self.posted, [])


class TestDomLocator(unittest.TestCase):
    """_dom_index_of: the comment panel is virtualised, so find the row by text, scrolling if needed."""

    class FakePage:
        def __init__(self, screens):
            self.screens, self.i = screens, 0          # one screen of rows per scroll

        def evaluate(self, js):
            if "querySelectorAll" in js and "texts" in js:
                return self.screens[min(self.i, len(self.screens) - 1)]
            self.i += 1                                  # the scroll script
            return "scrolled"

    class S:
        def __init__(self, page):
            self.page = page

    def locate(self, screens, comment, max_scrolls=14):
        from app import tiktok_monitor as tm
        import app.tiktok_watch as tw
        return tm._dom_index_of(self.S(self.FakePage(screens)), comment, max_scrolls)

    def test_matches_by_text_regardless_of_api_position(self):
        screen = {"texts": ["one", "two", "Cadê o endereço"], "names": ["a", "b", "c"]}
        self.assertEqual(self.locate([screen], {"text": "cadê  o endereço"}), 2)

    def test_duplicates_are_told_apart_by_the_authors_name(self):
        screen = {"texts": ["top demais", "top demais"], "names": ["Ana Silva", "Bruno Costa"]}
        self.assertEqual(self.locate([screen], {"text": "top demais", "user": {"nickname": "Bruno Costa"}}), 1)
        self.assertEqual(self.locate([screen], {"text": "top demais"}), 0)

    def test_scrolls_until_the_row_is_rendered(self):
        screens = [{"texts": ["a", "b"], "names": ["x", "y"]}, {"texts": ["a", "b", "c"], "names": ["x", "y", "z"]},
                   {"texts": ["b", "c", "target"], "names": ["y", "z", "w"]}]
        self.assertEqual(self.locate(screens, {"text": "target"}), 2)

    def test_gives_up_when_it_never_appears(self):
        self.assertIsNone(self.locate([{"texts": ["a"], "names": ["x"]}], {"text": "nope"}, max_scrolls=3))


class TestThreads(ServiceCase):
    def test_telegram_loop_handles_commands_and_remembers_offset(self):
        self.tg.queued_updates = [{"update_id": 10, "message": {"chat": {"id": 4242}, "text": "/status"}}]
        t = threading.Thread(target=self.svc._telegram_loop, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not any("Status" in m["text"] for m in self.tg.sent):
            time.sleep(0.05)
        self.svc.stop()
        t.join(3)
        self.assertTrue(any("Status" in m["text"] for m in self.tg.sent))
        self.assertEqual(self.db.meta_get("telegram_offset"), "11")

    def test_a_crashing_handler_does_not_kill_the_loop(self):
        self.tg.queued_updates = [{"update_id": 1, "callback_query": {"id": "x", "data": "ap:post:zzz", "message": None}},
                                  {"update_id": 2, "message": {"chat": {"id": 4242}, "text": "/help"}}]
        t = threading.Thread(target=self.svc._telegram_loop, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not any("Commands" in m["text"] for m in self.tg.sent):
            time.sleep(0.05)
        self.svc.stop()
        t.join(3)
        self.assertTrue(any("Commands" in m["text"] for m in self.tg.sent))

    def test_post_jobs_outrank_scans_in_the_worker(self):
        order = []
        self.svc.do_scan = lambda manual=False: order.append("scan")
        self.svc.do_post = lambda i: order.append("post")
        self.svc._jobs.put((P_SCAN, next(self.svc._seq), "scan", {}))
        self.svc.request_post("a")
        self.svc.request_post("b")
        t = threading.Thread(target=self.svc._worker, daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and len(order) < 3:
            time.sleep(0.05)
        self.svc.stop()
        t.join(3)
        self.assertEqual(order[:3], ["post", "post", "scan"])

    def test_start_publishes_the_command_menu(self):
        self.svc.start()
        self.svc.stop()
        names = [c for c, _d in self.tg.commands]
        self.assertIn("ai", names)
        self.assertIn("ai_context", names)


if __name__ == "__main__":
    unittest.main()
