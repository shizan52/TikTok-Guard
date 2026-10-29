"""/comment <link>: the Telegram flow, the cards, the daily cap, storage, the service job and the posting engine's
decisions - all offline (no Telegram, no OpenRouter, no browser)."""
import contextlib
import json
import pathlib
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

from _helpers import FakeSvc, FakeTG, make_ai, tmp_db  # noqa: F401
from test_bot import CHAT, BotCase, cb, msg
from test_service import ServiceCase

import config
from app import links, notifier
from app import tiktok_monitor as tm
from app.bot import Bot
from app.db import DB, utc_ago, utcnow
from app.service import Service

VID = "7695930429047688468"
CID = "7234567890123456789"
POST = f"https://www.tiktok.com/@shop/video/{VID}"
COMMENT_LINK = f"{POST}?comment_id={CID}"


def flat(markup) -> list:
    return [b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row]


class LinkCase(BotCase):
    def setUp(self):
        super().setUp()
        self._saved_link = (config.account_chain, config.load_accounts, config.LINK_COMMENTS_PER_DAY)
        config.account_chain = lambda: ["master", "acc2"]
        config.load_accounts = lambda: [{"name": "acc2", "username": "second_shop", "user_data_dir": "x"}]
        config.LINK_COMMENTS_PER_DAY = 10

    def tearDown(self):
        config.account_chain, config.load_accounts, config.LINK_COMMENTS_PER_DAY = self._saved_link
        super().tearDown()

    def latest(self) -> dict:
        row = self.db.conn().execute("SELECT * FROM approvals ORDER BY rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def offer(self, text=POST):
        self.send(f"/comment {text}")
        return self.latest()

    def card_text(self) -> str:
        a = self.latest()
        return notifier.format_card(self.db, a, "en")

    def mark_posted(self, cid, video="v9", hours_ago=0, account="master", kind="link_post"):
        aid = self.db.create_approval({"id": f"p{cid}"[:10], "cid": cid, "video_id": video, "video_url": POST, "status": "pending",
                                       "kind": kind, "reply": "x"})
        self.db.update_approval(aid, status="posted", posted_by=account,
                                posted_at=utc_ago(hours=hours_ago) if hours_ago else utcnow())
        return aid


class TestOfferingTheCard(LinkCase):
    def test_a_link_alone_gives_a_card_with_no_post_button_yet(self):
        a = self.offer()
        self.assertEqual((a["kind"], a["cid"], a["video_id"], a["status"]), ("link_post", f"link-post:{VID}", VID, "pending"))
        self.assertEqual(a["video_url"], POST)
        self.assertIsNone(a["reply"])
        text = self.tg.sent[-1]["text"]
        self.assertIn("Comment on a TikTok link", text)
        self.assertIn("New comment on a post", text)
        self.assertIn("No text yet", text)
        self.assertEqual(flat(self.tg.sent[-1]["markup"]), [f"ap:edit:{a['id']}", f"ap:ai:{a['id']}",
                                                            f"ap:acct:{a['id']}", f"ap:ignore:{a['id']}"])
        self.assertEqual(self.db.get_approval(a["id"])["message_id"], self.tg.sent[-1]["id"], "the card remembers its message")

    def test_a_link_with_text_gives_a_card_that_can_be_posted(self):
        a = self.offer(f"{POST} Great video, thank you!")
        self.assertEqual(a["reply"], "Great video, thank you!")
        self.assertIn(f"ap:post:{a['id']}", flat(self.tg.sent[-1]["markup"]))
        self.assertIn("Great video, thank you!", self.tg.sent[-1]["text"])
        self.assertIn(f"({len('Great video, thank you!')}/150)", self.tg.sent[-1]["text"], "the character counter")

    def test_text_before_the_link_works_too(self):
        self.assertEqual(self.offer(f"Great video! {POST}")["reply"], "Great video!")

    def test_a_text_over_150_characters_is_shortened_and_the_operator_is_told(self):
        long = "Thank you so much for this amazing video, we love it! " * 5
        a = self.offer(f"{POST} {long}")
        self.assertLessEqual(len(a["reply"]), 150)
        self.assertTrue(any("TikTok allows 150" in m["text"] for m in self.tg.sent))

    def test_pasting_a_bare_link_is_enough(self):
        self.send(POST)
        a = self.latest()
        self.assertEqual((a["kind"], a["video_id"]), ("link_post", VID))

    def test_chat_without_a_link_and_without_a_conversation_stays_quiet(self):
        before = len(self.tg.sent)
        self.send("hello there")
        self.assertEqual(len(self.tg.sent), before)
        self.assertIsNone(self.latest())

    def test_the_command_alone_asks_for_the_link_and_takes_the_next_message(self):
        self.send("/comment")
        self.assertIn("Send the TikTok link", self.tg.last_text())
        self.assertEqual(self.db.get_chat_state(CHAT)["mode"], "await_link")
        self.send(f"{POST} nice one")
        self.assertEqual(self.latest()["reply"], "nice one")
        self.assertIsNone(self.db.get_chat_state(CHAT), "the conversation is over")

    def test_the_command_with_no_link_explains_how_to_get_one(self):
        self.send("/comment great video")
        self.assertIn("could not find a link", self.tg.last_text())
        self.assertIn("Share", self.tg.last_text())
        self.assertIsNone(self.latest())

    def test_a_comment_link_makes_a_reply_card(self):
        a = self.offer(COMMENT_LINK + " Thanks!")
        self.assertEqual((a["kind"], a["cid"]), ("link_reply", CID))
        self.assertIn("Reply to a comment", self.tg.sent[-1]["text"])

    def test_a_short_link_is_expanded_in_the_background(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return 301, COMMENT_LINK
        self.bot.link_fetch = fetch
        self.send("/comment https://vm.tiktok.com/ZMabc123/ lovely")
        self.assertEqual(calls, ["https://vm.tiktok.com/ZMabc123/"])
        self.assertIn("Checking the link", self.tg.sent[-2]["text"])
        a = self.latest()
        self.assertEqual((a["kind"], a["cid"], a["reply"]), ("link_reply", CID, "lovely"))

    def test_bad_links_get_a_clear_message_and_no_card(self):
        self.bot.link_fetch = lambda url: (0, None)
        cases = {"https://example.com/watch?v=1": "not a TikTok link",
                 "https://www.tiktok.com/@shop": "does not point to a post or a comment",
                 "https://vm.tiktok.com/dead/": "could not open that link"}
        for link, expect in cases.items():
            self.send(f"/comment {link}")
            self.assertIn(expect, self.tg.last_text(), link)
        self.assertIsNone(self.latest())

    def test_nothing_is_obeyed_from_another_chat(self):
        self.bot.handle_update(msg(f"/comment {POST} hi", chat=999))
        self.bot.handle_update(msg(POST, chat=999))
        self.assertIsNone(self.latest())
        self.assertIn("private", self.tg.last_text())

    def test_one_open_card_per_target_until_it_is_closed(self):
        a = self.offer()
        self.send(f"/comment {POST}")
        self.assertIn("already has an open card", self.tg.last_text())
        self.assertEqual(self.db.conn().execute("SELECT COUNT(*) n FROM approvals").fetchone()["n"], 1)
        self.press(f"ap:ignore:{a['id']}")
        self.send(f"/comment {POST}")
        self.assertEqual(self.db.conn().execute("SELECT COUNT(*) n FROM approvals").fetchone()["n"], 2)

    def test_a_comment_link_and_a_post_link_do_not_block_each_other(self):
        self.offer(POST)
        self.offer(COMMENT_LINK)
        self.assertEqual(self.db.conn().execute("SELECT COUNT(*) n FROM approvals").fetchone()["n"], 2)

    def test_a_card_warns_when_the_target_was_already_commented_today(self):
        self.mark_posted(f"link-post:{VID}", video=VID, account="acc2")
        self.offer(POST)
        self.assertIn("Already posted here", self.tg.sent[-1]["text"])
        self.assertIn("@second_shop", self.tg.sent[-1]["text"])

    def test_an_old_comment_on_the_target_is_not_a_warning(self):
        self.mark_posted(f"link-post:{VID}", video=VID, hours_ago=30)
        self.offer(POST)
        self.assertNotIn("Already posted here", self.tg.sent[-1]["text"])


class TestTheButtons(LinkCase):
    def test_write_by_hand(self):
        a = self.offer()
        self.press(f"ap:edit:{a['id']}")
        self.assertEqual(self.db.get_chat_state(CHAT)["mode"], "await_reply_edit")
        self.assertIn("Send the comment text", self.tg.last_text())
        self.send("Obrigado por compartilhar!")
        done = self.db.get_approval(a["id"])
        self.assertEqual(done["reply"], "Obrigado por compartilhar!")
        self.assertIn("Comment text updated", self.tg.last_text())
        self.assertIn("Obrigado por compartilhar!", self.tg.edits[-1]["text"], "the card itself is refreshed")
        self.assertIn(f"ap:post:{a['id']}", flat(self.tg.edits[-1]["markup"]), "now it can be posted")

    def test_ai_writes_from_a_brief(self):
        a = self.offer()
        self.press(f"ap:ai:{a['id']}")
        self.assertEqual(self.db.get_chat_state(CHAT)["mode"], "await_link_brief")
        self.assertIn("what should the comment say", self.tg.last_text())
        self.send("thank them and invite them to our store")
        call = self.ai.calls[-1]
        self.assertIn("thank them and invite them to our store", call["user"])
        self.assertIn("NEW comment under someone's TikTok post", call["system"])
        self.assertIn(config.BRAND_NAME, call["system"])
        done = self.db.get_approval(a["id"])
        self.assertEqual(done["reply"], "Sorry about that! Please DM us your order number.")
        self.assertIn(f"ap:post:{a['id']}", flat(self.tg.edits[-1]["markup"]))
        self.assertIsNone(self.db.get_chat_state(CHAT))

    def test_ai_knows_when_it_is_answering_a_comment_and_avoids_repeating_itself(self):
        a = self.offer(COMMENT_LINK)
        self.press(f"ap:ai:{a['id']}")
        self.send("agree with them")
        self.assertIn("replying to another user's comment", self.ai.calls[-1]["system"])
        first = self.db.get_approval(a["id"])["reply"]
        self.press(f"ap:ai:{a['id']}")
        self.send("say it differently")
        self.assertIn(f"DIFFERENT comment than this one: {first}", self.ai.calls[-1]["user"])

    def test_ai_uses_the_owners_style_and_company_info(self):
        self.send("/ai Always sign with 'Equipe Example Store'.")
        self.send("/ai_context We are a mall in Central City. Open Monday to Saturday.")
        a = self.offer()
        self.press(f"ap:ai:{a['id']}")
        self.send("invite them to the mall, open hours")
        system = self.ai.calls[-1]["system"]
        self.assertIn("Equipe Example Store", system)
        self.assertIn("Central City", system)

    def test_ai_with_no_budget_left_says_so_and_costs_nothing(self):
        self.db.set_setting("ai_daily_requests", "0")
        a = self.offer()
        self.press(f"ap:ai:{a['id']}")
        self.send("thank them")
        self.assertIn("AI budget for today is used up", self.tg.last_text())
        self.assertEqual(self.ai.calls, [])
        self.assertIsNone(self.db.get_approval(a["id"])["reply"])

    def test_an_ai_failure_is_reported_and_the_card_survives(self):
        bad = make_ai(self.db, [{"content": None, "http": 500, "error": "boom"}])
        self.bot = Bot(self.db, bad, self.tg, self.svc)
        a = self.offer()
        self.press(f"ap:ai:{a['id']}")
        self.send("thank them")
        self.assertIn("Could not write the comment", self.tg.last_text())
        self.assertEqual(self.db.get_approval(a["id"])["status"], "pending")

    def test_the_account_button_cycles_auto_then_each_account_then_auto(self):
        a = self.offer(f"{POST} hi")
        seen = []
        for _ in range(3):
            self.press(f"ap:acct:{a['id']}")
            seen.append(self.db.get_approval(a["id"])["account"])
        self.assertEqual(seen, ["master", "acc2", None])
        self.press(f"ap:acct:{a['id']}")
        self.press(f"ap:acct:{a['id']}")
        self.assertIn("@second_shop", self.tg.edits[-1]["text"], "a registered account shows its @handle")
        self.assertIn("@second_shop", self.tg.edits[-1]["markup"]["inline_keyboard"][-1][0]["text"])
        self.assertIn("second_shop", self.tg.answers[-1]["text"])

    def test_an_account_that_left_the_chain_resets_to_auto(self):
        a = self.offer(f"{POST} hi")
        self.db.update_approval(a["id"], account="vanished")
        self.press(f"ap:acct:{a['id']}")
        self.assertIsNone(self.db.get_approval(a["id"])["account"])

    def test_cancel(self):
        a = self.offer(f"{POST} hi")
        self.press(f"ap:ignore:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "rejected")
        self.assertIn("Cancelled", self.tg.edits[-1]["text"])
        self.assertIsNone(self.tg.edits[-1]["markup"], "a closed card has no buttons")

    def test_post_queues_the_job_once(self):
        a = self.offer(f"{POST} hi")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "posting")
        self.assertEqual(self.db.get_approval(a["id"])["post_attempts"], 1)
        self.assertEqual(self.svc.posts, [a["id"]])
        self.press(f"ap:post:{a['id']}")                       # a second tap on a card that is already posting
        self.assertEqual(self.svc.posts, [a["id"]])
        self.assertIn("Already handled", self.tg.answers[-1]["text"])

    def test_post_with_posting_disabled_only_marks_it_ready(self):
        config.POSTING_ENABLED = False
        a = self.offer(f"{POST} hi")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "approved")
        self.assertEqual(self.svc.posts, [])
        self.assertIn("POSTING_ENABLED", self.tg.edits[-1]["text"])

    def test_post_without_text_is_refused(self):
        a = self.offer()
        self.bot.handle_update(cb(f"ap:post:{a['id']}"))
        self.assertEqual(self.db.get_approval(a["id"])["status"], "pending")
        self.assertEqual(self.svc.posts, [])
        self.assertTrue(self.tg.answers[-1]["alert"])
        self.assertIn("Write the comment first", self.tg.answers[-1]["text"])

    def test_a_failed_post_can_be_retried(self):
        a = self.offer(f"{POST} hi")
        self.db.update_approval(a["id"], status="post_failed", last_error="TikTok never answered")
        self.assertIn("TikTok never answered", self.card_text())
        self.assertIn("try again", self.card_text())
        self.assertIn(f"ap:post:{a['id']}", flat(notifier.card_keyboard(self.db.get_approval(a["id"]), "en")))
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.svc.posts, [a["id"]])

    def test_buttons_of_a_closed_card_do_nothing(self):
        a = self.offer(f"{POST} hi")
        self.press(f"ap:ignore:{a['id']}")
        for action in ("post", "edit", "ai", "acct"):
            self.press(f"ap:{action}:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "rejected")
        self.assertEqual(self.svc.posts, [])

    def test_other_chats_cannot_press_the_buttons(self):
        a = self.offer(f"{POST} hi")
        self.bot.handle_update(cb(f"ap:post:{a['id']}", chat=999))
        self.assertEqual(self.db.get_approval(a["id"])["status"], "pending")
        self.assertEqual(self.svc.posts, [])


class TestTheDailyCap(LinkCase):
    def test_the_cap_blocks_the_next_comment_and_explains_why(self):
        config.LINK_COMMENTS_PER_DAY = 2
        self.mark_posted("c-a", video="va")
        self.mark_posted("c-b", video="vb")
        a = self.offer(f"{POST} hi")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "pending")
        self.assertEqual(self.svc.posts, [])
        self.assertTrue(self.tg.answers[-1]["alert"])
        self.assertIn("Daily limit reached", self.tg.answers[-1]["text"])

    def test_below_the_cap_it_goes_through(self):
        config.LINK_COMMENTS_PER_DAY = 3
        self.mark_posted("c-a", video="va")
        a = self.offer(f"{POST} hi")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.svc.posts, [a["id"]])

    def test_zero_means_no_limit(self):
        config.LINK_COMMENTS_PER_DAY = 0
        for i in range(15):
            self.mark_posted(f"c-{i}", video=f"v{i}")
        a = self.offer(f"{POST} hi")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.svc.posts, [a["id"]])

    def test_only_todays_link_comments_count(self):
        self.mark_posted("c-old", video="v1", hours_ago=60)                       # two days ago
        self.mark_posted("c-alert", video="v2", kind="alert")                     # an ordinary reply to a comment
        self.assertEqual(self.db.link_posts_today(), 0)
        self.mark_posted("c-today", video="v3")
        aid = self.mark_posted("c-now", video="v4")
        self.db.update_approval(aid, status="posting")                            # being sent counts as well
        self.assertEqual(self.db.link_posts_today(), 2)


class TestAlongsideTheAlerts(LinkCase):
    def test_pending_lists_link_cards_next_to_alerts(self):
        self.make_negative()
        self.offer(f"{POST} lovely video")
        self.offer(COMMENT_LINK)
        text = self.send("/pending")
        self.assertIn("\U0001F517 post", text)
        self.assertIn("\U0001F517 reply", text)
        self.assertIn("lovely video", text)
        self.assertIn("@shop", text)
        self.assertIn("accuses", self.db.list_approvals(("pending",))[-1]["reason"] or "accuses")   # alert card untouched

    def test_the_returning_ai_budget_never_drafts_for_a_link_card(self):
        self.offer(POST)                                                          # a link card with no text yet
        done = notifier.fill_missing_replies(self.db, self.ai, self.tg, "en")
        self.assertEqual(done, 0)
        self.assertEqual(self.ai.calls, [], "a link comment waits for the operator's words, never for the AI budget")

    def test_alert_cards_still_default_to_kind_alert_and_the_old_buttons(self):
        _out, a = self.make_negative()
        self.assertEqual((a["kind"], a["account"]), ("alert", None))
        self.assertEqual(flat(notifier.card_keyboard(a, "en")),
                         [f"ap:post:{a['id']}", f"ap:edit:{a['id']}", f"ap:regen:{a['id']}", f"ap:ignore:{a['id']}"])

    def test_an_alert_card_cannot_be_driven_by_the_link_buttons(self):
        _out, a = self.make_negative()
        self.press(f"ap:acct:{a['id']}")
        self.assertIsNone(self.db.get_approval(a["id"])["account"])
        self.assertIn("Unknown approval", self.tg.answers[-1]["text"])


class TestStorage(unittest.TestCase):
    def test_an_older_database_gets_the_new_columns_and_keeps_its_rows(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""CREATE TABLE approvals (id TEXT PRIMARY KEY, cid TEXT NOT NULL, video_id TEXT NOT NULL, video_url TEXT,
            username TEXT, comment_text TEXT, lang TEXT, category TEXT, severity INTEGER, reason TEXT, reply TEXT, status TEXT NOT NULL,
            chat_id TEXT, message_id INTEGER, simulated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            posted_at TEXT, post_attempts INTEGER NOT NULL DEFAULT 0, regen_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            posted_by TEXT, posted_reply_cid TEXT, posted_visibility TEXT);
            INSERT INTO approvals(id,cid,video_id,status,created_at,updated_at) VALUES('a','c','v','posted','t','t');""")
        con.commit()
        con.close()
        db = DB(path)
        cols = [r[1] for r in db.conn().execute("PRAGMA table_info(approvals)")]
        self.assertIn("kind", cols)
        self.assertIn("account", cols)
        old = db.get_approval("a")
        self.assertEqual((old["status"], old["kind"], old["account"]), ("posted", "alert", None))
        self.assertEqual(db.create_approval({"id": "n", "cid": "c2", "video_id": "v", "status": "pending", "kind": "link_post",
                                             "account": "acc2"}), "n")
        self.assertEqual(db.get_approval("n")["kind"], "link_post")
        DB(path)                                                                   # opening it again is harmless

    def test_a_new_database_has_them_from_the_start(self):
        db = tmp_db()
        self.assertEqual(db.create_approval({"id": "x", "cid": "c", "video_id": "v"}), "x")
        self.assertEqual(db.get_approval("x")["kind"], "alert")


class TestTheServiceJob(ServiceCase):
    def link_card(self, text="Great video!", account="acc2", cid=None):
        target = links.resolve(f"{POST}?comment_id={cid}" if cid else POST)
        aid = notifier.create_link_approval(self.db, target, text, account=account, chat_id=4242)
        self.db.update_approval(aid, message_id=55, status="posting")
        return aid

    def test_a_link_comment_is_posted_through_the_poster_and_the_card_shows_it(self):
        aid = self.link_card()
        self.svc.do_post(aid)
        sent = self.posted[0]
        self.assertEqual((sent["kind"], sent["account"], sent["reply"], sent["video_url"]), ("link_post", "acc2", "Great video!", POST))
        done = self.db.get_approval(aid)
        self.assertEqual((done["status"], done["posted_by"], done["posted_reply_cid"]), ("posted", "real-account-1", "999"))
        self.assertIn("Posted on TikTok", self.tg.edits[-1]["text"])
        self.assertIn("Great video!", self.tg.edits[-1]["text"])
        self.assertIsNone(self.tg.edits[-1]["markup"])
        self.assertEqual(self.db.link_posts_today(), 1, "it counts against the daily cap")

    def test_a_failed_link_comment_keeps_the_reason_and_stays_retryable(self):
        self.post_result = {"ok": False, "error": "the comment box did not appear (comments may be turned off on this post)"}
        aid = self.link_card()
        self.svc.do_post(aid)
        done = self.db.get_approval(aid)
        self.assertEqual(done["status"], "post_failed")
        self.assertIn("comments may be turned off", self.tg.edits[-1]["text"])
        self.assertIn("try again", self.tg.edits[-1]["text"])
        self.assertIn(f"ap:post:{aid}", flat(self.tg.edits[-1]["markup"]))

    def test_the_screenshot_caption_describes_the_link_comment(self):
        shot = pathlib.Path(tempfile.mkdtemp()) / "proof.png"
        shot.write_bytes(b"\x89PNG fake")
        self.post_result = {**self.post_result, "proof_screenshot": str(shot)}
        aid = self.link_card(text="Obrigado!", cid=CID)
        self.svc.do_post(aid)
        caption = self.tg.photos[-1]["caption"]
        self.assertIn("Comment posted on TikTok", caption)
        self.assertIn("@shop", caption)
        self.assertIn("reply to a comment", caption)
        self.assertIn("Obrigado!", caption)

    def test_the_default_poster_routes_by_kind(self):
        svc = Service(self.db, tg=self.tg, ai=self.ai)
        with mock.patch.object(tm, "post_reply", return_value={"ok": True, "via": "reply"}) as reply, \
                mock.patch.object(tm, "post_link_comment", return_value={"ok": True, "via": "link"}) as link:
            alert = {"kind": "alert", "video_url": "u", "cid": "c1", "reply": "r"}
            self.assertEqual(svc._default_poster(alert)["via"], "reply")
            reply.assert_called_once_with("u", "c1", "r")
            self.assertEqual(svc._default_poster({"kind": "link_post", "video_url": "u", "cid": "link-post:v", "reply": "t",
                                                  "account": "acc2"})["via"], "link")
            link.assert_called_with("u", "t", cid=None, account="acc2", stage=None, comment_text=None)
            svc._default_poster({"kind": "link_reply", "video_url": "u", "cid": "123", "reply": "t", "account": None})
            link.assert_called_with("u", "t", cid="123", account=None, stage=None, comment_text=None)
            svc._default_poster({"kind": "link_reply", "video_url": "u", "cid": "123", "reply": "t", "comment_text": "o que?"})
            self.assertEqual(link.call_args.kwargs["comment_text"], "o que?",
                             "what the comment says goes along: a kept browser can find it on the page")
            svc._default_poster({"kind": "link_post", "video_url": "u", "cid": "link-post:v", "reply": "t", "comment_text": "x"})
            self.assertIsNone(link.call_args.kwargs["comment_text"], "a new comment on a post answers nothing"
                              )
            shown = []
            svc._default_poster({"kind": "link_reply", "video_url": "u", "cid": "123", "reply": "t", "_stage": shown.append})
            self.assertEqual(link.call_args.kwargs["stage"], shown.append, "what the browser is doing reaches the card")
            svc._default_poster({"video_url": "u", "cid": "c1", "reply": "r"})          # an old row without a kind
            self.assertEqual(reply.call_count, 2)
            svc._default_poster({"kind": "alert", "video_url": "u", "cid": "c1", "reply": "r", "account": "acc2"})
            reply.assert_called_with("u", "c1", "r", profile_names=["acc2"])           # the chosen account, and only that one


class FakeResponse:
    def __init__(self, url, payload=None, status=200, method="GET"):
        self.url, self.status, self._payload = url, status, payload
        self.request = types.SimpleNamespace(method=method, headers={"tt-csrf-token": "x", "user-agent": "u"})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def text(self):
        return json.dumps(self._payload) if self._payload is not None else ""


def time_after_cooldown() -> float:
    import time
    return time.monotonic() + tm.WALL_COOLDOWN_S + 1


class TestThePostingEngine(unittest.TestCase):
    @contextlib.contextmanager
    def engine(self, chain=("master", "acc2")):
        """Nothing real runs: no browser, no waiting, and the posting clock is observed."""
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        with mock.patch.object(tm, "_pace_posts") as pace, mock.patch.object(tm, "_mark_post_attempt") as mark, \
                mock.patch.object(tm, "_use_account", lambda name: contextlib.nullcontext()), \
                mock.patch.object(tm.time, "sleep"), mock.patch.object(config, "account_chain", lambda: list(chain)):
            yield types.SimpleNamespace(pace=pace, mark=mark)

    def test_a_post_link_uses_the_new_comment_flow_and_a_comment_link_uses_the_reply_flow(self):
        with self.engine(), mock.patch.object(tm, "_post_comment_once", return_value={"ok": True, "profile": "master"}) as new, \
                mock.patch.object(tm, "_post_reply_once", return_value={"ok": True, "profile": "master"}) as rep:
            self.assertTrue(tm.post_link_comment(POST, "hello")["ok"])
            new.assert_called_once()
            rep.assert_not_called()
            self.assertEqual(new.call_args.args, (POST, "hello"))
            self.assertTrue(tm.post_link_comment(POST, "hello", cid=CID)["ok"])
            rep.assert_called_once()
            self.assertEqual(rep.call_args.args, (POST, CID, "hello"))
            self.assertTrue(rep.call_args.kwargs["foreign"], "someone else's post: opened by URL, scrolled to the comment")

    def test_the_operators_account_is_used_alone_without_falling_back(self):
        walled = {"ok": False, "captcha": True, "profile": "acc2", "error": "wall"}
        with self.engine(), mock.patch.object(tm, "_post_comment_once", return_value=walled) as new:
            res = tm.post_link_comment(POST, "hello", account="acc2", max_captcha_retries=1)
        self.assertFalse(res["ok"])
        self.assertEqual({c.kwargs["profile_name"] for c in new.call_args_list}, {"acc2"}, "never the other account")
        self.assertEqual(len(new.call_args_list), 2, "its own captcha retries still happen")

    def test_on_auto_a_walled_account_hands_over_to_the_next(self):
        outcomes = {"master": {"ok": False, "captcha": True, "error": "wall"},
                    "acc2": {"ok": True, "profile": "acc2"}}
        with self.engine(), mock.patch.object(tm, "_post_comment_once",
                                              side_effect=lambda url, text, profile_name, **k: outcomes[profile_name]):
            res = tm.post_link_comment(POST, "hello", max_captcha_retries=0)
        self.assertTrue(res["ok"])
        self.assertEqual([(t["account"], t["ok"]) for t in res["attempts"]], [("master", False), ("acc2", True)])

    def test_reading_a_post_tries_at_most_three_accounts_and_never_repeats_a_wall(self):
        wall = {"ok": False, "captcha": True, "error": "wall"}
        with self.engine(chain=["a1", "a2", "a3", "a4", "a5"]), \
                mock.patch.object(tm, "_read_comments_once", return_value=dict(wall)) as read:
            res = tm.read_comments_of_post(POST)
        self.assertFalse(res["ok"])
        self.assertEqual([c.kwargs["profile_name"] for c in read.call_args_list], ["a1", "a2", "a3"],
                         "a wall is usually the network's: five accounts would only repeat it for minutes")

    def test_reading_starts_with_the_account_a_reply_on_auto_starts_with(self):
        """One kept browser serves the read AND the reply only when it is the same account: reading with a helper and replying
        with the page account would cost two profile page loads and a second browser start-up."""
        with self.engine(chain=["page", "helper1", "helper2"]):
            self.assertEqual(tm.reading_order(), ["page", "helper1", "helper2"])
            tm.mark_walled("page")
            self.assertEqual(tm.reading_order(), ["helper1", "helper2", "page"], "unless TikTok is refusing it right now")

    def test_a_page_that_will_not_open_hands_over_to_the_next_account_at_once(self):
        blocked = {"ok": False, "blocked": True, "error": "could not open the post (ERR_HTTP_RESPONSE_CODE_FAILURE)"}
        good = {"ok": True, "profile": "acc2", "comments": []}
        outcomes = {"master": blocked, "acc2": good}
        with self.engine() as e, mock.patch.object(tm, "_read_comments_once",
                                                   side_effect=lambda url, profile_name, **k: dict(outcomes[profile_name])) as read, \
                mock.patch.object(tm.time, "sleep") as sleeping:
            res = tm.read_comments_of_post(POST, max_captcha_retries=2)
        self.assertTrue(res["ok"])
        self.assertEqual([c.kwargs["profile_name"] for c in read.call_args_list], ["master", "acc2"])
        sleeping.assert_not_called()                                    # no waiting between accounts, no same-account retry
        self.assertEqual([(t["account"], t["blocked"]) for t in res["attempts"]], [("master", True), ("acc2", False)])
        e.mark.assert_not_called()                                      # a read is never counted against the posting clock

    def test_an_account_that_met_a_wall_goes_to_the_back_of_the_line_for_a_while(self):
        wall = {"ok": False, "blocked": True, "error": "x"}
        with self.engine(chain=["a1", "a2", "a3"]), mock.patch.object(tm, "_read_comments_once",
                                                                      return_value=dict(wall)):
            tm.read_comments_of_post(POST)
        self.assertTrue(tm.is_walled("a1"), "the account that was just refused is remembered")
        tm.reset_walls()
        tm.mark_walled("a1")
        self.assertEqual(tm.order_by_health(["a1", "a2", "a3"]), ["a2", "a3", "a1"])
        with mock.patch.object(tm.time, "monotonic", return_value=time_after_cooldown()):
            self.assertFalse(tm.is_walled("a1"), "the cooldown ends")
        tm.reset_walls()
        self.assertEqual(tm.order_by_health(["a1", "a2", "a3"]), ["a1", "a2", "a3"])

    def test_the_account_that_loaded_the_fewest_profile_pages_lately_goes_first(self):
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        tm.note_profile_load("a1")
        tm.note_profile_load("a1")                                      # two loads in the last ten minutes: TikTok may refuse a third
        tm.note_profile_load("a2")
        self.assertEqual(tm.recent_loads("a1"), 2)
        self.assertEqual(tm.order_by_health(["a1", "a2", "a3"]), ["a2", "a3", "a1"], "equals keep the operator's order")
        tm.mark_walled("a2")
        self.assertEqual(tm.order_by_health(["a1", "a2", "a3"]), ["a3", "a1", "a2"], "a wall counts more than a busy account")
        with mock.patch.object(tm.time, "monotonic", return_value=tm.time.monotonic() + tm.LOAD_WINDOW_S + 5):
            self.assertEqual(tm.recent_loads("a1"), 0, "old loads are forgotten")

    def test_how_long_an_account_is_expected_to_rest_after_a_refusal(self):
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        self.assertEqual(tm.rest_seconds("a1"), 0)
        tm.mark_walled("a1", seconds=300)
        self.assertTrue(290 <= tm.rest_seconds("a1") <= 300)
        with mock.patch.object(tm.time, "monotonic", return_value=tm.time.monotonic() + 400):
            self.assertEqual(tm.rest_seconds("a1"), 0, "it is not resting any more")

    def test_a_profile_page_that_really_loads_is_counted_a_reused_one_is_not(self):
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        s = self._Session()
        s.name = "acc9"
        s.page = types.SimpleNamespace(url="about:blank")
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_open_tile_fast", return_value=True):
            tm._open_post(s, VID, POST, profile_url="https://www.tiktok.com/@some.shop")
            self.assertEqual(tm.recent_loads("acc9"), 1)
            s.page.url = "https://www.tiktok.com/@some.shop"            # now it stands on the page: no new load
            with mock.patch.object(tm, "_back_to_grid"):
                tm._open_post(s, VID, POST, profile_url="https://www.tiktok.com/@some.shop")
        self.assertEqual(tm.recent_loads("acc9"), 1)

    def test_when_tiktok_refuses_every_account_the_message_says_why_and_what_to_do(self):
        wall = {"ok": False, "blocked": True, "error": "could not open the post (net::ERR_HTTP_RESPONSE_CODE_FAILURE ...)"}
        with self.engine(chain=["a1", "a2"]), mock.patch.object(tm, "_read_comments_once", return_value=dict(wall)):
            res = tm.read_comments_of_post(POST)
        self.assertFalse(res["ok"])
        self.assertIn("2 accounts", res["error"])
        self.assertIn("Wait a few minutes", res["error"])
        self.assertIn("ERR_HTTP_RESPONSE_CODE_FAILURE", res["detail"], "the raw reason is kept for the log")

    def test_a_try_that_only_met_a_wall_is_not_counted_against_the_posting_clock(self):
        outcomes = iter([{"ok": False, "blocked": True, "error": "x"}, {"ok": True, "profile": "acc2"}])
        with self.engine() as e, mock.patch.object(tm, "_post_comment_once", side_effect=lambda *a, **k: next(outcomes)):
            res = tm.post_link_comment(POST, "hello")
        self.assertTrue(res["ok"])
        e.pace.assert_called_once()                                    # waits once, before the first try
        e.mark.assert_called_once()                                    # only the try that really got as far as sending counts

    def test_a_chosen_account_gets_one_fresh_session_retry_but_auto_goes_to_the_next_account(self):
        wall = {"ok": False, "captcha": True, "profile": "x", "error": "wall"}
        with self.engine() as e, mock.patch.object(tm, "_post_comment_once", return_value=dict(wall)) as new:
            tm.post_link_comment(POST, "hello", account="acc2")
            self.assertEqual([c.kwargs["profile_name"] for c in new.call_args_list], ["acc2", "acc2"])
            new.reset_mock()
            tm.reset_walls()
            tm.post_link_comment(POST, "hello")
            self.assertEqual([c.kwargs["profile_name"] for c in new.call_args_list], ["master", "acc2"],
                             "on Auto the next account IS the retry")

    def test_the_screen_is_told_which_account_is_being_tried(self):
        said = []
        outcomes = iter([{"ok": False, "blocked": True, "error": "x"}, {"ok": True, "profile": "acc2"}])
        with self.engine(), mock.patch.object(tm, "_post_comment_once", side_effect=lambda *a, **k: next(outcomes)):
            tm.post_link_comment(POST, "hello", stage=said.append)
        self.assertEqual(said, ["Trying master (account 1 of 2)...", "Trying acc2 (account 2 of 2)..."])

    def test_a_failure_about_the_target_is_not_retried_on_another_account(self):
        with self.engine(), mock.patch.object(tm, "_post_comment_once",
                                              return_value={"ok": False, "error": "the comment box did not appear"}) as new:
            res = tm.post_link_comment(POST, "hello")
        self.assertEqual(new.call_count, 1)
        self.assertIn("comment box", res["error"])

    def test_an_account_that_is_gone_is_refused_before_any_browser_starts(self):
        with self.engine(), mock.patch.object(tm, "_post_comment_once") as new:
            res = tm.post_link_comment(POST, "hello", account="vanished")
        self.assertFalse(res["ok"])
        self.assertIn("not available any more", res["error"])
        new.assert_not_called()

    def test_a_dry_run_never_touches_the_shared_posting_clock(self):
        with self.engine() as e, mock.patch.object(tm, "_post_comment_once", return_value={"ok": True, "dry_run": True}) as new:
            res = tm.post_link_comment(POST, "hello", dry_run=True)
            self.assertTrue(res["dry_run"])
            self.assertTrue(new.call_args.kwargs["dry_run"])
            e.pace.assert_not_called()
            e.mark.assert_not_called()
            tm.post_link_comment(POST, "hello")
            e.pace.assert_called_once()
            e.mark.assert_called_once()

    def test_a_foreign_post_is_opened_by_its_url_not_through_our_profile_grid(self):
        class S:
            urls: list = []

            def goto(self, url):
                self.urls.append(url)
                return {"ok": True}
        s = S()
        with mock.patch("app.tiktok_watch._find_tile") as find:
            self.assertEqual(tm._open_post(s, VID, POST, via_profile=False), ({"ok": True}, False))
        self.assertEqual(s.urls, [POST])
        find.assert_not_called()

    def test_the_owner_of_a_post_is_read_from_its_link(self):
        self.assertEqual(tm._author_profile_url("https://www.tiktok.com/@some.shop/video/123"), "https://www.tiktok.com/@some.shop")
        self.assertEqual(tm._author_profile_url("https://www.tiktok.com/@some.shop/photo/123?x=1"), "https://www.tiktok.com/@some.shop")
        self.assertIsNone(tm._author_profile_url("https://www.tiktok.com/@_/video/123"), "'@_' = the owner is unknown")
        self.assertIsNone(tm._author_profile_url("https://www.tiktok.com/video/123"))
        self.assertIsNone(tm._author_profile_url(""))

    class _Session:
        page = object()

        def __init__(self, fail_on=()):
            self.urls, self.fail_on = [], fail_on

        def goto(self, url):
            self.urls.append(url)
            return {"ok": False, "error": "net::ERR_HTTP_RESPONSE_CODE_FAILURE"} if url in self.fail_on else {"ok": True}

    def test_a_foreign_post_is_opened_through_its_owners_profile_when_the_owner_is_known(self):
        s, owner = self._Session(), "https://www.tiktok.com/@some.shop"
        with mock.patch.object(tm, "_find_tile_fast", return_value=True) as find, \
                mock.patch.object(tm, "_open_tile_fast", return_value=True) as click, mock.patch.object(tm, "_captcha_present", return_value=False):
            self.assertEqual(tm._open_post(s, VID, POST, profile_url=owner), ({"ok": True}, True))
        self.assertEqual(s.urls, [owner], "the profile page, not the post document")
        self.assertEqual(find.call_args.args[2], owner)
        self.assertEqual(click.call_args.args[3], owner, "and a stuck tile is retried on THAT profile, not on ours")

    def test_a_post_that_is_not_on_the_owners_grid_falls_back_to_the_direct_load(self):
        s, owner = self._Session(), "https://www.tiktok.com/@some.shop"
        with mock.patch.object(tm, "_find_tile_fast", return_value=False), mock.patch.object(tm, "_captcha_present", return_value=False):
            self.assertEqual(tm._open_post(s, VID, POST, profile_url=owner), ({"ok": True}, False))
        self.assertEqual(s.urls, [owner, POST])

    def test_a_profile_page_tiktok_refuses_is_a_wall_and_the_post_url_is_not_hammered_next(self):
        owner = "https://www.tiktok.com/@some.shop"
        s = self._Session(fail_on=(owner,))
        goto, via_tile = tm._open_post(s, VID, POST, profile_url=owner)
        self.assertFalse(goto["ok"])
        self.assertTrue(goto["blocked"], "the caller hands over to the next account")
        self.assertIn("profile page did not open", goto["error"])
        self.assertEqual(s.urls, [owner], "loading the post URL straight after would only meet the same refusal")

    def test_a_direct_load_that_fails_is_a_wall_too(self):
        s = self._Session(fail_on=(POST,))
        goto, _ = tm._open_post(s, VID, POST, via_profile=False)
        self.assertEqual((goto["ok"], goto["blocked"]), (False, True))

    def test_the_trace_keeps_every_phase_and_tells_the_screen_only_what_asked_to_be_shown(self):
        said = []
        trace = tm._Trace("read", "acc2", VID, said.append)
        trace.mark("browser started", "Opening the post...")
        trace.mark("comments read")
        self.assertEqual(said, ["Opening the post..."])
        self.assertEqual([t["step"] for t in trace.timings()], ["browser started", "comments read"])
        broken = tm._Trace("read", "acc2", VID, lambda text: 1 / 0)
        broken.mark("x", "never breaks the work")                      # a screen that raises must not stop a send

    def test_the_listener_remembers_every_comment_page_of_this_post_only(self):
        captured = {"list_payload": None, "publish_response": None, "seen": {}}
        listen = tm._make_response_listener(VID, captured)
        listen(FakeResponse(f"https://www.tiktok.com/api/comment/list/?aweme_id={VID}&cursor=0",
                            {"comments": [{"cid": "1", "text": "a"}, {"cid": "2", "text": "b"}]}))
        listen(FakeResponse(f"https://www.tiktok.com/api/comment/list/?aweme_id={VID}&cursor=20",
                            {"comments": [{"cid": "3", "text": "c"}]}))
        listen(FakeResponse("https://www.tiktok.com/api/comment/list/?aweme_id=999&cursor=0", {"comments": [{"cid": "x"}]}))
        listen(FakeResponse(f"https://www.tiktok.com/api/comment/list/reply/?aweme_id={VID}", {"comments": [{"cid": "r"}]}))
        self.assertEqual([c["cid"] for c in captured["list_payload"]["comments"]], ["3"], "latest page, as before")
        self.assertEqual(sorted(captured["seen"]), ["1", "2", "3"], "but a comment from an earlier page is not lost")

    def test_the_listener_keeps_tiktoks_own_verdict_on_a_published_comment(self):
        captured = {"list_payload": None, "publish_response": None, "seen": {}}
        listen = tm._make_response_listener(VID, captured)
        body = {"status_code": 0, "comment": {"cid": "555", "status": 1}}
        listen(FakeResponse("https://www.tiktok.com/api/comment/publish/", body, method="POST"))
        listen(FakeResponse("https://www.tiktok.com/api/comment/publish/", {"status_code": 9}, method="POST"))
        self.assertEqual(captured["publish_response"]["body"], body, "the first answer wins")
        self.assertEqual(captured["publish_response"]["status"], 200)

    def test_publish_outcome_trusts_the_body_not_just_http_200(self):
        good = tm._publish_outcome({"status": 200, "body": {"status_code": 0, "comment": {"cid": "1", "status": 1}}})
        self.assertEqual((good["http_ok"], good["server_confirmed"], good["visibility"]), (True, True, "public"))
        limited = tm._publish_outcome({"status": 200, "body": {"status_code": 0, "comment": {"cid": "1", "status": 2}}})
        self.assertEqual(limited["visibility"], "limited")
        soft_reject = tm._publish_outcome({"status": 200, "body": None})            # HTTP 200 with an empty body
        self.assertEqual((soft_reject["http_ok"], soft_reject["server_confirmed"]), (True, False))
        refused = tm._publish_outcome({"status": 200, "body": {"status_code": 4001, "status_msg": "too fast"}})
        self.assertFalse(refused["server_confirmed"])
        self.assertEqual(tm._publish_outcome(None), {"http_ok": False, "server_confirmed": False, "comment_status": None,
                                                     "visibility": None})


if __name__ == "__main__":
    unittest.main()
