"""Single entry point for the TikTok comment-watch bot.

    python run.py serve              # THE command: Telegram bot + TikTok monitoring + approvals +
                                      # maintenance in one process. Control it from Telegram (/help):
                                      #   /ai          how the AI replies            /status  health, AI budget, db
                                      #   /ai_context  company info for the AI       /pending replies waiting for you
    python run.py scan               # one monitoring pass now (read TikTok, analyse, alert)
    python run.py simulate           # canned PT/ES comments through the same pipeline (demo)
    python run.py maintain           # database + disk clean-up now (also runs daily inside serve)
    python run.py db-stats           # database size, rows, AI usage, watched posts
    python run.py doctor             # check env/tools/credentials
    python run.py setup              # link your Telegram chat (one-time)
    python run.py login              # visible browser: log in to TikTok by hand
    python run.py add-account [NAME] # log in ONE more TikTok account for the posting fallback chain
    python run.py accounts           # list the accounts post_reply() tries, in order
    python run.py comment LINK TEXT  # comment on a post / reply to a comment from its link (dry run; --post sends it)

Replies are only ever posted for a comment YOU approved with the button on its Telegram card, through
the TikTok accounts you logged in yourself. With POSTING_ENABLED=false the button only marks it ready.
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
from app import telegram_notify, tiktok_monitor


def cmd_doctor(_args) -> int:
    ok = True
    print("== credentials ==")
    if config.OPENROUTER_API_KEY:
        print("[ok] OPENROUTER_API_KEY set")
    else:
        print("[!!] OPENROUTER_API_KEY missing -- set it in .env")
        ok = False

    if config.TELEGRAM_BOT_TOKEN:
        try:
            me = telegram_notify.get_me()
            print(f"[ok] TELEGRAM_BOT_TOKEN valid -- bot @{me['result']['username']}")
        except Exception as exc:  # noqa: BLE001
            print(f"[!!] TELEGRAM_BOT_TOKEN set but getMe failed: {exc}")
            ok = False
    else:
        print("[!!] TELEGRAM_BOT_TOKEN missing -- set it in .env")
        ok = False

    if config.TELEGRAM_CHAT_ID:
        print(f"[ok] TELEGRAM_CHAT_ID set ({config.TELEGRAM_CHAT_ID})")
    else:
        print("[!!] TELEGRAM_CHAT_ID not set yet -- run: python run.py setup")
        ok = False

    print("== target ==")
    print(f"profile: {config.TARGET_PROFILE_URL}")
    print(f"watching: latest {config.WATCH_LATEST_POSTS} + {config.WATCH_PINNED_POSTS} pinned posts "
          f"(adjustable with /watch), scan every {config.POLL_INTERVAL_SECONDS}s")

    print("== database / AI ==")
    try:
        from app.ai import AI
        db = _prepare_db()
        print(f"[ok] {db.path} ({round(db.size_bytes() / 1048576, 2)} MB, limit {config.DB_MAX_MB} MB), "
              f"rows: {db.table_counts()}")
        ai = AI(db)
        print(f"[ok] AI today: {ai.budget_text()}")
        print(f"[ok] model order: {' > '.join(ai.chain())}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!!] database/AI check failed: {exc}")
        ok = False

    print("== posting ==")
    print(f"POSTING_ENABLED={config.POSTING_ENABLED}; accounts in order: {' -> '.join(config.account_chain())}")
    return 0 if ok else 1


def cmd_setup(_args) -> int:
    try:
        me = telegram_notify.get_me()
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach Telegram with this bot token: {exc}")
        return 1
    username = me["result"]["username"]

    if config.TELEGRAM_CHAT_ID:
        print(f"TELEGRAM_CHAT_ID already set to {config.TELEGRAM_CHAT_ID}.")
        print("Sending a confirmation message...")
        telegram_notify.send_message(f"✅ Setup confirmed -- @{username} is connected and ready.")
        return 0

    print(f"Bot is @{username}. Open Telegram, search for it, and send it any message (e.g. /start).")
    print("Waiting for that message (up to 60s)...")
    deadline = time.monotonic() + 60
    chat_id = None
    while time.monotonic() < deadline and chat_id is None:
        updates = telegram_notify.get_updates(offset=0, timeout=0)
        for upd in updates.get("result", []):
            msg = upd.get("message")
            if msg and msg.get("chat", {}).get("id"):
                chat_id = msg["chat"]["id"]
                break
        if chat_id is None:
            time.sleep(2)

    if chat_id is None:
        print("No message received yet. Send the bot a message, then re-run: python run.py setup")
        return 1

    from app.env_loader import set_env_value
    set_env_value(config.ROOT / ".env", "TELEGRAM_CHAT_ID", str(chat_id))
    config.TELEGRAM_CHAT_ID = str(chat_id)
    print(f"Saved TELEGRAM_CHAT_ID={chat_id} to .env")
    telegram_notify.send_message(f"✅ Connected! @{username} will send comment alerts here.")
    return 0


def cmd_login(_args) -> int:
    print(f"Opening {config.TARGET_PROFILE_URL} in a VISIBLE browser window.")
    print("Log in there with YOUR TikTok account -- this tool never sees your password,")
    print("it only waits for the page to show you're logged in, then saves that session.")
    print("Waiting up to 10 minutes...")
    result = tiktok_monitor.wait_for_login(config.TARGET_PROFILE_URL, timeout_s=600)
    if result["logged_in"]:
        print(f"Logged in and session saved under profile {result['profile']!r}.")
        print("Every other command (demo, monitor, check-approvals) will reuse this session.")
        if config.TELEGRAM_CHAT_ID:
            telegram_notify.send_message(
                f"✅ Logged in to {config.TARGET_PROFILE_URL} -- the bot can now post approved replies "
                f"({'posting ENABLED' if config.POSTING_ENABLED else 'posting still disabled, set POSTING_ENABLED=true in .env'})."
            )
        return 0
    print("No login detected within the timeout. Run 'python run.py login' again when ready.")
    return 1


def cmd_add_account(args) -> int:
    from app import accounts
    flag = Path(args.done_flag) if args.done_flag else None
    result = accounts.add_account(args.name, done_flag=flag, timeout_s=args.timeout)
    if not result["registered"]:
        print("Not saved: no TikTok login was detected. Run the command again and log in.")
        return 1
    ident = result.get("identity") or {}
    user = result.get("username") or ident.get("username")
    region = result.get("region") or ident.get("region")
    if result.get("duplicate_of") or ident.get("duplicate"):
        print(f"Saved but DISABLED: @{user} is already in the chain (master or another account).")
    else:
        print(f"Saved {result['name']!r} (@{user}, region {region}).")
    return 0


def cmd_accounts(args) -> int:
    if args.refresh:
        from app import accounts
        for a in config.load_accounts():
            if args.refresh == "all" or a["name"] == args.refresh:
                print("refresh:", accounts.refresh_identity(a["name"]))
    print(f"1. {config.BFP_PROFILE_NAME}  (master, from .env)  -> {config.BFP_MASTER_USER_DATA_DIR}")
    for i, a in enumerate(config.load_accounts(), start=2):
        state = "on " if a.get("enabled", True) else "OFF"
        print(f"{i}. {a['name']}  [{state}] @{a.get('username')} region={a.get('region')} "
              f"added {a.get('added')}  -> {a['user_data_dir']}")
    print("order used by post_reply:", " -> ".join(config.account_chain()))
    return 0


def cmd_warm(_args) -> int:
    print(f"Running one warming session per account ({', '.join(config.BFP_PROFILE_NAMES)}) "
          f"- safe browsing only, no posting...")
    for result in tiktok_monitor.warm_all_accounts():
        print(f"  {result['profile']}: {result['actions_done']} actions over "
              f"~{result['minutes_budget']} min (age: {result['age_days']} day(s), "
              f"session #{result['sessions']}).")
    print("Run this between posting sessions every so often, not instead of spacing them out --")
    print("it builds real session activity, which is what actually lowers restriction risk.")
    return 0


def cmd_import_cookies(args) -> int:
    print(f"Importing cookies from {args.file} ...")
    result = tiktok_monitor.import_login_cookies(args.file)
    print(json.dumps(result, indent=2))
    if not result["has_session_cookie"]:
        print("\nNo TikTok session cookie (sessionid/sid_tt/sid_guard) found in that export.")
        print("Make sure you exported cookies while on tiktok.com in a browser where you're")
        print("already logged in (Cookie-Editor extension -> Export -> JSON).")
        return 1
    print("\nSession cookie found. Verifying against the live site...")
    check = tiktok_monitor.verify_login()
    print(json.dumps(check, indent=2))
    if check["logged_in"]:
        print("\nLogged in! Every other command (demo, monitor, check-approvals) will reuse this session.")
        if config.TELEGRAM_CHAT_ID:
            telegram_notify.send_message(
                f"✅ TikTok session imported for {config.TARGET_PROFILE_URL} -- the bot can now post "
                f"approved replies ({'posting ENABLED' if config.POSTING_ENABLED else 'posting still disabled, set POSTING_ENABLED=true in .env'})."
            )
        return 0
    print("\nCookie was imported but the live check still shows logged out -- it may have expired "
          "already, or belongs to a different TikTok domain/session. See the screenshot at "
          f"{check.get('screenshot')}")
    return 1


def _prepare_db():
    """Open the database and import what the old JSON-file version had already seen (once)."""
    from app import migrate
    from app.db import DB
    db = DB()
    migrate.import_legacy(db)
    return db


def _setup_logging() -> None:
    """Warnings/errors (bfp, the service) into data/logs/bot.log: 2 MB x 3 files, never grows without bound."""
    import logging
    from logging.handlers import RotatingFileHandler
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_DIR / "bot.log", maxBytes=2 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    """The production process: TikTok monitoring + approvals + maintenance, controlled from Telegram (when it is set up)
    and/or from the desktop app through the local API (--api, or API_ENABLED=true)."""
    from app.service import Service
    _setup_logging()
    db = _prepare_db()
    svc = Service(db, api=True if getattr(args, "api", False) else None)
    print(f"Serving. Target {config.TARGET_PROFILE_URL} · scan every {config.POLL_INTERVAL_SECONDS}s · "
          f"watching the latest {db.get_setting('watch_latest', config.WATCH_LATEST_POSTS)} + "
          f"{db.get_setting('watch_pinned', config.WATCH_PINNED_POSTS)} pinned posts · "
          f"posting {'ENABLED' if config.POSTING_ENABLED else 'disabled'} · db {db.path}")
    svc.start()
    print("Telegram: " + ("on - control it with /help" if svc.telegram_enabled
                          else "off (no bot token / chat linked) - the desktop app is the control panel"))
    if svc.api_server is not None:
        print(f"Desktop API: {svc.api_server.url}/   (console page in a browser; token: python run.py api-token)")
    elif svc._api_wanted:
        print("Desktop API: could NOT start (is the port taken? see `python run.py db-stats` events / API_PORT).")
    print("Ctrl+C to stop.")
    try:
        while not svc.stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        svc.stop()
    return 0


def cmd_api_token(_args) -> int:
    """The token the desktop app (or the console page) must send with every API call."""
    from app import api
    print(api.load_token())
    return 0


def cmd_scan(args) -> int:
    """One monitoring pass right now (also the old `demo`): read TikTok, analyse, send alerts."""
    from app.service import Service
    db = _prepare_db()
    svc = Service(db)
    svc.do_scan(manual=True)
    print(f"last_scan_error: {db.meta_get('last_scan_error') or '-'}")
    print(json.dumps({"comments": db.comment_status_counts(), "ai": svc.ai.budget_text(),
                      "pending_approvals": len(db.list_approvals(('pending',), 500))}, indent=2))
    return 0


def cmd_comment(args) -> int:
    """Comment on a TikTok post (or reply to a comment) from its link - the command-line twin of /comment in Telegram.
    A DRY RUN unless --post is given: the page is opened, the text typed and checked, a screenshot saved, nothing sent."""
    from app import links, notifier
    try:
        target = links.resolve(args.link)
    except links.LinkError as err:
        print(f"Cannot use that link: {err.key}")
        return 2
    text, shortened = notifier.sanitize_manual_reply(args.text)
    if not text:
        print("The comment text is empty.")
        return 2
    live = bool(args.post)
    if live and not config.POSTING_ENABLED:
        print("POSTING_ENABLED is false in .env - refusing to post. (Without --post this is a safe dry run.)")
        return 2
    print(f"target : {target.kind} {target.url}" + (f"  (comment {target.comment_id})" if target.comment_id else ""))
    print(f"account: {args.account or 'auto (first that works)'}   mode: {'POST FOR REAL' if live else 'dry run - nothing is sent'}")
    print(f"text   : {text}" + ("   (shortened to TikTok's 150 characters)" if shortened else ""))
    res = tiktok_monitor.post_link_comment(target.url, text, cid=target.comment_id, account=args.account or None,
                                           dry_run=not live)
    shown = {k: res.get(k) for k in ("ok", "dry_run", "profile", "visibility", "error", "before_screenshot",
                                     "after_screenshot", "proof_screenshot") if res.get(k) is not None}
    shown["attempts"] = [f"#{t['attempt']} {t['account']}: {t['error'] or ('captcha' if t['captcha'] else 'ok')}"
                         for t in res.get("attempts", [])]
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_simulate(_args) -> int:
    """Canned PT/ES comments through the real classify -> draft -> Telegram-card pipeline."""
    import uuid
    from app import analysis, notifier
    from app.ai import AI
    from app.sample_comments import SIMULATED_COMMENTS
    from app import telegram_notify
    db = _prepare_db()
    ai = AI(db)
    db.upsert_post({"video_id": "simulated", "url": "https://www.tiktok.com/@simulated/video/0",
                    "description": "PT/ES sample set", "is_pinned": 0, "comment_count": len(SIMULATED_COMMENTS)})
    run = uuid.uuid4().hex[:6]
    comments = [{**c, "cid": f"{c['cid']}-{run}", "create_time": int(time.time()), "reply_count": 0}
                for c in SIMULATED_COMMENTS]
    analysis.ingest_comments(db, "simulated", comments, first_scan=False)
    stats = analysis.classify_pending(db, ai)
    sent = notifier.notify_pending(db, ai, telegram_notify, "en",
                                   include_questions=False, burst=20)
    print(f"{len(comments)} sample comments -> analysed {stats}; {sent['sent']} alert(s) sent to Telegram "
          f"(tagged SIMULATED, never postable).")
    return 0


def cmd_maintain(args) -> int:
    from app import maintenance
    db = _prepare_db()
    print(json.dumps(maintenance.run_all(db, force_vacuum=args.vacuum, force_caches=args.caches), indent=2))
    return 0


def cmd_db_stats(_args) -> int:
    db = _prepare_db()
    from app.ai import AI
    print(json.dumps({
        "file": str(db.path), "size_mb": round(db.size_bytes() / 1048576, 2), "limit_mb": config.DB_MAX_MB,
        "rows": db.table_counts(), "comments_by_status": db.comment_status_counts(),
        "ai_today": db.usage_for_day(), "ai_budget": AI(db).budget_text(),
        "watched_posts": [{"id": p["video_id"], "pinned": bool(p["is_pinned"]), "comments": p["comment_count"],
                           "checked": p["last_checked_at"]} for p in db.watched_posts()],
        "last_maintenance": db.meta_get("last_maintenance_report"),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_package(args) -> int:
    from app import deploy
    out = Path(args.out)
    only = [n for n in (args.accounts or "").split(",") if n.strip()] or None
    res = deploy.package(out, include_sessions=not args.no_sessions, include_db=not args.no_db, force=args.force,
                         only_accounts=only)
    print(f"\nbundle: {res['out']}  ({res['files']} files, {res['mb']} MB)")
    for p in res["profiles"]:
        print(f"  {p['name']}: @{p['username']}  cookies={p['cookies']} session_cookie={p['session_cookie']} "
              f"localStorage={p['local_storage_keys']} idb={p['idb']}")
    if res["problems"]:
        print("\nPROBLEMS - do not ship this bundle:")
        for pr in res["problems"]:
            print("  -", pr)
        return 1
    print("\nverified: no history, passwords, other sites' data, Local State, extensions or secrets inside.")
    if not args.no_sessions:
        print("NOTE: profiles/*/state.json contain live TikTok session cookies - send the bundle over an encrypted "
              "channel (SFTP/SCP); `restore-profiles` deletes them after use.")
    return 0


def cmd_restore_profiles(args) -> int:
    from app import deploy
    root = Path(args.root) if args.root else config.ROOT
    res = deploy.restore_profiles(Path(args.bundle), root, keep_secrets=args.keep_secrets)
    print("\nput this in .env on the server:\n  " + res["env_line"])
    if res.get("missing"):
        print("NOT restored yet:", ", ".join(res["missing"]), "- run this command again")
    print("all profiles logged in:", res["all_logged_in"])
    return 0 if res["all_logged_in"] else 1


def cmd_verify_bundle(args) -> int:
    from app import deploy
    problems = deploy.verify_bundle(Path(args.bundle))
    print("clean" if not problems else "\n".join(problems))
    return 1 if problems else 0


def cmd_check_approvals(_args) -> int:
    print("Approvals are now handled live by `python run.py serve` (buttons on each Telegram card).")
    print("Nothing to do here; this command is kept only so old scripts do not break.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check credentials and target config").set_defaults(func=cmd_doctor)
    sub.add_parser("setup", help="link your Telegram chat (one-time)").set_defaults(func=cmd_setup)
    sub.add_parser("login", help="open a visible browser for you to log in to TikTok").set_defaults(func=cmd_login)
    p_add = sub.add_parser("add-account", help="open a visible real-Brave window, log in to ONE more TikTok account, save it for the fallback chain")
    p_add.add_argument("name", nargs="?", help="account name (default: next free real-account-N)")
    p_add.add_argument("--done-flag", help="finish when this file appears (and a login is detected)")
    p_add.add_argument("--timeout", type=float, default=1800.0, help="seconds to wait for the login")
    p_add.set_defaults(func=cmd_add_account)
    p_acc = sub.add_parser("accounts", help="list the accounts post_reply() will try, in order")
    p_acc.add_argument("--refresh", metavar="NAME|all", help="re-read username/region of an account from its saved browser folder")
    p_acc.set_defaults(func=cmd_accounts)
    sub.add_parser("warm", help="run one safe browsing session to build organic account activity (no posting)").set_defaults(func=cmd_warm)

    p_cookies = sub.add_parser("import-cookies", help="import a session exported from your normal browser")
    p_cookies.add_argument("file", help="path to the Cookie-Editor JSON export")
    p_cookies.set_defaults(func=cmd_import_cookies)

    p_serve = sub.add_parser("serve", help="run the backend: TikTok monitoring + approvals + maintenance, controlled from Telegram and/or the desktop app")
    p_serve.add_argument("--api", action="store_true", help="also start the desktop app's local API on 127.0.0.1 (same as API_ENABLED=true)")
    p_serve.set_defaults(func=cmd_serve)
    sub.add_parser("api-token", help="print the token the desktop app must send to the local API").set_defaults(func=cmd_api_token)
    p_monitor = sub.add_parser("monitor", help="same as serve")
    p_monitor.add_argument("--api", action="store_true", help="same as serve --api")
    p_monitor.set_defaults(func=cmd_serve)
    sub.add_parser("scan", help="one monitoring pass now (read TikTok, analyse, alert)").set_defaults(func=cmd_scan)
    p_demo = sub.add_parser("demo", help="same as scan")
    p_demo.add_argument("--video", help="ignored (kept for old scripts)")
    p_demo.set_defaults(func=cmd_scan)
    p_cmt = sub.add_parser("comment", help="comment on a TikTok post / reply to a comment from its link (dry run unless --post)")
    p_cmt.add_argument("link", help="the TikTok link of the post or comment (vm.tiktok.com short links work too)")
    p_cmt.add_argument("text", help="the comment text (max 150 characters)")
    p_cmt.add_argument("--account", metavar="NAME", help="post through this account only (default: the first one that works)")
    p_cmt.add_argument("--post", action="store_true", help="really post it (needs POSTING_ENABLED=true); without it nothing is sent")
    p_cmt.set_defaults(func=cmd_comment)
    sub.add_parser("simulate", help="run canned PT/ES comments through the pipeline (for demos)").set_defaults(func=cmd_simulate)
    p_maint = sub.add_parser("maintain", help="run database + disk maintenance now")
    p_maint.add_argument("--vacuum", action="store_true", help="force a full VACUUM")
    p_maint.add_argument("--caches", action="store_true", help="also clear browser cache folders now")
    p_maint.set_defaults(func=cmd_maintain)
    sub.add_parser("db-stats", help="database size, rows, AI usage, watched posts").set_defaults(func=cmd_db_stats)
    p_pkg = sub.add_parser("package", help="build the deployment bundle: code + TikTok-only state of the used profiles")
    p_pkg.add_argument("--out", required=True, help="empty folder to write the bundle into")
    p_pkg.add_argument("--no-sessions", action="store_true", help="leave the login cookies out (log in again on the server)")
    p_pkg.add_argument("--no-db", action="store_true", help="do not include data/bot.db (settings, seen comments)")
    p_pkg.add_argument("--force", action="store_true", help="replace a non-empty --out folder")
    p_pkg.add_argument("--accounts", default="", help="comma-separated account names to export (default: all), e.g. one newly added account")
    p_pkg.set_defaults(func=cmd_package)
    p_res = sub.add_parser("restore-profiles", help="(server) rebuild clean profile folders from a bundle and prove they are logged in")
    p_res.add_argument("--bundle", required=True)
    p_res.add_argument("--root", help="install root (default: this project)")
    p_res.add_argument("--keep-secrets", action="store_true", help="keep the clear-text state.json files")
    p_res.set_defaults(func=cmd_restore_profiles)
    p_ver = sub.add_parser("verify-bundle", help="check a bundle contains nothing that must not travel")
    p_ver.add_argument("bundle")
    p_ver.set_defaults(func=cmd_verify_bundle)
    sub.add_parser("check-approvals", help="(obsolete) approvals are handled live by serve").set_defaults(func=cmd_check_approvals)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
