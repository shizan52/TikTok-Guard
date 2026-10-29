"""The long-running service: `python run.py serve`.

Two threads, one rule - nothing slow ever runs in the Telegram thread:

  telegram thread   long-polls Telegram (instant replies to /commands and button taps) and hands every
                    slow thing to the worker.
  worker thread     the ONLY thread that touches the browser: scans, reply posting, daily maintenance,
                    plus AI-only background jobs queued through run_background().

Posting jobs outrank scans. A scan failure alerts you once (not every cycle).

Monitoring is ALWAYS ON: the page is read every POLL_INTERVAL_SECONDS (120) from the moment the service starts, with no
pause/resume and no manual scan - the operator has nothing to remember. (`python run.py scan` remains for debugging.)
"""
import concurrent.futures
import html as html_lib
import itertools
import logging
import queue
import re
import threading
import time
import traceback
from pathlib import Path

import config
from app import maintenance, notifier, sysguard, tiktok_watch
from app.account_manager import AccountManager
from app.ai import AI
from app.analyzer import PostAnalyzer
from app.bot import COMMANDS, Bot, CheckedTG
from app.controller import Controller
from app.db import DB, utc_day, utcnow
from app.events import EventBus
from app.i18n import tr
from app.notifier import esc

P_POST, P_AI, P_SCAN, P_MAINT = 0, 1, 2, 3          # lower number = runs first
LOG = logging.getLogger("service")


class Service:
    def __init__(self, db: DB | None = None, *, tg=None, ai: AI | None = None, scanner=None, poster=None,
                 api: bool | None = None, monitor: bool = True, telegram: bool | None = None, reader=None,
                 watchdog_s: float | None = None, verifier=None) -> None:
        """`monitor=False` (the desktop app): nothing is scanned or cleaned up by itself, the worker only does what it is
        asked. `telegram=False` never polls Telegram, whatever .env says. `reader` replaces the real comment reader (tests).
        `watchdog_s`: a browser job that has run this long is given up on - its browser is closed so the single worker is free
        again (None = no watchdog)."""
        from app import telegram_notify
        self.db = db or DB()
        self.tg = CheckedTG(tg or telegram_notify, self.db)
        # Telegram is optional: with no bot token / linked chat the service runs for the desktop app alone
        self.telegram_enabled = (tg is not None or bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)) \
            if telegram is None else bool(telegram)
        self.monitoring = monitor
        self.ai = ai or AI(self.db)
        self._scan_fn = scanner or tiktok_watch.scan
        self._post_fn = poster or self._default_poster
        self._verify_fn = verifier or self._default_verifier
        self._checking: set = set()                       # approval ids being checked on TikTok right now
        # the desktop app's side: what happened (events), the accounts, and the one set of rules both clients use
        self.events = EventBus()
        self.db.add_listener(self._on_db_change)
        self.accounts = AccountManager(self.events, worker=self.call_in_worker,
                                       memory_check=lambda: self._memory_check(), release=self._release_browser,
                                       rest=self._rest_of)
        # reading and judging a post's comments on demand (the browser part runs on the worker thread, like every browser job)
        self.analyzer = PostAnalyzer(self.db, self.ai, run=lambda fn, timeout: self.call_in_worker(fn, timeout=timeout),
                                     reader=reader)
        self.controller = Controller(self.db, self.ai, self, tg=self.tg, bus=self.events, accounts=self.accounts,
                                     telegram_enabled=lambda: self.telegram_enabled, analyzer=self.analyzer)
        self.bot = Bot(self.db, self.ai, self.tg, self)
        self._api_wanted = config.API_ENABLED if api is None else api
        self.api_server = None
        self.stop_event = threading.Event()
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._scan_pending = threading.Event()
        self._scan_running = threading.Event()
        self._idle_scans = 0
        self._threads: list = []
        # (ok, available MB) - the shared-server guard; replaceable so tests do not depend on this machine's RAM
        self._memory_check = lambda: sysguard.enough_memory(config.MIN_FREE_MB)
        self.watchdog_s = watchdog_s
        self._current: dict | None = None                 # the job the worker is busy with: {"kind", "id", "since"}
        self._stages: dict = {}                           # approval id -> {"text", "since"} while it is being sent

    # ------------------------------------------------------------------ what is going on right now
    def busy(self) -> dict:
        """Is anything in flight that must not be cut off - a reply being sent, a post being read, a login window open?"""
        work = self._current
        flow = self.accounts.current()
        analysis = self.analyzer.current()
        reading = bool(analysis and not analysis.get("finished"))
        sending = [r["id"] for r in self.db.list_approvals(("posting",), 50)]
        adding = bool(flow and not flow.get("finished"))
        return {"busy": bool(work or sending or reading or adding),
                "job": dict(work) if work else None, "sending": sending, "reading": reading,
                "adding_account": adding, "queued": self._jobs.qsize()}

    @staticmethod
    def _monitor():
        """The posting engine module, if something has loaded it already (it is never imported just to look)."""
        import sys
        return sys.modules.get("app.tiktok_monitor")

    def _rest_of(self, name: str) -> int:
        tm = self._monitor()
        return tm.rest_seconds(name) if tm is not None else 0

    def _release_browser(self, name: str) -> None:
        """Close the browser that is being kept open for account `name` (it holds the account's folder). The browser belongs to
        the worker thread, so the worker closes it - after the job it may be in the middle of."""
        tm = self._monitor()
        if tm is not None and tm.warm_account() == name:
            self.call_in_worker(tm.close_warm, name, timeout=90)

    def _expire_warm(self) -> None:
        tm = self._monitor()
        if tm is not None and tm.WARM_SECONDS:
            try:
                tm.expire_warm()
            except Exception:  # noqa: BLE001
                LOG.exception("closing the idle browser failed")

    def _first_account(self, a: dict) -> str | None:
        """The account a job will try first: the one the person chose, else the first of the normal order."""
        if a.get("account"):
            return a["account"]
        tm = self._monitor()
        try:
            return tm.order_by_health(config.account_chain())[0] if tm is not None else None
        except Exception:  # noqa: BLE001
            return None

    def send_stage(self, approval_id: str) -> dict | None:
        """What the browser is doing for a reply that is being sent: {"text", "seconds"} - or None when nothing is known."""
        st = self._stages.get(approval_id)
        if not st:
            return None
        return {"text": st["text"], "seconds": int(time.monotonic() - st["since"])}

    def _set_stage(self, approval_id: str, text: str) -> None:
        cur = self._stages.get(approval_id)
        self._stages[approval_id] = {"text": text, "since": cur["since"] if cur else time.monotonic()}
        self.events.publish("post.stage", {"id": approval_id, "text": text})

    # ------------------------------------------------------------------ hooks used by the bot
    def lang(self) -> str:
        return "en"                                  # the bot speaks English only

    def scan_running(self) -> bool:
        return self._scan_running.is_set() or self._scan_pending.is_set()

    def request_post(self, approval_id: str) -> None:
        self._jobs.put((P_POST, next(self._seq), "post", {"id": approval_id}))

    def request_verify(self, approval_id: str) -> None:
        """'Check on TikTok' for a reply that failed or was interrupted: a read of the post, queued like a send."""
        self._checking.add(approval_id)
        self._jobs.put((P_POST, next(self._seq), "verify", {"id": approval_id}))

    def is_checking(self, approval_id: str) -> bool:
        return approval_id in self._checking

    def run_background(self, fn, *args) -> None:
        self._jobs.put((P_AI, next(self._seq), "call", {"fn": fn, "args": args}))

    def call_in_worker(self, fn, *args, timeout: float = 180.0):
        """Run fn(*args) on the worker thread - the ONLY thread that opens browsers for scans and posts - and return its
        result. Used for browser work that must not collide with them (an account's session check, removing its folder)."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((P_AI, next(self._seq), "call", {"fn": fn, "args": args, "future": future}))
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("the browser is busy with a scan or a post - try again in a moment") from None

    def notify(self, key: str, html_text: str, level: str = "info", **send) -> None:
        """A message for the operator: always an event for the desktop app, and a Telegram message too when Telegram is
        on. `send` are extra send_message arguments (chat_id, reply_to) for the Telegram copy."""
        plain = html_lib.unescape(re.sub(r"<[^>]+>", "", html_text))
        self.events.publish("notice", {"key": key, "text": plain}, level)
        if self.telegram_enabled and config.TELEGRAM_CHAT_ID:
            self.tg.send_message(html_text, **send)

    def _on_db_change(self, kind: str, data: dict) -> None:
        """Approvals, settings and logged events become live events for the desktop app."""
        self.events.publish(kind, data, data.get("level", "info") if kind == "log" else "info")

    def next_scan_delay(self) -> float:
        """Seconds until the next scheduled scan: POLL_INTERVAL_SECONDS while there is news, stretched x1.5 per
        quiet scan up to POLL_INTERVAL_MAX_SECONDS, so an idle page costs the server almost nothing."""
        base = float(config.POLL_INTERVAL_SECONDS)
        return min(max(base, float(config.POLL_INTERVAL_MAX_SECONDS)), base * (1.5 ** self._idle_scans))

    def next_scan_start(self, started: float, finished: float) -> float:
        """When the next scan begins (monotonic clock): one interval after the previous one STARTED - a fixed rhythm,
        so with POLL_INTERVAL_SECONDS=120 the page is read every 120 s however long the scan itself took. A scan that
        overran its slot is followed by a short breather (SCAN_MIN_GAP_SECONDS), never started back to back."""
        return max(started + self.next_scan_delay(), finished + float(config.SCAN_MIN_GAP_SECONDS))

    def last_scan_text(self) -> str:
        ts = self.db.meta_get("last_scan_ts")
        if not ts:
            return tr(self.lang(), "never")
        out = notifier.age_text(float(ts), self.lang())
        err = self.db.meta_get("last_scan_error")
        return f"{out} ⚠ {err[:80]}" if err else out

    def run_cleanup(self) -> dict:
        return maintenance.run_all(self.db, force_caches=False)

    # ------------------------------------------------------------------ jobs
    def _default_poster(self, a: dict) -> dict:
        from app import tiktok_monitor
        kind = a.get("kind") or "alert"
        if kind == "alert":                          # an answer to a comment found by the monitor
            # the operator may have chosen the account (desktop app): only that one then, no fallback to the others
            if a.get("account"):
                return tiktok_monitor.post_reply(a["video_url"], a["cid"], a["reply"], profile_names=[a["account"]])
            return tiktok_monitor.post_reply(a["video_url"], a["cid"], a["reply"])
        # /comment <link>: a new comment on a post, or a reply to the comment the link points at - through the account
        # the operator picked (only that one), or the whole chain when it is left on Auto
        return tiktok_monitor.post_link_comment(a["video_url"], a["reply"], cid=a["cid"] if kind == "link_reply" else None,
                                                account=a.get("account") or None, stage=a.get("_stage"),
                                                comment_text=a.get("comment_text") if kind == "link_reply" else None)

    def _default_verifier(self, a: dict) -> dict:
        """Read the post until the comment is seen and report how many replies it has and TikTok's preview of the first ones."""
        from app import tiktok_monitor
        res = tiktok_monitor.read_comments_of_post(a["video_url"], account=a.get("account") or None, max_comments=400,
                                                   stage=a.get("_stage"), until_cid=a["cid"])
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "The post could not be read."}
        found = next((c for c in res.get("comments") or [] if c.get("cid") == a["cid"]), None)
        if found is None:
            return {"ok": False, "error": "The comment is not on the post any more (it may have been deleted)."}
        return {"ok": True, "count": found.get("reply_count"), "preview": found.get("reply_preview") or []}

    def do_verify(self, approval_id: str) -> None:
        a = self.db.get_approval(approval_id)
        try:
            if not a or a["status"] != "post_failed":
                return
            ok, free = self._memory_ok(wait_s=30, reuse=self._first_account(a))
            if not ok:
                result = {"ok": False, "error": f"the computer is short of memory ({free:.0f} MB free) - try again in a minute"}
            else:
                self._set_stage(approval_id, "Opening the post...")
                try:
                    result = self._verify_fn({**a, "_stage": lambda text: self._set_stage(approval_id, text)})
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("checking %s failed", approval_id)
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._stages.pop(approval_id, None)
            self._checking.discard(approval_id)
            self.controller.apply_verification(approval_id, result)
            if self.telegram_enabled:
                try:
                    notifier.refresh_card(self.tg, self.db, approval_id, self.lang())
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._stages.pop(approval_id, None)
            self._checking.discard(approval_id)

    def _memory_ok(self, wait_s: float = 0.0, reuse: str | None = None) -> tuple[bool, float | None]:
        """Is there room for a browser right now? Optionally give a short dip `wait_s` seconds to pass. `reuse`: the account the
        job will use - when a browser is already kept open for it the job needs no new memory at all."""
        ok, free = self._memory_check()
        tm = self._monitor()
        if not ok and reuse and tm is not None and tm.warm_account() == reuse:
            return True, free
        if not ok:
            if tm is not None and tm.warm_account():          # a browser kept for another job is the cheapest memory to give back
                try:
                    tm.close_warm()
                except Exception:  # noqa: BLE001
                    LOG.exception("closing the kept browser failed")
                ok, free = self._memory_check()
        deadline = time.monotonic() + wait_s
        while not ok and time.monotonic() < deadline and not self.stop_event.is_set():
            self.stop_event.wait(5.0)
            ok, free = self._memory_check()
        return ok, free

    def do_post(self, approval_id: str) -> None:
        lang = self.lang()
        a = self.db.get_approval(approval_id)
        if not a or a["status"] != "posting":
            return
        ok, free = self._memory_ok(wait_s=90, reuse=self._first_account(a))
        if not ok:                                    # the approval stays retryable: the card shows the reason
            self.db.update_approval(approval_id, status="post_failed",
                                    last_error=f"the server is short of memory ({free:.0f} MB free) - tap Retry in a minute"[:300])
            self.db.log_event("post_deferred_low_memory", f"{approval_id} free={free}", "warn")
            notifier.refresh_card(self.tg, self.db, approval_id, lang)
            return
        self._set_stage(approval_id, "Starting the browser...")
        try:
            result = self._post_fn({**a, "_stage": lambda text: self._set_stage(approval_id, text)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("sending %s failed", approval_id)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._stages.pop(approval_id, None)
        tries = result.get("attempts") or []
        if len(tries) > 1 or (tries and not result.get("ok")):      # a reply that needed several goes: keep the reasons
            self.db.log_event("post_attempts", "; ".join(
                f"#{t['attempt']} {t['account']}: "
                f"{t['error'] or ('captcha' if t.get('captcha') else 'ok' if t.get('ok') else '?')}" for t in tries)[:480],
                "info" if result.get("ok") else "warn")
        if result.get("ok"):
            pub = (result.get("publish_response") or {}).get("body") or {}
            self.db.update_approval(approval_id, status="posted", posted_by=result.get("profile"),
                                    posted_at=utcnow(), last_error=None,
                                    posted_reply_cid=((pub.get("comment") or {}).get("cid")),
                                    posted_visibility=result.get("visibility"))
        else:
            err = (result.get("error") or str((result.get("publish_response") or {}).get("raw_text"))[:200]
                   or "the reply was not sent (no reason given)")
            self.db.update_approval(approval_id, status="post_failed", last_error=str(err)[:300])
        self.db.log_event("post", f"{approval_id} ok={result.get('ok')} acct={result.get('profile')}")
        notifier.refresh_card(self.tg, self.db, approval_id, lang)
        if result.get("ok"):
            self._send_proof(approval_id, result, lang)
            if result.get("visibility") == "limited":                # posted, but nobody except the poster can see it
                self.db.log_event("reply_limited", f"{approval_id} by {result.get('profile')}: TikTok status "
                                  f"{result.get('comment_status')} (visible to the author only)", "warn")
                self.notify("reply_limited", tr(lang, "reply_limited", account=esc(result.get("profile") or "?")), "warn",
                            chat_id=a.get("chat_id") or None, reply_to=a.get("message_id"))

    def _send_proof(self, approval_id: str, result: dict, lang: str) -> None:
        """Send the operator a screenshot of the reply that was just posted, so they can see exactly what went out."""
        a = self.db.get_approval(approval_id)
        shot = result.get("proof_screenshot") or result.get("after_screenshot")
        if not a or not shot or not Path(shot).exists():
            return
        self.events.publish("proof", {"approval_id": approval_id, "file": Path(shot).name})
        if not (self.telegram_enabled and config.TELEGRAM_CHAT_ID):
            return
        if notifier.is_link_kind(a):
            target = (f"@{a['username']} " if a.get("username") else "") + ("(reply to a comment)" if a["kind"] == "link_reply"
                                                                           else "(post)")
            caption = tr(lang, "lk_proof_caption", account=esc(a.get("posted_by") or "?"), target=esc(target),
                         text=esc((a.get("reply") or "")[:150]))
        else:
            caption = tr(lang, "proof_caption", account=esc(a.get("posted_by") or "?"), user=esc(a.get("username") or "?"),
                         comment=esc((a.get("comment_text") or "")[:150]), reply=esc((a.get("reply") or "")[:150]))
        try:
            self.tg.send_photo(shot, caption, chat_id=a.get("chat_id") or None, reply_to=a.get("message_id"))
        except Exception as exc:  # noqa: BLE001 - the post already succeeded; a failed picture must not undo that
            self.db.log_event("proof_send_failed", f"{type(exc).__name__}: {exc}", "warn")

    def do_scan(self, manual: bool = False) -> None:
        lang = self.lang()
        self._scan_pending.clear()
        ok, free = self._memory_ok(wait_s=60 if manual else 0)
        if not ok:                                    # scheduled scan: skip this slot, the next one is only 120 s away
            self._note_low_memory(free, manual, lang)
            return
        self._scan_running.set()
        self.events.publish("scan.started", {"manual": manual})
        summary = None
        try:
            summary = self._scan_fn(self.db)
            self.events.publish("scan.finished", {k: (summary or {}).get(k) for k in ("opened", "new")})
            self.db.meta_set("last_scan_ts", time.time())
            self.db.meta_set("last_scan_error", "")
            self.db.meta_set("scan_error_alerted_at", "0")
            self._captcha_notice((summary or {}).get("captcha"), lang)
        except Exception as exc:  # noqa: BLE001
            self.db.meta_set("last_scan_error", f"{type(exc).__name__}: {exc}"[:200])
            self.db.log_event("scan_failed", f"{type(exc).__name__}: {exc}", "error")
            self.events.publish("scan.finished", {"error": f"{type(exc).__name__}: {exc}"[:200]}, "error")
            self._alert_scan_error(str(exc), lang)
            if manual:
                self.notify("scan_failed", tr(lang, "scan_failed", error=esc(str(exc)[:300])), "error")
        finally:
            self._scan_running.clear()
        # analysis + alerts run even after a failed scan: comments stored earlier may still be waiting
        try:
            from app import analysis
            ai_stats = analysis.classify_pending(self.db, self.ai)
            sent = notifier.notify_pending(
                self.db, self.ai, self.tg, lang,
                include_questions=notifier.questions_enabled(self.db), burst=config.NOTIFY_BURST)
            notifier.fill_missing_replies(self.db, self.ai, self.tg, lang)
            if sent["backlog"] and sent["sent"]:
                self.notify("backlog", tr(lang, "alert_backlog", n=sent["backlog"]))
            self._budget_alert(lang)
            quiet = (summary is not None and not manual and not summary.get("opened") and not summary.get("new")
                     and not sent["sent"])
            self._idle_scans = self._idle_scans + 1 if quiet else (self._idle_scans if summary is None else 0)
            if manual and summary is not None:
                text = (f"{summary['opened']} post(s) opened, {summary['new']} new comment(s), "
                        f"{ai_stats['classified']} analysed by AI, {sent['sent']} alert(s)")
                self.notify("scan_done", tr(lang, "scan_done", summary=esc(text)))
        except Exception as exc:  # noqa: BLE001
            self.db.log_event("analysis_failed", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}", "error")

    def _captcha_notice(self, info: dict | None, lang: str) -> None:
        """Tell the operator about a TikTok security check once per episode (and again if it keeps coming back
        after the pauses have grown to an hour) - never once per 120 s slot."""
        if not info:
            return
        last = float(self.db.meta_get("captcha_alerted_at", 0) or 0)
        quiet_for = time.time() - last
        if (info["streak"] == 1 and quiet_for > 3600) or quiet_for > 6 * 3600:       # new episode / still walled hours on
            self.db.meta_set("captcha_alerted_at", time.time())
            self.notify("captcha", tr(lang, "captcha_alert", minutes=info["minutes"]), "warn")

    def _note_low_memory(self, free: float | None, manual: bool, lang: str) -> None:
        self.db.meta_set("last_scan_skipped", time.time())
        self.db.meta_set("last_scan_skip_reason", f"low memory ({free:.0f} MB free, need {config.MIN_FREE_MB})")
        last = float(self.db.meta_get("low_memory_logged_at", 0) or 0)
        if time.time() - last > 3600:                 # one log line an hour, not one per 120 s slot
            self.db.meta_set("low_memory_logged_at", time.time())
            self.db.log_event("scan_skipped_low_memory", f"free={free} need={config.MIN_FREE_MB}", "warn")
        if manual:
            self.notify("scan_failed", tr(lang, "scan_failed", error=esc(
                f"the server is short of memory right now ({free:.0f} MB free) - the next 2-minute round will try again")), "warn")

    def _alert_scan_error(self, error: str, lang: str) -> None:
        last = float(self.db.meta_get("scan_error_alerted_at", 0) or 0)
        if time.time() - last > 6 * 3600:
            self.db.meta_set("scan_error_alerted_at", time.time())
            self.notify("scan_error", tr(lang, "scan_error_alert", error=esc(error[:300])), "error")

    def _budget_alert(self, lang: str) -> None:
        if self.ai.exhausted() and self.db.meta_get("budget_alert_day") != utc_day():
            self.db.meta_set("budget_alert_day", utc_day())
            self.notify("ai_budget", tr(lang, "alert_budget"), "warn")

    def do_maintenance(self) -> None:
        report = maintenance.run_all(self.db)
        self.db.meta_set("last_maintenance_report", f"freed {report['freed_mb_total']} MB")

    # ------------------------------------------------------------------ threads
    def _worker(self) -> None:
        next_scan = time.monotonic() + 3
        next_maint = time.monotonic() + 120
        while not self.stop_event.is_set():
            try:
                prio, _seq, kind, payload = self._jobs.get(timeout=1.0)
            except queue.Empty:
                prio = None
            if prio is not None:
                self._current = {"kind": kind, "id": payload.get("id"), "since": time.time()}
                try:
                    if kind == "post":
                        self.do_post(payload["id"])
                    elif kind == "verify":
                        self.do_verify(payload["id"])
                    elif kind == "scan":
                        started = time.monotonic()
                        self.do_scan()
                        next_scan = self.next_scan_start(started, time.monotonic())
                    elif kind == "call":
                        future = payload.get("future")
                        if future is None:
                            payload["fn"](*payload["args"])
                        else:                                # a caller is waiting (call_in_worker): hand it the outcome
                            try:
                                future.set_result(payload["fn"](*payload["args"]))
                            except Exception as exc:  # noqa: BLE001
                                future.set_exception(exc)
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("worker job %s failed", kind)
                    self.db.log_event("job_failed", f"{kind}: {type(exc).__name__}: {exc}", "error")
                finally:
                    self._current = None
                continue
            self._expire_warm()                        # a kept browser nobody used for a few minutes goes away
            if not self.monitoring:                    # the desktop app: nothing runs by itself, only what is asked
                continue
            now = time.monotonic()
            if now >= next_scan and not self.scan_running():
                self._scan_pending.set()
                self._jobs.put((P_SCAN, next(self._seq), "scan", {}))
                next_scan = now + self.next_scan_delay()
            if now >= next_maint:
                try:
                    self.do_maintenance()
                except Exception as exc:  # noqa: BLE001
                    self.db.log_event("maintenance_failed", f"{type(exc).__name__}: {exc}", "error")
                next_maint = now + 24 * 3600

    def _watchdog(self) -> None:
        """The worker is ONE thread: a browser call that never returns would freeze every read and send. After `watchdog_s` the
        browsers of this app are closed - the call then fails at once, the job ends as a failure, the worker is free again."""
        given_up = set()
        while not self.stop_event.wait(max(0.05, min(15.0, self.watchdog_s / 3))):
            cur = self._current
            if not cur:
                continue
            age = time.time() - cur["since"]
            key = (cur["kind"], cur.get("id"), cur["since"])
            if age < self.watchdog_s or key in given_up:
                continue
            given_up.add(key)
            LOG.error("the %s job %s has run for %d s: closing its browser", cur["kind"], cur.get("id"), age)
            self.db.log_event("job_stuck", f"{cur['kind']} {cur.get('id') or ''} ran {int(age)} s - its browser was closed", "error")
            try:
                from app import orphans
                orphans.close_stale_browsers(orphans.our_profile_dirs())
            except Exception:  # noqa: BLE001
                LOG.exception("the watchdog could not close the browser")

    def _telegram_loop(self) -> None:
        offset = int(self.db.meta_get("telegram_offset", 0) or 0)
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                resp = self.tg.get_updates(offset=offset, timeout=25)
                if not resp.get("ok", True) and resp.get("error_code") == 409:
                    self.db.log_event("telegram_conflict", "another process is polling this bot", "error")
                    time.sleep(15)
                    continue
                for upd in resp.get("result", []):
                    offset = max(offset, upd["update_id"] + 1)
                    self.bot.handle_update(upd)
                    self.db.meta_set("telegram_offset", offset)
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001 - network blips: back off, never die
                self.db.log_event("telegram_poll_error", f"{type(exc).__name__}: {exc}", "warn")
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def start(self) -> None:
        try:                                                  # a removed account whose folder was still in use when the app closed
            self.accounts.finish_pending_removals()
        except Exception:  # noqa: BLE001
            LOG.warning("could not finish removing an account folder", exc_info=True)
        # a card left 'posting' by the previous run is not being sent by anyone: say so instead of leaving it spinning forever
        for approval_id in self.controller.recover_interrupted():
            if self.telegram_enabled:
                try:
                    notifier.refresh_card(self.tg, self.db, approval_id, self.lang())
                except Exception:  # noqa: BLE001
                    pass
        if self.telegram_enabled:
            try:
                self.tg.set_my_commands(COMMANDS)
            except Exception:  # noqa: BLE001
                pass
        loops = [(self._worker, "worker")] + ([(self._telegram_loop, "telegram")] if self.telegram_enabled else []) \
            + ([(self._watchdog, "watchdog")] if self.watchdog_s else [])
        for target, name in loops:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if self._api_wanted:
            self.start_api()

    def start_api(self) -> None:
        """The desktop app's local API (127.0.0.1 + token). A port that is taken is logged, never fatal."""
        from app.api import ApiServer
        try:
            self.api_server = ApiServer(self.controller, self.events, self.accounts, self)
            self.api_server.start()
            self.db.log_event("api_started", f"http://{self.api_server.host}:{self.api_server.port}/")
        except OSError as exc:
            self.api_server = None
            self.db.log_event("api_failed", f"{type(exc).__name__}: {exc}", "error")

    def stop(self, wait: float = 0.0) -> None:
        """Stop. `wait` > 0: first let the job the worker is in the middle of (a reply being sent) finish, for at most that long."""
        deadline = time.monotonic() + max(0.0, wait)
        while wait and self._current is not None and time.monotonic() < deadline:
            time.sleep(0.25)
        tm = self._monitor()
        if tm is not None and tm.warm_account() and any(t.name == "worker" and t.is_alive() for t in self._threads):
            try:                                       # the kept browser must not outlive the app (it would hold the profile)
                self.call_in_worker(tm.close_warm, timeout=30)
            except Exception:  # noqa: BLE001
                LOG.warning("the kept browser could not be closed at shutdown")
        self.stop_event.set()
        self.accounts.close()
        if self.api_server is not None:
            self.api_server.stop()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
