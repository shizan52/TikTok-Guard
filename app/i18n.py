"""Bot interface texts (Telegram). English only - the bot always speaks English to its operators.
Texts are HTML (parse_mode=HTML): callers must html.escape() any user-provided value."""

STRINGS = {
    "en": {
        # cards
        "card_neg": "\U0001F6A8 <b>Negative comment</b>",
        "card_q": "❓ <b>Question that needs an answer</b>",
        "sev_1": "mild", "sev_2": "serious", "sev_3": "hostile / accusation",
        "cat_price": "price", "cat_delivery": "delivery", "cat_quality": "quality", "cat_service": "service",
        "cat_scam": "scam accusation", "cat_other": "other",
        "post_pinned": "\U0001F4CC Pinned post",
        "post_latest": "\U0001F195 Latest post #{n}",
        "post_line": "{kind} · \U0001F4AC {count} comments",
        "from_line": "\U0001F464 <b>@{user}</b> · ❤️ {likes} · \U0001F552 {when}",
        "why_line": "\U0001F9E0 <i>{why}</i>",
        "reply_line": "✍️ <b>Suggested reply</b> ({code}):",
        "no_reply": "⏳ <i>No reply drafted yet (AI budget). Tap ✍️ Draft or write your own with ✏️ Edit.</i>",
        "btn_post": "✅ Post reply", "btn_edit": "✏️ Edit", "btn_regen": "\U0001F504 Regenerate",
        "btn_ignore": "\U0001F6AB Ignore", "btn_draft": "✍️ Draft reply",
        "st_posting": "⏳ Posting to TikTok… (this can take a few minutes)",
        "st_posted": "✅ <b>Posted on TikTok</b> as @{account}",
        "st_failed": "⚠️ <b>Could not post automatically</b>\n{error}\nYou can reply by hand.",
        "st_ignored": "\U0001F6AB Ignored",
        "proof_caption": "\u2705 <b>Reply posted on TikTok</b> as @{account}\n\U0001F4AC @{user}: \u201c{comment}\u201d\n\u270D\ufe0f <b>{reply}</b>\n\U0001F4F8 Screenshot taken right after posting.",
        "st_ready": "✅ Approved — not posted ({reason})",
        "st_expired": "⌛ Expired",
        "reason_posting_off": "posting is disabled: set POSTING_ENABLED=true",
        "reason_simulated": "simulated example",
        "ask_edit": "✏️ Send the new reply text as your next message (max 150 characters). /cancel to abort.",
        "edit_saved": "✅ Reply updated.",
        "edit_empty": "The text is empty - nothing was changed. Send the new text, or /cancel.",
        "edit_too_long": "That is {n} characters; TikTok allows 150. It will be shortened to:\n{text}",
        "regen_limit": "Regenerate limit reached for this comment (3).",
        "regen_wait": "\U0001F504 Generating a new reply…",
        "draft_failed": "Could not draft a reply: {error}",
        "already_handled": "Already handled.",
        "unknown_approval": "Unknown approval.",
        # generic
        "unauthorized": "This bot is private.",
        "cancelled": "Cancelled.",
        "nothing_to_cancel": "Nothing to cancel.",
        "unknown_command": "Unknown command. Send /help.",
        "state_expired": "That edit session expired. Send the command again.",
        # help
        "help": (
            "<b>Commands</b>\n"
            "/ai — how the AI replies (view / edit)\n"
            "/ai_context — company information the AI uses (view / edit)\n"
            "/status — health, AI budget, database\n"
            "/pending — replies waiting for your decision\n"
            "/posts — the posts being watched\n"
            "/questions on|off — customer questions get an alert + a drafted answer (on by default)\n"
            "/watch — how many posts to watch (/watch latest 5, /watch pinned 2)\n"
            "/budget N — AI requests per day (free tier is small)\n"
            "/backfill [days] — also analyse older comments seen on the first scan (default 30 days)\n"
            "/accounts — TikTok accounts used for posting\n"
            "/comment &lt;link&gt; [text] — comment on any TikTok post, or reply to a comment, from its link "
            "(or just paste the link here)\n"
            "/cleanup — free disk space now\n"
            "/cancel — abort an edit"),
        # /comment <link>
        "lk_title": "\U0001F517 <b>Comment on a TikTok link</b>",
        "lk_target_post": "\U0001F3AC New comment on a post",
        "lk_target_comment": "\U0001F4AC Reply to a comment",
        "lk_as_line": "\U0001F464 Posted by: {who}",
        "lk_auto": "<b>automatic</b> (the first account that works)",
        "lk_dupe": "⚠️ Already posted here {when} (by @{account}).",
        "lk_text_line": "✍️ <b>Comment</b> ({n}/150):",
        "lk_no_text": "<i>No text yet. Write it with ✏️ Write, or describe what it should say and let ✨ AI write it.</i>",
        "btn_lk_post": "✅ Post comment", "btn_lk_edit": "✏️ Write", "btn_lk_ai": "✨ AI write",
        "btn_lk_account": "\U0001F464 {who}", "btn_lk_cancel": "\U0001F6AB Cancel",
        "lk_ask": "\U0001F517 Send the TikTok link of the post or comment (add the comment text after the link if you like). "
                  "/cancel to abort.",
        "lk_none": "I could not find a link in that message.\nUse: <code>/comment https://vm.tiktok.com/… your comment</code>\n"
                   "Or just paste the link here. In TikTok: open the post (or hold a comment) → Share → Copy link.",
        "lk_checking": "\U0001F50E Checking the link…",
        "lk_err_no_link": "I could not find a link in that message. Send the TikTok link of the post or comment.",
        "lk_err_not_tiktok": "That is not a TikTok link. Send a link from tiktok.com (or a vm.tiktok.com short link).",
        "lk_err_no_post": "That link does not point to a post or a comment (a profile or hashtag page cannot be commented on). "
                          "In TikTok open the video (or hold the comment) → Share → Copy link, and send that.",
        "lk_err_unresolved": "I could not open that link right now (it may have expired, or TikTok did not answer). "
                             "Try again in a minute, or send the full link copied from the browser.",
        "lk_already_open": "This link already has an open card above. Finish or cancel it first (/pending lists open ones).",
        "lk_ask_edit": "✏️ Send the comment text as your next message (max 150 characters). /cancel to abort.",
        "lk_ask_brief": "✨ In a few words, what should the comment say? For example: “thank them and invite them to visit "
                        "our store”. I will write it. /cancel to abort.",
        "lk_saved": "✅ Comment text updated.",
        "lk_no_text_alert": "Write the comment first: tap ✏️ Write or ✨ AI write.",
        "lk_ai_budget": "The AI budget for today is used up. Write the comment yourself with ✏️ Write.",
        "lk_ai_failed": "Could not write the comment: {error}\nYou can write it yourself with ✏️ Write.",
        "lk_cap": "Daily limit reached: {n} link comments were already posted today (LINK_COMMENTS_PER_DAY). "
                  "This protects the accounts from looking like spam. Try again tomorrow (UTC time), or raise the limit "
                  "(Controls in the desktop app, LINK_COMMENTS_PER_DAY in .env).",
        "lk_account_now": "\U0001F464 {who}",
        "lk_pending_post": "\U0001F517 post",
        "lk_pending_comment": "\U0001F517 reply",
        "st_failed_link": "⚠️ <b>Could not post automatically</b>\n{error}\nTap ✅ to try again, or comment by hand in TikTok.",
        "lk_proof_caption": "✅ <b>Comment posted on TikTok</b> as @{account}\n\U0001F517 {target}\n✍️ <b>{text}</b>\n"
                            "\U0001F4F8 Screenshot taken right after posting.",
        # /ai
        "ai_title": "\U0001F916 <b>AI reply instructions</b> ({n} characters)",
        "ai_hint": "These rules shape every reply the AI drafts. Tap ✏️ Edit and send the new text, "
                   "or: <code>/ai your new instructions</code>",
        "ai_saved": "✅ AI instructions saved ({n} characters).",
        "ai_too_long": "Too long ({n} characters). Maximum is {max}.",
        "ai_reset_done": "♻️ Back to the default instructions.",
        "ai_undo_done": "↩️ Previous version restored.",
        "ai_undo_none": "No earlier version to restore.",
        "ai_ask": "✏️ Send the new AI instructions as your next message (max {max} characters). /cancel to abort.",
        "ai_test_ask": "\U0001F9EA Send a sample comment and I will show the reply it would get (costs 1 AI request).",
        "ai_test_result": "\U0001F9EA <b>Sample reply</b>\n{reply}",
        "btn_ai_edit": "✏️ Edit", "btn_ai_reset": "♻️ Default", "btn_ai_undo": "↩️ Undo",
        "btn_ai_test": "\U0001F9EA Test",
        # /ai_context
        "cx_title": "\U0001F3E2 <b>Company context</b> ({n} characters, {chunks} sections)",
        "cx_empty": "<i>Nothing yet.</i> Tell the AI about the company: what it sells, addresses, opening hours, "
                    "shipping and return policies, payment methods, FAQs… The more precise, the better the answers.",
        "cx_hint": "Only the sections relevant to each comment are sent to the AI, so a long text does not waste "
                   "the budget. ✏️ Replace, ➕ Add, or send a .txt file.",
        "cx_saved": "✅ Company context saved ({n} characters).",
        "cx_added": "✅ Added. Context is now {n} characters.",
        "cx_too_long": "Too long ({n} characters). Maximum is {max}.",
        "cx_reset_done": "\U0001F5D1 Company context cleared.",
        "cx_undo_done": "↩️ Previous version restored.",
        "cx_undo_none": "No earlier version to restore.",
        "cx_ask_replace": "✏️ Send the NEW company context (replaces everything) as your next message, or upload a .txt file. /cancel to abort.",
        "cx_ask_append": "➕ Send the text to ADD to the company context, or upload a .txt file. /cancel to abort.",
        "cx_file_bad": "I can only read plain text files (.txt / .md) up to 200 KB.",
        "cx_truncated": "… ({more} more characters — tap \U0001F4C4 Download for the full text)",
        "btn_cx_replace": "✏️ Replace", "btn_cx_add": "➕ Add", "btn_cx_undo": "↩️ Undo",
        "btn_cx_clear": "\U0001F5D1 Clear", "btn_cx_download": "\U0001F4C4 Download",
        # status & others
        "status": (
            "<b>Status</b>\n"
            "Monitoring: always on (TikTok is checked every {every} s)\n"
            "Last scan: {last_scan}\n"
            "AI today: {budget}\n"
            "Posts watched: {posts} · comments stored: {comments}\n"
            "Waiting for you: {pending} · waiting to be announced: {backlog}\n"
            "Database: {db_mb} MB (limit {db_max} MB)\n"
            "Posting: {posting} · accounts: {accounts}"),
        "on": "on", "off": "off",
        "never": "never", "posting_on": "enabled", "posting_off": "disabled",
        "pending_none": "Nothing is waiting for you. ✅",
        "pending_title": "<b>Waiting for your decision</b> ({n})",
        "posts_title": "<b>Watched posts</b>",
        "posts_empty": "No posts yet - the first check runs within a minute or two of the bot starting.",
        "scan_done": "✅ Scan finished: {summary}",
        "scan_failed": "⚠️ Scan failed: {error}",
        "always_on": "Monitoring is always on - TikTok is checked every {every} s by itself, so there is nothing to start, "
                     "pause or resume. Use /status to see the last check.",
        "questions_state": "Question alerts are {state}.",
        "watch_state": "Watching the latest {latest} posts and {pinned} pinned posts.",
        "watch_set": "✅ Now watching the latest {latest} posts and {pinned} pinned posts.",
        "watch_bad": "Use: /watch latest 5   or   /watch pinned 2   (0–10)",
        "budget_state": "AI budget: {budget}. Change with /budget N.",
        "budget_set": "✅ AI budget set to {n} requests per day.",
        "budget_bad": "Use: /budget 40",
        "backfill_done": "✅ {n} older comment(s) queued for analysis (about {req} AI request(s)). The next rounds work through them; alerts arrive in batches of {burst}.",
        "backfill_none": "No older stored comments to analyse in the last {days} days.",
        "backfill_bad": "Use: /backfill 30   (1–365 days)",
        "cleanup_running": "\U0001F9F9 Cleaning…",
        "cleanup_done": "\U0001F9F9 Done: freed about {mb} MB. Database {before} → {after} MB.",
        "accounts_title": "<b>TikTok accounts</b> (used in this order)",
        "alert_backlog": "ℹ️ {n} more alert(s) are waiting; they will arrive over the next scans.",
        "alert_budget": "⚠️ The free AI quota for today is used up. Replies are paused until tomorrow (UTC); "
                        "comments with strongly negative words are still announced.",
        "scan_error_alert": "⚠️ TikTok scan problem: {error}",
        "st_posted_limited": "⚠️ TikTok shows this reply ONLY to the account that posted it - customers cannot see it.",
        "reply_limited": ("⚠️ The reply was sent, but TikTok published it as visible only to {account}: nobody else can see it. "
                          "TikTok decides this on its own servers (account trust or the page's comment filters). Replies from the "
                          "page's own account are not hidden - add that account (python run.py add-account) and set "
                          "REPLY_ACCOUNT_FIRST in .env. You can delete this reply in TikTok."),
        "captcha_alert": "⚠️ TikTok is showing a security check (captcha), so opening posts is paused for {minutes} min. "
                         "The bot retries by itself and nothing is lost - new comments are read as soon as it clears. "
                         "You will not be told again unless it keeps happening.",
        "ago_min": "{n} min ago", "ago_h": "{n} h ago", "ago_d": "{n} d ago", "just_now": "just now",
    },
}


def tr(lang: str, key: str, /, **kw) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    text = table.get(key) or STRINGS["en"].get(key) or key
    return text.format(**kw) if kw else text
