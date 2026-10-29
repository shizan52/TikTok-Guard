"""Telegram command / button handling. Transport-agnostic: `tg` is any object with the
telegram_notify API, `svc` any object offering the few service hooks used below (see
ServiceHooks), so the whole thing is testable without Telegram or a browser.

Only the configured TELEGRAM_CHAT_ID is ever obeyed.
"""
import calendar
import time
import traceback

import config
from app import links, notifier
from app.ai import DEFAULT_INSTRUCTIONS, AI
from app.context import split_chunks
from app.controller import ControlError, Controller
from app.db import DB
from app.i18n import tr
from app.notifier import MAX_REGEN, esc

STATE_TTL_S = 15 * 60
COMMANDS = [("ai", "AI reply instructions"), ("ai_context", "Company information for the AI"),
            ("status", "Health, AI budget, database"), ("pending", "Replies waiting for you"),
            ("posts", "Posts being watched"), ("questions", "Alerts for customer questions"),
            ("watch", "How many posts to watch"), ("budget", "AI requests per day"), ("backfill", "Analyse older comments too"),
            ("accounts", "TikTok accounts used for posting"), ("comment", "Comment on a TikTok link"),
            ("cleanup", "Free disk space"),
            ("help", "All commands")]


class CheckedTG:
    """Wraps the Telegram API object: Telegram answers a bad request (e.g. unparsable HTML) with
    {"ok": false}, which would otherwise vanish silently. Every such failure is written to the
    database event log (/status shows recent problems; `db-stats` and the events table keep the detail)."""

    def __init__(self, tg, db: DB) -> None:
        self._tg, self._db = tg, db

    def __getattr__(self, name):
        fn = getattr(self._tg, name)
        if not callable(fn):
            return fn

        def call(*args, **kwargs):
            res = fn(*args, **kwargs)
            if isinstance(res, dict) and res.get("ok") is False:
                desc = str(res.get("description"))
                benign = "not modified" in desc
                self._db.log_event("telegram_error", f"{name}: {res.get('error_code')} {desc}",
                                   "warn" if benign else "error")
            return res
        return call


class ServiceHooks:
    """What the bot needs from the running service (implemented by app.service.Service)."""
    def scan_running(self) -> bool: ...
    def request_post(self, approval_id: str) -> None: ...
    def run_background(self, fn, *args) -> None: ...
    def last_scan_text(self) -> str: ...


class Bot:
    link_fetch = None            # how short links are expanded (None = real HTTP); tests replace it

    def __init__(self, db: DB, ai: AI, tg, svc) -> None:
        self.db, self.ai, self.tg, self.svc = db, ai, tg, svc
        # the rules (what Post does, the daily cap, validation) live in the Controller; inside the running service it is
        # the very same one the desktop app's API uses
        self.ctl = getattr(svc, "controller", None) or Controller(db, ai, svc, tg=tg, telegram_enabled=lambda: True)

    # ------------------------------------------------------------------ helpers
    def lang(self) -> str:
        return "en"                                  # English only, by the operator's explicit instruction

    def allowed(self, chat_id) -> bool:
        return bool(config.TELEGRAM_CHAT_ID) and str(chat_id) == str(config.TELEGRAM_CHAT_ID)

    def say(self, chat_id, text: str, markup: dict | None = None) -> dict:
        return self.tg.send_message(text, markup, chat_id=chat_id)

    def t(self, key: str, **kw) -> str:
        return tr(self.lang(), key, **kw)

    # ------------------------------------------------------------------ dispatch
    def handle_update(self, upd: dict) -> None:
        try:
            if "callback_query" in upd:
                self.on_callback(upd["callback_query"])
            elif "message" in upd:
                self.on_message(upd["message"])
        except Exception as exc:  # noqa: BLE001 - one bad update must never kill the poll loop
            self.db.log_event("bot_error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}", "error")

    def on_message(self, msg: dict) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        if not self.allowed(chat_id):
            self.db.log_event("unauthorized_chat", str(chat_id), "warn")
            if chat_id:
                self.tg.send_message(tr("en", "unauthorized"), None, chat_id=chat_id)
            return
        if msg.get("document"):
            return self.on_document(chat_id, msg)
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            head, _, args = text.partition(" ")
            cmd = head[1:].split("@")[0].lower()
            return self.command(chat_id, cmd, args.strip())
        return self.on_text(chat_id, text)

    # ------------------------------------------------------------------ commands
    def command(self, chat_id, cmd: str, args: str) -> None:
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            self.say(chat_id, self.t("unknown_command"))
            return
        handler(chat_id, args)

    def cmd_start(self, chat_id, args):
        self.say(chat_id, self.t("help"))

    cmd_help = cmd_start

    def cmd_cancel(self, chat_id, args):
        had = self.db.get_chat_state(chat_id)
        self.db.clear_chat_state(chat_id)
        self.say(chat_id, self.t("cancelled" if had else "nothing_to_cancel"))

    # ---- /ai ------------------------------------------------------------------
    def _instructions(self) -> str:
        return self.db.get_setting("ai_instructions", DEFAULT_INSTRUCTIONS)

    def show_ai(self, chat_id) -> None:
        text = self._instructions()
        body = f"{self.t('ai_title', n=len(text))}\n\n<pre>{esc(text[:3000])}</pre>\n{self.t('ai_hint')}"
        kb = {"inline_keyboard": [[
            {"text": self.t("btn_ai_edit"), "callback_data": "ai:edit"},
            {"text": self.t("btn_ai_reset"), "callback_data": "ai:reset"},
            {"text": self.t("btn_ai_undo"), "callback_data": "ai:undo"},
            {"text": self.t("btn_ai_test"), "callback_data": "ai:test"}]]}
        self.say(chat_id, body, kb)

    def save_ai(self, chat_id, text: str) -> None:
        text = text.strip()
        if len(text) > config.AI_INSTRUCTIONS_MAX_CHARS:
            self.say(chat_id, self.t("ai_too_long", n=len(text), max=config.AI_INSTRUCTIONS_MAX_CHARS))
            return
        self.db.set_setting("ai_instructions", text)
        self.db.clear_chat_state(chat_id)
        self.say(chat_id, self.t("ai_saved", n=len(text)))
        self.show_ai(chat_id)

    def cmd_ai(self, chat_id, args):
        if args:
            return self.save_ai(chat_id, args)
        self.show_ai(chat_id)

    # ---- /ai_context ----------------------------------------------------------------
    def _context(self) -> str:
        return self.db.get_setting("company_context", "")

    def show_context(self, chat_id) -> None:
        text = self._context()
        if not text.strip():
            body = f"{self.t('cx_title', n=0, chunks=0)}\n\n{self.t('cx_empty')}"
        else:
            shown = text[:2500]
            more = len(text) - len(shown)
            body = (f"{self.t('cx_title', n=len(text), chunks=len(split_chunks(text)))}\n\n"
                    f"<pre>{esc(shown)}</pre>"
                    + (f"\n{self.t('cx_truncated', more=more)}" if more > 0 else "")
                    + f"\n\n{self.t('cx_hint')}")
        kb = {"inline_keyboard": [
            [{"text": self.t("btn_cx_replace"), "callback_data": "cx:replace"},
             {"text": self.t("btn_cx_add"), "callback_data": "cx:add"},
             {"text": self.t("btn_cx_undo"), "callback_data": "cx:undo"}],
            [{"text": self.t("btn_cx_download"), "callback_data": "cx:download"},
             {"text": self.t("btn_cx_clear"), "callback_data": "cx:clear"}]]}
        self.say(chat_id, body, kb)

    def save_context(self, chat_id, text: str, *, append: bool) -> None:
        text = text.strip()
        new = (self._context().rstrip() + "\n\n" + text).strip() if append else text
        if len(new) > config.AI_CONTEXT_MAX_CHARS:
            self.say(chat_id, self.t("cx_too_long", n=len(new), max=config.AI_CONTEXT_MAX_CHARS))
            return
        self.db.set_setting("company_context", new)
        self.db.clear_chat_state(chat_id)
        self.say(chat_id, self.t("cx_added" if append else "cx_saved", n=len(new)))
        self.show_context(chat_id)

    def cmd_ai_context(self, chat_id, args):
        if args:
            if args.lower().startswith("add "):
                return self.save_context(chat_id, args[4:], append=True)
            return self.save_context(chat_id, args, append=False)
        self.show_context(chat_id)

    # ---- the rest ---------------------------------------------------------------------
    def cmd_status(self, chat_id, args):
        counts = self.db.comment_status_counts()
        pending = len(self.db.list_approvals(("pending",), 500))
        backlog = len(self.db.negatives_without_approval(notifier.questions_enabled(self.db), 500))
        self.say(chat_id, self.t(
            "status",
            every=config.POLL_INTERVAL_SECONDS,
            last_scan=esc(self.svc.last_scan_text()), budget=esc(self.ai.budget_text()),
            posts=len(self.db.watched_posts()), comments=sum(counts.values()), pending=pending, backlog=backlog,
            db_mb=round(self.db.size_bytes() / 1048576, 2), db_max=config.DB_MAX_MB,
            posting=self.t("posting_on" if config.POSTING_ENABLED else "posting_off"),
            accounts=len(config.account_chain())))

    def cmd_pending(self, chat_id, args):
        rows = self.db.list_approvals(("pending", "post_failed"), 15)
        if not rows:
            return self.say(chat_id, self.t("pending_none"))
        lines = [self.t("pending_title", n=len(rows)), ""]
        for a in rows:
            if notifier.is_link_kind(a):                                   # a /comment <link> card
                who = f"@{esc(a['username'])}" if a.get("username") else "TikTok"
                lines.append(f"• {self.t('lk_pending_comment' if a['kind'] == 'link_reply' else 'lk_pending_post')} "
                             f"<b>{who}</b> ({notifier.age_text(self._created_ts(a), self.lang())}) "
                             f"{esc((a.get('reply') or '')[:70])}" + ("" if a.get("reply") else " ✍️"))
                continue
            lines.append(f"• <b>@{esc(a['username'])}</b> ({notifier.age_text(self._created_ts(a), self.lang())}) "
                         f"{esc((a['comment_text'] or '')[:70])}"
                         + ("" if a.get("reply") else " ⏳"))
        self.say(chat_id, "\n".join(lines))

    @staticmethod
    def _created_ts(a: dict):
        try:
            return calendar.timegm(time.strptime(a["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001
            return None

    def cmd_posts(self, chat_id, args):
        posts = self.db.watched_posts()
        if not posts:
            return self.say(chat_id, self.t("posts_empty"))
        lines = [self.t("posts_title"), ""]
        pinned = sum(1 for p in posts if p.get("is_pinned"))
        for p in posts:
            kind = "\U0001F4CC" if p.get("is_pinned") else f"#{(p['watch_rank'] or 0) - pinned}"
            checked = p.get("last_checked_at") or "-"
            lines.append(f'{kind} <a href="{esc(p["url"])}">{esc((p.get("description") or "")[:45].replace(chr(10), " ")) or "post"}</a>'
                         f" · \U0001F4AC {p['comment_count']} · ⏱ {esc(checked[5:16].replace('T', ' '))}")
        self.say(chat_id, "\n".join(lines))

    def cmd_always_on(self, chat_id, args):
        """/scan, /pause and /resume were removed on purpose: monitoring never stops and never needs a nudge. Not listed
        in the menu or /help; this only answers a stale menu entry kindly."""
        self.say(chat_id, self.t("always_on", every=config.POLL_INTERVAL_SECONDS))

    cmd_scan = cmd_pause = cmd_resume = cmd_always_on

    def cmd_questions(self, chat_id, args):
        arg = args.lower()
        if arg in ("on", "off"):
            self.db.set_setting("notify_questions", "1" if arg == "on" else "0")
        state = self.t("on" if notifier.questions_enabled(self.db) else "off")
        self.say(chat_id, self.t("questions_state", state=state))

    def watch_numbers(self) -> tuple:
        latest = int(self.db.get_setting("watch_latest", config.WATCH_LATEST_POSTS))
        pinned = int(self.db.get_setting("watch_pinned", config.WATCH_PINNED_POSTS))
        return latest, pinned

    def cmd_watch(self, chat_id, args):
        parts = args.lower().split()
        if len(parts) == 2 and parts[0] in ("latest", "pinned") and parts[1].isdigit() and 0 <= int(parts[1]) <= 10:
            self.db.set_setting(f"watch_{parts[0]}", parts[1])
            latest, pinned = self.watch_numbers()
            return self.say(chat_id, self.t("watch_set", latest=latest, pinned=pinned))
        if parts:
            return self.say(chat_id, self.t("watch_bad"))
        latest, pinned = self.watch_numbers()
        self.say(chat_id, self.t("watch_state", latest=latest, pinned=pinned))

    def cmd_budget(self, chat_id, args):
        if args.strip().isdigit():
            n = max(0, min(1000, int(args.strip())))
            self.db.set_setting("ai_daily_requests", str(n))
            return self.say(chat_id, self.t("budget_set", n=n))
        if args:
            return self.say(chat_id, self.t("budget_bad"))
        self.say(chat_id, self.t("budget_state", budget=esc(self.ai.budget_text())))

    def cmd_backfill(self, chat_id, args):
        arg = args.strip() or "30"
        if not arg.isdigit() or not 1 <= int(arg) <= 365:
            return self.say(chat_id, self.t("backfill_bad"))
        days = int(arg)
        n = self.db.backfill_legacy(days)
        if not n:
            return self.say(chat_id, self.t("backfill_none", days=days))
        self.say(chat_id, self.t("backfill_done", n=n, req=-(-n // config.AI_BATCH_SIZE), burst=config.NOTIFY_BURST))

    def cmd_accounts(self, chat_id, args):
        lines = [self.t("accounts_title"), ""]
        extra = {a["name"]: a for a in config.load_accounts()}
        chain = config.account_chain()
        for i, name in enumerate(chain, start=1):                 # the real order: the first account answers first
            a = extra.get(name)
            if a:
                lines.append(f"{i}. <b>{esc(name)}</b> @{esc(a.get('username'))} {esc(a.get('region'))}")
            else:
                lines.append(f"{i}. <b>{esc(name)}</b> (master)")
        for a in config.load_accounts():
            if not a.get("enabled", True) and a["name"] not in chain:
                lines.append(f"- <b>{esc(a['name'])}</b> @{esc(a.get('username'))} ⛔")
        self.say(chat_id, "\n".join(lines))

    # ---- /comment <link> ----------------------------------------------------------------------
    def cmd_comment(self, chat_id, args):
        if not args.strip():
            self.db.set_chat_state(chat_id, "await_link")
            return self.say(chat_id, self.t("lk_ask"))
        self.start_link_flow(chat_id, args)

    def start_link_flow(self, chat_id, text: str) -> None:
        """A link (and optionally the comment text after it) arrived: check it and show the confirmation card."""
        url, rest = links.split_message(text)
        if url is None:
            return self.say(chat_id, self.t("lk_none"))
        self.db.clear_chat_state(chat_id)
        if links.needs_expansion(url):          # vm.tiktok.com / tiktok.com/t/: one HTTP request - off the Telegram thread
            self.say(chat_id, self.t("lk_checking"))
            return self.svc.run_background(self._link_job, chat_id, url, rest)
        self._link_job(chat_id, url, rest)

    def _link_job(self, chat_id, url: str, text: str) -> None:
        try:
            view = self.ctl.offer_link(url, text, chat_id=chat_id, fetch=self.link_fetch)
        except ControlError as err:
            if err.code == "link_already_open":
                return self.say(chat_id, self.t("lk_already_open"))
            return self.say(chat_id, self.t(f"lk_err_{err.code[5:]}") if err.code.startswith("link_") else esc(err.message))
        if view["shortened"]:
            self.say(chat_id, self.t("edit_too_long", n=len(" ".join(text.split())), text=esc(view["reply"])))
        a = self.db.get_approval(view["id"])
        resp = self.say(chat_id, notifier.format_card(self.db, a, self.lang()), notifier.card_keyboard(a, self.lang()))
        mid = (resp.get("result") or {}).get("message_id")
        if mid:
            self.db.update_approval(view["id"], message_id=mid)

    def start_link_ai(self, chat_id, approval_id: str, brief: str) -> None:
        a = self.db.get_approval(approval_id)
        self.db.clear_chat_state(chat_id)
        if not a or a["status"] not in ("pending", "post_failed"):
            return self.say(chat_id, self.t("already_handled"))
        if not self.ai.can("draft"):
            return self.say(chat_id, self.t("lk_ai_budget"))
        self.say(chat_id, self.t("regen_wait"))
        self.svc.run_background(self._link_ai_job, chat_id, approval_id, brief)

    def _link_ai_job(self, chat_id, approval_id: str, brief: str) -> None:
        try:
            self.ctl.regenerate(approval_id, brief)
        except ControlError as err:
            if err.code in ("ai_failed", "ai_budget"):
                self.say(chat_id, self.t("lk_ai_failed", error=esc(err.data.get("detail") or err.message)))

    def _answer_post(self, cqid, approval_id: str, *, link: bool) -> None:
        """The Post button of any card: the Controller decides, this only words the answer."""
        try:
            view = self.ctl.request_post(approval_id)
        except ControlError as err:
            if err.code == "no_reply":
                return self.tg.answer_callback_query(cqid, self.t("lk_no_text_alert" if link else "no_reply"), True)
            if err.code == "cap_reached":
                return self.tg.answer_callback_query(cqid, self.t("lk_cap", n=err.data["limit"])[:200], True)
            return self.tg.answer_callback_query(cqid, self.t("already_handled" if err.code == "already_handled"
                                                              else "unknown_approval"), err.code == "not_found")
        if view["status"] == "approved":
            return self.tg.answer_callback_query(cqid, "✅")
        self.tg.answer_callback_query(cqid, tr(self.lang(), "st_posting"))

    def cb_link_approval(self, chat_id, cqid, action: str, a: dict) -> None:
        """Buttons of a /comment <link> card (the shared checks - known, still open - ran in cb_approval)."""
        approval_id = a["id"]
        if action == "ignore":
            self.ctl.ignore(approval_id)
            return self.tg.answer_callback_query(cqid, "\U0001F6AB")
        if action == "edit":
            self.db.set_chat_state(chat_id, "await_reply_edit", {"id": approval_id})
            self.tg.answer_callback_query(cqid, "✏️")
            return self.say(chat_id, self.t("lk_ask_edit"))
        if action == "ai":
            self.db.set_chat_state(chat_id, "await_link_brief", {"id": approval_id})
            self.tg.answer_callback_query(cqid, "✨")
            return self.say(chat_id, self.t("lk_ask_brief"))
        if action == "acct":                                   # Auto -> account 1 -> account 2 ... -> Auto
            options = [""] + config.account_chain()
            current = a.get("account") or ""
            chosen = options[(options.index(current) + 1) % len(options)] if current in options else ""
            self.ctl.set_account(approval_id, chosen)
            return self.tg.answer_callback_query(
                cqid, self.t("lk_account_now", who=notifier.account_label(chosen) if chosen else "Auto"))
        if action == "post":
            return self._answer_post(cqid, approval_id, link=True)
        self.tg.answer_callback_query(cqid, self.t("unknown_approval"))

    def cmd_cleanup(self, chat_id, args):
        self.say(chat_id, self.t("cleanup_running"))
        self.svc.run_background(self._cleanup_job, chat_id)

    def _cleanup_job(self, chat_id) -> None:
        report = self.svc.run_cleanup()
        self.say(chat_id, self.t("cleanup_done", mb=report["freed_mb_total"], before=report["db"]["db_before_mb"],
                                 after=report["db"]["db_after_mb"]))

    # ------------------------------------------------------------------ free text / files
    def _state(self, chat_id):
        st = self.db.get_chat_state(chat_id)
        if st is None:
            return None
        try:
            age = time.time() - calendar.timegm(time.strptime(st["updated_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001
            age = 0
        if age > STATE_TTL_S:
            self.db.clear_chat_state(chat_id)
            self.say(chat_id, self.t("state_expired"))
            return None
        return st

    def on_text(self, chat_id, text: str) -> None:
        st = self._state(chat_id)
        if st is None:
            if links.find_links(text):                                  # a pasted link needs no command
                self.start_link_flow(chat_id, text)
            return
        mode, payload = st["mode"], st["payload"]
        if mode == "await_link":
            self.start_link_flow(chat_id, text)
        elif mode == "await_link_brief":
            self.start_link_ai(chat_id, payload["id"], text)
        elif mode == "await_ai":
            self.save_ai(chat_id, text)
        elif mode == "await_ctx_replace":
            self.save_context(chat_id, text, append=False)
        elif mode == "await_ctx_append":
            self.save_context(chat_id, text, append=True)
        elif mode == "await_reply_edit":
            self.save_reply_edit(chat_id, payload["id"], text)
        elif mode == "await_ai_test":
            self.db.clear_chat_state(chat_id)
            self.say(chat_id, self.t("regen_wait"))
            self.svc.run_background(self._test_job, chat_id, text)

    def on_document(self, chat_id, msg: dict) -> None:
        st = self._state(chat_id)
        if st is None or st["mode"] not in ("await_ctx_replace", "await_ctx_append"):
            return
        doc = msg["document"]
        name = (doc.get("file_name") or "").lower()
        if (doc.get("file_size") or 0) > 200_000 or not (name.endswith((".txt", ".md")) or
                                                         str(doc.get("mime_type", "")).startswith("text/")):
            return self.say(chat_id, self.t("cx_file_bad"))
        data = self.tg.get_file_bytes(doc["file_id"])
        if not data:
            return self.say(chat_id, self.t("cx_file_bad"))
        text = data.decode("utf-8-sig", errors="replace")
        self.save_context(chat_id, text, append=st["mode"] == "await_ctx_append")

    def _test_job(self, chat_id, text: str) -> None:
        reply, _m, err = self.ai.draft_reply({"text": text, "username": "test", "category": "other"}, kind="test")
        self.say(chat_id, self.t("ai_test_result", reply=esc(reply)) if reply
                 else self.t("draft_failed", error=esc(err)))

    # ------------------------------------------------------------------ approvals
    def save_reply_edit(self, chat_id, approval_id: str, text: str) -> None:
        self.db.clear_chat_state(chat_id)
        try:
            view = self.ctl.edit_reply(approval_id, text)
        except ControlError as err:
            return self.say(chat_id, self.t("edit_empty" if err.code == "bad_request" else "already_handled"))
        if view["shortened"]:
            self.say(chat_id, self.t("edit_too_long", n=len(" ".join(text.split())), text=esc(view["reply"])))
        self.say(chat_id, self.t("edit_saved" if view["kind"] == "alert" else "lk_saved"))

    def _regen_job(self, chat_id, approval_id: str) -> None:
        try:
            self.ctl.regenerate(approval_id)
        except ControlError as err:
            if err.code == "ai_failed":
                self.say(chat_id, self.t("draft_failed", error=esc(err.data.get("detail"))))

    def on_callback(self, cq: dict) -> None:
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        cqid = cq["id"]
        if not self.allowed(chat_id):
            return self.tg.answer_callback_query(cqid, tr("en", "unauthorized"), True)
        parts = (cq.get("data") or "").split(":")
        kind = parts[0]
        if kind == "ap" and len(parts) == 3:
            return self.cb_approval(chat_id, cqid, parts[1], parts[2])
        if kind == "ai" and len(parts) == 2:
            return self.cb_ai(chat_id, cqid, parts[1])
        if kind == "cx" and len(parts) == 2:
            return self.cb_context(chat_id, cqid, parts[1])
        self.tg.answer_callback_query(cqid, self.t("unknown_approval"))

    def cb_approval(self, chat_id, cqid, action: str, approval_id: str) -> None:
        a = self.db.get_approval(approval_id)
        if not a:
            return self.tg.answer_callback_query(cqid, self.t("unknown_approval"), True)
        if a["status"] not in ("pending", "post_failed"):
            return self.tg.answer_callback_query(cqid, self.t("already_handled"))
        if notifier.is_link_kind(a):
            return self.cb_link_approval(chat_id, cqid, action, a)
        lang = self.lang()
        if action == "ignore":
            self.ctl.ignore(approval_id)
            return self.tg.answer_callback_query(cqid, tr(lang, "st_ignored"))
        if action == "edit":
            self.db.set_chat_state(chat_id, "await_reply_edit", {"id": approval_id})
            self.tg.answer_callback_query(cqid, "✏️")
            return self.say(chat_id, self.t("ask_edit"))
        if action == "regen":
            if a.get("reply") and (a.get("regen_count") or 0) >= MAX_REGEN:
                return self.tg.answer_callback_query(cqid, self.t("regen_limit"), True)
            self.tg.answer_callback_query(cqid, self.t("regen_wait"))
            return self.svc.run_background(self._regen_job, chat_id, approval_id)
        if action == "post":
            return self._answer_post(cqid, approval_id, link=False)
        self.tg.answer_callback_query(cqid, self.t("unknown_approval"))

    def cb_ai(self, chat_id, cqid, action: str) -> None:
        self.tg.answer_callback_query(cqid, "")
        if action == "edit":
            self.db.set_chat_state(chat_id, "await_ai")
            return self.say(chat_id, self.t("ai_ask", max=config.AI_INSTRUCTIONS_MAX_CHARS))
        if action == "reset":
            self.db.delete_setting("ai_instructions")
            self.say(chat_id, self.t("ai_reset_done"))
            return self.show_ai(chat_id)
        if action == "undo":
            ok = self.db.undo_setting("ai_instructions")
            self.say(chat_id, self.t("ai_undo_done" if ok else "ai_undo_none"))
            return self.show_ai(chat_id)
        if action == "test":
            self.db.set_chat_state(chat_id, "await_ai_test")
            return self.say(chat_id, self.t("ai_test_ask"))

    def cb_context(self, chat_id, cqid, action: str) -> None:
        self.tg.answer_callback_query(cqid, "")
        if action == "replace":
            self.db.set_chat_state(chat_id, "await_ctx_replace")
            return self.say(chat_id, self.t("cx_ask_replace"))
        if action == "add":
            self.db.set_chat_state(chat_id, "await_ctx_append")
            return self.say(chat_id, self.t("cx_ask_append"))
        if action == "clear":
            self.db.delete_setting("company_context")
            self.say(chat_id, self.t("cx_reset_done"))
            return self.show_context(chat_id)
        if action == "undo":
            ok = self.db.undo_setting("company_context")
            self.say(chat_id, self.t("cx_undo_done" if ok else "cx_undo_none"))
            return self.show_context(chat_id)
        if action == "download":
            text = self._context() or ""
            return self.tg.send_document("company_context.txt", text.encode("utf-8"), chat_id=chat_id)
