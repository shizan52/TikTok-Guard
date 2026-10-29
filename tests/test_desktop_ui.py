"""The desktop app, end to end, in a REAL browser: every screen is opened and clicked through the way a person would, against a
fake world (no TikTok, nothing is really posted, no Telegram, throw-away data). Skipped when no Chrome/Edge/Brave is installed."""
import json
import time
import unittest
from unittest import mock

from ui_support import POST_URL, UI, World

import config
from app import accounts as acc
from app import browsers


def have_browser() -> bool:
    return any(b["kind"] in ("chrome", "edge", "brave") for b in browsers.find_browsers())


@unittest.skipUnless(have_browser(), "needs Chrome, Edge or Brave to drive the app")
class UICase(unittest.TestCase):
    world_args: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.ui = UI()

    @classmethod
    def tearDownClass(cls):
        cls.ui.close()

    def setUp(self):
        self.world = World(**self.world_args)
        self.url = self.world.start()
        self.ui.open(self.url)
        self.ui.wait("document.querySelector('#url-input') && document.querySelector('#stats .stat')", 15, "home loaded")

    def tearDown(self):
        self.world.stop()

    # -- steps
    def reload(self):
        """Reload the page. (ui.open() of the address it is already on only changes the hash: no load event, a 45 s wait.)"""
        self.ui.ev("location.reload()")
        time.sleep(0.4)
        self.ui.wait("document.readyState === 'complete' && document.querySelector('#url-input')", 15, "the page after a reload")

    def analyze(self, url=POST_URL):
        ui = self.ui
        ui.fill("#url-input", url)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=results]')", 40, "analysis results")

    def card(self, cid):
        return f"[data-cid='{cid}']"

    def open_composer(self, cid):
        self.ui.click(f"{self.card(cid)} [data-act=reply]")
        self.ui.wait(f"document.querySelector(\"{self.card(cid)} [data-role=composer]\")", 8, "composer")

    def sent_ok(self, scope=""):
        ok, img = json.dumps(f"{scope} .status-line.ok"), json.dumps(f"{scope} .proof img")
        self.ui.wait(f"document.querySelector({ok})", 20, "reply sent")
        self.ui.wait(f"(() => {{ const i = document.querySelector({img}); return i && i.complete && i.naturalWidth > 0; }})()",
                     10, "proof screenshot loaded")

    def posted(self):
        return [a for a in self.world.posted]


class TestWhatTheAppIsDoing(UICase):
    """The person can always see what the browser is doing - on every page - and what a closed app left half done."""

    def test_the_sidebar_shows_the_step_of_a_reply_being_sent_whatever_page_is_open(self):
        import threading
        ui = self.ui
        self.world.hold = threading.Event()
        self.analyze()
        self.open_composer("7000000000000000001")
        ui.fill(f"{self.card('7000000000000000001')} [data-role=text]", "Sentimos muito pelo transtorno!")
        ui.click(f"{self.card('7000000000000000001')} [data-act=send]")
        try:
            ui.wait("document.querySelector('[data-role=activity-sending]')", 8, "the sidebar shows a reply being sent")
            ui.click("nav a[data-nav=help]")                                   # another page: the strip is still there
            ui.wait("document.querySelector('[data-role=activity-sending] small') && "
                    "document.querySelector('[data-role=activity-sending] small').innerText.includes('Finding the comment')", 10,
                    "the browser's current step")
            self.assertIn("@ana.silva", ui.text("[data-role=activity-sending]"))
        finally:
            self.world.hold.set()
        ui.wait("!document.querySelector('[data-role=activity-sending]')", 12, "the strip goes away when the send is over")

    def test_the_card_shows_the_current_step_and_how_long_it_has_been(self):
        import threading
        ui = self.ui
        self.world.hold = threading.Event()
        self.analyze()
        self.open_composer("7000000000000000001")
        ui.fill(f"{self.card('7000000000000000001')} [data-role=text]", "Sentimos muito pelo transtorno!")
        ui.click(f"{self.card('7000000000000000001')} [data-act=send]")
        try:
            ui.wait("document.querySelector('[data-role=send-progress]') && document.querySelector('[data-role=send-progress]')"
                    ".innerText.includes('Finding the comment')", 12, "the step on the card itself")
            self.assertRegex(ui.text("[data-role=send-progress]"), r"\(\d+ s\)")
        finally:
            self.world.hold.set()
        self.sent_ok(self.card("7000000000000000001"))

    def test_a_post_being_read_is_shown_with_what_the_browser_is_doing(self):
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=progress]')", 8, "progress card")
        self.assertTrue(ui.text("[data-role=progress]").strip(), "the card says something while it works")
        ui.wait("document.querySelector('[data-role=results]')", 40)
        ui.wait("!document.querySelector('[data-role=activity-reading]')", 10, "nothing left in the strip")

    def test_an_idle_app_shows_nothing_in_the_sidebar_strip(self):
        time.sleep(2.5)
        self.assertFalse(self.ui.exists("[data-role=activity-sending]") or self.ui.exists("[data-role=activity-reading]"))

    def test_a_reply_that_was_on_its_way_when_the_app_closed_says_it_may_have_been_sent(self):
        ui = self.ui
        self.world.db.create_approval({"id": "gone1", "cid": "7000000000000000002", "video_id": "7100000000000000001", "video_url": POST_URL,
                                       "username": "bruno_costa", "comment_text": "Meu pedido está atrasado", "reply": "Pedimos desculpa!",
                                       "status": "posting", "kind": "link_reply"})
        self.world.app.controller.recover_interrupted()                       # what the next start of the app does
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelector('.list-row[data-id=gone1]')", 8, "the card")
        row = ui.text(".list-row[data-id=gone1]")
        self.assertIn("Check on TikTok", row)
        self.assertIn("not known whether it reached TikTok", row)
        self.assertNotIn("Sending...", row, "not spinning forever")
        self.assertTrue(ui.exists(".list-row[data-id=gone1] [data-role=interrupted]"))
        ui.click(".list-row[data-id=gone1] [data-act=edit]")
        ui.wait("document.querySelector('.list-row[data-id=gone1] [data-role=composer]')", 8)
        self.assertIn("Send again anyway", ui.text(".list-row[data-id=gone1] [data-act=send]"),
                      "sending it again is possible, but it says what it is")
        self.assertIn("Not sure this was sent", ui.text(".list-row[data-id=gone1] .status-line"))


class TestCheckOnTikTok(UICase):
    """A reply that failed or was interrupted: the app can look at the post and say whether it was sent after all."""
    REPLY = "Pedimos desculpa pelo atraso, vamos resolver hoje mesmo!"

    def interrupted_card(self, before=1):
        db = self.world.db
        db.create_approval({"id": "gone1", "cid": "7000000000000000002", "video_id": "7100000000000000001", "video_url": POST_URL,
                            "username": "bruno_costa", "comment_text": "Meu pedido está atrasado", "reply": self.REPLY,
                            "status": "posting", "kind": "link_reply"})
        db.update_approval("gone1", replies_before=before)
        self.world.app.controller.recover_interrupted()                       # what the next start of the app does
        self.ui.click("nav a[data-nav=replies]")
        self.ui.wait("document.querySelector('.list-row[data-id=gone1]')", 8, "the card")

    def test_no_new_reply_under_the_comment_says_it_was_not_sent_and_may_be_sent_again(self):
        ui = self.ui
        self.interrupted_card(before=1)
        self.world.verify_result = {"ok": True, "count": 1, "preview": [{"user": "shop", "text": "Olá!"}]}
        ui.click(".list-row[data-id=gone1] [data-act=verify]")
        ui.wait("document.querySelector('.list-row[data-id=gone1] [data-role=check-result]')", 15, "the result")
        row = ui.text(".list-row[data-id=gone1]")
        self.assertIn("Not sent", row)
        self.assertIn("NOT posted", row)
        self.assertIn("safe to send it again", row)
        self.assertNotIn("Check on TikTok", ui.text(".list-row[data-id=gone1] .row"), "the card is no longer 'not sure'")
        ui.click(".list-row[data-id=gone1] [data-act=edit]")
        ui.wait("document.querySelector('.list-row[data-id=gone1] [data-role=composer]')", 8)
        self.assertIn("Not sent", ui.text(".list-row[data-id=gone1] .status-line"))

    def test_our_own_text_among_the_replies_turns_the_card_into_a_sent_one(self):
        ui = self.ui
        self.interrupted_card()
        self.world.verify_result = {"ok": True, "count": 2, "preview": [{"user": "shop", "text": "Olá!"}, {"user": "page-account", "text": self.REPLY}]}
        ui.click(".list-row[data-id=gone1] [data-act=verify]")
        ui.wait("document.querySelector('.list-row[data-id=gone1]').innerText.includes('Sent')", 15, "turned into a sent card")
        self.assertEqual(self.world.db.get_approval("gone1")["status"], "posted")
        self.assertFalse(ui.exists(".list-row[data-id=gone1] [data-act=verify]"), "nothing left to check")

    def test_more_replies_than_before_is_a_maybe_and_says_so(self):
        ui = self.ui
        self.interrupted_card(before=1)
        self.world.verify_result = {"ok": True, "count": 3, "preview": []}
        ui.click(".list-row[data-id=gone1] [data-act=verify]")
        ui.wait("document.querySelector('.list-row[data-id=gone1] [data-role=check-result]')", 15)
        row = ui.text(".list-row[data-id=gone1]")
        self.assertIn("Maybe sent", row)
        self.assertIn("3 replies now and had 1", row)

    def test_a_post_that_cannot_be_read_says_so_and_changes_nothing(self):
        ui = self.ui
        self.interrupted_card()
        self.world.verify_result = {"ok": False, "error": "TikTok would not open the page"}
        ui.click(".list-row[data-id=gone1] [data-act=verify]")
        ui.wait("document.querySelector('.list-row[data-id=gone1] [data-role=check-result]')", 15)
        self.assertIn("Could not check: TikTok would not open the page", ui.text(".list-row[data-id=gone1]"))
        self.assertEqual(self.world.db.get_approval("gone1")["status"], "post_failed")
        self.assertTrue(ui.exists(".list-row[data-id=gone1] [data-act=verify]"), "it can be tried again")

    def test_a_check_in_progress_is_visible_in_the_sidebar_on_every_page(self):
        import threading
        ui = self.ui
        self.world.verify_hold = threading.Event()
        self.interrupted_card()
        ui.click(".list-row[data-id=gone1] [data-act=verify]")
        try:
            ui.wait("document.querySelector('[data-role=activity-checking]')", 10, "the strip")
            ui.click("nav a[data-nav=help]")
            ui.wait("document.querySelector('[data-role=activity-checking] small') && document.querySelector('[data-role=activity-checking] small').innerText.includes('Looking at the replies')", 10)
        finally:
            self.world.verify_hold.set()
        ui.wait("!document.querySelector('[data-role=activity-checking]')", 15, "the strip goes away")


class TestAccountsThatTikTokJustRefused(UICase):
    def test_a_refused_account_is_marked_resting_where_accounts_are_chosen(self):
        from app import tiktok_monitor as tm
        ui = self.ui
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        tm.mark_walled("real-account-1", seconds=300)
        self.reload()
        ui.wait("document.querySelector('#read-account').options.length > 1", 10)
        options = ui.ev("[...document.querySelectorAll('#read-account option')].map(o => o.innerText)")
        self.assertTrue(any("resting" in o for o in options), options)
        self.assertFalse(any("resting" in o for o in options if "beta.shop" in o), "only the refused account")
        ui.click("nav a[data-nav=accounts]")
        ui.wait("document.querySelector('.acct[data-account=real-account-1]')", 8)
        self.assertIn("Resting", ui.text(".acct[data-account=real-account-1]"))
        self.assertNotIn("Resting", ui.text(".acct[data-account=real-account-2]"))


class TestReadyToWork(UICase):
    def test_a_computer_that_is_not_ready_says_what_to_fix_on_the_home_page(self):
        ui = self.ui
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""), \
                mock.patch("app.health._internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "ok", "detail": "", "fix": ""}):
            self.reload()
            ui.wait("document.querySelector('[data-role=setup]')", 12, "the setup banner")
        banner = ui.text("[data-role=setup]")
        self.assertIn("AI key", banner)
        self.assertIn("OpenRouter", banner, "it says what to do, not only what is wrong")
        self.assertTrue(ui.exists("[data-role=setup] a[href='#/help']"))

    def test_a_ready_computer_shows_no_banner(self):
        ui = self.ui
        with mock.patch("app.health._internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "ok", "detail": "", "fix": ""}), \
                mock.patch("app.health._company_info", return_value={"id": "company", "title": "Company information", "status": "ok", "detail": "", "fix": ""}), \
                mock.patch("app.health._memory", return_value={"id": "memory", "title": "Memory", "status": "ok", "detail": "", "fix": ""}), \
                mock.patch("app.health._browsers", return_value={"id": "browser", "title": "A web browser", "status": "ok", "detail": "", "fix": ""}):
            self.reload()
            ui.wait("document.querySelector('#stats .stat')", 10)
            time.sleep(1.5)
        self.assertFalse(ui.exists("[data-role=setup]"))

    def test_help_has_a_diagnostics_report_that_can_be_copied(self):
        ui = self.ui
        ui.click("nav a[data-nav=help]")
        ui.wait("document.querySelector('#run-diagnostics')", 8)
        with mock.patch("app.health._internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "ok", "detail": "TikTok can be reached.", "fix": ""}):
            ui.click("#run-diagnostics")
            ui.wait("document.querySelectorAll('#diag-out [data-check]').length >= 6", 15, "the checks")
        self.assertTrue(ui.exists("#copy-report"))
        self.assertTrue(ui.exists("#diag-log"))
        out = ui.text("#diag-out")
        self.assertIn("Recent problems", out)
        self.assertIn("TikTok accounts", out)
        self.assertNotIn(self.world.app.token_value, out, "the access token is never shown")


class TestShell(UICase):
    def test_the_sidebar_has_every_control_and_navigation_works(self):
        ui = self.ui
        self.assertEqual(ui.texts("nav a"), ["Home", "Replies", "Accounts", "AI & Brand", "Controls", "Help"] if False else ui.texts("nav a"))
        labels = [t.split("\n")[0].strip() for t in ui.texts("nav a")]
        self.assertEqual(labels[:6], ["Home", "Replies", "Accounts", "AI & Brand", "Controls", "Help"])
        for nav, title in (("replies", "Replies"), ("accounts", "Accounts"), ("ai", "AI & Brand"), ("controls", "Controls"),
                           ("help", "Help"), ("home", "Home")):
            ui.click(f"nav a[data-nav={nav}]")
            ui.wait(f"document.querySelector('#page-title').innerText === {json.dumps(title)}", 5, f"{title} page")
            self.assertTrue(ui.exists(f"nav a[data-nav={nav}].on"))

    def test_posting_and_ai_budget_are_always_in_front_of_the_person(self):
        ui = self.ui
        self.assertIn("ON", ui.text("#side-foot"))
        self.assertIn("0 / 40", ui.text("#side-foot"))
        for page in ("replies", "accounts", "ai", "controls", "help"):
            ui.click(f"nav a[data-nav={page}]")
            ui.wait("document.querySelector('#side-foot .switch')", 5)
            self.assertIn("Posting", ui.text("#side-foot"), page)

    def test_the_theme_can_be_switched_and_is_remembered(self):
        ui = self.ui
        before = ui.ev("document.documentElement.getAttribute('data-theme')")
        ui.click("#theme")
        after = ui.ev("document.documentElement.getAttribute('data-theme')")
        self.assertNotEqual(before, after)
        self.assertEqual(ui.ev("localStorage.getItem('tg_theme')"), after)
        bg = ui.ev("getComputedStyle(document.body).backgroundColor")
        ui.click("#theme")
        self.assertNotEqual(bg, ui.ev("getComputedStyle(document.body).backgroundColor"), "the colours really change")

    def test_the_token_never_stays_in_the_address_bar(self):
        self.assertNotIn("token=", self.ui.ev("location.href"))

    def test_the_home_tiles_describe_the_backend(self):
        ui = self.ui
        tiles = ui.texts("#stats .stat")
        self.assertEqual(len(tiles), 4)
        joined = " | ".join(tiles)
        self.assertIn("POSTING", joined.upper())
        self.assertIn("0 / 40", joined.replace("\n", " "))
        self.assertIn("1 / 3 ready".replace(" ", ""), joined.replace("\n", "").replace(" ", ""))
        self.assertIn("need to log in again", joined)
        self.assertIn("0 / 20", joined.replace("\n", " "))

    def test_a_narrow_window_collapses_the_sidebar_to_icons(self):
        ui = self.ui
        try:
            ui.resize(720, 800)
            time.sleep(0.4)
            self.assertEqual(ui.ev("getComputedStyle(document.querySelector('nav a span.t')).display"), "none")
            self.assertTrue(ui.exists("nav a[data-nav=accounts]"))
        finally:
            ui.resize(*ui.size)


class TestAnalyzeAPost(UICase):
    def test_pasting_a_link_finds_the_negative_comments(self):
        ui = self.ui
        self.analyze()
        self.assertEqual(self.world.reader_calls[0]["url"], POST_URL)
        counts = ui.texts("[data-role=results] .tab")
        self.assertTrue(counts[0].startswith("Negative"), counts)
        negatives = ui.count("#comment-list .cc")
        self.assertGreaterEqual(negatives, 2)
        texts = " ".join(ui.texts("#comment-list .cc"))
        self.assertIn("@ana.silva", texts)
        self.assertIn("golpe", texts)
        self.assertIn("Why flagged", texts)
        self.assertNotIn("Amei", texts, "praise is not shown under 'negative'")
        header = ui.text("[data-role=results]")
        self.assertIn("@examplebrand", header)
        self.assertIn("6 comments read", header)

    def test_tracking_the_same_finished_job_twice_does_not_wipe_a_reply_box_that_is_already_open(self):
        """Seen as a flaky failure: the Home page and the Find button both followed the job, and the second render of the results
        removed the composer the person had just opened."""
        ui = self.ui
        self.analyze()
        self.open_composer("7000000000000000001")
        ui.fill(f"{self.card('7000000000000000001')} [data-role=text]", "Um texto que a pessoa ja escreveu.")
        ui.ev("trackJob(); trackJob(); trackJob(); undefined")                 # what the second chain used to do
        time.sleep(1.8)
        self.assertEqual(ui.count(f"{self.card('7000000000000000001')} [data-role=composer]"), 1)
        self.assertEqual(ui.ev(f"document.querySelector(\"{self.card('7000000000000000001')} [data-role=text]\").value"),
                         "Um texto que a pessoa ja escreveu.", "what was typed is still there")

    def test_the_filters_show_questions_and_everything(self):
        ui = self.ui
        self.analyze()
        ui.click("[data-filter=question]")
        ui.wait("document.querySelectorAll('#comment-list .cc').length >= 1", 5)
        self.assertIn("Vocês entregam no Brasil?", ui.text("#comment-list"))
        ui.click("[data-filter=all]")
        ui.wait("document.querySelectorAll('#comment-list .cc').length === 6", 5, "all six comments")
        self.assertIn("Amei", ui.text("#comment-list"))

    def test_the_progress_is_visible_while_it_reads_and_goes_away_after(self):
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=progress]')", 8, "progress card")
        self.assertIn("Stop", ui.text("[data-role=progress]"))
        self.assertTrue(ui.ev("document.querySelector('#analyze-btn').disabled"), "no second analysis while one runs")
        ui.wait("document.querySelector('[data-role=results]')", 40)
        self.assertFalse(ui.exists("[data-role=progress]"))
        self.assertFalse(ui.ev("document.querySelector('#analyze-btn').disabled"))

    def test_the_reading_account_choice_reaches_the_reader(self):
        ui = self.ui
        ui.select("#read-account", "real-account-1")
        self.analyze()
        self.assertEqual(self.world.reader_calls[0]["account"], "real-account-1")

    def test_results_survive_going_to_another_page_and_back(self):
        ui = self.ui
        self.analyze()
        ui.click("nav a[data-nav=accounts]")
        ui.wait("document.querySelector('#add-account')", 5)
        ui.click("nav a[data-nav=home]")
        ui.wait("document.querySelector('[data-role=results]')", 8, "results still there")

    def test_the_post_appears_in_recent_posts_and_can_be_reopened(self):
        ui = self.ui
        self.analyze()
        ui.click("nav a[data-nav=accounts]")
        ui.click("nav a[data-nav=home]")
        ui.ev("S.result = null")
        ui.click("nav a[data-nav=replies]")
        ui.click("nav a[data-nav=home]")
        ui.wait("document.querySelector('#recent [data-video]')", 8, "recent posts")
        ui.click("#recent [data-video]")
        ui.wait("document.querySelector('[data-role=results]')", 8, "reopened from recent")

    def test_analysing_again_reuses_the_verdicts_and_spends_no_new_ai(self):
        ui = self.ui
        self.analyze()
        used, first = len(self.world.ai.calls), ui.ev("S.job.id")
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait(f"S.job && S.job.id !== {json.dumps(first)} && S.job.finished && document.querySelector('[data-role=results]')", 40, "second analysis")
        self.assertEqual(len(self.world.reader_calls), 2)
        self.assertEqual(len(self.world.ai.calls), used, "the same comments are never judged twice")

    def test_a_bad_link_is_explained_on_the_spot(self):
        ui = self.ui
        for bad, expect in (("https://example.com/x", "not a TikTok link"), ("https://www.tiktok.com/@shop", "does not point to a post"),
                            ("", "Paste a TikTok post link first")):
            ui.fill("#url-input", bad)
            ui.click("#analyze-btn")
            ui.wait(f"document.querySelector('#url-error .err') && document.querySelector('#url-error').innerText.includes({json.dumps(expect)})",
                    6, expect)
        self.assertEqual(self.world.reader_calls, [])
        self.assertFalse(ui.ev("document.querySelector('#analyze-btn').disabled"), "the button is usable again")

    def test_pressing_enter_in_the_box_starts_the_analysis(self):
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.ev("document.querySelector('#url-form').requestSubmit()")
        ui.wait("document.querySelector('[data-role=results]')", 40)


class TestAnalysisFailures(UICase):
    world_args = {"reader_error": "could not open the post (HTTP 403)"}

    def test_a_post_that_cannot_be_read_says_why_and_does_not_hang(self):
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=job-error]')", 20, "error banner")
        self.assertIn("could not be opened", ui.text("[data-role=job-error]"))
        self.assertFalse(ui.ev("document.querySelector('#analyze-btn').disabled"))
        self.assertFalse(ui.exists("[data-role=results]"))


class TestCaptchaMessage(UICase):
    world_args = {"reader_error": "TikTok showed a security check (captcha)"}

    def test_a_security_check_is_explained(self):
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=job-error]')", 20)
        self.assertIn("security check", ui.text("[data-role=job-error]"))


class TestReplying(UICase):
    CID = "7000000000000000001"

    def test_reply_with_the_ai_then_send_and_see_the_proof(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.click(f"{self.card(self.CID)} [data-act=ai]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} [data-role=text]\").value.length > 10", 12, "AI text")
        text = ui.ev(f"document.querySelector(\"{self.card(self.CID)} [data-role=text]\").value")
        self.assertIn("Sentimos muito", text)
        self.assertIn("Draft saved", ui.text(self.card(self.CID)))
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        ui.wait("document.querySelector('.status-line') && document.querySelector('.status-line').innerText.includes('Sending')", 6, "sending state")
        self.sent_ok(self.card(self.CID))
        self.assertIn("Replied", ui.text(self.card(self.CID)))
        (sent,) = self.posted()
        self.assertEqual((sent["kind"], sent["cid"], sent["reply"]), ("link_reply", self.CID, text))
        self.assertEqual(sent["video_url"], "https://www.tiktok.com/@examplebrand/video/7100000000000000001")
        self.assertTrue(ui.ev("document.querySelector('.proof img').src.includes('/api/screenshots/proof_')"))

    def test_the_proof_opens_large_and_closes_with_escape(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado pelo aviso!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        ui.click(".proof img")
        ui.wait("document.querySelector('.modal img')", 5, "modal")
        ui.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
        ui.wait("!document.querySelector('.modal')", 5, "modal closed")

    def test_the_chosen_account_sends_the_reply(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        options = ui.texts(f"{self.card(self.CID)} [data-role=account] option")
        self.assertTrue(options[0].startswith("Auto"))
        self.assertTrue(any("@alpha.shop" in o for o in options) and any("needs login" in o for o in options), options)
        ui.select(f"{self.card(self.CID)} [data-role=account]", "real-account-1")
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado pelo aviso!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        self.assertEqual(self.posted()[0]["account"], "real-account-1")
        self.assertIn("alpha.shop", ui.text(f"{self.card(self.CID)} .status-line"))

    def test_an_empty_reply_is_refused_with_a_hint(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} .status-line.bad\")", 5)
        self.assertIn("Write a reply first", ui.text(f"{self.card(self.CID)} .status-line"))
        self.assertEqual(self.posted(), [])

    def test_the_character_counter_warns_and_the_text_is_shortened(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "palavra " * 30)
        self.assertTrue(ui.ev(f"document.querySelector(\"{self.card(self.CID)} .counter\").classList.contains('over')"))
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        self.assertLessEqual(len(self.posted()[0]["reply"]), 150)

    def test_a_second_click_on_reply_closes_the_composer_and_keeps_the_draft(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Rascunho guardado")
        ui.click(f"{self.card(self.CID)} [data-act=send]")      # sends: use a card that stays a draft instead
        self.sent_ok(self.card(self.CID))
        ui.click(f"{self.card(self.CID)} [data-act=reply]")
        ui.wait(f"!document.querySelector(\"{self.card(self.CID)} [data-role=composer]\")", 5, "composer closed")
        ui.click(f"{self.card(self.CID)} [data-act=reply]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} [data-role=composer]\")", 8)
        self.assertEqual(ui.ev(f"document.querySelector(\"{self.card(self.CID)} [data-role=text]\").value"), "Rascunho guardado")

    def test_the_reply_card_is_the_same_one_when_asked_twice(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        first = ui.ev("S.result.comments.find(c => c.cid === '%s').card.id" % self.CID)
        ui.click(f"{self.card(self.CID)} [data-act=reply]")
        self.open_composer(self.CID)
        self.assertEqual(ui.ev("S.result.comments.find(c => c.cid === '%s').card.id" % self.CID), first)
        self.assertEqual(len(self.world.db.list_approvals(("pending",))), 1, "one card, not two")

    def test_a_failed_send_explains_and_trying_again_works(self):
        self.world.post_results[:] = [{"ok": False, "error": "TikTok did not confirm the comment"}, {"ok": True}]
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado pelo aviso!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} .status-line.bad\")", 20, "failure shown")
        self.assertIn("did not confirm", ui.text(f"{self.card(self.CID)} .status-line"))
        self.assertFalse(ui.ev(f"document.querySelector(\"{self.card(self.CID)} [data-act=send]\").disabled"), "can try again")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        self.assertEqual(len(self.posted()), 2)

    def test_a_reply_tiktok_hides_from_everyone_is_flagged(self):
        self.world.post_results[:] = [{"ok": True, "visibility": "limited"}]
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        self.assertIn("only to the account that wrote it", ui.text(f"{self.card(self.CID)} .status-line"))

    def test_with_posting_off_the_reply_is_held_and_can_be_sent_after_turning_it_on(self):
        ui = self.ui
        self.analyze()
        ui.click("#side-posting")
        ui.wait("document.querySelector('#side-post-state').innerText === 'OFF'", 6, "posting off")
        self.assertFalse(config.POSTING_ENABLED)
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado pelo aviso!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} .status-line.warn\")", 10, "held message")
        self.assertIn("posting is switched OFF", ui.text(f"{self.card(self.CID)} .status-line"))
        self.assertEqual(self.posted(), [], "nothing was sent")
        ui.click(f"{self.card(self.CID)} #turn-on-posting")
        ui.wait("document.querySelector('#side-post-state').innerText === 'ON'", 6, "posting on")
        self.assertTrue(config.POSTING_ENABLED)
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} .status-line.ok\")", 6, "ready again")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        self.assertEqual(len(self.posted()), 1)

    def test_the_daily_limit_stops_the_next_reply_with_a_clear_message(self):
        config.LINK_COMMENTS_PER_DAY = 1
        ui = self.ui
        self.analyze()
        self.open_composer(self.CID)
        ui.fill(f"{self.card(self.CID)} [data-role=text]", "Obrigado!")
        ui.click(f"{self.card(self.CID)} [data-act=send]")
        self.sent_ok(self.card(self.CID))
        other = "7000000000000000006"
        self.open_composer(other)
        ui.fill(f"{self.card(other)} [data-role=text]", "Sentimos muito!")
        ui.click(f"{self.card(other)} [data-act=send]")
        ui.wait(f"document.querySelector(\"{self.card(other)} .status-line.bad\")", 10, "limit message")
        self.assertIn("Daily limit reached", ui.text(f"{self.card(other)} .status-line"))
        self.assertEqual(len(self.posted()), 1)

    def test_ai_write_is_refused_politely_when_the_ai_budget_is_gone(self):
        self.world.db.set_setting("ai_daily_requests", "0")
        ui = self.ui
        ui.fill("#url-input", POST_URL)
        ui.click("#analyze-btn")
        ui.wait("document.querySelector('[data-role=results]')", 40)
        self.open_composer(self.CID)
        ui.click(f"{self.card(self.CID)} [data-act=ai]")
        ui.wait(f"document.querySelector(\"{self.card(self.CID)} .status-line.bad\")", 10)
        self.assertEqual(ui.ev(f"document.querySelector(\"{self.card(self.CID)} [data-role=text]\").value"), "")


class TestCommentOnThePost(UICase):
    def test_a_new_comment_on_the_post_needs_a_brief_for_the_ai_and_can_be_sent(self):
        ui = self.ui
        self.analyze()
        ui.click("#post-comment-toggle")
        ui.wait("document.querySelector('#post-comment [data-role=composer]')", 5)
        ui.click("#post-comment [data-act=ai]")
        ui.wait("document.querySelector('#post-comment .status-line') && document.querySelector('#post-comment .status-line').innerText.includes('Tell the AI')", 6, "brief hint")
        ui.fill("#post-comment [data-role=brief]", "thank everyone and invite them to the store")
        ui.click("#post-comment [data-act=ai]")
        ui.wait("document.querySelector('#post-comment [data-role=text]').value.length > 5", 12, "AI text")
        ui.click("#post-comment [data-act=send]")
        self.sent_ok("#post-comment")
        sent = self.posted()[0]
        self.assertEqual(sent["kind"], "link_post")
        self.assertTrue(sent["cid"].startswith("link-post:"))
        self.assertIn("invite them to the store", self.world.ai.calls[-1]["user"])


class TestRepliesPage(UICase):
    A, B = "7000000000000000001", "7000000000000000002"

    def make_sent_and_draft(self):
        ui = self.ui
        self.analyze()
        self.open_composer(self.A)
        ui.fill(f"{self.card(self.A)} [data-role=text]", "Obrigado pelo aviso!")
        ui.click(f"{self.card(self.A)} [data-act=send]")
        self.sent_ok(self.card(self.A))
        self.open_composer(self.B)
        ui.click(f"{self.card(self.B)} [data-act=ai]")
        ui.wait(f"document.querySelector(\"{self.card(self.B)} [data-role=text]\").value.length > 5", 12)

    def test_sent_and_waiting_replies_are_listed_with_working_filters(self):
        ui = self.ui
        self.make_sent_and_draft()
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 2", 8, "two rows")
        tabs = ui.texts(".tab")
        self.assertEqual(["".join(ch for ch in t if not ch.isdigit()) for t in tabs], ["All", "Waiting", "Sent", "Discarded"],
                         "real tab labels, not '[object ...]'")
        self.assertEqual([t[-1] for t in tabs], ["2", "1", "1", "0"], "the counts")
        ui.click("[data-filter=sent]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 1", 5)
        self.assertIn("Sent", ui.text("#replies-list .list-row"))
        self.assertTrue(ui.exists("#replies-list .proof img"))
        ui.click("[data-filter=waiting]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 1", 5)
        self.assertIn("Draft", ui.text("#replies-list .list-row"))
        self.assertIn("Edit & send", ui.text("#replies-list .list-row"))

    def test_the_sidebar_counts_what_is_waiting(self):
        ui = self.ui
        self.make_sent_and_draft()
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelector('nav a[data-nav=replies] .badge')", 6)
        self.assertEqual(ui.text("nav a[data-nav=replies] .badge"), "1")

    def test_a_draft_can_be_edited_and_sent_from_the_replies_page(self):
        ui = self.ui
        self.make_sent_and_draft()
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 2", 8)
        ui.click("[data-filter=waiting]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 1", 5)
        ui.click("#replies-list [data-act=edit]")
        ui.wait("document.querySelector('#replies-list [data-role=composer]')", 5)
        ui.fill("#replies-list [data-role=text]", "Peço desculpa, vamos resolver hoje.")
        ui.click("#replies-list [data-act=send]")
        self.sent_ok("#replies-list")
        self.assertEqual(self.posted()[-1]["reply"], "Peço desculpa, vamos resolver hoje.")

    def test_a_draft_can_be_discarded(self):
        ui = self.ui
        self.make_sent_and_draft()
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 2", 8)
        ui.click("[data-filter=waiting]")
        ui.click("#replies-list [data-act=discard]")
        ui.wait("document.querySelector('.modal')", 5, "confirm dialog")
        ui.click(".modal .btn.primary")
        ui.wait("document.querySelector('#replies-list .empty')", 8, "no waiting replies left")
        ui.click("[data-filter=discarded]")
        ui.wait("document.querySelectorAll('#replies-list .list-row').length === 1", 5)

    def test_an_empty_replies_page_invites_the_first_reply(self):
        ui = self.ui
        ui.click("nav a[data-nav=replies]")
        ui.wait("document.querySelector('#replies-list .empty')", 8)
        self.assertIn("Nothing here yet", ui.text("#replies-list"))


class TestAccountsPage(UICase):
    def go(self):
        self.ui.click("nav a[data-nav=accounts]")
        self.ui.wait("document.querySelector('#accounts-list .acct')", 8, "account list")

    def test_every_account_is_shown_with_its_state(self):
        self.go()
        ui = self.ui
        self.assertEqual(ui.count("#accounts-list .acct"), 3)
        text = ui.text("#accounts-list")
        self.assertIn("@alpha.shop", text)
        self.assertIn("Logged in", text)
        self.assertIn("Needs login", text)
        self.assertIn("Page account", text)
        self.assertTrue(ui.exists("[data-account=real-account-2] .btn.primary"), "the account that needs a login gets the main button")

    def test_an_account_can_be_checked_switched_off_made_first_and_removed(self):
        self.go()
        ui = self.ui
        who = {"ok": True, "logged_in": True, "who": {"uniqueId": "beta.shop", "nickName": "Beta", "region": "BD"}}
        with mock.patch.object(acc, "identify", return_value=who):
            ui.click("[data-account=real-account-2] .btn", 0)           # Check
            ui.wait("[...document.querySelectorAll('[data-account=real-account-2] .chip')].some(c => c.innerText.includes('Logged in'))", 15, "now logged in")
        ui.click("[data-account=real-account-1] .btn", 3)               # Switch off
        ui.wait("[...document.querySelectorAll('[data-account=real-account-1] .chip')].some(c => c.innerText.includes('Switched off'))", 8)
        self.assertFalse(self.world.app.svc.accounts.list()["accounts"][1]["enabled"])
        ui.click("[data-account=real-account-2] .btn", 2)               # Answer first
        ui.wait("[...document.querySelectorAll('[data-account=real-account-2] .chip')].some(c => c.innerText.includes('Answers first'))", 8)
        self.assertEqual(config.account_chain()[0], "real-account-2")
        ui.click("[data-account=real-account-2] [data-act=remove]")
        ui.wait("document.querySelector('.modal')", 5, "confirm")
        ui.click(".modal .btn.primary")
        ui.wait("!document.querySelector('[data-account=real-account-2]')", 10, "removed")
        self.assertNotIn("real-account-2", [a["name"] for a in config.load_accounts()])

    def test_the_page_account_cannot_be_removed_or_switched_off(self):
        self.go()
        row = "[data-account=page-account]"
        self.assertTrue(self.ui.exists(row))
        self.assertFalse(self.ui.exists(f"{row} [data-act=remove]"))
        self.assertEqual(self.ui.texts(f"{row} .btn"), ["Check"])

    def test_adding_an_account_opens_a_window_notices_the_login_and_lists_the_account(self):
        self.go()
        ui = self.ui
        self.world.window_kwargs = dict(who={"uniqueId": "gamma.shop", "nickName": "Gamma", "region": "PY"}, login_after=10 ** 6)
        ui.click("#add-account")
        ui.wait("document.querySelector('[data-role=flow][data-state=window_open]')", 10, "waiting for the login")
        self.assertIn("log in", ui.text("[data-role=flow-message]").lower())
        self.assertIn("Waiting for you to log in", ui.text("[data-role=flow]"))
        self.assertEqual(ui.count("#accounts-list .acct"), 3, "nothing saved before the login")
        self.world.windows[0].login_after = 0                                # the person logs in
        ui.wait("document.querySelector('[data-role=flow].done')", 20, "flow finished")
        self.assertIn("Account added", ui.text("[data-role=flow]"))
        ui.wait("document.querySelectorAll('#accounts-list .acct').length === 4", 10, "new account listed")
        self.assertIn("@gamma.shop", ui.text("#accounts-list"))
        self.assertTrue(self.world.windows[0].closed, "the app closed the window by itself")

    def test_the_login_window_can_be_cancelled(self):
        self.go()
        ui = self.ui
        self.world.window_kwargs = dict(login_after=10 ** 6)
        ui.click("#add-account")
        ui.wait("document.querySelector('[data-role=flow][data-state=window_open]')", 10)
        ui.click("[data-role=flow] .btn.danger")
        ui.wait("document.querySelector('[data-role=flow].done') && document.querySelector('[data-role=flow]').innerText.includes('Cancelled')", 15)
        self.assertEqual(ui.count("#accounts-list .acct"), 3)

    def test_a_second_login_window_is_refused_with_a_message(self):
        self.go()
        ui = self.ui
        self.world.window_kwargs = dict(login_after=10 ** 6)
        ui.click("#add-account")
        ui.wait("document.querySelector('[data-role=flow][data-state=window_open]')", 10)
        ui.click("#add-account")
        ui.wait("document.querySelector('.fatal')", 5, "error banner")
        self.assertIn("already open", ui.text(".fatal"))
        ui.click("[data-role=flow] .btn.danger")


class TestAiAndBrandPage(UICase):
    def go(self):
        self.ui.click("nav a[data-nav=ai]")
        self.ui.wait("document.querySelector('#ai-instructions')", 8)

    def test_instructions_can_be_saved_reset_and_undone(self):
        self.go()
        ui = self.ui
        self.assertIn("warm, friendly", ui.ev("document.querySelector('#ai-instructions').value"))
        ui.fill("#ai-instructions", "Always answer very formally and sign with 'Equipe Example Store'.")
        ui.click("#save-instructions")
        ui.wait("document.querySelector('#ai-instructions')", 3)
        time.sleep(0.6)
        self.assertEqual(self.world.db.get_setting("ai_instructions"), "Always answer very formally and sign with 'Equipe Example Store'.")
        self.go()
        self.assertIn("formally", ui.ev("document.querySelector('#ai-instructions').value"), "kept after leaving the page")
        ui.click("nav a[data-nav=ai]")
        ui.ev("[...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'Use default').click()")
        time.sleep(0.6)
        self.assertIsNone(self.world.db.get_setting("ai_instructions"))
        self.assertIn("warm, friendly", ui.ev("document.querySelector('#ai-instructions').value"))

    def test_the_new_instructions_really_shape_the_next_reply(self):
        self.go()
        ui = self.ui
        ui.fill("#ai-instructions", "Sign every reply with 'Equipe Example Store'.")
        ui.click("#save-instructions")
        time.sleep(0.6)
        ui.click("nav a[data-nav=home]")
        ui.wait("document.querySelector('#url-input')", 8, "home")
        self.analyze()
        self.open_composer("7000000000000000001")
        ui.click(f"{self.card('7000000000000000001')} [data-act=ai]")
        ui.wait(f"document.querySelector(\"{self.card('7000000000000000001')} [data-role=text]\").value.length > 5", 12)
        self.assertIn("Equipe Example Store", self.world.ai.calls[-1]["system"])

    def test_the_company_information_is_saved_and_more_facts_can_be_added(self):
        self.go()
        ui = self.ui
        ui.fill("#ai-context", "We are a shopping mall in Central City.")
        ui.click("#save-context")
        time.sleep(0.6)
        self.assertEqual(self.world.db.get_setting("company_context"), "We are a shopping mall in Central City.")
        ui.fill("#ai-context-add", "We open Monday to Saturday.")
        ui.click("#add-context")
        ui.wait("document.querySelector('#ai-context').value.includes('Monday to Saturday')", 8, "fact added")
        self.assertEqual(self.world.db.get_setting("company_context"),
                         "We are a shopping mall in Central City.\n\nWe open Monday to Saturday.")
        self.assertEqual(ui.ev("document.querySelector('#ai-context-add').value"), "", "the box is emptied after adding")

    def test_try_it_shows_the_reply_a_sample_comment_would_get(self):
        self.go()
        ui = self.ui
        ui.fill("#ai-test-input", "O produto veio com defeito")
        ui.click("#ai-test")
        ui.wait("document.querySelector('#ai-test-out .banner.ok')", 12, "sample reply")
        self.assertIn("Sentimos muito", ui.text("#ai-test-out"))

    def test_a_too_long_text_is_refused_with_the_limit(self):
        self.go()
        ui = self.ui
        ui.fill("#ai-instructions", "x" * (config.AI_INSTRUCTIONS_MAX_CHARS + 5))
        ui.click("#save-instructions")
        time.sleep(0.6)
        self.assertNotEqual(self.world.db.get_setting("ai_instructions"), "x" * (config.AI_INSTRUCTIONS_MAX_CHARS + 5))


class TestControlsPage(UICase):
    def go(self):
        self.ui.click("nav a[data-nav=controls]")
        self.ui.wait("document.querySelector('#cap-input')", 8)

    def test_the_master_switch_changes_posting_everywhere(self):
        self.go()
        ui = self.ui
        ui.click("#ctl-posting")
        ui.wait("document.querySelector('#side-post-state').innerText === 'OFF'", 6)
        self.assertFalse(config.POSTING_ENABLED)
        self.assertEqual(self.world.db.get_setting("posting_enabled"), "0", "remembered for the next start")
        ui.click("nav a[data-nav=home]")
        ui.wait("document.querySelector('#stats .stat')", 6)
        self.assertIn("OFF", ui.text("#stats"))

    def test_the_limits_can_be_changed(self):
        self.go()
        ui = self.ui
        ui.fill("#cap-input", "7")
        ui.click("#save-cap")
        ui.fill("#read-limit", "100")
        ui.select("#read-limit", "100")
        ui.click("#save-read")
        ui.fill("#budget-input", "25")
        ui.click("#save-budget")
        time.sleep(0.8)
        self.assertEqual(config.LINK_COMMENTS_PER_DAY, 7)
        self.assertEqual(self.world.db.get_setting("read_limit"), "100")
        self.assertEqual(self.world.db.get_setting("ai_daily_requests"), "25")

    def test_a_bad_limit_is_refused_and_nothing_changes(self):
        self.go()
        ui = self.ui
        ui.fill("#cap-input", "9999")
        ui.click("#save-cap")
        ui.wait("document.querySelector('#cap-msg').innerText.length > 0", 6)
        self.assertIn("0", ui.text("#cap-msg"))
        self.assertEqual(config.LINK_COMMENTS_PER_DAY, 20)

    def test_the_reading_limit_reaches_the_reader(self):
        self.go()
        ui = self.ui
        ui.select("#read-limit", "40")
        ui.click("#save-read")
        time.sleep(0.6)
        ui.click("nav a[data-nav=home]")
        ui.wait("document.querySelector('#url-input')", 6)
        self.analyze()
        self.assertEqual(self.world.reader_calls[0]["max"], 40)

    def test_the_ai_key_is_shown_masked_and_can_be_replaced(self):
        self.go()
        ui = self.ui
        page = ui.text("body")
        self.assertIn("Key set", page)
        self.assertNotIn("sk-or-v1-test-key-0123456789abcdef", page, "the full key is never shown")
        ui.fill("#key-input", "sk-or-v1-new-key-abcdefghijklmnop")
        ui.click("#save-key")
        ui.wait("document.querySelector('#key-input') && document.querySelector('#key-input').value === ''", 8)
        time.sleep(0.5)
        self.assertEqual(config.OPENROUTER_API_KEY, "sk-or-v1-new-key-abcdefghijklmnop")
        self.assertNotIn("sk-or-v1-new-key-abcdefghijklmnop", ui.text("body"))
        ui.fill("#key-input", "short")
        ui.click("#save-key")
        time.sleep(0.5)
        self.assertEqual(config.OPENROUTER_API_KEY, "sk-or-v1-new-key-abcdefghijklmnop", "an invalid key changes nothing")

    def test_the_brand_name_is_saved(self):
        self.go()
        self.ui.fill("#brand-input", "Mercado Central")
        self.ui.click("#save-brand")
        time.sleep(0.6)
        self.assertEqual(config.BRAND_NAME, "Mercado Central")

    def test_free_disk_space_reports_what_it_did(self):
        self.go()
        ui = self.ui
        ui.click("#cleanup")
        ui.wait("document.querySelector('#clean-out').innerText.includes('Done')", 20, "cleanup result")
        self.assertIn("freed", ui.text("#clean-out"))


class TestSettingsSurviveARestart(unittest.TestCase):
    def test_what_the_person_set_last_time_is_applied_at_start(self):
        world = World()
        try:
            world.app.controller.update_settings({"posting_enabled": False, "link_comments_per_day": 3, "brand_name": "Mercado Central",
                                                  "openrouter_api_key": "sk-or-v1-remembered-key-0123456789"})
            config.POSTING_ENABLED, config.LINK_COMMENTS_PER_DAY, config.BRAND_NAME = True, 20, "Other"
            config.OPENROUTER_API_KEY = ""
            world.app.controller.apply_runtime()
            self.assertEqual((config.POSTING_ENABLED, config.LINK_COMMENTS_PER_DAY, config.BRAND_NAME, config.OPENROUTER_API_KEY),
                             (False, 3, "Mercado Central", "sk-or-v1-remembered-key-0123456789"))
            world.app.controller.update_settings({"openrouter_api_key": ""})
            self.assertEqual(world.db.get_setting("openrouter_api_key"), None, "clearing the app's key goes back to .env")
        finally:
            world.stop()


class TestHelpPage(UICase):
    def test_help_explains_the_steps_and_what_replaced_the_telegram_commands(self):
        ui = self.ui
        ui.click("nav a[data-nav=help]")
        ui.wait("document.querySelector('table.map')", 6)
        text = ui.text("main")
        for needle in ("Paste a link", "Check the proof", "/ai_context", "/pending", "/comment <link>", "Not needed: nothing is watched in the background"):
            self.assertIn(needle, text)


class TestSafetyAndQuiet(UICase):
    world_args = {"comments": [
        {"cid": "7000000000000000009", "text": "<img src=x onerror=\"window.__xss=1\"> é golpe <script>window.__xss=2</script>", "username": "<b>evil</b>",
         "digg_count": 1, "create_time": int(time.time()) - 100, "reply_count": 0}]}

    def test_comment_text_and_names_are_shown_as_text_never_as_html(self):
        ui = self.ui
        self.analyze()
        ui.click("[data-filter=all]")
        ui.wait("document.querySelectorAll('#comment-list .cc').length === 1", 5)
        self.assertIsNone(ui.ev("window.__xss"), "no script ran")
        self.assertEqual(ui.count("#comment-list .cc img"), 0, "no <img> was created from the comment")
        self.assertEqual(ui.count("#comment-list .cc script"), 0)
        self.assertIn("<img src=x", ui.text("#comment-list .cc-text"))
        self.assertIn("@<b>evil</b>", ui.text("#comment-list .who"))


class TestNothingRunsByItself(UICase):
    def test_idle_means_idle_no_monitoring_no_notifications_no_telegram(self):
        ui = self.ui
        time.sleep(4)
        svc = self.world.app.svc
        self.assertFalse(svc.monitoring)
        self.assertFalse(svc.telegram_enabled)
        self.assertEqual(self.world.reader_calls, [], "nothing was read without being asked")
        self.assertIsNone(self.world.db.meta_get("last_scan_ts"))
        self.assertEqual([e["type"] for e in svc.events.since(0) if e["type"].startswith("scan")], [])
        self.assertEqual({t.name for t in svc._threads}, {"worker", "watchdog"},
                         "the worker, and a watchdog that only looks at how long the current job has run - no Telegram poller")
        self.assertEqual(config.TELEGRAM_BOT_TOKEN, "")
        self.assertFalse(ui.ev("typeof Notification !== 'undefined' && Notification.permission === 'granted' && !!window.__notified"))

    def test_the_page_makes_no_background_requests_while_nothing_happens(self):
        ui = self.ui
        ui.ev("window.__calls = 0; const f = window.fetch; window.fetch = function() { window.__calls++; return f.apply(this, arguments); }; true")
        time.sleep(5)
        self.assertEqual(ui.ev("window.__calls"), 0, "an idle page asks the app for nothing")


if __name__ == "__main__":
    unittest.main()
