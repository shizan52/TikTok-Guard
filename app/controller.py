"""Everything an operator can do, as plain data in / data out - no Telegram texts, no HTTP.

Two thin clients sit on top of this one class: the Telegram bot (app/bot.py, which turns the results into cards and
button answers) and the desktop app's local API (app/api.py, which turns them into JSON). So the rules - what "Post"
does, when the daily cap stops a link comment, how a setting is validated - exist exactly once.

Failures are `ControlError(code, message)`; the code is stable and machine-readable (not_found, already_handled,
no_reply, cap_reached, regen_limit, ai_budget, ai_failed, bad_request, link_not_tiktok ...).
"""
import threading
import time
import uuid

import config
from app import links, notifier
from app.ai import DEFAULT_INSTRUCTIONS
from app.context import split_chunks
from app.db import DB, utcnow
from app.i18n import tr
from app.notifier import MAX_REGEN

OPEN = ("pending", "post_failed", "approved")     # an approval the operator can still act on ('approved' = saved while
#                                                   posting was OFF: turn posting on and it can still be sent)
HTTP_OF = {"not_found": 404, "already_handled": 409, "no_reply": 409, "cap_reached": 429, "regen_limit": 429,
           "ai_budget": 429, "ai_failed": 502, "bad_request": 400, "link_already_open": 409, "busy": 409,
           "not_available": 501}


# what a card says when the app was closed (or died) while it was being sent: the reply may or may not be on TikTok
INTERRUPTED_ERROR = ("The app was closed while this reply was being sent, so it is not known whether it reached TikTok. "
                     "Open the comment on TikTok and look before you send it again.")


# what a card says after it was checked on TikTok
CHECKED_ABSENT = ("Checked on TikTok: there is no new reply under that comment, so this reply was NOT posted. "
                  "It is safe to send it again.")
CHECKED_MAYBE = ("Checked on TikTok: the comment has {now} replies now and had {before} when you sent this - yours may be one of "
                 "them, or somebody else's. Open the post to look; if it is not there, send it again.")


class ControlError(Exception):
    def __init__(self, code: str, message: str, **data) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    @property
    def http_status(self) -> int:
        return HTTP_OF.get(self.code, 422 if self.code.startswith("link_") else 400)


class Controller:
    def __init__(self, db: DB, ai, hooks, *, tg=None, bus=None, accounts=None, telegram_enabled=lambda: False,
                 link_fetch=None, analyzer=None) -> None:
        """`hooks` offers what only the running service can: request_post(id), scan_running(), last_scan_text(),
        run_cleanup(). `tg` (when Telegram is on) lets a change made in the app refresh the card shown in Telegram.
        `analyzer` (the desktop app) reads and judges the comments of a post on demand."""
        self.db, self.ai, self.hooks, self.tg, self.bus = db, ai, hooks, tg, bus
        self.accounts, self.telegram_enabled, self.link_fetch, self.analyzer = accounts, telegram_enabled, link_fetch, analyzer
        self.started_at = time.time()
        self.checks: dict = {}                              # approval id -> {"outcome", "message"} of its last check on TikTok
        self._env_key = config.OPENROUTER_API_KEY          # what .env says, to go back to when the app's own key is cleared

    # ------------------------------------------------------------------ approvals
    def _get(self, approval_id: str) -> dict:
        a = self.db.get_approval(approval_id)
        if a is None:
            raise ControlError("not_found", "There is no such card.")
        return a

    def _open(self, approval_id: str) -> dict:
        a = self._get(approval_id)
        if a["status"] not in OPEN:
            raise ControlError("already_handled", "This card was already handled.", status=a["status"])
        return a

    def _changed(self, approval_id: str) -> dict:
        """After any change: keep the Telegram card (if there is one) in step, return the fresh view."""
        if self.tg is not None and self.telegram_enabled():
            try:
                notifier.refresh_card(self.tg, self.db, approval_id, "en")
            except Exception:  # noqa: BLE001 - a Telegram hiccup must never fail the operation itself
                pass
        return self.approval(approval_id)

    def approval(self, approval_id: str) -> dict:
        return self.approval_view(self._get(approval_id))

    def approval_view(self, a: dict) -> dict:
        link = notifier.is_link_kind(a)
        is_open = a["status"] in OPEN
        has_comment = not link or bool(a.get("comment_text"))        # an alert, or a reply made from an analysed post
        comment = (self.db.get_comment(a["cid"]) or {}) if has_comment else {}
        post = (self.db.get_post(a["video_id"]) or {}) if has_comment else {}
        view = {
            "id": a["id"], "kind": a.get("kind") or "alert", "status": a["status"], "simulated": bool(a.get("simulated")),
            "created_at": a["created_at"], "updated_at": a["updated_at"],
            "reply": a.get("reply"), "reply_length": len(a["reply"]) if a.get("reply") else 0,
            "regen_count": a.get("regen_count") or 0, "post_attempts": a.get("post_attempts") or 0,
            "error": a.get("last_error"), "interrupted": a.get("last_error") == INTERRUPTED_ERROR and a["status"] == "post_failed",
            "checked": ("absent" if a.get("last_error") == CHECKED_ABSENT else
                        "maybe" if (a.get("last_error") or "").startswith("Checked on TikTok: the comment has") else None),
            "checking": bool(getattr(self.hooks, "is_checking", lambda _id: False)(a["id"])),
            "check": self.checks.get(a["id"]),
            "can_check": a["status"] == "post_failed" and a.get("kind") != "link_post" and bool(a.get("cid") and a.get("video_url")),
            "posted_at": a.get("posted_at"), "posted_by": a.get("posted_by"),
            "visibility": a.get("posted_visibility"), "account": a.get("account"),
            "account_label": notifier.account_label(a["account"]) if a.get("account") else None,
            "target": {"video_id": a["video_id"], "video_url": a.get("video_url"),
                       "comment_id": a["cid"] if a.get("kind") != "link_post" else None, "username": a.get("username")},
            "can": {"post": is_open and bool(a.get("reply")), "edit": is_open, "ignore": is_open,
                    "regenerate": is_open and (link or (a.get("regen_count") or 0) < MAX_REGEN or not a.get("reply")),
                    "choose_account": is_open}}
        proof = self._proof_file(a)
        if proof:
            view["proof_file"] = proof                 # GET /api/screenshots/<proof_file>
        if a["status"] == "posting":                   # what the browser is doing right now ("Opening the post ...")
            stage = getattr(self.hooks, "send_stage", None)
            view["progress"] = stage(a["id"]) if stage else None
        if has_comment:
            view["comment"] = {"cid": a["cid"], "text": a.get("comment_text"), "username": a.get("username"),
                               "likes": comment.get("digg_count", 0), "created_ts": comment.get("create_ts"),
                               "category": a.get("category") or comment.get("category"),
                               "severity": a.get("severity") or comment.get("severity") or 0,
                               "reason": a.get("reason") or comment.get("reason"), "lang": a.get("lang"),
                               "is_question": comment.get("status") == "question" or comment.get("sentiment") == "Q"}
            view["post"] = ({"video_id": post.get("video_id"), "url": post.get("url"),
                             "description": post.get("description"), "comment_count": post.get("comment_count"),
                             "pinned": bool(post.get("is_pinned"))} if post else None)
        return view

    @staticmethod
    def _proof_file(a: dict) -> str | None:
        """The screenshot the bot took right after posting this card, if it still exists."""
        if a["status"] != "posted":
            return None
        name = (f"proof_comment_{a['video_id']}.png" if a.get("kind") == "link_post"
                else f"proof_{a['video_id']}_{a['cid']}.png")
        return name if (config.SCREENSHOTS_DIR / name).exists() else None

    def list_approvals(self, statuses: tuple = ("pending", "post_failed"), *, kind: str | None = None,
                       limit: int = 50) -> list:
        if tuple(statuses) == ("all",):
            statuses = ("pending", "approved", "posting", "posted", "post_failed", "rejected", "expired", "superseded")
        rows = self.db.list_approvals(tuple(statuses), 500)
        if kind == "link":
            rows = [a for a in rows if notifier.is_link_kind(a)]
        elif kind == "alert":
            rows = [a for a in rows if not notifier.is_link_kind(a)]
        return [self.approval_view(a) for a in rows[:max(1, min(int(limit), 200))]]

    def edit_reply(self, approval_id: str, text: str) -> dict:
        """The operator's own words for a card (an alert's reply, or a link comment). Shortened to TikTok's 150."""
        a = self._open(approval_id)
        clean, shortened = notifier.sanitize_manual_reply(text or "")
        if not clean:
            raise ControlError("bad_request", "The text is empty.")
        was_long = len(" ".join((text or "").split())) > 150
        self.db.update_approval(approval_id, reply=clean, lang=a.get("lang"))
        view = self._changed(approval_id)
        view["shortened"] = bool(shortened and was_long)
        return view

    def regenerate(self, approval_id: str, brief: str | None = None) -> dict:
        """A new AI text for the card. An alert is answered from its comment; a link comment from the operator's brief
        ('thank them and invite them to the store')."""
        a = self._open(approval_id)
        # a comment WE read from a post (a reply card with the comment's text) is answered from that comment, like an alert,
        # unless the operator gives a brief; a bare link comment can only be written from a brief
        if notifier.is_link_kind(a) and not (a.get("comment_text") and not (brief or "").strip()):
            if not (brief or "").strip():
                raise ControlError("bad_request", "Say in a few words what the comment should say.")
            if not self.ai.can("draft"):
                raise ControlError("ai_budget", tr("en", "lk_ai_budget"))
            text, _m, err = self.ai.draft_link_comment(
                brief, target="comment" if a["kind"] == "link_reply" else "post", previous=a.get("reply"))
        else:
            if a.get("reply") and (a.get("regen_count") or 0) >= MAX_REGEN:
                raise ControlError("regen_limit", tr("en", "regen_limit"))
            comment = self.db.get_comment(a["cid"]) or {"cid": a["cid"], "text": a["comment_text"], "lang": a["lang"],
                                                       "username": a["username"], "category": a["category"]}
            post = self.db.get_post(a["video_id"]) or {}
            text, _m, err = self.ai.draft_reply(comment, post_desc=post.get("description") or "",
                                                previous=a.get("reply"), kind="regen")
        if not text:
            raise ControlError("ai_failed", f"Could not write the text: {err}", detail=err)
        self.db.update_approval(approval_id, reply=text, regen_count=(a.get("regen_count") or 0) + 1)
        return self._changed(approval_id)

    def ignore(self, approval_id: str) -> dict:
        self._open(approval_id)
        self.db.update_approval(approval_id, status="rejected")
        return self._changed(approval_id)

    def set_account(self, approval_id: str, account: str | None) -> dict:
        """Which account sends a reply or link comment (None / '' = automatic: the first one that works)."""
        self._open(approval_id)
        if account and account not in config.account_chain():
            raise ControlError("bad_request", f"'{account}' is not an active account.")
        self.db.update_approval(approval_id, account=account or None)
        return self._changed(approval_id)

    def request_post(self, approval_id: str) -> dict:
        """The Post button. -> the card, now 'posting' (queued), or 'approved' when posting is switched off."""
        a = self._open(approval_id)
        if not a.get("reply"):
            raise ControlError("no_reply", tr("en", "lk_no_text_alert" if notifier.is_link_kind(a) else "no_reply")
                               .replace("<i>", "").replace("</i>", ""))
        if notifier.is_link_kind(a):
            cap = config.LINK_COMMENTS_PER_DAY
            if cap and self.db.link_posts_today() >= cap:
                raise ControlError("cap_reached", tr("en", "lk_cap", n=cap), limit=cap)
            held_back = not config.POSTING_ENABLED
        else:
            held_back = bool(a.get("simulated") or not config.POSTING_ENABLED or not a.get("video_url"))
        if held_back:
            self.db.update_approval(approval_id, status="approved")
            return self._changed(approval_id)
        comment = self.db.get_comment(a["cid"]) if a.get("kind") != "link_post" else None
        self.db.update_approval(approval_id, status="posting", post_attempts=(a.get("post_attempts") or 0) + 1,
                                replies_before=(comment or {}).get("reply_count"))     # what "a new reply" will be measured against
        self.checks.pop(approval_id, None)
        view = self._changed(approval_id)
        self.hooks.request_post(approval_id)
        return view

    def verify(self, approval_id: str) -> dict:
        """'Check on TikTok': was this reply posted after all (the app was closed mid-send, TikTok never answered)? Reads the post
        again and compares - see apply_verification(). Runs in the background."""
        a = self._get(approval_id)
        if a["status"] != "post_failed" or a.get("kind") == "link_post" or not (a.get("cid") and a.get("video_url")):
            raise ControlError("bad_request", "Only a reply that failed or was interrupted can be checked on TikTok.")
        if not getattr(self.hooks, "is_checking", lambda _id: False)(approval_id):
            self.checks.pop(approval_id, None)
            self.hooks.request_verify(approval_id)
        return self._changed(approval_id)

    def apply_verification(self, approval_id: str, result: dict) -> dict:
        """The outcome of a check: found (TikTok's own preview of the replies shows our text) -> the card is 'posted'; more replies
        than when it was sent -> 'maybe'; none -> 'not posted, safe to send again'; the post could not be read -> nothing changes."""
        a = self._get(approval_id)
        if a["status"] != "post_failed":
            return self.approval_view(a)
        if not result.get("ok"):
            msg = str(result.get("error") or "The post could not be read.")[:300]
            self.checks[approval_id] = {"outcome": "error", "message": f"Could not check: {msg}"}
            self.db.log_event("post_verify_failed", f"{approval_id}: {msg}", "warn")
            return self._changed(approval_id)
        mine = " ".join((a.get("reply") or "").split()).lower()[:40]
        hit = next((r for r in (result.get("preview") or []) if mine and mine in " ".join((r.get("text") or "").split()).lower()), None)
        now, before = result.get("count"), a.get("replies_before")
        if hit:
            self.db.update_approval(approval_id, status="posted", posted_by=hit.get("user") or a.get("posted_by"), posted_at=utcnow(),
                                    last_error=None, posted_visibility="public")
            self.checks[approval_id] = {"outcome": "found", "message": "Found it: this reply is on TikTok."}
            self.db.log_event("post_verified", f"{approval_id}: found under the comment (by @{hit.get('user')})")
        elif before is not None and now is not None and int(now) > int(before):
            self.db.update_approval(approval_id, last_error=CHECKED_MAYBE.format(now=now, before=before))
            self.checks[approval_id] = {"outcome": "maybe", "message": CHECKED_MAYBE.format(now=now, before=before)}
            self.db.log_event("post_verified", f"{approval_id}: more replies than before ({before} -> {now})")
        else:
            self.db.update_approval(approval_id, last_error=CHECKED_ABSENT)
            self.checks[approval_id] = {"outcome": "absent", "message": CHECKED_ABSENT}
            self.db.log_event("post_verified", f"{approval_id}: not under the comment ({before} -> {now} replies)")
        return self._changed(approval_id)

    def recover_interrupted(self) -> list:
        """Run once at start-up: a card that was 'posting' when the app last stopped is not being sent by anyone.
        -> the ids of the cards that were put right."""
        ids = self.db.interrupt_posting(INTERRUPTED_ERROR)
        for approval_id in ids:
            self.db.log_event("post_interrupted", f"{approval_id}: the app stopped while it was being sent", "warn")
        return ids

    # ------------------------------------------------------------------ comment on a link
    def preview_link(self, link: str) -> dict:
        try:
            t = links.resolve(link, fetch=self.link_fetch or links.fetch_once)
        except links.LinkError as err:
            raise ControlError(f"link_{err.key}", tr("en", f"lk_err_{err.key}")) from err
        return {"kind": t.kind, "video_id": t.video_id, "comment_id": t.comment_id, "username": t.username,
                "url": t.url, "media": t.media}

    def offer_link(self, link: str, text: str = "", *, account: str | None = None, chat_id=None, fetch=None) -> dict:
        """A card for a comment on `link` (a post or one comment). Nothing is sent until request_post()."""
        try:
            target = links.resolve(link, fetch=fetch or self.link_fetch or links.fetch_once)
        except links.LinkError as err:
            raise ControlError(f"link_{err.key}", tr("en", f"lk_err_{err.key}")) from err
        if account and account not in config.account_chain():
            raise ControlError("bad_request", f"'{account}' is not an active account.")
        clean, shortened = notifier.sanitize_manual_reply(text) if text else ("", False)
        approval_id = notifier.create_link_approval(self.db, target, clean, account=account or "", chat_id=chat_id)
        if approval_id is None:
            existing = self.db.conn().execute(
                "SELECT id FROM approvals WHERE cid=? AND status IN ('pending','approved','posting')",
                (target.comment_id or f"link-post:{target.video_id}",)).fetchone()
            raise ControlError("link_already_open", tr("en", "lk_already_open"),
                               existing_id=existing["id"] if existing else None)
        view = self.approval(approval_id)
        view["shortened"] = bool(clean and shortened and len(" ".join(text.split())) > 150)
        return view

    # ------------------------------------------------------------------ status / posts
    def status(self) -> dict:
        db = self.db
        counts = db.comment_status_counts()
        ts = db.meta_get("last_scan_ts")
        return {
            "monitoring": {"always_on": True, "poll_interval_seconds": config.POLL_INTERVAL_SECONDS,
                           "scan_running": bool(self.hooks.scan_running()),
                           "last_scan_at": float(ts) if ts else None, "last_scan_text": self.hooks.last_scan_text(),
                           "last_scan_error": db.meta_get("last_scan_error") or None,
                           "last_scan_skipped_reason": db.meta_get("last_scan_skip_reason") or None},
            "ai": self._ai_state(),
            "posts_watched": len(db.watched_posts()), "comments_stored": sum(counts.values()),
            "waiting": len(db.list_approvals(("pending", "post_failed"), 500)),
            "backlog": len(db.negatives_without_approval(notifier.questions_enabled(db), 500)),
            "database": {"mb": round(db.size_bytes() / 1048576, 2), "limit_mb": config.DB_MAX_MB},
            "posting_enabled": bool(config.POSTING_ENABLED),
            "link_comments": {"today": db.link_posts_today(), "per_day": config.LINK_COMMENTS_PER_DAY},
            "accounts": {"in_order": len(config.account_chain())},
            "telegram_enabled": bool(self.telegram_enabled()), "uptime_seconds": int(time.time() - self.started_at),
            "recent_problems": [e for e in db.recent_events(30) if e["level"] in ("warn", "error")][:8]}

    def _ai_state(self) -> dict:
        ai = self.ai
        return {"daily_requests": ai.limit(), "used": ai.used(), "remaining": ai.remaining(), "exhausted": ai.exhausted(),
                "text": ai.budget_text()}

    def posts(self) -> list:
        return [{"video_id": p["video_id"], "url": p["url"], "description": p.get("description"),
                 "pinned": bool(p.get("is_pinned")), "rank": p.get("watch_rank"), "comment_count": p["comment_count"],
                 "last_checked_at": p.get("last_checked_at"), "last_error": p.get("last_error")}
                for p in self.db.watched_posts()]

    # ------------------------------------------------------------------ settings
    def watch_numbers(self) -> tuple:
        return (int(self.db.get_setting("watch_latest", config.WATCH_LATEST_POSTS)),
                int(self.db.get_setting("watch_pinned", config.WATCH_PINNED_POSTS)))

    def settings(self) -> dict:
        db = self.db
        instr, ctx = db.get_setting("ai_instructions"), db.get_setting("company_context", "")
        latest, pinned = self.watch_numbers()
        return {
            "ai_instructions": {"value": instr if instr is not None else DEFAULT_INSTRUCTIONS, "is_default": instr is None,
                                "max_chars": config.AI_INSTRUCTIONS_MAX_CHARS,
                                "can_undo": db.setting_version_count("ai_instructions") > 0},
            "company_context": {"value": ctx, "chars": len(ctx), "sections": len(split_chunks(ctx)) if ctx.strip() else 0,
                                "max_chars": config.AI_CONTEXT_MAX_CHARS,
                                "can_undo": db.setting_version_count("company_context") > 0},
            "notify_questions": notifier.questions_enabled(db),
            "watch": {"latest": latest, "pinned": pinned},
            "ai_budget": {**self._ai_state(), "max": 1000},
            "posting_enabled": bool(config.POSTING_ENABLED),
            "link_comments_per_day": config.LINK_COMMENTS_PER_DAY,
            "read_limit": int(db.get_setting("read_limit", config.MAX_COMMENT_PAGES * 20)),
            "brand_name": config.BRAND_NAME,
            "ai_key": {"set": bool(config.OPENROUTER_API_KEY), "hint": self._key_hint()},
            "poll_interval_seconds": config.POLL_INTERVAL_SECONDS}

    @staticmethod
    def _key_hint() -> str | None:
        """Enough of the AI key to recognise it, never the key itself."""
        key = config.OPENROUTER_API_KEY or ""
        return (key[:6] + "..." + key[-4:]) if len(key) >= 16 else ("set" if key else None)

    def apply_runtime(self) -> None:
        """Settings the desktop app lets the person change are kept in the database; put them into effect (at start-up)."""
        get = self.db.get_setting
        if get("posting_enabled") is not None:
            config.POSTING_ENABLED = get("posting_enabled") == "1"
        if get("link_comments_per_day") is not None:
            config.LINK_COMMENTS_PER_DAY = int(get("link_comments_per_day"))
        if get("openrouter_api_key"):
            config.OPENROUTER_API_KEY = get("openrouter_api_key")
        if get("brand_name"):
            config.BRAND_NAME = get("brand_name")

    def update_settings(self, patch: dict) -> dict:
        """Partial update, validated like the Telegram commands. Nothing is applied unless EVERYTHING in the request is valid."""
        db, todo = self.db, []
        if "ai_instructions" in patch:
            text = str(patch["ai_instructions"] or "").strip()
            if not text or len(text) > config.AI_INSTRUCTIONS_MAX_CHARS:
                raise ControlError("bad_request", f"The AI instructions must be 1-{config.AI_INSTRUCTIONS_MAX_CHARS} characters.")
            todo.append(lambda: db.set_setting("ai_instructions", text))
        if "company_context" in patch:
            text = str(patch["company_context"] or "").strip()
            new = (db.get_setting("company_context", "").rstrip() + "\n\n" + text).strip() \
                if patch.get("company_context_append") else text
            if len(new) > config.AI_CONTEXT_MAX_CHARS:
                raise ControlError("bad_request", f"The company context may be at most {config.AI_CONTEXT_MAX_CHARS} characters "
                                                  f"(this one would be {len(new)}).")
            todo.append(lambda: db.set_setting("company_context", new) if new else db.delete_setting("company_context"))
        if "notify_questions" in patch:
            if not isinstance(patch["notify_questions"], bool):
                raise ControlError("bad_request", "notify_questions must be true or false.")
            on = patch["notify_questions"]
            todo.append(lambda: db.set_setting("notify_questions", "1" if on else "0"))
        for key in ("watch_latest", "watch_pinned"):
            if key in patch:
                v = patch[key]
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10:
                    raise ControlError("bad_request", f"{key} must be a whole number from 0 to 10.")
                todo.append(lambda k=key, n=v: db.set_setting(k, str(n)))
        if "ai_daily_requests" in patch:
            v = patch["ai_daily_requests"]
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000:
                raise ControlError("bad_request", "ai_daily_requests must be a whole number from 0 to 1000.")
            todo.append(lambda n=v: db.set_setting("ai_daily_requests", str(n)))
        if "posting_enabled" in patch:
            if not isinstance(patch["posting_enabled"], bool):
                raise ControlError("bad_request", "posting_enabled must be true or false.")
            on = patch["posting_enabled"]

            def apply_posting(on=on):
                db.set_setting("posting_enabled", "1" if on else "0")
                config.POSTING_ENABLED = on
            todo.append(apply_posting)
        if "link_comments_per_day" in patch:
            v = patch["link_comments_per_day"]
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 500:
                raise ControlError("bad_request", "link_comments_per_day must be a whole number from 0 (no limit) to 500.")

            def apply_cap(v=v):
                db.set_setting("link_comments_per_day", str(v))
                config.LINK_COMMENTS_PER_DAY = v
            todo.append(apply_cap)
        if "read_limit" in patch:
            v = patch["read_limit"]
            if isinstance(v, bool) or not isinstance(v, int) or not 20 <= v <= 1000:
                raise ControlError("bad_request", "read_limit must be a whole number from 20 to 1000.")
            todo.append(lambda v=v: db.set_setting("read_limit", str(v)))
        if "brand_name" in patch:
            name = str(patch["brand_name"] or "").strip()
            if not 1 <= len(name) <= 80:
                raise ControlError("bad_request", "The brand name must be 1-80 characters.")

            def apply_brand(name=name):
                db.set_setting("brand_name", name)
                config.BRAND_NAME = name
            todo.append(apply_brand)
        if "openrouter_api_key" in patch:
            key = str(patch["openrouter_api_key"] or "").strip()
            if key and (len(key) < 16 or len(key) > 300 or any(ch.isspace() for ch in key)):
                raise ControlError("bad_request", "That does not look like an OpenRouter API key.")

            def apply_key(key=key):
                if key:
                    db.set_setting("openrouter_api_key", key)
                    config.OPENROUTER_API_KEY = key
                else:                                    # empty = go back to the key in .env
                    db.delete_setting("openrouter_api_key")
                    config.OPENROUTER_API_KEY = self._env_key
            todo.append(apply_key)
        unknown = set(patch) - {"ai_instructions", "company_context", "company_context_append", "notify_questions",
                                "watch_latest", "watch_pinned", "ai_daily_requests", "posting_enabled", "link_comments_per_day",
                                "read_limit", "brand_name", "openrouter_api_key"}
        if unknown:
            raise ControlError("bad_request", f"Unknown setting(s): {', '.join(sorted(unknown))}.")
        for apply in todo:
            apply()
        return self.settings()

    def reset_setting(self, key: str) -> dict:
        """Back to the built-in default (ai_instructions) / empty (company_context)."""
        if key not in ("ai_instructions", "company_context"):
            raise ControlError("bad_request", "Only ai_instructions and company_context can be reset.")
        self.db.delete_setting(key)
        return self.settings()

    def undo_setting(self, key: str) -> dict:
        if key not in ("ai_instructions", "company_context"):
            raise ControlError("bad_request", "Only ai_instructions and company_context have earlier versions.")
        if not self.db.undo_setting(key):
            raise ControlError("bad_request", "There is no earlier version to restore.")
        return self.settings()

    def test_ai(self, text: str) -> dict:
        """What reply a sample comment would get (costs one AI request)."""
        if not (text or "").strip():
            raise ControlError("bad_request", "Send a sample comment.")
        reply, model, err = self.ai.draft_reply({"text": text, "username": "test", "category": "other"}, kind="test")
        if not reply:
            raise ControlError("ai_failed", f"Could not draft a reply: {err}", detail=err)
        return {"reply": reply, "model": model}

    def backfill(self, days: int) -> dict:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ControlError("bad_request", "days must be a whole number from 1 to 365.")
        n = self.db.backfill_legacy(days)
        return {"queued": n, "ai_requests": -(-n // config.AI_BATCH_SIZE) if n else 0, "days": days}

    def cleanup_now(self) -> dict:
        """Free disk space and wait for the answer (the desktop app shows the numbers right away)."""
        report = self.hooks.run_cleanup()
        return {"freed_mb": report["freed_mb_total"], "db_before_mb": report["db"]["db_before_mb"],
                "db_after_mb": report["db"]["db_after_mb"]}

    def start_cleanup(self) -> dict:
        """Free disk space now. Runs in the background; the report arrives as a `cleanup.done` event."""
        def run():
            try:
                report = self.hooks.run_cleanup()
                if self.bus:
                    self.bus.publish("cleanup.done", {"freed_mb": report["freed_mb_total"],
                                                      "db_before_mb": report["db"]["db_before_mb"],
                                                      "db_after_mb": report["db"]["db_after_mb"]})
            except Exception as exc:  # noqa: BLE001
                if self.bus:
                    self.bus.publish("cleanup.done", {"error": str(exc)[:200]}, "error")
        threading.Thread(target=run, name="cleanup", daemon=True).start()
        return {"started": True}

    # ------------------------------------------------------------------ the desktop app: analyse a post, answer its comments
    def _need_analyzer(self):
        if self.analyzer is None:
            raise ControlError("not_available", "Analysing a post is not available in this mode.")
        return self.analyzer

    def analyze(self, url: str, account: str | None = None) -> dict:
        """Start reading and judging the comments of the post behind `url` (any TikTok post link)."""
        return self._need_analyzer().start(str(url or ""), account or None)

    def analysis_current(self) -> dict | None:
        return self.analyzer.current() if self.analyzer else None

    def analysis_job(self, job_id: str) -> dict:
        return self._need_analyzer().job(job_id)

    def analysis_cancel(self, job_id: str) -> dict:
        return self._need_analyzer().cancel(job_id)

    def analysis_result(self, video_id: str) -> dict:
        return self._need_analyzer().result(video_id)

    @staticmethod
    def _author_of(url: str | None) -> str | None:
        if url and "/@" in url and "/@_/" not in url:
            return url.split("/@")[1].split("/")[0]
        return None

    def recent_posts(self, limit: int = 8) -> list:
        return [{"video_id": p["video_id"], "url": p["url"], "description": p.get("description") or "",
                 "author": self._author_of(p["url"]), "comment_count": p["comment_count"], "negatives": p["negatives"],
                 "questions": p["questions"], "stored": p["stored"], "last_checked_at": p["last_checked_at"]}
                for p in self.db.recent_analyzed_posts(max(1, min(int(limit), 30)))]

    def reply_to_comment(self, video_id: str, cid: str, account: str | None = None) -> dict:
        """A reply card for one comment of an analysed post - nothing is sent until request_post(). One open card per comment:
        asking again returns the card that is already there (`existing: true`)."""
        comment = self.db.get_comment(cid)
        if not comment or comment["video_id"] != video_id:
            raise ControlError("not_found", "That comment is not in the analysed post.")
        if account and account not in config.account_chain():
            raise ControlError("bad_request", f"'{account}' is not an active account.")
        existing = self.db.open_approval_for(cid)
        if existing:
            if account is not None and (existing.get("account") or None) != (account or None) and existing["status"] in OPEN:
                self.db.update_approval(existing["id"], account=account or None)
            view = self.approval(existing["id"])
            view["existing"] = True
            return view
        post = self.db.get_post(video_id) or {}
        approval_id = uuid.uuid4().hex[:10]
        self.db.create_approval({
            "id": approval_id, "cid": cid, "video_id": video_id,
            "video_url": post.get("url") or f"https://www.tiktok.com/@_/video/{video_id}", "username": comment.get("username"),
            "comment_text": comment.get("text"), "lang": comment.get("lang"), "category": comment.get("category"),
            "severity": comment.get("severity"), "reason": comment.get("reason"), "reply": None, "status": "pending",
            "chat_id": "", "kind": "link_reply", "account": account or None})
        view = self.approval(approval_id)
        view["existing"] = False
        return view

    def _accounts_summary(self) -> dict:
        rows = self.accounts.list()["accounts"] if self.accounts else []
        usable = [a for a in rows if a["enabled"]]
        return {"total": len(rows), "usable": len(usable), "ready": sum(a["session"] == "ok" for a in usable),
                "need_login": sum(a["session"] == "expired" for a in usable),
                "unchecked": sum(a["session"] == "unknown" for a in usable)}

    def activity(self) -> dict:
        """What is in flight right now - a reply on its way to TikTok, a post being read - for the strip that is visible on
        every page. Cheap: it only looks at memory and one small query."""
        stage = getattr(self.hooks, "send_stage", None)
        sending = []
        for a in self.db.list_approvals(("posting",), 20):
            st = stage(a["id"]) if stage else None
            sending.append({"id": a["id"], "username": a.get("username"), "account": notifier.account_label(a.get("account")),
                            "text": st["text"] if st else "Waiting for the browser...", "seconds": st["seconds"] if st else 0})
        checking = []
        for approval_id in sorted(getattr(self.hooks, "_checking", ())):
            st = stage(approval_id) if stage else None
            checking.append({"id": approval_id, "text": st["text"] if st else "Waiting for the browser...",
                             "seconds": st["seconds"] if st else 0})
        job = self.analysis_current()
        reading = None
        if job and not job.get("finished"):
            reading = {"id": job["id"], "message": job["message"], "seconds": int(time.time() - job["started_at"]),
                       "progress": job["progress"], "url": job["url"]}
        return {"busy": bool(sending or reading or checking), "sending": sending, "reading": reading, "checking": checking}

    def health(self, internet: bool = False) -> dict:
        """Is this computer ready to do the job? (browser, accounts, AI key, disk ...) - see app/health.py."""
        from app import health
        items = health.run(self.db, accounts=self._accounts_summary(), internet=internet)
        return {"status": health.worst(items), "items": items}

    def diagnostics(self) -> dict:
        """Everything a person needs to send when something is wrong: versions, the checks, the recent warnings and the end
        of the log file (secrets masked)."""
        import platform
        from app import logs
        problems = [{"at": e["ts"], "level": e["level"], "kind": e["kind"], "detail": e["detail"]}
                    for e in self.db.recent_events(80) if e["level"] in ("warn", "error")][:25]
        from app import __version__, backup
        return {"app": {"version": __version__, "python": platform.python_version(), "platform": platform.platform(),
                        "data_dir": str(config.DATA_DIR), "log_file": str(logs.path()), "uptime_seconds": int(time.time() - self.started_at)},
                "health": self.health(internet=True), "problems": problems, "log": logs.tail(60),
                "backups": backup.list_backups()}

    def dashboard(self) -> dict:
        """What the home screen shows at a glance."""
        return {"posting_enabled": bool(config.POSTING_ENABLED), "ai": self._ai_state(),
                "accounts": self._accounts_summary(),
                "replies": {"today": self.db.replies_posted_today(), "daily_limit": config.LINK_COMMENTS_PER_DAY,
                            "counted_today": self.db.link_posts_today()},
                "waiting": len(self.db.list_approvals(("pending", "post_failed"), 500)),
                "recent_posts": self.recent_posts(6), "analysis": self.analysis_current()}