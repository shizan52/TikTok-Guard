"""Telegram bot: /ai, /ai_context, approvals, settings commands, authorisation - offline."""
import unittest

from _helpers import FakeSvc, FakeTG, comment, make_ai, tmp_db  # noqa: F401

import config
from app import analysis, notifier
from app.ai import DEFAULT_INSTRUCTIONS
from app.bot import Bot

CHAT = 4242


def msg(text, chat=CHAT, **extra):
    return {"message": {"chat": {"id": chat}, "text": text, **extra}}


def cb(data, chat=CHAT, cqid="q1"):
    return {"callback_query": {"id": cqid, "data": data, "message": {"chat": {"id": chat}, "message_id": 7}}}


class BotCase(unittest.TestCase):
    def setUp(self):
        self._saved = (config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED)
        config.TELEGRAM_CHAT_ID = str(CHAT)
        config.POSTING_ENABLED = True
        self.db = tmp_db()
        self.tg, self.svc = FakeTG(), FakeSvc()
        self.ai = make_ai(self.db, [{"content": "Sorry about that! Please DM us your order number."}])
        self.bot = Bot(self.db, self.ai, self.tg, self.svc)

    def tearDown(self):
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED = self._saved

    def send(self, text, **extra):
        self.bot.handle_update(msg(text, **extra))
        return self.tg.last_text()

    def press(self, data):
        self.bot.handle_update(cb(data))

    def make_negative(self, cid="c1", text="É golpe, nunca mais!"):
        self.db.upsert_post({"video_id": "v1", "url": "https://www.tiktok.com/@x/video/v1", "description": "iPhone 17",
                             "is_pinned": 0, "comment_count": 3})
        self.db.set_watch_set(["v1"])
        analysis.ingest_comments(self.db, "v1", [comment(cid, text)], first_scan=False)
        self.db.update_comment(cid, status="negative", sentiment="NEG", lang="pt", severity=3, category="scam",
                               reason="accuses the shop of fraud", source="ai")
        out = notifier.notify_pending(self.db, self.ai, self.tg, self.bot.lang(), include_questions=False, burst=5)
        a = self.db.list_approvals(("pending",))[0]
        return out, a


class TestAuth(BotCase):
    def test_other_chats_are_refused(self):
        self.bot.handle_update(msg("/ai", chat=999))
        self.assertIn("private", self.tg.last_text())
        self.assertEqual(self.tg.sent[-1]["chat_id"], 999)
        self.assertIsNone(self.db.get_setting("ai_instructions"))
        self.bot.handle_update(msg("/ai hacked", chat=999))
        self.assertIsNone(self.db.get_setting("ai_instructions"))

    def test_callbacks_from_other_chats_are_refused(self):
        _out, a = self.make_negative()
        self.bot.handle_update(cb(f"ap:post:{a['id']}", chat=999))
        self.assertEqual(self.db.get_approval(a["id"])["status"], "pending")
        self.assertEqual(self.svc.posts, [])

    def test_nothing_obeyed_when_no_chat_is_configured(self):
        config.TELEGRAM_CHAT_ID = ""
        self.bot.handle_update(msg("/ai new rules"))
        self.assertIsNone(self.db.get_setting("ai_instructions"))


class TestAiCommand(BotCase):
    def test_view_shows_default_and_buttons(self):
        text = self.send("/ai")
        self.assertIn(DEFAULT_INSTRUCTIONS[:30], text)
        data = [b["callback_data"] for row in self.tg.sent[-1]["markup"]["inline_keyboard"] for b in row]
        self.assertEqual(data, ["ai:edit", "ai:reset", "ai:undo", "ai:test"])

    def test_edit_flow_via_button_then_message(self):
        self.press("ai:edit")
        self.assertEqual(self.db.get_chat_state(CHAT)["mode"], "await_ai")
        self.send("Be very formal. Always answer in Portuguese.")
        self.assertEqual(self.db.get_setting("ai_instructions"), "Be very formal. Always answer in Portuguese.")
        self.assertIsNone(self.db.get_chat_state(CHAT))

    def test_inline_form_and_undo_and_reset(self):
        self.send("/ai first rules")
        self.send("/ai second rules")
        self.press("ai:undo")
        self.assertEqual(self.db.get_setting("ai_instructions"), "first rules")
        self.press("ai:reset")
        self.assertIsNone(self.db.get_setting("ai_instructions"))
        self.assertIn(DEFAULT_INSTRUCTIONS[:30], self.tg.last_text())

    def test_too_long_is_rejected(self):
        self.send("/ai " + "x" * (config.AI_INSTRUCTIONS_MAX_CHARS + 1))
        self.assertIn("Too long", self.tg.last_text())
        self.assertIsNone(self.db.get_setting("ai_instructions"))

    def test_new_instructions_reach_the_prompt(self):
        self.send("/ai Sign every reply with 'Equipe Example Store'.")
        self.ai.draft_reply({"text": "ruim", "lang": "pt"})
        self.assertIn("Equipe Example Store", self.ai.calls[-1]["system"])

    def test_test_button_runs_one_draft(self):
        self.press("ai:test")
        self.send("O produto veio com defeito")
        self.assertIn("Sample reply", self.tg.last_text())
        self.assertEqual(len(self.ai.calls), 1)


class TestContextCommand(BotCase):
    def test_empty_then_replace_then_add(self):
        self.assertIn("Nothing yet", self.send("/ai_context"))
        self.press("cx:replace")
        self.send("We are a mall in Central City.\n\nOpen Monday to Saturday.")
        self.assertIn("mall in Central City", self.db.get_setting("company_context"))
        self.press("cx:add")
        self.send("Shipping: pickup only.")
        ctx = self.db.get_setting("company_context")
        self.assertTrue(ctx.endswith("Shipping: pickup only."))
        self.assertIn("Open Monday", ctx)

    def test_inline_add_and_replace_forms(self):
        self.send("/ai_context First version")
        self.send("/ai_context add Second paragraph")
        self.assertEqual(self.db.get_setting("company_context"), "First version\n\nSecond paragraph")
        self.send("/ai_context Brand new text")
        self.assertEqual(self.db.get_setting("company_context"), "Brand new text")
        self.press("cx:undo")
        self.assertEqual(self.db.get_setting("company_context"), "First version\n\nSecond paragraph")

    def test_text_file_upload(self):
        self.press("cx:replace")
        self.tg.file_bytes = "﻿Horário: 8h às 18h.\nEndereço: Av. Brasil.".encode("utf-8")
        self.bot.handle_update(msg("", document={"file_id": "f1", "file_name": "info.txt", "file_size": 100,
                                                  "mime_type": "text/plain"}))
        self.assertEqual(self.db.get_setting("company_context"), "Horário: 8h às 18h.\nEndereço: Av. Brasil.")

    def test_bad_file_types_and_sizes_are_refused(self):
        self.press("cx:replace")
        self.bot.handle_update(msg("", document={"file_id": "f", "file_name": "x.exe", "file_size": 10,
                                                  "mime_type": "application/octet-stream"}))
        self.assertIn(".txt", self.tg.last_text())
        self.bot.handle_update(msg("", document={"file_id": "f", "file_name": "big.txt", "file_size": 900_000,
                                                  "mime_type": "text/plain"}))
        self.assertIsNone(self.db.get_setting("company_context"))

    def test_size_cap(self):
        self.send("/ai_context " + "y" * (config.AI_CONTEXT_MAX_CHARS + 5))
        self.assertIn("Too long", self.tg.last_text())

    def test_download_sends_the_full_text(self):
        self.send("/ai_context Some long company text")
        self.press("cx:download")
        self.assertEqual(self.tg.docs[-1]["content"], b"Some long company text")

    def test_clear(self):
        self.send("/ai_context abc")
        self.press("cx:clear")
        self.assertIsNone(self.db.get_setting("company_context"))

    def test_cancel(self):
        self.press("cx:replace")
        self.send("/cancel")
        self.send("this is ignored now")
        self.assertIsNone(self.db.get_setting("company_context"))


class TestApprovals(BotCase):
    def test_card_has_all_the_details(self):
        out, a = self.make_negative()
        self.assertEqual((out["sent"], out["backlog"]), (1, 0))
        card = self.tg.sent[-1]["text"]
        for needle in ("Negative comment", "scam accusation", "iPhone 17", "@user", "É golpe", "fraud",
                       "Suggested reply", "Sorry about that", "Latest post #1"):
            self.assertIn(needle, card)
        data = [b["callback_data"] for row in self.tg.sent[-1]["markup"]["inline_keyboard"] for b in row]
        self.assertEqual(data, [f"ap:post:{a['id']}", f"ap:edit:{a['id']}", f"ap:regen:{a['id']}", f"ap:ignore:{a['id']}"])
        self.assertEqual(len(self.ai.calls), 1, "one AI request for the reply draft")

    def test_pinned_post_is_labelled(self):
        self.db.upsert_post({"video_id": "v1", "url": "https://t/v1", "description": "d", "is_pinned": 1, "comment_count": 9})
        self.db.set_watch_set(["v1"])
        analysis.ingest_comments(self.db, "v1", [comment("c1", "golpe")], first_scan=False)
        self.db.update_comment("c1", status="negative", severity=2, category="other")
        notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)
        self.assertIn("Pinned post", self.tg.sent[-1]["text"])

    def test_same_comment_is_announced_once(self):
        self.make_negative()
        out = notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(len(self.db.list_approvals(("pending",))), 1)

    def test_burst_limit_and_backlog(self):
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "is_pinned": 0, "comment_count": 9})
        for i in range(7):
            analysis.ingest_comments(self.db, "v1", [comment(f"c{i}", f"golpe {i}")], first_scan=False)
            self.db.update_comment(f"c{i}", status="negative", severity=1 + i % 3, category="other")
        out = notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=3)
        self.assertEqual((out["sent"], out["backlog"]), (3, 4))
        out2 = notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)
        self.assertEqual((out2["sent"], out2["backlog"]), (4, 0))

    def test_most_severe_first(self):
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "is_pinned": 0, "comment_count": 9})
        for cid, sev in (("a", 1), ("b", 3), ("c", 2)):
            analysis.ingest_comments(self.db, "v1", [comment(cid, f"texto {cid}")], first_scan=False)
            self.db.update_comment(cid, status="negative", severity=sev, category="other")
        notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=1)
        self.assertEqual(self.db.list_approvals(("pending",))[0]["cid"], "b")

    def test_questions_only_when_enabled(self):
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "is_pinned": 0, "comment_count": 1})
        analysis.ingest_comments(self.db, "v1", [comment("q", "qual o horário?")], first_scan=False)
        self.db.update_comment("q", status="question", severity=0, category="other")
        self.assertEqual(notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)["sent"], 0)
        self.assertEqual(notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=True, burst=5)["sent"], 1)
        self.assertIn("Question", self.tg.sent[-1]["text"])

    def test_post_button_queues_the_job_when_posting_enabled(self):
        _o, a = self.make_negative()
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "posting")
        self.assertEqual(self.svc.posts, [a["id"]])
        self.assertIn("Posting to TikTok", self.tg.edits[-1]["text"])
        self.assertIsNone(self.tg.edits[-1]["markup"], "buttons are removed while posting")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.svc.posts, [a["id"]], "a second tap cannot queue a second post")

    def test_post_button_when_posting_disabled_only_marks_ready(self):
        config.POSTING_ENABLED = False
        _o, a = self.make_negative()
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "approved")
        self.assertEqual(self.svc.posts, [])
        self.assertIn("not posted", self.tg.edits[-1]["text"])

    def test_ignore(self):
        _o, a = self.make_negative()
        self.press(f"ap:ignore:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["status"], "rejected")
        self.press(f"ap:post:{a['id']}")
        self.assertEqual(self.svc.posts, [])

    def test_edit_reply_flow_and_150_limit(self):
        _o, a = self.make_negative()
        self.press(f"ap:edit:{a['id']}")
        self.send("Olá! Sentimos muito pelo ocorrido. Pode nos chamar no direct?")
        self.assertEqual(self.db.get_approval(a["id"])["reply"], "Olá! Sentimos muito pelo ocorrido. Pode nos chamar no direct?")
        self.assertIn("Sentimos muito", self.tg.edits[-1]["text"])
        self.press(f"ap:edit:{a['id']}")
        self.send("Muito obrigado pelo contato. " * 12)
        self.assertLessEqual(len(self.db.get_approval(a["id"])["reply"]), 150)

    def test_regenerate_and_limit(self):
        _o, a = self.make_negative()
        before = self.db.get_approval(a["id"])["reply"]
        for i in range(3):
            self.press(f"ap:regen:{a['id']}")
        self.assertEqual(self.db.get_approval(a["id"])["regen_count"], 3)
        calls = len(self.ai.calls)
        self.press(f"ap:regen:{a['id']}")
        self.assertEqual(len(self.ai.calls), calls, "regenerate is capped at 3 per comment")
        self.assertTrue(self.tg.answers[-1]["alert"])

    def test_no_budget_means_card_without_reply_then_draft_later(self):
        self.db.set_setting("ai_daily_requests", "0")
        out, a = self.make_negative()
        self.assertIsNone(a["reply"])
        self.assertIn("No reply drafted yet", self.tg.sent[-1]["text"])
        data = [b["callback_data"] for row in self.tg.sent[-1]["markup"]["inline_keyboard"] for b in row]
        self.assertNotIn(f"ap:post:{a['id']}", data)
        self.press(f"ap:post:{a['id']}")                       # cannot post nothing
        self.assertEqual(self.svc.posts, [])
        self.db.set_setting("ai_daily_requests", "40")           # budget returns
        self.assertEqual(notifier.fill_missing_replies(self.db, self.ai, self.tg, "en"), 1)
        self.assertTrue(self.db.get_approval(a["id"])["reply"])
        self.assertIn("Suggested reply", self.tg.edits[-1]["text"])

    def test_html_in_comments_is_escaped(self):
        self.make_negative(text="<b>golpe</b> & <script>")
        card = self.tg.sent[-1]["text"]
        self.assertIn("&lt;b&gt;golpe&lt;/b&gt; &amp; &lt;script&gt;", card)


class TestBackfill(BotCase):
    def seed(self):
        import time
        self.db.upsert_post({"video_id": "v1", "url": "u", "description": "d", "is_pinned": 1, "comment_count": 5})
        day = 86400
        rows = [comment("recent", "recente", create_time=int(time.time()) - 3 * day),
                comment("mid", "do mês passado", create_time=int(time.time()) - 20 * day),
                comment("old", "muito antigo", create_time=int(time.time()) - 200 * day)]
        self.db.insert_new_comments("v1", rows, legacy_before_ts=int(time.time()) - 1)       # all stored as legacy
        self.db.conn().execute("INSERT INTO comments(cid, video_id, first_seen_at, status, source) "
                               "VALUES('no-text','v1','2026-01-01T00:00:00Z','legacy','legacy')")   # migrated: no text

    def test_backfill_queues_only_recent_comments_that_kept_their_text(self):
        self.seed()
        self.send("/backfill 30")
        self.assertIn("2 older comment", self.tg.last_text())
        status = {r["cid"]: r["status"] for r in self.db.conn().execute("SELECT cid, status FROM comments")}
        self.assertEqual(status, {"recent": "new", "mid": "new", "old": "legacy", "no-text": "legacy"})
        self.assertIn("next rounds", self.tg.last_text(), "the normal 2-minute rounds analyse them; no manual scan exists")

    def test_backfill_window_and_validation(self):
        self.seed()
        self.send("/backfill 7")
        self.assertIn("1 older comment", self.tg.last_text())
        self.send("/backfill 0")
        self.assertIn("Use: /backfill", self.tg.last_text())
        self.send("/backfill abc")
        self.assertIn("Use: /backfill", self.tg.last_text())
        self.send("/backfill 1")
        self.assertIn("No older stored comments", self.tg.last_text())

    def test_backfilled_negative_is_then_analysed_and_announced(self):
        self.seed()
        self.db.conn().execute("UPDATE comments SET text='É golpe, nunca mais!' WHERE cid='mid'")
        self.send("/backfill 30")
        # the batch is newest-first: 'recent' is line 1, 'mid' (the complaint) is line 2
        ai = make_ai(self.db, [{"content": "1|OK|pt|0|other|fine\n2|NEG|pt|3|scam|accuses fraud"}])
        analysis.classify_pending(self.db, ai)
        self.assertEqual(self.db.get_comment("mid")["status"], "negative")


class TestStrings(unittest.TestCase):
    def test_english_only_and_every_string_formats(self):
        import re
        from app.i18n import STRINGS
        self.assertEqual(list(STRINGS), ["en"], "the bot speaks English only")
        for key, text in STRINGS["en"].items():
            self.assertIsNone(re.search(r"[\u0980-\u09FF]", text), f"Bengali characters in {key}")
            fields = set(re.findall(r"\{(\w+)\}", text))
            try:
                text.format(**{f: "x" for f in fields})
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{key} does not format: {exc}")

    def test_every_command_in_the_menu_has_a_handler(self):
        from app.bot import COMMANDS, Bot
        for name, _desc in COMMANDS:
            self.assertTrue(hasattr(Bot, f"cmd_{name}"), name)

    def test_no_python_source_sends_bengali_to_telegram(self):
        import pathlib, re
        for f in pathlib.Path(config.ROOT, "app").glob("*.py"):
            self.assertIsNone(re.search(r"[\u0980-\u09FF]", f.read_text(encoding="utf-8")), f.name)


class TestSettingsCommands(BotCase):
    def test_budget(self):
        self.send("/budget 12")
        self.assertEqual(self.ai.limit(), 12)
        self.send("/budget abc")
        self.assertIn("Use: /budget", self.tg.last_text())
        self.send("/budget")
        self.assertIn("/12", self.tg.last_text())

    def test_watch(self):
        self.send("/watch latest 3")
        self.send("/watch pinned 1")
        self.assertEqual(self.bot.watch_numbers(), (3, 1))
        self.send("/watch latest 99")
        self.assertIn("Use: /watch", self.tg.last_text())
        self.assertEqual(self.bot.watch_numbers(), (3, 1))

    def test_questions_toggle(self):
        self.send("/questions on")
        self.assertEqual(self.db.get_setting("notify_questions"), "1")
        self.send("/questions off")
        self.assertEqual(self.db.get_setting("notify_questions"), "0")

    def test_monitoring_is_always_on_so_scan_pause_resume_are_gone(self):
        from app.bot import COMMANDS
        menu = [name for name, _ in COMMANDS]
        help_text = self.send("/help")
        for gone in ("scan", "pause", "resume"):
            self.assertNotIn(gone, menu)
            self.assertNotIn(f"/{gone}", help_text)
            answer = self.send(f"/{gone}")                      # a stale menu entry gets a kind answer, does nothing
            self.assertIn("always on", answer)
            self.assertNotIn("Unknown", answer)
        self.assertFalse(hasattr(self.svc, "paused"))
        status = self.send("/status")
        self.assertIn("always on", status)
        self.assertIn(f"every {config.POLL_INTERVAL_SECONDS} s", status)
        self.assertNotIn("paused", status.lower())

    def test_bot_speaks_only_english_and_has_no_language_switch(self):
        self.db.set_setting("bot_lang", "bn")                     # a stale setting from an older version
        self.assertEqual(self.bot.lang(), "en")
        self.assertIn("Unknown command", self.send("/lang bn"))
        text = self.send("/help")
        self.assertIn("how the AI replies", text)
        self.assertNotIn("/lang", text)

    def test_status_and_posts_and_pending(self):
        self.make_negative()
        status = self.send("/status")
        for needle in ("Monitoring", "AI today", "Posts watched: 1", "Waiting for you: 1", "Database"):
            self.assertIn(needle, status)
        self.assertIn("iPhone 17", self.send("/posts"))
        self.assertIn("@user", self.send("/pending"))

    def test_accounts_lists_the_real_answering_order(self):
        import json, tempfile, pathlib
        saved = (config.ACCOUNTS_FILE, config.REPLY_ACCOUNT_FIRST)
        tmp = pathlib.Path(tempfile.mkdtemp()) / "accounts.json"
        tmp.write_text(json.dumps({"accounts": [{"name": "real-account-1", "user_data_dir": "x", "username": "ana", "region": "BD"},
                                                {"name": "owner", "user_data_dir": "y", "username": "shop", "region": "BR"},
                                                {"name": "off", "user_data_dir": "z", "username": "old", "enabled": False}]}), encoding="utf-8")
        config.ACCOUNTS_FILE = tmp
        try:
            config.REPLY_ACCOUNT_FIRST = "owner"
            text = self.send("/accounts")
            self.assertLess(text.index("owner"), text.index("real-account-1"))
            self.assertLess(text.index("owner"), text.index(config.BFP_PROFILE_NAME))
            self.assertIn("1. <b>owner</b> @shop", text)
            self.assertIn("(master)", text)
            self.assertIn("off", text)
            self.assertIn("⛔", text)
        finally:
            config.ACCOUNTS_FILE, config.REPLY_ACCOUNT_FIRST = saved

    def test_unknown_command(self):
        self.assertIn("Unknown", self.send("/nonsense"))

    def test_cleanup_command(self):
        self.send("/cleanup")
        self.assertIn("freed about 1.5 MB", self.tg.last_text())

    def test_cards_are_english_even_with_a_stale_bengali_setting(self):
        import re
        self.db.set_setting("bot_lang", "bn")
        self.make_negative()
        card = self.tg.sent[-1]["text"]
        self.assertIn("Negative comment", card)
        self.assertIsNone(re.search(r"[\u0980-\u09FF]", card))


if __name__ == "__main__":
    unittest.main()
