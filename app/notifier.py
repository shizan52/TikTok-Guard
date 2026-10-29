"""Telegram alert cards: what the user sees for a negative comment, and the approval records
behind the buttons. One approval per comment (enforced by a unique index in the database)."""
import calendar
import html
import time
import uuid

import config
from app.ai import AI, sanitize_reply
from app.db import DB
from app.i18n import STRINGS, tr
from app.lang import lang_name, resolve_language

OPEN = ("pending", "approved", "posting")
MAX_REGEN = 3


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def age_text(ts, lang: str) -> str:
    if not ts:
        return "-"
    secs = max(0, int(time.time()) - int(ts))
    if secs < 90:
        return tr(lang, "just_now")
    if secs < 5400:
        return tr(lang, "ago_min", n=secs // 60)
    if secs < 172800:
        return tr(lang, "ago_h", n=secs // 3600)
    return tr(lang, "ago_d", n=secs // 86400)


def _post_kind(db: DB, post: dict | None, lang: str) -> str:
    if not post:
        return "-"
    if post.get("is_pinned"):
        return tr(lang, "post_pinned")
    watched = db.watched_posts()
    pinned = sum(1 for p in watched if p.get("is_pinned"))
    rank = post.get("watch_rank")
    n = (rank - pinned) if rank else 0
    return tr(lang, "post_latest", n=n if n > 0 else "?")


def questions_enabled(db: DB) -> bool:
    """Are customer questions announced (and answered) like complaints? On unless the operator sent /questions off."""
    return db.get_setting("notify_questions", "1" if config.NOTIFY_QUESTIONS_DEFAULT else "0") == "1"


def _iso_ts(value) -> int | None:
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:  # noqa: BLE001
        return None


def account_label(name: str | None) -> str:
    """Plain text for a button / a line: the account's @handle when it is registered, else its profile name; 'auto'
    for no preference (the first account of the chain that works)."""
    if not name:
        return "auto"
    for acc in config.load_accounts():
        if acc["name"] == name and acc.get("username"):
            return "@" + str(acc["username"]).lstrip("@")
    return name


def is_link_kind(a: dict) -> bool:
    return (a.get("kind") or "alert") != "alert"


def format_link_card(db: DB, a: dict, lang: str) -> str:
    """The card of a comment the operator asked for with /comment <link>: what it targets, who posts it, the text."""
    is_reply = a["kind"] == "link_reply"
    who = f"@{esc(a['username'])}" if a.get("username") else "TikTok"
    ref = f"…{esc(a['video_id'][-6:])}" + (f" · \U0001F4AC …{esc(a['cid'][-6:])}" if is_reply else "")
    lines = [tr(lang, "lk_title"), tr(lang, "lk_target_comment" if is_reply else "lk_target_post"),
             f'<a href="{esc(a["video_url"])}">{who} · {ref}</a>', ""]
    lines.append(tr(lang, "lk_as_line", who=tr(lang, "lk_auto") if not a.get("account")
                    else f"<b>{esc(account_label(a['account']))}</b>"))
    status = a["status"]
    if status in ("pending", "post_failed"):                      # a warning when the same target was commented today
        before = db.recent_link_post(a["cid"])
        if before:
            lines.append(tr(lang, "lk_dupe", when=age_text(_iso_ts(before.get("posted_at")), lang),
                            account=esc(account_label(before.get("posted_by")))))
    lines.append("")
    if a.get("reply"):
        lines.append(tr(lang, "lk_text_line", n=len(a["reply"])))
        lines.append(f"<b>{esc(a['reply'])}</b>")
    elif status in ("pending", "post_failed"):
        lines.append(tr(lang, "lk_no_text"))
    tail = {
        "posting": tr(lang, "st_posting"),
        "posted": tr(lang, "st_posted", account=esc(a.get("posted_by") or "?")),
        "post_failed": tr(lang, "st_failed_link", error=esc((a.get("last_error") or "")[:300])),
        "rejected": "\U0001F6AB " + tr(lang, "cancelled"),
        "expired": tr(lang, "st_expired"),
        "superseded": "\U0001F6AB " + tr(lang, "cancelled"),
    }.get(status)
    if status == "posted" and a.get("posted_visibility") == "limited":
        tail += "\n" + tr(lang, "st_posted_limited")
    if status == "approved":
        tail = tr(lang, "st_ready", reason=tr(lang, "reason_posting_off"))
    if tail:
        lines += ["", tail]
    return "\n".join(lines)


def link_keyboard(a: dict, lang: str) -> dict | None:
    if a["status"] not in ("pending", "post_failed"):
        return None
    i = a["id"]
    rows = []
    if a.get("reply"):
        rows.append([{"text": tr(lang, "btn_lk_post"), "callback_data": f"ap:post:{i}"}])
    rows.append([{"text": tr(lang, "btn_lk_edit"), "callback_data": f"ap:edit:{i}"},
                 {"text": tr(lang, "btn_lk_ai"), "callback_data": f"ap:ai:{i}"}])
    rows.append([{"text": tr(lang, "btn_lk_account", who=account_label(a["account"]) if a.get("account") else "Auto"),
                  "callback_data": f"ap:acct:{i}"},
                 {"text": tr(lang, "btn_lk_cancel"), "callback_data": f"ap:ignore:{i}"}])
    return {"inline_keyboard": rows}


def create_link_approval(db: DB, target, text: str = "", *, account: str = "", chat_id=None) -> str | None:
    """Store the card record of a /comment <link>. One open card per target: the comment id for a reply, a synthetic
    link-post:<video> for a new comment on a post (the unique index on open approvals does the de-duplication).
    Returns None when this target already has an open card."""
    return db.create_approval({
        "id": uuid.uuid4().hex[:10], "cid": target.comment_id or f"link-post:{target.video_id}",
        "video_id": target.video_id, "video_url": target.url, "username": target.username, "reply": text or None,
        "status": "pending", "chat_id": str(chat_id or config.TELEGRAM_CHAT_ID or ""),
        "kind": "link_reply" if target.comment_id else "link_post", "account": account or None})


def format_card(db: DB, a: dict, lang: str) -> str:
    if is_link_kind(a):
        return format_link_card(db, a, lang)
    comment = db.get_comment(a["cid"]) or {}
    post = db.get_post(a["video_id"])
    status = a["status"]
    title = tr(lang, "card_q") if (comment.get("status") == "question" or comment.get("sentiment") == "Q") \
        else tr(lang, "card_neg")
    sev = a.get("severity") or comment.get("severity") or 0
    cat = a.get("category") or comment.get("category") or "other"
    flag = {3: "\U0001F534", 2: "\U0001F7E0", 1: "\U0001F7E1"}.get(sev, "⚪")
    sev_text = tr(lang, f"sev_{sev}") if sev in (1, 2, 3) else ""
    cat_text = tr(lang, f"cat_{cat}") if f"cat_{cat}" in STRINGS["en"] else esc(cat)
    lines = ["🧪 <b>SIMULATED example</b> (not from TikTok)"] if a.get("simulated") else []
    lines.append(f"{title}  {flag} {sev_text} · {cat_text}".replace("  ⚪  ", "  "))
    if post:
        desc = esc((post.get("description") or "")[:70].replace("\n", " ")) or "post"
        link = f'<a href="{esc(post["url"])}">{desc}</a>'
        lines.append(f"{tr(lang, 'post_line', kind=_post_kind(db, post, lang), count=post.get('comment_count', 0))}\n{link}")
    lines.append("")
    lines.append(tr(lang, "from_line", user=esc(a.get("username") or "?"),
                    likes=comment.get("digg_count", 0), when=age_text(comment.get("create_ts"), lang)))
    lines.append(f"“{esc((a.get('comment_text') or '')[:500])}”")
    why = a.get("reason") or comment.get("reason")
    if why:
        lines.append(tr(lang, "why_line", why=esc(why)))
    lines.append("")
    if a.get("reply"):
        lines.append(tr(lang, "reply_line", code=esc(lang_name(a.get("lang")) or a.get("lang") or "?")))
        lines.append(f"<b>{esc(a['reply'])}</b>")
    elif status in ("pending", "post_failed"):
        lines.append(tr(lang, "no_reply"))
    tail = {
        "posting": tr(lang, "st_posting"),
        "posted": tr(lang, "st_posted", account=esc(a.get("posted_by") or "?")),
        "post_failed": tr(lang, "st_failed", error=esc((a.get("last_error") or "")[:300])),
        "rejected": tr(lang, "st_ignored"),
        "expired": tr(lang, "st_expired"),
        "superseded": tr(lang, "st_ignored"),
    }.get(status)
    if status == "posted" and a.get("posted_visibility") == "limited":
        tail += "\n" + tr(lang, "st_posted_limited")
    if status == "approved":
        reason = tr(lang, "reason_simulated") if a.get("simulated") else tr(lang, "reason_posting_off")
        tail = tr(lang, "st_ready", reason=reason)
    if tail:
        lines.append("")
        lines.append(tail)
    return "\n".join(lines)


def card_keyboard(a: dict, lang: str) -> dict | None:
    if is_link_kind(a):
        return link_keyboard(a, lang)
    i = a["id"]
    if a["status"] not in ("pending", "post_failed"):
        return None
    rows = []
    if a.get("reply"):
        rows.append([{"text": tr(lang, "btn_post"), "callback_data": f"ap:post:{i}"}])
        rows.append([{"text": tr(lang, "btn_edit"), "callback_data": f"ap:edit:{i}"},
                     {"text": tr(lang, "btn_regen"), "callback_data": f"ap:regen:{i}"},
                     {"text": tr(lang, "btn_ignore"), "callback_data": f"ap:ignore:{i}"}])
    else:
        rows.append([{"text": tr(lang, "btn_draft"), "callback_data": f"ap:regen:{i}"},
                     {"text": tr(lang, "btn_edit"), "callback_data": f"ap:edit:{i}"},
                     {"text": tr(lang, "btn_ignore"), "callback_data": f"ap:ignore:{i}"}])
    return {"inline_keyboard": rows}


def refresh_card(tg, db: DB, approval_id: str, lang: str) -> None:
    a = db.get_approval(approval_id)
    if not a or not a.get("message_id") or not a.get("chat_id"):
        return
    tg.edit_message_text(a["chat_id"], a["message_id"], format_card(db, a, lang), card_keyboard(a, lang))


def create_approval(db: DB, ai: AI | None, comment: dict, *, draft: bool = True, simulated: bool = False,
                    video_url: str | None = None) -> str | None:
    """Draft a reply (when the AI budget allows) and store the approval. None if one is already open."""
    post = db.get_post(comment["video_id"]) or {}
    reply = None
    if draft and ai is not None and ai.can("draft"):
        reply, _model, _err = ai.draft_reply(comment, post_desc=post.get("description") or "")
    approval_id = uuid.uuid4().hex[:10]
    return db.create_approval({
        "id": approval_id, "cid": comment["cid"], "video_id": comment["video_id"],
        "video_url": video_url or post.get("url"), "username": comment.get("username"),
        "comment_text": comment.get("text"), "lang": resolve_language(comment.get("text", ""), comment.get("lang")),
        "category": comment.get("category"),
        "severity": comment.get("severity"), "reason": comment.get("reason"), "reply": reply,
        "status": "pending", "chat_id": str(config.TELEGRAM_CHAT_ID or ""), "simulated": simulated})


def notify_pending(db: DB, ai: AI, tg, lang: str, *, include_questions: bool, burst: int) -> dict:
    """Announce comments that were judged negative (or questions) but have no approval card yet.
    Newest/most severe first, at most `burst` per call so a backlog never floods the chat."""
    todo = db.negatives_without_approval(include_questions, limit=burst + 50)
    sent = 0
    for comment in todo:
        if sent >= burst:
            break
        approval_id = create_approval(db, ai, comment, simulated=(comment['video_id'] == 'simulated'))
        if approval_id is None:
            continue
        a = db.get_approval(approval_id)
        if config.TELEGRAM_CHAT_ID:
            resp = tg.send_message(format_card(db, a, lang), card_keyboard(a, lang))
            mid = (resp.get("result") or {}).get("message_id")
            if mid:
                db.update_approval(approval_id, message_id=mid)
            else:
                db.log_event("telegram_send_failed", str(resp)[:300], "error")
        sent += 1
    left = max(0, len(todo) - sent)
    return {"sent": sent, "backlog": left}


def fill_missing_replies(db: DB, ai: AI, tg, lang: str, *, limit: int = 3) -> int:
    """When the AI budget returns (new UTC day, or /budget raised), draft the replies that were
    postponed and update their Telegram cards in place."""
    done = 0
    for a in db.approvals_missing_reply(limit):
        if not ai.can("draft"):
            break
        comment = db.get_comment(a["cid"]) or {"cid": a["cid"], "text": a["comment_text"], "lang": a["lang"],
                                              "username": a["username"], "category": a["category"]}
        post = db.get_post(a["video_id"]) or {}
        reply, _m, _e = ai.draft_reply(comment, post_desc=post.get("description") or "")
        if reply:
            db.update_approval(a["id"], reply=reply)
            refresh_card(tg, db, a["id"], lang)
            done += 1
    return done


def sanitize_manual_reply(text: str) -> tuple:
    """-> (clean_text, was_shortened)"""
    clean = sanitize_reply(text)
    return clean, clean != " ".join((text or "").split())
