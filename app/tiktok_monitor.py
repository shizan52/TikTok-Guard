"""Reads TikTok comments for a target video through bfp's stealth browser.

The comment *text* always comes from the real GET /api/comment/list/ response
(captured via a Playwright response listener while bfp drives the page) --
the same endpoint reverse-engineered and documented in
network/endpoints/comment_endpoints.md. The DOM is only used afterwards, to
grab a small screenshot crop per comment for the client demo; it is never
used to extract the comment text itself (fragile, and unnecessary when the
JSON is sitting right there).
"""
import contextlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from pathlib import Path

import config

from app.bfp_loader import ensure_bfp_on_path  # noqa: E402

ensure_bfp_on_path()
import bfp  # noqa: E402

_LAST_POST_FILE = config.DATA_DIR / "last_post_at.txt"
LOG = logging.getLogger("tiktok_monitor")

# A person is waiting at the screen: when TikTok will not open a page for an account, do not sit through bfp's patient
# 30/60/120 s back-off (built for the unattended watcher) - one short retry, then the next account.
FAST_RECOVERY = {"max_attempts": 1, "real_profile_backoff_s": (4,)}
WALL_COOLDOWN_S = 600                       # an account that just met a wall goes to the back of the line for this long
_WALLED: dict = {}                          # account -> time.monotonic() until which it is left to the end of the line
_WALL_LOCK = threading.Lock()


def mark_walled(account: str, seconds: float = WALL_COOLDOWN_S) -> None:
    with _WALL_LOCK:
        _WALLED[account] = time.monotonic() + seconds


def is_walled(account: str) -> bool:
    with _WALL_LOCK:
        return _WALLED.get(account, 0.0) > time.monotonic()


def rest_seconds(account: str) -> int:
    """How much longer TikTok's refusal of this account is expected to last (0 = it is not resting): the screen says
    "resting" so a person does not pick the account that was just refused."""
    with _WALL_LOCK:
        left = _WALLED.get(account, 0.0) - time.monotonic()
    return max(0, int(left))


def reset_walls() -> None:
    with _WALL_LOCK:
        _WALLED.clear()
        _LOADS.clear()


# TikTok counts how often an account loads a profile page: after a few loads in a short time it answers the next one with an
# empty HTTP 403 for several minutes (measured 2026-10-14/15: the second or third load within about ten minutes). So the app
# remembers when each account last loaded one, and a new job prefers the account that has loaded the fewest lately.
LOAD_WINDOW_S = 600
_LOADS: dict = {}                           # account -> [time.monotonic() of each profile page it loaded lately]


def note_profile_load(account: str) -> None:
    with _WALL_LOCK:
        now = time.monotonic()
        _LOADS[account] = [t for t in _LOADS.get(account, []) if now - t < LOAD_WINDOW_S] + [now]


def recent_loads(account: str) -> int:
    with _WALL_LOCK:
        now = time.monotonic()
        return sum(1 for t in _LOADS.get(account, []) if now - t < LOAD_WINDOW_S)


def order_by_health(accounts: list) -> list:
    """The same accounts, best first: those that did NOT just meet a wall, and among them those that loaded the fewest profile
    pages lately (stable otherwise - the operator's order wins between equals). The next try should not start with the account
    TikTok is refusing right now, nor with the one it is about to."""
    ranked = sorted(enumerate(accounts), key=lambda ia: (is_walled(ia[1]), recent_loads(ia[1]) >= 2, ia[0]))
    return [a for _i, a in ranked]


# --- the warm browser ------------------------------------------------------------------------------------------------
# TikTok refuses a profile page that is loaded again and again by one account (measured 2026-10-15: the third load within a
# few minutes is answered with an empty HTTP 403 for several minutes), and a post opened by its address meets a captcha. So
# the browser that just opened a profile page is KEPT, already standing on it, for WARM_SECONDS: the next job (another
# reply under the same post, another post of the same owner) clicks a tile instead of loading anything - seconds instead
# of half a minute, and no second page load for TikTok to count. 0 = off (a job closes its browser, as always): the
# unattended server runs that way; the desktop app switches it on. Only ever touched from the worker thread.
WARM_SECONDS = 0
_WARM: dict | None = None                   # {"session", "account", "used", "thread"} - at most one browser is kept
_WARM_LOCK = threading.Lock()


def _close_session(session) -> None:
    try:
        session.finish()
    except Exception:  # noqa: BLE001
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _alive(session) -> bool:
    try:
        return not session.page.is_closed() and session.page.evaluate("() => 1") == 1
    except Exception:  # noqa: BLE001
        return False


def close_warm(account: str | None = None) -> bool:
    """Close the kept browser (of `account`, or whichever it is). -> was one closed? Call it on the worker thread."""
    global _WARM
    with _WARM_LOCK:
        w = _WARM
        if w is None or (account and w["account"] != account):
            return False
        _WARM = None
    _close_session(w["session"])
    LOG.info("closed the kept browser of %s", w["account"])
    return True


def expire_warm(max_idle: float | None = None) -> bool:
    """Close the kept browser when nobody used it for `max_idle` seconds (default WARM_SECONDS). Worker thread, idle tick."""
    w = _WARM
    if w is None or time.monotonic() - w["used"] < (WARM_SECONDS if max_idle is None else max_idle):
        return False
    return close_warm(w["account"])


def warm_account() -> str | None:
    w = _WARM
    return w["account"] if w else None


class _SessionView:
    """What one operation gets: the session, but `finish()` only ends the OPERATION - the browser may stay for the next one
    (`keep`, set by the operation when it ended well) - and the response listeners it adds go away when it is done."""

    def __init__(self, session, reused: bool) -> None:
        self._s, self.reused, self.keep, self._listeners = session, reused, False, []

    def __getattr__(self, name):
        return getattr(self._s, name)

    def finish(self, *args, **kwargs) -> None:
        return None

    def listen(self, fn) -> None:
        self._s.page.on("response", fn)
        self._listeners.append(fn)

    def _detach(self) -> None:
        for fn in self._listeners:
            try:
                self._s.page.remove_listener("response", fn)
            except Exception:  # noqa: BLE001
                pass
        self._listeners.clear()


@contextlib.contextmanager
def _browser(profile, vault, name: str, *, kind: str, video_id: str, headless: bool, hitl: str,
             hitl_timeout_s: float | None = None):
    """The browser for one operation: the kept one when it belongs to this account and is still alive, else a new one (any
    other kept browser is closed first - one at a time). Afterwards it is kept if the operation said so (`s.keep`) and
    WARM_SECONDS > 0, else closed. An exception always closes it."""
    global _WARM
    session, reused = None, False
    with _WARM_LOCK:
        w, _WARM = _WARM, None
    if w is not None:
        if w["account"] == name and w["thread"] == threading.get_ident() and WARM_SECONDS > 0 and _alive(w["session"]):
            session, reused = w["session"], True
        else:
            _close_session(w["session"])
    if session is None:
        kwargs = {"hitl_timeout_s": hitl_timeout_s} if hitl_timeout_s is not None else {}
        session = bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=name,
                              backend=config.BFP_BACKEND, out_dir=str(config.BFP_RUNS_DIR / f"{kind}-{name}-{video_id}"),
                              headless=headless, hitl=hitl, block_resources=config.BROWSER_BLOCK_RESOURCES, **kwargs).start()
    view = _SessionView(session, reused)
    try:
        yield view
    except BaseException:
        view._detach()
        _close_session(session)
        raise
    view._detach()
    if view.keep and WARM_SECONDS > 0:
        with _WARM_LOCK:
            _WARM = {"session": session, "account": name, "used": time.monotonic(), "thread": threading.get_ident()}
    else:
        _close_session(session)


def _label(account: str) -> str:
    try:
        from app import notifier
        return notifier.account_label(account)
    except Exception:  # noqa: BLE001
        return account


def _say(stage, text: str) -> None:
    """Tell the screen what the browser is doing now. Never allowed to disturb the work itself."""
    if stage:
        try:
            stage(text)
        except Exception:  # noqa: BLE001
            pass


class _Trace:
    """Phase timings of one browser attempt: written to the log (so a slow run can be explained afterwards), returned with the
    result, and - when `show` is given - handed to the screen as the current step."""

    def __init__(self, what: str, account: str, video_id: str, stage=None) -> None:
        self.what, self.account, self.video_id, self.stage = what, account, video_id, stage
        self.t0 = self.last = time.monotonic()
        self.marks: list = []

    def mark(self, label: str, show: str | None = None) -> None:
        now = time.monotonic()
        self.marks.append((label, round(now - self.last, 1)))
        LOG.info("%s %s %s: %s (+%.1fs, %.1fs in all)", self.what, self.account, self.video_id, label, now - self.last, now - self.t0)
        self.last = now
        if show:
            _say(self.stage, show)

    def timings(self) -> list:
        return [{"step": label, "seconds": seconds} for label, seconds in self.marks]


def comment_visibility(status) -> str | None:
    """Who can see a comment, from TikTok's own `status` field: 1 = published (4 = "published by you" in other
    projects' notes) -> "public"; 2 -> "limited": measured live (2026-10-08), TikTok shows such a comment ONLY to the
    account that wrote it - every other account, a logged-out visitor and the profile's comment count never get it.
    Anything else is unknown (None) rather than guessed."""
    if status in (1, 4, "1", "4"):
        return "public"
    if status in (2, "2"):
        return "limited"
    return None


def _pace_posts() -> None:
    """Block until config.POST_MIN_GAP_SECONDS (jittered) have passed since the
    last reply-post attempt made by ANY process - the timestamp lives in a
    file, so a one-shot `check-approvals` and a long-running `monitor` share it."""
    try:
        last = float(_LAST_POST_FILE.read_text().strip())
    except Exception:  # noqa: BLE001
        return
    wait = last + config.POST_MIN_GAP_SECONDS * random.uniform(0.8, 1.4) - time.time()
    if wait > 0:
        print(f"pacing: waiting {wait:.0f}s since the previous reply attempt")
        time.sleep(wait)


def _mark_post_attempt() -> None:
    try:
        _LAST_POST_FILE.write_text(str(time.time()))
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def _use_account(profile_name: str):
    """Point bfp's real-profile mode (BFP_USER_DATA_DIR) at this account's own
    saved browser folder for the duration of one session - the master account
    uses the folder from .env, extra accounts (app/accounts.py) their own."""
    folder = config.account_user_data_dir(profile_name)
    saved = os.environ.get("BFP_USER_DATA_DIR")
    exe = config.account_browser(profile_name)           # an account added from the desktop app keeps its own browser
    saved_exe = os.environ.get("BFP_PATCHED_CHROMIUM")
    if folder:
        os.environ["BFP_USER_DATA_DIR"] = folder
    if exe:
        os.environ["BFP_PATCHED_CHROMIUM"] = exe
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("BFP_USER_DATA_DIR", None)
        else:
            os.environ["BFP_USER_DATA_DIR"] = saved
        if exe:
            if saved_exe is None:
                os.environ.pop("BFP_PATCHED_CHROMIUM", None)
            else:
                os.environ["BFP_PATCHED_CHROMIUM"] = saved_exe


def _get_or_create_profile(vault: "bfp.ProfileVault", name: str | None = None):
    name = name or config.BFP_PROFILE_NAME
    try:
        return vault.load(name)
    except Exception:  # noqa: BLE001
        # country is config.BFP_PROFILE_COUNTRY - auto-detected from this
        # machine's real egress IP unless explicitly overridden (see its
        # own comment in config.py for why that matters: a profile claiming
        # a country this automation doesn't actually run from is a
        # confirmed-live cause of TikTok silently rejecting write actions).
        return vault.new(name=name, kind="desktop", country=config.BFP_PROFILE_COUNTRY)


def _ensure_comments_tab(page) -> None:
    """The side panel opened by [data-e2e="comment-icon"] can default to
    "You may like" instead of "Comments" (confirmed live, 2026-10-06, on a
    freshly-imported session - no data-e2e on either tab, both are
    TikTok's own hashed-class tux-tabbar-item elements, text-distinguished
    only). Without this, /api/comment/list/ never fires at all because the
    Comments tab's own content was never requested - not a session/login/
    identity problem, just the wrong tab being active."""
    try:
        # Scoped to "tux-tabbar-item" (the individual clickable tab), not
        # "tux-tabbar__item-container" (its outer wrapper, a DIFFERENT class
        # that also happens to contain the substring "item") - only the
        # inner element's own text is the single tab label ("Comments" or
        # "You may like"), not both concatenated.
        tab = page.locator('[class*="tux-tabbar-item"]', has_text="Comments").first
        if tab.count():
            tab.click()
    except Exception:  # noqa: BLE001
        pass


def _captcha_present(page) -> bool:
    """TikTok's captcha wall - either the visual slider-puzzle ("Drag the
    slider to fit the puzzle") or its audio fallback ("Play the audio and
    enter the code you hear"). Confirmed live (2026-10-07): an inconclusive
    slider attempt can ESCALATE to the audio challenge rather than clearing
    - checking only the slider text would misreport that as "solved" since
    the slider text genuinely disappears, even though a (different) wall is
    still up. Detecting either form here lets the caller retry with a
    completely fresh session instead of grinding through an unrelated-
    looking "comment not found" timeout."""
    try:
        return (page.locator("text=/drag the slider/i").count() > 0
                or page.locator("text=/enter the code you hear/i").count() > 0)
    except Exception:  # noqa: BLE001
        return False


def extract_video_id(video_url: str) -> str:
    match = re.search(r"/video/(\d+)", video_url)
    if not match:
        raise ValueError(f"could not find a video id in {video_url!r}")
    return match.group(1)


def import_login_cookies(cookie_file: str, *, localstorage_file: str | None = None) -> dict:
    """Import a session exported (via the Cookie-Editor extension) from a
    normal, already-logged-in browser, instead of driving TikTok/Google's
    login UI inside the automated one (Google rejects that outright).

    localstorage_file: optional path to a DevTools-snippet JSON dump of that
    same browser's localStorage/IndexedDB. Confirmed live (2026-10-06) this
    is what actually makes writes work, not just reads -- see the long
    comment in cookie_import.import_cookie_editor_file for why."""
    from app.cookie_import import import_cookie_editor_file
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault)
    storage = vault.storage(profile)
    return import_cookie_editor_file(cookie_file, storage.state_file,
                                      localstorage_path=localstorage_file)


def verify_login(*, headless: bool | None = None) -> dict:
    """Open the target profile with the saved session and report whether
    TikTok actually considers us logged in (cookie-based check, see
    is_logged_in)."""
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault)
    headless = config.BFP_HEADLESS if headless is None else headless

    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=config.BFP_PROFILE_NAME, backend=config.BFP_BACKEND,
                      out_dir=str(config.BFP_RUNS_DIR / "verify-login"),
                      headless=headless, hitl="off") as s:
        goto = s.goto(config.TARGET_PROFILE_URL)
        time.sleep(2)
        logged_in = is_logged_in(s.page)
        whoami = None
        if logged_in:
            try:
                whoami = s.page.evaluate(
                    """() => { const a = document.querySelector('[data-e2e="nav-profile"] a'); return a ? a.href : null; }"""
                )
            except Exception:  # noqa: BLE001
                pass
        shot = config.SCREENSHOTS_DIR / "_login_verify.png"
        try:
            s.page.screenshot(path=str(shot))
        except Exception:  # noqa: BLE001
            shot = None
        s.finish()
    return {"logged_in": logged_in, "goto_ok": goto.get("ok"), "whoami_url": whoami,
            "screenshot": str(shot) if shot else None}


def is_logged_in(page) -> bool:
    """The only reliable signal: TikTok's own session cookie. The 'Log in'
    button's data-e2e can be absent for other reasons (layout, locale, timing),
    but confirmed live that Google-OAuth-without-a-completed-handoff leaves
    plenty of Google cookies and *zero* TikTok session cookies while the page
    still renders the logged-out nav (literally shows "Entrar"/"Log in") --
    so cookie presence is the ground truth, not DOM text."""
    try:
        names = {c["name"] for c in page.context.cookies()}
    except Exception:  # noqa: BLE001
        return False
    return bool(names & {"sessionid", "sid_tt", "sid_guard"})


def wait_for_login(profile_url: str, *, timeout_s: float = 900.0) -> dict:
    """Open profile_url in a VISIBLE browser and wait for a human to log in.

    This never touches the login form itself -- it only polls for TikTok's
    session cookie appearing (see is_logged_in). Closing the session persists
    cookies/localStorage into the profile vault automatically
    (bfp.Session.close() -> StorageProfile.export_from()), so every later
    Session(name=config.BFP_PROFILE_NAME) reuses this same logged-in state.

    NOTE: "Continue with Google" reliably fails here (confirmed live: Google's
    own sign-in succeeds -- its cookies show up -- but the OAuth handoff back
    to TikTok never completes, so TikTok never sets a session cookie). Log in
    with TikTok's own "Use phone / email / username" + a direct password
    instead; Google is a dead end for an automated browser.
    """
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault)

    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=config.BFP_PROFILE_NAME, backend=config.BFP_BACKEND,
                      out_dir=str(config.BFP_RUNS_DIR / "login"), headless=False,
                      hitl="wait", hitl_timeout_s=180) as s:
        s.goto(profile_url)
        deadline = time.monotonic() + timeout_s
        logged_in = False
        while time.monotonic() < deadline:
            if is_logged_in(s.page):
                logged_in = True
                break
            time.sleep(2)
        if logged_in:
            time.sleep(2)  # let post-login redirects/cookies settle before saving
        s.finish()
    return {"logged_in": logged_in, "profile": config.BFP_PROFILE_NAME}


def warm_session(*, profile_name: str | None = None, extra_urls: list[str] | None = None,
                  headless: bool | None = None) -> dict:
    """Run one bfp AccountWarmer pass for ONE account: safe, read-only
    browsing (scroll, idle, mouse movement - no clicks that write anything)
    to build real organic session activity on that profile. bfp's own
    AccountWarmer docstring is explicit about why this, not a clever
    technical trick, is what actually lowers an account's restriction rate:
    "Cookies age, history accumulates and login frequency ramps up."

    Confirmed live (2026-10-07): going straight from a fresh native login
    into many back-to-back real write attempts (unavoidable while actively
    debugging) is exactly the pattern bfp's own design assumes a caller
    will NOT do - both headed and headless post_reply() started hitting
    TikTok's slider-puzzle wall after that volume, on an account that had
    posted successfully just beforehand. This gives a safe way to
    interleave real recovery activity between write bursts going forward:
    run this between posting sessions, not instead of spacing them out.
    Every account in the fallback chain needs its own warming - a second
    account only helps if IT also has organic activity behind it, not just
    a bare login (see warm_all_accounts()).

    age_days is computed from real elapsed wall-clock time since this
    profile's warming state was first created, never advanced manually -
    there is no shortcut for the thing that actually needs to happen, which
    is time passing with ordinary use.
    """
    import datetime as _dt
    profile_name = profile_name or config.BFP_PROFILE_NAME
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault, profile_name)
    headless = config.BFP_HEADLESS if headless is None else headless

    warmer = bfp.AccountWarmer(config.DATA_DIR / f"warming_{profile_name}.json")
    created = warmer.state.get("created")
    if created:
        try:
            created_dt = _dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            now = _dt.datetime.now(_dt.timezone.utc)
            warmer.state["age_days"] = max(0, (now - created_dt).days)
        except Exception:  # noqa: BLE001
            pass

    urls = [config.TARGET_PROFILE_URL] + list(extra_urls or [])
    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None),
                      name=profile_name, backend=config.BFP_BACKEND,
                      out_dir=str(config.BFP_RUNS_DIR / f"warm-{profile_name}"),
                      headless=headless, hitl="wait", hitl_timeout_s=60) as s:
        result = warmer.run(s, urls)
        s.finish()
    return {**result, "profile": profile_name}


def warm_all_accounts(*, extra_urls: list[str] | None = None,
                       headless: bool | None = None) -> list[dict]:
    """warm_session() for every account in config.BFP_PROFILE_NAMES, in
    order. Each account's trust is independent, so each needs its own
    warming - running this covers the whole fallback chain in one call."""
    return [warm_session(profile_name=name, extra_urls=extra_urls, headless=headless)
            for name in config.BFP_PROFILE_NAMES]


def fetch_last_video_url(profile_url: str, *, headless: bool | None = None,
                          hitl_timeout_s: float = 150.0) -> str:
    """Open the channel and return the first (most recent) video URL in the grid."""
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault)
    headless = config.BFP_HEADLESS if headless is None else headless

    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=config.BFP_PROFILE_NAME, backend=config.BFP_BACKEND,
                      out_dir=str(config.BFP_RUNS_DIR / "last-video-lookup"),
                      headless=headless, hitl="wait", hitl_timeout_s=hitl_timeout_s) as s:
        goto = s.goto(profile_url)
        if not goto.get("ok"):
            raise RuntimeError(f"could not open {profile_url}: {goto.get('error')}")
        time.sleep(1.5)
        href = s.page.evaluate("""
            () => {
                const a = document.querySelector('a[href*="/video/"]');
                return a ? a.href : null;
            }
        """)
        s.finish()
    if not href:
        raise RuntimeError(f"no video link found on {profile_url}")
    return href


def fetch_comments_with_screenshots(video_url: str, *, max_comments: int = 20,
                                     headless: bool | None = None,
                                     hitl_timeout_s: float = 150.0) -> dict:
    """-> {"video_id", "comments": [...], "panel_screenshot": path|None}

    Each comment: {cid, text, username, digg_count, create_time, reply_count,
    screenshot: path|None}
    """
    video_id = extract_video_id(video_url)
    captured: dict = {"payload": None}

    def on_response(response):
        # Always overwrite with the LATEST matching response, never latch
        # onto the first. Confirmed live (2026-10-07): opening the comment
        # panel can fire more than one /api/comment/list/ request (an
        # initial one, then another once _ensure_comments_tab's click
        # settles) - latching onto the first could keep a premature/partial
        # response even though the page itself goes on to render the full,
        # correct comment list a moment later (seen live: the screenshot
        # showed the target comment clearly, first in the list, while the
        # captured payload had 0 comments - a stale first response, not a
        # missing comment. Also scoped to THIS video's aweme_id - confirmed
        # live (2026-10-07) that broadening to "latest, not first" alone
        # still intermittently captured 0 comments despite the page clearly
        # showing them, most likely because the same /api/comment/list/
        # path is also hit for OTHER videos (sidebar/"You may like"
        # prefetching) and an unrelated video's response was winning the
        # "latest" race.
        url = response.url
        if ("/api/comment/list/" in url and "/reply/" not in url
                and f"aweme_id={video_id}" in url):
            try:
                captured["payload"] = response.json()
            except Exception:  # noqa: BLE001
                pass

    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault)
    headless = config.BFP_HEADLESS if headless is None else headless

    panel_shot = None
    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=config.BFP_PROFILE_NAME, backend=config.BFP_BACKEND,
                      out_dir=str(config.BFP_RUNS_DIR / f"monitor-{video_id}"),
                      headless=headless, hitl="wait", hitl_timeout_s=hitl_timeout_s) as s:
        s.page.on("response", on_response)
        goto = s.goto(video_url)
        if not goto.get("ok"):
            raise RuntimeError(f"could not open {video_url}: {goto.get('error')}")

        s.click('[data-e2e="comment-icon"]', by="css")
        time.sleep(0.6)
        _ensure_comments_tab(s.page)
        deadline = time.monotonic() + 15
        while captured["payload"] is None and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(1.0)  # let the panel finish rendering before screenshots

        panel_shot = config.SCREENSHOTS_DIR / f"panel_{video_id}.png"
        try:
            s.page.screenshot(path=str(panel_shot))
        except Exception:  # noqa: BLE001
            panel_shot = None

        boxes = s.page.evaluate("""
            () => Array.from(document.querySelectorAll('[data-e2e="comment-level-1"]')).map(el => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            })
        """) or []

        comments = []
        payload = captured["payload"] or {}
        for i, c in enumerate((payload.get("comments") or [])[:max_comments]):
            shot_path = None
            if i < len(boxes):
                box = boxes[i]
                clip = {"x": max(box["x"], 0), "y": max(box["y"], 0),
                         "width": max(box["w"], 60), "height": min(max(box["h"], 40), 220)}
                candidate = config.SCREENSHOTS_DIR / f"comment_{video_id}_{c.get('cid')}.png"
                try:
                    s.page.screenshot(path=str(candidate), clip=clip)
                    shot_path = candidate
                except Exception:  # noqa: BLE001
                    shot_path = None
            user = c.get("user") or {}
            comments.append({
                "cid": c.get("cid"),
                "text": c.get("text", ""),
                "username": user.get("unique_id") or user.get("nickname") or "?",
                "digg_count": c.get("digg_count", 0),
                "create_time": c.get("create_time"),
                "reply_count": c.get("reply_comment_total", 0),
                "screenshot": str(shot_path) if shot_path else None,
            })

        s.finish()

    return {"video_id": video_id, "comments": comments,
            "panel_screenshot": str(panel_shot) if panel_shot else None}


def _panel_open(page) -> bool:
    """True when at least one comment row is actually VISIBLE (not merely present in the DOM: after a
    close/reopen the rows can linger while the panel is hidden or off-screen)."""
    return bool(page.evaluate("""() => [...document.querySelectorAll('[data-e2e="comment-level-1"]')].some(e => {
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && r.right > 0 && r.left < innerWidth && r.bottom > 0
            && r.top < innerHeight && cs.visibility !== 'hidden' && cs.display !== 'none'; })"""))


def _ensure_panel_open(s) -> bool:
    """Make the comment panel visible without ever toggling it shut. A freshly opened post renders its panel a
    moment late (and some layouts open it by themselves), so first WAIT for it; the comment icon is a toggle,
    so only click it when the panel really did not appear, and if that did not help the first click probably
    closed a panel that was just about to show - toggle once more."""
    def wait(seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if _panel_open(s.page):
                return True
            time.sleep(0.5)
        return False

    if wait(4):
        return True
    for _ in range(2):
        s.click('[data-e2e="comment-icon"]', by="css")
        if wait(4):
            return True
    return False


def _checked_click(s, target, label: str):
    """Trusted click at the element's centre, but ONLY after proving that element is what sits under that
    point. Returns None when clicked, or the reason it refused.

    Confirmed live (2026-10-07): clicking blindly at coordinates inside the profile-opened overlay missed
    the reply box; the following Ctrl+A and the typed text then went to the TikTok PAGE, whose keyboard
    shortcuts reacted (r = repost, m = mute, c = comments panel, space = play/pause - a "Repost removed"
    toast appeared). Never act on something that was not verified."""
    try:
        # ONE stable handle: the comment panel is virtualised and re-renders while it scrolls, so a positional
        # locator (nth(i)) can end up pointing at nothing (seen live: 30 s TimeoutError)
        handle = target.element_handle(timeout=5000) if hasattr(target, "element_handle") else target
    except Exception as exc:  # noqa: BLE001
        return f"{label} is not available ({type(exc).__name__})"
    if handle is None:
        return f"{label} is not available"
    for attempt in range(3):
        try:
            if attempt == 0:
                handle.scroll_into_view_if_needed(timeout=5000)
            else:       # at the panel's edge the sticky header / bottom comment bar can sit on top: centre it
                handle.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
            time.sleep(0.5)
            box = handle.bounding_box()
        except Exception as exc:  # noqa: BLE001
            return f"{label} is not available ({type(exc).__name__})"
        if not box or box["width"] < 2 or box["height"] < 2:
            return f"{label} is not visible"
        cx, cy = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
        vw, vh = s.page.evaluate("() => [innerWidth, innerHeight]")
        if not (0 <= cx <= vw and 0 <= cy <= vh):
            return f"{label} is outside the visible window"
        cover = handle.evaluate(
            "(el, p) => { const hit = document.elementFromPoint(p[0], p[1]);"
            " if (hit && (el === hit || el.contains(hit))) return null;"
            " return hit ? hit.tagName + '[' + (hit.getAttribute('data-e2e') || '') + '|' +"
            " String(hit.className).slice(0, 40) + ']' : 'nothing'; }", [cx, cy])
        if cover is None:
            break
        if attempt == 2:
            return f"{label} is covered by something else at the click point ({cover})"
        time.sleep(0.6)
    s.behavior.click(s.page, cx, cy)
    return None


def _input_focused(page) -> bool:
    """Keyboard focus is inside an editable comment box (never on the page body)."""
    return bool(page.evaluate("""() => { const a = document.activeElement; if (!a) return false;
        const editable = a.isContentEditable || a.tagName === 'INPUT' || a.tagName === 'TEXTAREA';
        return editable && [...document.querySelectorAll('[data-e2e="comment-input"]')]
            .some(inp => inp === a || inp.contains(a)); }"""))


def _focused_text(page) -> str:
    return page.evaluate("() => { const a = document.activeElement; return a ? (a.innerText || a.value || '') : ''; }") or ""


_AUTHOR = re.compile(r"tiktok\.com/@([^/?#]+)/(?:video|photo)/")


def _author_profile_url(video_url: str) -> str | None:
    """The profile page of the account that owns the post, when the link names it ('@_' = unknown)."""
    m = _AUTHOR.search(video_url or "")
    return f"https://www.tiktok.com/@{m.group(1)}" if m and m.group(1) != "_" else None


def _standing_on(s, profile_url: str) -> bool:
    """Is this (kept) browser on the owner's profile page - or on a post opened over it?"""
    try:
        from urllib.parse import urlparse
        here, wanted = urlparse(s.page.url).path.lower().rstrip("/"), urlparse(profile_url).path.lower().rstrip("/")
        return bool(wanted) and (here == wanted or here.startswith(wanted + "/"))
    except Exception:  # noqa: BLE001
        return False


def _open_tile_fast(s, tile: str, video_id: str, profile_url: str, *, attempts: int = 3, wait_s: float = 5.0) -> bool:
    """Click the post's tile until THAT post is open. -> did it open?

    Confirmed live (2026-10-15, screenshot scanfail_*.png): the grid can shift under the pointer while it scrolls and the click
    opens a DIFFERENT post; clicking on while that post's overlay covers the grid then times out three times and the watcher's
    recovery reloads the profile page - a page load TikTok counts and refuses (70 s lost, then a refused reply). So a wrong post
    is closed first, the grid is given a moment to settle after scrolling, and only then is the click repeated."""
    from app import tiktok_watch as tw
    page = s.page
    for attempt in range(attempts):
        if "/video/" in page.url and f"/video/{video_id}" not in page.url:
            _back_to_grid(s, profile_url)                       # the wrong post: close it, do not click through it
        loc = page.locator(tile).first
        try:
            loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)                              # let the grid settle: tiles moving under the cursor is the cause
        try:
            if attempt == 0:
                s.click(tile, by="css")                         # humanised first, like a person
            else:
                loc.click(timeout=4000)                         # then a plain trusted click
        except Exception:  # noqa: BLE001
            pass
        if tw._wait_for(lambda: f"/video/{video_id}" in page.url, wait_s, page=page):
            return True
    return False


def _back_to_grid(s, profile_url: str) -> None:
    """A post is open over the profile grid: close it. Two quick Escapes (the first only closes the comment box), then the
    patient fallbacks of the watcher (more Escapes, history.back, the profile document)."""
    from app import tiktok_watch as tw
    page = s.page
    if not tw._overlay_open(page):
        return
    for _ in range(2):
        page.keyboard.press("Escape")
        page.wait_for_timeout(350)
        if not tw._overlay_open(page):
            return
    tw._close_overlay(s, profile_url)


def _tile_count(page) -> int:
    """How many post tiles the page holds. A page that is busy navigating answers evaluate() with an error: that is 'none yet'."""
    try:
        return int(page.evaluate("() => document.querySelectorAll('a[href*=\"/video/\"], a[href*=\"/photo/\"]').length") or 0)
    except Exception:  # noqa: BLE001
        return 0


_TILE_HREFS_JS = """() => [...document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]')].map(a => a.getAttribute('href'))"""


def _scroll_grid(page) -> None:
    """One step down the profile grid, the way a person scrolls: the pointer on the grid, then the wheel. Measured live
    (2026-10-15, snapshot tilefail_*.png): with the pointer parked over TikTok's fixed side menu the wheel scrolled NOTHING and the
    scan ended with the first 19 tiles - so the pointer is moved onto the grid first, and when the page still did not move the page
    itself is scrolled."""
    try:
        before = page.evaluate("() => scrollY")
        width, height = page.evaluate("() => [innerWidth, innerHeight]")
        page.mouse.move(width * 0.62, height * 0.62)           # over the grid, not over the fixed menu on the left
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(400)
        if page.evaluate("() => scrollY") == before:           # the wheel went nowhere: scroll the page itself
            page.evaluate("() => window.scrollBy(0, 900)")
        page.wait_for_timeout(400)
    except Exception:  # noqa: BLE001 - a page that is busy navigating: try again at the next step
        time.sleep(0.8)


def _at_page_bottom(page) -> bool:
    try:
        return bool(page.evaluate("() => scrollY + innerHeight >= document.documentElement.scrollHeight - 8"))
    except Exception:  # noqa: BLE001
        return False


def _snapshot(s, name: str, why: str) -> None:
    """A picture of what the page looked like when something did not work - the only way to learn why afterwards."""
    try:
        s.page.screenshot(path=str(config.SCREENSHOTS_DIR / f"{name}.png"))
        LOG.info("saved %s.png (%s)", name, why)
    except Exception:  # noqa: BLE001
        pass


def _tile_signature(page) -> tuple:
    """Which tiles the page holds right now: how many, and the first and last one. TikTok's grid is VIRTUALISED - scrolling keeps
    roughly the same NUMBER of tiles in the page and swaps which ones - so progress is a change of tiles, not of their count."""
    try:
        hrefs = page.evaluate(_TILE_HREFS_JS) or []
    except Exception:  # noqa: BLE001
        hrefs = []
    return (len(hrefs), hrefs[0] if hrefs else None, hrefs[-1] if hrefs else None)


def _find_tile_fast(s, tile: str, profile_url: str, *, max_steps: int = 40) -> bool:
    """Make the post's tile present on a profile grid: wait for the first tiles to render (not a fixed pause), then scroll in
    steps until the tile shows up or the end of the grid is really reached. Measured live (2026-10-15): after a scroll the SAME
    tiles stay for a moment while TikTok fetches more (19 tiles, 19, then 34) - so "no change" alone is not the end: it is the end
    when the page is scrolled to its bottom and nothing new came for several scrolls in a row (or for very many without it).
    The page is reloaded only when NOTHING rendered - never after a complete scan: a second profile page load is what TikTok
    refuses."""
    from app import tiktok_watch as tw
    page = s.page
    for reload_first in (False, True):
        if reload_first and not s.goto(profile_url).get("ok"):
            return False
        tw._wait_for(lambda: _tile_count(page) > 0, 15, 0.5, page)
        if _tile_count(page) == 0 and not reload_first:
            continue                                        # an empty page: one reload is worth a try
        last, same = None, 0
        for _ in range(max_steps):
            try:
                if page.locator(tile).count():
                    return True
            except Exception:  # noqa: BLE001 - the page is busy: look again after the next scroll
                pass
            signature = _tile_signature(page)
            same = same + 1 if signature == last else 0
            last = signature
            if same >= 12 or (same >= 6 and _at_page_bottom(page)):
                break
            _scroll_grid(page)
        _snapshot(s, f"tilefail_{tile.split('/video/')[-1].rstrip(chr(34) + ']')}", f"{last[0]} tiles at the end")
        return False
    return False


def _open_post(s, video_id: str, video_url: str, *, via_profile: bool = True, profile_url: str | None = None,
               trace=None, keep_open: bool = False) -> tuple:
    """Open a post the way a person does: the profile page, then a click on its tile. -> (goto_result, via_tile).

    Confirmed live (2026-10-07): TikTok answers repeated loads of one /video/<id> DOCUMENT with an empty
    HTTP 403 while the profile page and the click-through keep working. A post of somebody else is opened the same way,
    through ITS OWNER's profile (`profile_url`); `via_profile=False` is the direct load alone (no owner known).

    When the PROFILE page itself will not open (confirmed live 2026-10-14: TikTok refuses one account for minutes while
    another account loads the same page at once) the answer is {"ok": False, "blocked": True}: the caller moves on to the
    next account at once - loading the post URL straight after would only meet the same refusal. The direct load is only
    the fallback for a post that is not on the owner's grid. `keep_open`: when a kept browser already shows THIS post, leave it
    as it is (a reply needs the comments on the page, not a fresh list - a read does need the list, so it never sets this)."""
    from app import tiktok_watch as tw
    if not via_profile:
        goto = s.goto(video_url)
        return ({**goto, "blocked": True} if not goto.get("ok") else goto), False
    profile_url = profile_url or config.TARGET_PROFILE_URL
    tile = f'a[href*="/video/{video_id}"]'
    if _standing_on(s, profile_url):                        # a kept browser: already on this owner's page - no page load at all
        try:
            if keep_open and f"/video/{video_id}" in s.page.url:
                if trace:                                   # this very post is open already: nothing to navigate
                    trace.mark("post still open", "Finding the comment...")
                return {"ok": True}, True
            _back_to_grid(s, profile_url)                   # a post may be open over the grid: back to the grid
        except Exception:  # noqa: BLE001
            pass
        if trace:
            trace.mark("profile page reused", "Looking for the post on its page...")
    else:
        g = s.goto(profile_url)
        note_profile_load(getattr(s, "name", None) or "?")
        if not g.get("ok"):
            return {"ok": False, "blocked": True, "error": f"the profile page did not open ({g.get('error') or 'no answer'})"}, False
        if trace:
            trace.mark("profile page loaded", "Looking for the post on its page...")
    if _captcha_present(s.page):
        return {"ok": True}, False                          # the caller reports the wall it sees on this very page
    try:
        found = _find_tile_fast(s, tile, profile_url)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("post %s: the profile page could not be searched: %s", video_id, str(exc)[:160])
        _snapshot(s, f"tilefail_{video_id}", "an error while searching the grid")
        return {"ok": False, "blocked": True, "error": f"the profile page could not be read ({str(exc)[:100]})"}, False
    if found:
        if trace:
            trace.mark("post found on the grid", "Opening the post...")
        try:
            opened = _open_tile_fast(s, tile, video_id, profile_url)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("post %s: clicking its tile failed: %s", video_id, str(exc)[:160])
            opened = False
        if not opened:
            _snapshot(s, f"tileclickfail_{video_id}", "the post did not open")
            return {"ok": False, "blocked": True, "error": "the post did not open when its tile was clicked"}, False
        return {"ok": True}, True
    # the post is not on the owner's grid at all: its address is the only way left (it often meets a captcha)
    LOG.info("post %s is not on the owner's grid - loading it by its address", video_id)
    goto = s.goto(video_url)
    return ({**goto, "blocked": True} if not goto.get("ok") else goto), False


def _reopen_post(s, video_id: str, via_tile: bool, profile_url: str | None = None) -> None:
    """Fresh view of the post without a document reload when it was opened by tile."""
    from app import tiktok_watch as tw
    if not via_tile:
        s.page.reload(wait_until="domcontentloaded", timeout=20000)
        return
    profile_url = profile_url or config.TARGET_PROFILE_URL
    _back_to_grid(s, profile_url)
    tile = f'a[href*="/video/{video_id}"]'
    if _find_tile_fast(s, tile, profile_url):
        _open_tile_fast(s, tile, video_id, profile_url)


_PANEL_TOP_JS = """() => {
  const el = document.querySelector('[data-e2e="comment-level-1"]');
  if (!el) return 'no-list';
  let p = el.parentElement;
  while (p && p !== document.body) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight + 5) { p.scrollTop = 0; return 'top'; }
    p = p.parentElement;
  }
  return 'window';
}"""


def _dom_index_of(s, comment: dict, max_scrolls: int = 14):
    """Index, among the comment rows currently rendered in the panel, of `comment` (a /api/comment/list/ entry).

    The panel is virtualised: in a headless 634px-high window only ~6 rows exist in the DOM, so the entry's
    position in the API list is NOT its DOM position (seen live: list index 7, 6 rows rendered). Match by
    text (and the author's display name when the rows line up), scrolling the panel until it shows up."""
    from app import tiktok_watch as tw
    page = s.page
    norm = lambda t: " ".join((t or "").split()).lower()
    want = norm(comment.get("text"))
    nick = norm((comment.get("user") or {}).get("nickname"))
    try:                                                     # start at the top: an earlier search may have scrolled far down
        page.evaluate(_PANEL_TOP_JS)
        time.sleep(0.4)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(max_scrolls + 1):
        rows = page.evaluate("""() => ({
            texts: [...document.querySelectorAll('[data-e2e="comment-level-1"]')].map(e => e.innerText),
            names: [...document.querySelectorAll('[data-e2e="comment-username-1"]')].map(e => e.innerText)})""")
        texts, names = rows["texts"], rows["names"]
        hits = [i for i, t in enumerate(texts) if norm(t) == want]
        if len(hits) > 1 and nick and len(names) == len(texts):
            named = [i for i in hits if nick in norm(names[i])]
            hits = named or hits
        if hits:
            return hits[0]
        page.evaluate(tw._SCROLL_PANEL_JS)
        time.sleep(0.9)
    return None


def _proof_screenshot(s, reply_text: str, path) -> str | None:
    """Picture of the comment panel with the reply that was just posted, for the operator to check.

    The new reply is scrolled to the middle of the panel (so the comment it answers is visible above it) and the
    shot is cropped to the panel, which stays readable on a phone. Falls back to the right-hand third of the
    window, then to the whole window. None when no screenshot could be taken at all."""
    page = s.page
    snippet = " ".join(reply_text.split())[:24]
    shown = False
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and not shown:       # the reply renders a moment after the publish response
            shown = bool(page.evaluate("""(snippet) => {
                const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
                const hit = [...document.querySelectorAll('[data-e2e^="comment-level"]')]
                    .find(e => norm(e.innerText).startsWith(snippet));
                if (hit) hit.scrollIntoView({block: 'center'});
                return !!hit; }""", snippet))
            if not shown:
                time.sleep(0.5)
        time.sleep(0.4)
        clip = page.evaluate("""() => {
            const e = document.querySelector('[data-e2e="comment-level-1"]');
            let node = e ? e.parentElement : null, best = null;
            while (node && node !== document.body) {                    // the widest-but-still-side-panel ancestor
                const r = node.getBoundingClientRect();
                if (r.width >= 250 && r.width <= innerWidth * 0.5 && r.height >= 300) best = r;
                node = node.parentElement;
            }
            if (!best) { const x = innerWidth * 0.64; return {x, y: 0, width: innerWidth - x, height: innerHeight}; }
            const x = Math.max(0, best.x), y = Math.max(0, best.y);
            return {x, y, width: Math.min(best.width, innerWidth - x), height: Math.min(best.height, innerHeight - y)}; }""")
        page.screenshot(path=str(path), clip=clip)
        return str(path)
    except Exception:  # noqa: BLE001
        try:
            page.screenshot(path=str(path))
            return str(path)
        except Exception:  # noqa: BLE001
            return None


def _make_response_listener(video_id: str, captured: dict):
    """Response listener for ONE post's page. Keeps (a) the latest /api/comment/list/ payload of THIS post and every
    comment seen so far (captured["seen"], by id: a linked comment can sit several pages down), and (b) the first
    /api/comment/publish/ response - TikTok's own verdict on a comment we sent."""
    def on_response(response):
        url = response.url
        # Always overwrite with the LATEST list response, never latch onto
        # the first, AND scoped to THIS video's aweme_id - see the matching
        # comment in fetch_comments_with_screenshots for why (confirmed
        # live, 2026-10-07: without the aweme_id scope, an unrelated
        # video's prefetched comment list can win the "latest" race and
        # mask the correct one).
        if ("/api/comment/list/" in url and "/reply/" not in url
                and f"aweme_id={video_id}" in url):
            try:
                captured["list_payload"] = response.json()
                for c in captured["list_payload"].get("comments") or []:      # every comment seen so far, by id
                    if c.get("cid"):
                        captured.setdefault("seen", {})[c["cid"]] = c
            except Exception:  # noqa: BLE001
                pass
        # Tightened to the actual publish endpoint (confirmed live,
        # 2026-10-06, via instrumented capture of every /comment/-matching
        # response): the old broad "/api/comment/" substring could in
        # principle also match some other POST under that path and silently
        # report ITS 200 as success. Also keeps the raw response text, not
        # just a parsed-or-None body - confirmed live that a real soft
        # rejection here looks like HTTP 200 with a completely EMPTY body
        # (not malformed JSON, not an error field - nothing), which a bare
        # `body: None` can't be told apart from "no response captured yet".
        if (captured["publish_response"] is None and "/api/comment/publish/" in url
                and response.request.method == "POST"):
            raw_text = None
            try:
                raw_text = response.text()
            except Exception:  # noqa: BLE001
                pass
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = None
            # Confirmed live (2026-10-06) via a HAR of a successful MANUAL
            # reply from a real browser: the request that actually works
            # carries a whole family of "Ticket Guard" headers
            # (tt-ticket-guard-client-data/-version/-public-key/-iteration-
            # version/-web-version) plus tt-csrf-token and x-secsdk-csrf-
            # token - a cryptographically signed, per-device anti-abuse
            # layer separate from X-Bogus/X-Gnarly, gating sensitive writes
            # specifically. The signing secret behind tt-ticket-guard-
            # client-data almost certainly lives in IndexedDB (set up once
            # during a real login), never carried over by a cookie-only
            # import - recording these headers here (present or absent)
            # confirms or rules this out directly, instead of guessing.
            try:
                req_headers = dict(response.request.headers)
            except Exception:  # noqa: BLE001
                req_headers = {}
            ticket_guard_keys = [k for k in req_headers if k.lower().startswith("tt-ticket-guard")
                                 or k.lower() in ("tt-csrf-token", "x-secsdk-csrf-token")]
            captured["publish_response"] = {"url": url, "status": response.status,
                                            "body": body, "raw_text": raw_text,
                                            "ticket_guard_headers_present": sorted(ticket_guard_keys),
                                            "req_headers": req_headers}
    return on_response


class _SkipRecheck(Exception):
    """Not an error: the publish answer already proved the reply is on TikTok, the slow re-check is not needed."""


def _post_reply_once(video_url: str, cid: str, reply_text: str, *, profile_name: str,
                      headless: bool | None = None, hitl_timeout_s: float = 120.0,
                      foreign: bool = False, dry_run: bool = False, stage=None, comment_text: str | None = None) -> dict:
    """One real attempt, through ONE specific account (profile_name). Post
    reply_text as a reply to top-level comment `cid`, using the logged-in
    account's own browser UI (Reply button -> type -> Post) -- never a raw
    API call, since TikTok's request signing (X-Bogus/X-Gnarly) is computed
    by its own JS per-request and isn't something to replicate reliably
    outside a real browser session.

    foreign=True: the post belongs to somebody else (a /comment <link>): it is opened by its URL (it is not on our
    profile grid) and the panel is scrolled until the linked comment has loaded, however far down it sits.
    dry_run=True: everything up to the click on Post - the text is typed and checked, a screenshot is taken, the box
    is emptied again and NOTHING is sent ({"ok": True, "dry_run": True}).

    Confirmed live (logged in): clicking a comment's [data-e2e="comment-reply-1"]
    inserts a NEW inline [data-e2e="comment-input"]/[data-e2e="comment-post"]
    pair right after that comment (placeholder switches to "Add a reply..."),
    ahead of the page's persistent bottom comment box in DOM order -- so
    index 0 of each after a reply-click is reliably the one we just opened.

    A result with "captcha": True means _captcha_present() saw TikTok's
    slider wall - see post_reply() (the public wrapper below) for the retry
    policy; this function itself never retries.
    """
    video_id = extract_video_id(video_url)
    trace = _Trace("reply", profile_name, video_id, stage)
    captured: dict = {"list_payload": None, "publish_response": None, "seen": {}}

    on_response = _make_response_listener(video_id, captured)

    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault, profile_name)
    headless = config.BFP_HEADLESS if headless is None else headless

    with _browser(profile, vault, profile_name, kind="post-reply", video_id=video_id, headless=headless, hitl="wait",
                  hitl_timeout_s=hitl_timeout_s) as s:
        s.listen(on_response)
        if foreign:
            s.recovery.update(FAST_RECOVERY)
        trace.mark("browser reused" if s.reused else "browser started", f"Opening the post with {_label(profile_name)}...")
        owner = _author_profile_url(video_url) if foreign else None
        goto, via_tile = _open_post(s, video_id, video_url, via_profile=(not foreign) or bool(owner), profile_url=owner, trace=trace,
                                    keep_open=foreign)
        if not goto.get("ok"):
            s.finish()
            return {"ok": False, "blocked": bool(goto.get("blocked")), "profile": profile_name,
                    "error": f"could not open {video_url}: {goto.get('error')}"}
        if _captcha_present(s.page):
            s.finish()
            return {"ok": False, "captcha": True, "profile": profile_name,
                    "error": "TikTok's slider-puzzle wall appeared on page load"}
        if not is_logged_in(s.page):
            s.finish()
            return {"ok": False, "profile": profile_name,
                    "error": "not logged in -- run 'python run.py login' or 'import-cookies'"}

        trace.mark("post opened", "Finding the comment...")
        # Poll until the TARGET comment actually shows up, not just until
        # "a" list response arrives. Confirmed live (2026-10-07): opening
        # the panel can fire more than one /api/comment/list/ request (an
        # initial one, then another once the tab-check's click settles);
        # stopping at the first arrival could mean checking a premature,
        # incomplete response even though the correct one (with the target
        # comment in it) lands a moment later.
        #
        # Wrapped in a retry (reload + reopen), not just a longer single
        # wait: confirmed live (2026-10-07) that even with the above fix
        # AND the capture scoped to this exact aweme_id, the panel
        # occasionally still never surfaces the target comment within one
        # full wait window on this specific, heavily-reused test account/
        # video today - most plausibly transient server-side variance from
        # how many times this exact video has been hit in a short span,
        # not a code bug. A reload-and-retry (the same recovery pattern
        # already used below for the post-publish recheck) is the robust
        # answer regardless of which it is.
        comments: list = []
        index = None
        dom_hint = None                          # where the on-page search found the comment (no need to search twice)
        for attempt in range(3):
            if attempt > 0:
                _say(stage, f"Looking again for the comment (try {attempt + 1} of 3)...")
                _reopen_post(s, video_id, via_tile, owner)
            _ensure_panel_open(s)                # waits first: an opened post may show its panel by itself
            time.sleep(0.4)
            if _captcha_present(s.page):
                s.finish()
                return {"ok": False, "captcha": True, "profile": profile_name,
                        "error": "TikTok's slider-puzzle wall appeared after opening the comment panel"}
            _ensure_comments_tab(s.page)
            # proxy-aware: residential proxies add real latency; a linked comment (foreign) can be pages down, so it
            # gets longer and the panel is scrolled to make TikTok send the next pages
            deadline = time.monotonic() + (45 if foreign else 25)
            next_scroll = time.monotonic() + 2.5
            on_screen_at = time.monotonic() + (1.5 if s.reused else 6) if (comment_text and cid) else None
            while time.monotonic() < deadline:
                if on_screen_at is not None and time.monotonic() >= on_screen_at and captured["list_payload"] is None:
                    # No comment list came: a kept browser shows a post it has shown before from its own cache. The comment is
                    # on the page all the same - find it by its text, which the app knows because it read it.
                    on_screen_at = None
                    try:
                        found_at = _dom_index_of(s, {"cid": cid, "text": comment_text}, max_scrolls=10)
                        if found_at is not None:
                            comments, index, dom_hint = [{"cid": cid, "text": comment_text}], 0, found_at
                            trace.mark("comment found on the page", None)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if captured["list_payload"] is not None:
                    comments = captured["list_payload"].get("comments") or []
                    index = next((i for i, c in enumerate(comments) if c.get("cid") == cid), None)
                    if index is None and foreign and cid in captured["seen"]:      # it arrived with an earlier page
                        comments, index = [captured["seen"][cid]], 0
                    if index is not None:
                        break
                if foreign and time.monotonic() >= next_scroll:
                    next_scroll = time.monotonic() + 2.5
                    try:
                        from app import tiktok_watch as tw
                        s.page.evaluate(tw._SCROLL_PANEL_JS)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.5)
            if index is not None:
                break
        if index is None:
            # Diagnostic-only (2026-10-07): this path started failing
            # specifically through post_reply() under backend="patchright"
            # while an otherwise-identical manual replication of the same
            # steps succeeded every time - capturing a screenshot and the
            # page title here, instead of guessing blind, to see what the
            # page actually looked like at the moment of failure.
            fail_shot = config.SCREENSHOTS_DIR / f"fail_list_{video_id}_{cid}.png"
            try:
                s.page.screenshot(path=str(fail_shot))
            except Exception:  # noqa: BLE001
                fail_shot = None
            try:
                title = s.page.title()
            except Exception:  # noqa: BLE001
                title = None
            try:
                page_url = s.page.url
            except Exception:  # noqa: BLE001
                page_url = None
            s.finish()
            return {"ok": False, "error": f"comment {cid} not found among the first "
                                           f"{len(comments)} top-level comments loaded",
                    "diag_list_payload_arrived": captured["list_payload"] is not None,
                    "diag_page_title": title, "diag_page_url": page_url,
                    "diag_screenshot": str(fail_shot) if fail_shot else None}

        # A REAL, trusted click (page.mouse.down/up at a real coordinate via
        # CDP, exactly what bfp's own Behavior.click/click_selector does),
        # NOT page.evaluate(() => el.click()). Confirmed live
        # (2026-10-06): TikTok's own /api/comment/publish/ request - built
        # entirely by TikTok's own JS, text/reply_id/signing all correct -
        # came back HTTP 200 with a completely empty body, and the reply
        # never actually appeared even after a fresh reload. A raw
        # el.click() in JS produces a DOM event with isTrusted=false; a
        # well-known anti-bot signal many platforms (TikTok included, very
        # plausibly) check specifically on write/mutation actions while
        # leaving reads (page loads, comment list fetches) completely
        # unaffected - which matches exactly what was observed: everything
        # up to and including the network request looked perfect, only the
        # actual effect silently never landed.
        dom_index = dom_hint if dom_hint is not None else _dom_index_of(s, comments[index])
        if dom_index is None or dom_index >= s.page.locator('[data-e2e="comment-reply-1"]').count():
            s.finish()
            return {"ok": False, "error": "reply button not found in the DOM for that comment"}
        reply_btn = s.page.locator('[data-e2e="comment-reply-1"]').nth(dom_index)
        refusal = _checked_click(s, reply_btn, "the Reply button")
        if refusal:
            s.finish()
            return {"ok": False, "error": f"refused to act: {refusal}"}
        time.sleep(1.0)

        # Explicitly click INTO the input (don't just assume focus survived
        # from the reply-button click above), then clear any stale draft,
        # then type through bfp's own humanized Behavior.type_text() -- NOT
        # raw page.keyboard.type(..., delay=25). Confirmed live (2026-10-06)
        # that a flat, uniform 25ms-per-character rhythm is exactly the kind
        # of input-timing signal TikTok's own anti-bot JS (the code that
        # builds X-Bogus/X-Gnarly) is built to notice right before a write
        # action -- this is a real, distinct automation tell, separate from
        # click-trust (already fixed) and IP/region (already fixed), and was
        # still present in every post_reply() attempt so far even after
        # those two fixes landed. Behavior.type_text() gives variable
        # per-character delay, realistic pauses on spaces, and occasional
        # human-like typo+backspace+correction -- the same engine bfp uses
        # for every other field it fills.
        # Confirmed live (2026-10-06), twice: the text that actually reaches
        # the box is missing its OWN FIRST character ("Oi!" arrived as "i!")
        # -- a race between the click's focus event settling and the very
        # first keystroke landing, not a selector/clearing problem (Control+A
        # and Delete both land fine; only the first char of the typed text
        # is swallowed). A short settle after focus and after clearing fixes
        # the race without faking anything about the typing itself.
        # Nothing is typed unless keyboard focus is VERIFIED to be inside the comment box (see _checked_click:
        # typing into the page itself triggers TikTok's keyboard shortcuts).
        trace.mark("comment found", "Typing the reply...")
        refusal = _checked_click(s, s.page.locator('[data-e2e="comment-input"]').first, "the reply box")
        time.sleep(0.3)
        if not refusal and not _input_focused(s.page):
            refusal = "the reply box did not take keyboard focus"
        if refusal:
            s.finish()
            return {"ok": False, "error": f"refused to type: {refusal}"}
        s.page.keyboard.press("Control+A")
        s.page.keyboard.press("Delete")
        time.sleep(0.2)
        if not _input_focused(s.page):
            s.finish()
            return {"ok": False, "error": "refused to type: focus left the reply box"}
        s.behavior.type_text(s.page, reply_text)
        time.sleep(0.5)
        if " ".join(reply_text.split())[:12].lower() not in " ".join(_focused_text(s.page).split()).lower():
            s.finish()
            return {"ok": False, "error": "refused to post: the typed text is not in the reply box"}

        before_shot = config.SCREENSHOTS_DIR / f"before_post_{video_id}_{cid}.png"
        try:
            s.page.screenshot(path=str(before_shot))
        except Exception:  # noqa: BLE001
            before_shot = None

        post_btns = s.page.locator('[data-e2e="comment-post"]')
        if post_btns.count() == 0:
            s.finish()
            return {"ok": False, "error": "post button not found",
                    "before_screenshot": str(before_shot) if before_shot else None}
        if dry_run:                          # everything worked up to the click: leave the box empty, send nothing
            visible = post_btns.first.is_visible()
            s.page.keyboard.press("Control+A")
            s.page.keyboard.press("Delete")
            s.keep = bool(visible)
            s.finish()
            return {"ok": visible, "dry_run": True, "profile": profile_name, "publish_response": None,
                    "error": None if visible else "the Post button is not visible",
                    "before_screenshot": str(before_shot) if before_shot else None}
        trace.mark("reply typed", "Sending the reply...")
        refusal = _checked_click(s, post_btns.first, "the Post button")
        if refusal:
            s.finish()
            return {"ok": False, "error": f"refused to post: {refusal}",
                    "before_screenshot": str(before_shot) if before_shot else None}

        deadline = time.monotonic() + 20  # proxy-aware: residential proxies add real latency
        while captured["publish_response"] is None and time.monotonic() < deadline:
            time.sleep(0.3)
        time.sleep(1.5)

        after_shot = config.SCREENSHOTS_DIR / f"after_post_{video_id}_{cid}.png"
        try:
            s.page.screenshot(path=str(after_shot))
        except Exception:  # noqa: BLE001
            after_shot = None
        proof_shot = None
        trace.mark("publish answered", "Taking the proof screenshot...")
        if captured["publish_response"] and captured["publish_response"].get("status") == 200:
            proof_shot = _proof_screenshot(s, reply_text, config.SCREENSHOTS_DIR / f"proof_{video_id}_{cid}.png")

        # Ground-truth verification, not just trusting the response: confirmed
        # live (2026-10-06) that TikTok's own /api/comment/publish/ can return
        # HTTP 200 with a completely empty body while never actually posting
        # anything - `status == 200` alone is not evidence of success. The
        # only real proof is this exact comment's reply_comment_total going
        # up, re-checked via a full reload + re-open (not the stale pre-post
        # snapshot) through the SAME /api/comment/list/ capture already used
        # and proven elsewhere in this file, rather than a new, unverified
        # selector for the per-comment "view replies" expander.
        captured["recheck_payload"] = None

        def on_recheck(response):
            # Latest, not first, scoped to THIS video's aweme_id - see the
            # matching comment above.
            if ("/api/comment/list/" in response.url and "/reply/" not in response.url
                    and f"aweme_id={video_id}" in response.url):
                try:
                    captured["recheck_payload"] = response.json()
                except Exception:  # noqa: BLE001
                    pass

        s.listen(on_recheck)
        before_count = next((c.get("reply_comment_total", 0) for c in (captured["list_payload"] or {}).get("comments", [])
                            if c.get("cid") == cid), 0)
        after_count = before_count
        try:
            if _publish_outcome(captured["publish_response"])["server_confirmed"]:
                # TikTok itself said "published" (status 0 and the new comment's id): re-opening the post for 20 s to count
                # replies would only repeat that. The re-check stays for the ambiguous answers (an empty HTTP 200).
                raise _SkipRecheck()
            _reopen_post(s, video_id, via_tile, owner)
            _ensure_panel_open(s)
            time.sleep(0.4)
            _ensure_comments_tab(s.page)
            # Poll until the target comment actually shows up in a recheck
            # payload, not just until any response arrives (same reasoning
            # as the main wait loop above).
            deadline = time.monotonic() + 20  # proxy-aware: residential proxies add real latency
            found = False
            while time.monotonic() < deadline:
                if captured["recheck_payload"] is not None:
                    match = next((c for c in captured["recheck_payload"].get("comments", [])
                                 if c.get("cid") == cid), None)
                    if match is not None:
                        after_count = match.get("reply_comment_total", before_count)
                        found = True
                        break
                time.sleep(0.3)
            if not found and captured["recheck_payload"] is not None:
                after_count = next((c.get("reply_comment_total", 0)
                                   for c in captured["recheck_payload"].get("comments", [])
                                   if c.get("cid") == cid), before_count)
        except Exception:  # noqa: BLE001 - includes _SkipRecheck
            pass

        actually_posted = after_count > before_count
        trace.mark("finished")
        s.keep = _publish_outcome(captured["publish_response"])["server_confirmed"]     # TikTok confirmed: the page is in good order
        s.finish()

    pub = captured["publish_response"]
    http_ok = bool(pub and pub.get("status") == 200)
    # Confirmed live (2026-10-07): the reload-based recheck above can race
    # ahead of TikTok's own write-then-read propagation - a post that
    # genuinely succeeded (status_code 0, the real new comment object
    # returned, e.g. "status_msg": "Comment sent successfully") still
    # showed after_count == before_count moments later, a false negative
    # from the recheck's timing, not from the post itself. The publish
    # response's own body is the server's direct, authoritative
    # confirmation when present - trust it over a secondary derived count
    # that can simply not have caught up yet.
    body = pub.get("body") if pub else None
    server_confirmed = bool(
        isinstance(body, dict) and body.get("status_code") == 0
        and isinstance(body.get("comment"), dict) and body["comment"].get("cid")
    )
    actually_posted = actually_posted or server_confirmed
    ok = http_ok and actually_posted
    cmt = body.get("comment") if isinstance(body, dict) else None
    comment_status = cmt.get("status") if isinstance(cmt, dict) else None
    visibility = comment_visibility(comment_status)
    if not pub:
        return {"ok": False, "http_ok": False, "actually_posted": False, "server_confirmed": False,
                "profile": profile_name, "publish_response": None,
                "error": "TikTok never answered the publish request (nothing was sent)"}
    return {"ok": ok, "http_ok": http_ok, "actually_posted": actually_posted,
            "server_confirmed": server_confirmed, "profile": profile_name,
            "comment_status": comment_status, "visibility": visibility,
            "reply_count_before": before_count, "reply_count_after": after_count,
            "publish_response": pub,
            "before_screenshot": str(before_shot) if before_shot else None,
            "after_screenshot": str(after_shot) if after_shot else None,
            "proof_screenshot": proof_shot, "timings": trace.timings()}


def post_reply(video_url: str, cid: str, reply_text: str, *, headless: bool | None = None,
                hitl_timeout_s: float = 120.0, max_captcha_retries: int = 2,
                profile_names: list[str] | None = None) -> dict:
    """Public entry point. Two layers of fallback:

    1. Per account: retry with a completely FRESH session (new browser
       launch, not a reload of the same one) when TikTok's slider-puzzle
       wall shows up, with growing backoff, up to max_captcha_retries times.
    2. Across accounts: if an account is STILL unusable after its own
       retries - walled, OR not actually logged in yet - move on to the
       next name in config.BFP_PROFILE_NAMES (or the explicit profile_names
       override) and start its own retry cycle there - "if this doesn't
       work, do this; if that doesn't work, do this", per the user's own
       framing. Only reports overall failure once every account in the
       chain has been tried.

    "Not logged in" is deliberately treated the same as a captcha wall here
    (both mean THIS account can't act right now, not that the request
    itself is wrong) - confirmed live (2026-10-07) that without this, a
    chain of [v4, account2, account3] with account2 mid-login would return
    account2's "not logged in" immediately and never even try account3,
    defeating the whole point of having a chain. A target-level failure
    (comment not found, reply button missing) is NOT retried across
    accounts - a different account would hit the exact same wall, since
    that failure has nothing to do with which account is driving.
    """
    return _run_chain(
        lambda account: _post_reply_once(video_url, cid, reply_text, profile_name=account, headless=headless,
                                         hitl_timeout_s=hitl_timeout_s),
        profile_names or config.account_chain(), max_captcha_retries=max_captcha_retries)


def _remember_session(account: str, state: str | None) -> None:
    """An attempt just showed whether this account is logged in: keep it (the desktop app offers 'log in again' for an
    expired one). Never allowed to disturb a post."""
    if state is None:
        return
    try:
        from app import accounts
        accounts.record_session(account, state)
    except Exception:  # noqa: BLE001
        pass


def _run_chain(call, accounts: list, *, max_captcha_retries: int = 2, pace: bool = True, stage=None,
               max_accounts: int | None = None) -> dict:
    """The two fallback layers of post_reply(), shared with post_link_comment() and the desktop app's reader: `call(account)
    -> result` is one real attempt through one account. A captcha wall is retried with a fresh session (up to
    `max_captcha_retries`); a logged-out account, or one whose pages TikTok will not open at all (`blocked`), hands over to the
    next account AT ONCE; a failure about the TARGET is returned immediately (another account would hit the same wall). An
    account that met a wall goes to the back of the line for a while (order_by_health), so the next request does not start
    with it. `max_accounts` bounds how many different accounts are tried.
    `pace=False` (a dry run, a read) neither waits for nor updates the shared posting clock; a real send waits for it once
    before the first try - a try that only met a wall sent nothing and is not counted against it.
    `stage(text)` tells the screen which account is being tried."""
    result: dict = {}
    attempts: list = []          # why each try ended - an operator must be able to see WHY a reply needed a second go
    accounts = order_by_health(list(accounts))
    if max_accounts:
        accounts = accounts[:max_accounts]
    first_try = True
    for number, account in enumerate(accounts, 1):
        delay_s = 10.0
        for attempt in range(max_captcha_retries + 1):
            _say(stage, f"Trying {_label(account)}" + (f" (account {number} of {len(accounts)})" if len(accounts) > 1 else "") + "...")
            if pace and first_try:
                _pace_posts()
            first_try = False
            try:
                with _use_account(account):
                    result = call(account)
            except Exception as exc:  # noqa: BLE001
                # Confirmed live (2026-10-07): a captcha wall can appear at a
                # point _post_reply_once doesn't explicitly check for one
                # (e.g. right after the reply-button click, before the input
                # box attaches) and surface as an ordinary Playwright
                # TimeoutError instead of a clean {"captcha": True} result.
                # Treated the same as an explicit captcha result: retried,
                # then falls through to the next account.
                LOG.warning("attempt via %s raised %s: %s", account, type(exc).__name__, str(exc)[:200])
                result = {"ok": False, "captcha": True, "profile": account,
                          "error": f"unexpected error (treated as a possible wall): {exc}"}
            wall = bool(result.get("captcha") or result.get("blocked"))
            if pace and not wall:
                _mark_post_attempt()
            not_logged_in = "not logged in" in (result.get("error") or "")
            # a read that worked proves nothing about the login (public comments need none): only an explicit
            # `logged_in` from the attempt (the session cookie, checked on the page) counts as evidence either way
            _remember_session(account, "expired" if (not_logged_in or result.get("logged_in") is False)
                              else "ok" if (result.get("ok") and result.get("logged_in", True)) else None)
            if wall:
                mark_walled(account)
            attempts.append({"account": account, "attempt": attempt + 1, "ok": bool(result.get("ok")),
                             "captcha": bool(result.get("captcha")), "blocked": bool(result.get("blocked")),
                             "not_logged_in": not_logged_in, "error": str(result.get("error") or "")[:200]})
            if not result.get("ok"):
                LOG.warning("attempt %d via %s did not succeed: %s", attempt + 1, account,
                            attempts[-1]["error"] or ("captcha" if result.get("captcha") else "no reason given"))
            if not (wall or not_logged_in):
                result["attempts"] = attempts
                return result
            if not_logged_in or result.get("blocked"):
                break  # the same account would meet the same refusal / is not logged in: the next account now
            if attempt < max_captcha_retries:
                time.sleep(delay_s)
                delay_s = min(delay_s * 1.5, 60.0)
        # this account is still unusable after its own retries - fall through to the next one
    result["attempts"] = attempts
    if result.get("blocked") and not result.get("ok"):
        tried = len({a["account"] for a in attempts})
        result["detail"] = result.get("error")
        result["error"] = (f"TikTok would not open the page for {'this account' if tried == 1 else f'{tried} accounts'} just now - it "
                           "limits how often an account may load a profile page. Wait a few minutes, or choose another account.")
    return result


# ---------------------------------------------------------------------------------------------------------------
# /comment <link>: a comment the operator asked for on ANY post (a new comment, or a reply to a linked comment)
# ---------------------------------------------------------------------------------------------------------------
def _comment_box_visible(page) -> bool:
    return bool(page.evaluate("""() => [...document.querySelectorAll('[data-e2e="comment-input"]')].some(e => {
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
            && cs.visibility !== 'hidden' && cs.display !== 'none'; })"""))


def _ensure_comment_box(s) -> bool:
    """Make the post's comment box visible without ever toggling the panel shut. (_ensure_panel_open judges by comment
    ROWS, which a post without comments does not have, and its icon is a toggle - clicking it twice would close a panel
    that was open all along.) Wait for the box first - it renders a moment after the page - and click the comment icon
    at most once, only when it really did not appear."""
    def wait(seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if _comment_box_visible(s.page):
                return True
            time.sleep(0.5)
        return False

    if wait(6):
        return True
    try:
        s.click('[data-e2e="comment-icon"]', by="css")
    except Exception:  # noqa: BLE001
        return False
    return wait(5)


def _type_into_box(s, text: str, what: str = "the comment box") -> str | None:
    """Click into the first comment box, PROVE keyboard focus is inside it, type `text` like a person and check it arrived.
    -> None when the text is in the box, else the reason nothing was typed. Never types without verified focus: keystrokes
    that reach the page itself trigger TikTok's keyboard shortcuts (see _checked_click)."""
    boxes = s.page.locator('[data-e2e="comment-input"]')
    if boxes.count() == 0:
        return "the comment box was not found (comments may be turned off on this post)"
    refusal = _checked_click(s, boxes.first, what)
    time.sleep(0.3)
    if not refusal and not _input_focused(s.page):
        refusal = f"{what} did not take keyboard focus"
    if refusal:
        return f"refused to type: {refusal}"
    s.page.keyboard.press("Control+A")
    s.page.keyboard.press("Delete")
    time.sleep(0.2)
    if not _input_focused(s.page):
        return "refused to type: focus left the comment box"
    s.behavior.type_text(s.page, text)
    time.sleep(0.5)
    if " ".join(text.split())[:12].lower() not in " ".join(_focused_text(s.page).split()).lower():
        return "refused to post: the typed text is not in the comment box"
    return None


def _publish_outcome(pub: dict | None) -> dict:
    """What TikTok itself said about a comment we sent. The publish response's own body is the authority (HTTP 200 alone
    is not proof: a soft rejection is a 200 with an empty body)."""
    body = pub.get("body") if pub else None
    confirmed = bool(isinstance(body, dict) and body.get("status_code") == 0
                     and isinstance(body.get("comment"), dict) and body["comment"].get("cid"))
    cmt = body.get("comment") if isinstance(body, dict) else None
    status = cmt.get("status") if isinstance(cmt, dict) else None
    return {"http_ok": bool(pub and pub.get("status") == 200), "server_confirmed": confirmed,
            "comment_status": status, "visibility": comment_visibility(status)}


def _post_comment_once(video_url: str, text: str, *, profile_name: str, headless: bool | None = None,
                       hitl_timeout_s: float = 120.0, dry_run: bool = False, stage=None) -> dict:
    """One real attempt through ONE account: a NEW top-level comment under a post (any post, not only ours), typed and sent
    through the account's own browser UI like a person would - never a raw API call (see _post_reply_once). Same result
    shape as _post_reply_once. dry_run=True stops before the click on Post: the text is typed and checked, a screenshot is
    taken, the box is emptied again and nothing is sent."""
    video_id = extract_video_id(video_url)
    trace = _Trace("comment", profile_name, video_id, stage)
    captured: dict = {"list_payload": None, "publish_response": None, "seen": {}}
    on_response = _make_response_listener(video_id, captured)
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault, profile_name)
    headless = config.BFP_HEADLESS if headless is None else headless

    with _browser(profile, vault, profile_name, kind="post-comment", video_id=video_id, headless=headless, hitl="wait",
                  hitl_timeout_s=hitl_timeout_s) as s:
        s.listen(on_response)
        s.recovery.update(FAST_RECOVERY)
        trace.mark("browser reused" if s.reused else "browser started", f"Opening the post with {_label(profile_name)}...")
        owner = _author_profile_url(video_url)
        goto, _via_tile = _open_post(s, video_id, video_url, via_profile=bool(owner), profile_url=owner, trace=trace)
        if not goto.get("ok"):
            s.finish()
            return {"ok": False, "blocked": bool(goto.get("blocked")), "profile": profile_name,
                    "error": f"could not open {video_url}: {goto.get('error')}"}
        if _captcha_present(s.page):
            s.finish()
            return {"ok": False, "captcha": True, "profile": profile_name,
                    "error": "TikTok's slider-puzzle wall appeared on page load"}
        if not is_logged_in(s.page):
            s.finish()
            return {"ok": False, "profile": profile_name,
                    "error": "not logged in -- run 'python run.py login' or 'import-cookies'"}
        trace.mark("post opened", "Opening the comment box...")
        box_ready = _ensure_comment_box(s)
        if _captcha_present(s.page):
            s.finish()
            return {"ok": False, "captcha": True, "profile": profile_name,
                    "error": "TikTok's slider-puzzle wall appeared after opening the comment panel"}
        if not box_ready:
            s.finish()
            return {"ok": False, "profile": profile_name,
                    "error": "the comment box did not appear (comments may be turned off on this post)"}
        time.sleep(0.4)
        trace.mark("comment box ready", "Typing the comment...")
        refusal = _type_into_box(s, text)
        if refusal:
            s.finish()
            return {"ok": False, "profile": profile_name, "error": refusal}

        before_shot = config.SCREENSHOTS_DIR / f"before_comment_{video_id}.png"
        try:
            s.page.screenshot(path=str(before_shot))
        except Exception:  # noqa: BLE001
            before_shot = None
        before = str(before_shot) if before_shot else None
        post_btns = s.page.locator('[data-e2e="comment-post"]')
        if post_btns.count() == 0:
            s.finish()
            return {"ok": False, "profile": profile_name, "error": "post button not found", "before_screenshot": before}
        if dry_run:
            visible = post_btns.first.is_visible()
            s.page.keyboard.press("Control+A")
            s.page.keyboard.press("Delete")
            s.keep = bool(visible)
            s.finish()
            return {"ok": visible, "dry_run": True, "profile": profile_name, "publish_response": None,
                    "error": None if visible else "the Post button is not visible", "before_screenshot": before}
        trace.mark("comment typed", "Sending the comment...")
        refusal = _checked_click(s, post_btns.first, "the Post button")
        if refusal:
            s.finish()
            return {"ok": False, "profile": profile_name, "error": f"refused to post: {refusal}",
                    "before_screenshot": before}

        deadline = time.monotonic() + 20  # proxy-aware: residential proxies add real latency
        while captured["publish_response"] is None and time.monotonic() < deadline:
            time.sleep(0.3)
        time.sleep(1.5)
        after_shot = config.SCREENSHOTS_DIR / f"after_comment_{video_id}.png"
        try:
            s.page.screenshot(path=str(after_shot))
        except Exception:  # noqa: BLE001
            after_shot = None
        pub = captured["publish_response"]
        proof_shot = None
        if pub and pub.get("status") == 200:
            proof_shot = _proof_screenshot(s, text, config.SCREENSHOTS_DIR / f"proof_comment_{video_id}.png")
        s.keep = _publish_outcome(pub)["server_confirmed"]
        s.finish()

    if not pub:
        return {"ok": False, "http_ok": False, "server_confirmed": False, "profile": profile_name, "publish_response": None,
                "error": "TikTok never answered the publish request (nothing was sent)", "before_screenshot": before}
    out = _publish_outcome(pub)
    body = pub.get("body") if isinstance(pub.get("body"), dict) else {}
    ok = out["http_ok"] and out["server_confirmed"]
    return {"ok": ok, **out, "actually_posted": ok, "profile": profile_name, "publish_response": pub,
            "error": None if ok else (str(body.get("status_msg") or "")[:200]
                                      or "TikTok answered but did not confirm that the comment was published"),
            "before_screenshot": before, "after_screenshot": str(after_shot) if after_shot else None,
            "proof_screenshot": proof_shot, "timings": trace.timings()}


LINK_ACCOUNTS_TRIED = 3                  # on Auto, at most this many different accounts are tried for one comment


def post_link_comment(video_url: str, text: str, *, cid: str | None = None, account: str | None = None,
                      dry_run: bool = False, headless: bool | None = None, hitl_timeout_s: float = 120.0,
                      max_captcha_retries: int | None = None, stage=None, comment_text: str | None = None) -> dict:
    """Public entry point of /comment <link>. `cid` None: a new comment under the post; a comment id: a reply to that
    comment. `account`: post through exactly that account (no fallback to the others - the operator chose it); None:
    the chain (the accounts that are not walled first), at most LINK_ACCOUNTS_TRIED of them. `comment_text` (a reply): what
    the comment says - lets a kept browser find it on the page even when TikTok sends no comment list. dry_run=True: do
    everything except the final click on Post. `max_captcha_retries` None: one fresh-session retry for a chosen account (it has no
    fallback), none on Auto (the next account is the better retry). `stage(text)` reports what the browser is doing."""
    chain = config.account_chain()
    if account and account not in chain:
        return {"ok": False, "error": f"the account '{account}' is not available any more - choose another one",
                "attempts": []}

    def call(acct: str) -> dict:
        if cid:
            return _post_reply_once(video_url, cid, text, profile_name=acct, headless=headless,
                                    hitl_timeout_s=hitl_timeout_s, foreign=True, dry_run=dry_run, stage=stage,
                                    comment_text=comment_text)
        return _post_comment_once(video_url, text, profile_name=acct, headless=headless,
                                  hitl_timeout_s=hitl_timeout_s, dry_run=dry_run, stage=stage)

    retries = (1 if account else 0) if max_captcha_retries is None else max_captcha_retries
    return _run_chain(call, [account] if account else chain, max_captcha_retries=retries, pace=not dry_run, stage=stage,
                      max_accounts=None if account else LINK_ACCOUNTS_TRIED)

# ---------------------------------------------------------------------------------------------------------------
# The desktop app's "analyse a post": read ALL the comments of any post from its link
# ---------------------------------------------------------------------------------------------------------------
_POST_INFO_JS = """(() => { const t = sel => { const e = document.querySelector(sel); return e ? (e.innerText || '').trim() : ''; };
  return JSON.stringify({desc: t('[data-e2e="browse-video-desc"]') || t('[data-e2e="video-desc"]'),
                         author: t('[data-e2e="browse-username"]') || t('[data-e2e="video-author-uniqueid"]')}); })()"""


def _read_comments_once(video_url: str, *, profile_name: str, max_comments: int, progress=None, should_stop=None,
                        headless: bool | None = None, stage=None, until_cid: str | None = None) -> dict:
    """One attempt through ONE account: open the post by its URL and page through its comments the way a person does (scroll
    the comment list), taking the text from TikTok's own /api/comment/list/ answers. Reading needs no login, but the page's
    session cookie is reported (`logged_in`) so a logged-out account is noticed."""
    from app import tiktok_watch as tw
    video_id = extract_video_id(video_url)
    trace = _Trace("read", profile_name, video_id, stage)
    captured: dict = {"list_payload": None, "publish_response": None, "seen": {}}
    on_response = _make_response_listener(video_id, captured)
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = _get_or_create_profile(vault, profile_name)
    headless = config.BFP_HEADLESS if headless is None else headless
    say = progress or (lambda *a, **k: None)

    with _browser(profile, vault, profile_name, kind="read", video_id=video_id, headless=headless, hitl="off") as s:
        s.listen(on_response)
        s.recovery.update(FAST_RECOVERY)
        trace.mark("browser reused" if s.reused else "browser started", f"Opening the post with {_label(profile_name)}...")
        owner = _author_profile_url(video_url)               # through the owner's profile: a direct load of a post is what TikTok blocks
        goto, _via_tile = _open_post(s, video_id, video_url, via_profile=bool(owner), profile_url=owner, trace=trace)
        if not goto.get("ok"):
            s.finish()
            return {"ok": False, "blocked": bool(goto.get("blocked")), "profile": profile_name,
                    "error": f"could not open the post ({goto.get('error') or 'TikTok did not answer'})"}
        if _captcha_present(s.page):
            s.finish()
            return {"ok": False, "captcha": True, "profile": profile_name, "error": "TikTok showed a security check (captcha)"}
        logged_in = is_logged_in(s.page)
        trace.mark("post opened", "Opening the comments...")
        _ensure_panel_open(s)
        time.sleep(0.4)
        _ensure_comments_tab(s.page)
        if not tw._wait_for(lambda: captured["list_payload"] is not None, 14, page=s.page):
            s.finish()
            return {"ok": False, "profile": profile_name, "logged_in": logged_in,
                    "error": "TikTok did not send the comments (the post may have comments turned off, or the page did not load)"}
        first = captured["list_payload"] or {}
        total = int(first.get("total") or 0) or None
        trace.mark("first comments arrived")
        say(len(captured["seen"]), total)
        stalls = 0
        for _ in range(max(6, (max_comments // 20 + 2) * 3)):          # attempts, not pages: a try that loads nothing is no page
            if should_stop and should_stop():
                s.finish()
                return {"ok": False, "cancelled": True, "profile": profile_name, "error": "stopped"}
            loaded = len(captured["seen"])
            last = captured["list_payload"] or {}
            if until_cid and until_cid in captured["seen"]:                     # the one comment that was wanted is here
                break
            if loaded >= max_comments or not last.get("has_more") or (total and loaded >= total):
                break
            tw._load_more_comments(s.page, stalls)
            if tw._wait_for(lambda: len(captured["seen"]) > loaded, 8, page=s.page):
                stalls = 0
                say(len(captured["seen"]), total)
            else:
                stalls += 1
                if stalls >= 3:                                         # three different scroll styles, nothing more: done
                    break
        info = {}
        try:
            info = json.loads(s.page.evaluate(_POST_INFO_JS) or "{}")
        except Exception:  # noqa: BLE001
            pass
        more_left = bool((captured["list_payload"] or {}).get("has_more"))     # TikTok's `total` counts replies too: has_more is the truth
        trace.mark(f"{len(captured['seen'])} comments read")
        s.keep = True
        s.finish()
    comments = tw.normalize_comments([{"comments": list(captured["seen"].values())}])
    return {"ok": True, "profile": profile_name, "logged_in": logged_in, "comments": comments[:max_comments],
            "total": total, "truncated": len(comments) > max_comments or more_left,
            "description": info.get("desc") or "", "author": (info.get("author") or "").lstrip("@"),
            "timings": trace.timings()}


READ_ACCOUNTS_TRIED = 3                  # reading tries at most this many different accounts


def reading_order() -> list:
    """The accounts in the order READING should use them: exactly the order a reply on Auto uses (the operator's order, accounts
    that just met a wall or loaded many profile pages lately last). The browser that reads a post is kept for the reply, so it
    must be the account that will reply: reading with a helper and replying with the page account would cost TWO profile
    page loads (one per account) and a second browser start-up instead of one load and a reused browser."""
    return order_by_health(config.account_chain())


def read_comments_of_post(video_url: str, *, account: str | None = None, max_comments: int = 240, progress=None,
                          should_stop=None, headless: bool | None = None, max_captcha_retries: int = 0, stage=None,
                          until_cid: str | None = None) -> dict:
    """Public entry of the desktop app's analysis: every comment (up to `max_comments`) of the post behind `video_url`.
    `account`: read through exactly that account; None: up to READ_ACCOUNTS_TRIED accounts in reading_order() (a walled one
    hands over to the next at once). `stage(text)` reports what the browser is doing; `progress(read, total)` the count.
    -> {ok, comments[], total, truncated, description, author, profile, logged_in, timings} or {ok: False, error, captcha?, blocked?}"""
    chain = config.account_chain()
    if account and account not in chain:
        return {"ok": False, "error": f"the account '{account}' is not available any more - choose another one"}

    def call(acct: str) -> dict:
        return _read_comments_once(video_url, profile_name=acct, max_comments=max_comments, progress=progress,
                                   should_stop=should_stop, headless=headless, stage=stage, until_cid=until_cid)

    return _run_chain(call, [account] if account else reading_order(), max_captcha_retries=max_captcha_retries, pace=False,
                      stage=stage, max_accounts=None if account else READ_ACCOUNTS_TRIED)
