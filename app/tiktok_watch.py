"""Reads the target TikTok profile the way a person does, in as few page loads as possible.

  1. ONE load of the profile page. Its own /api/post/item_list/ response lists the posts with
     their comment counts and pinned flags - so deciding *which posts have news* costs no video loads.
  2. A post is opened only when its comment count changed since the last read (or it has not been
     read for RECHECK_MAX_HOURS). It is opened by clicking its tile (client-side navigation), NOT by
     loading its /video/<id> URL: confirmed live that TikTok answers repeated loads of one video
     document with an empty HTTP 403 while the profile page and the click-through keep working.
  3. Comment pages are paginated by scrolling the panel (cursor / has_more / total).

Which posts are watched: the first WATCH_PINNED_POSTS pinned posts + the newest WATCH_LATEST_POSTS
non-pinned posts (both adjustable at runtime with /watch).
"""
import calendar
import random
import re
import time

import config
from app import analysis
from app.db import DB

_AWEME = re.compile(r"[?&]aweme_id=(\d+)")
MAX_COMMENT_PAGES = config.MAX_COMMENT_PAGES


class ScanError(Exception):
    """The profile could not be read (wall, 403, not loaded)."""


# ----------------------------------------------------------------------------- pure helpers
def parse_item_list(blobs: list, handle: str) -> list:
    """Normalise TikTok's item_list payloads -> posts, in the order TikTok returned them."""
    seen, out = set(), []
    for blob in blobs:
        for it in (blob or {}).get("itemList") or []:
            vid = str(it.get("id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            stats = it.get("stats") or {}
            author = (it.get("author") or {}).get("uniqueId") if isinstance(it.get("author"), dict) else None
            user = author or handle
            out.append({
                "video_id": vid, "url": f"https://www.tiktok.com/@{user}/video/{vid}",
                "description": (it.get("desc") or "").strip(), "created_ts": int(it.get("createTime") or 0),
                "is_pinned": bool(it.get("isPinnedItem")),
                "comment_count": int(stats.get("commentCount") or 0),
                "like_count": int(stats.get("diggCount") or 0), "play_count": int(stats.get("playCount") or 0)})
    return out


def select_watch(posts: list, latest_n: int, pinned_n: int) -> list:
    """Pinned (profile order) first, then the newest non-pinned posts."""
    pinned = [p for p in posts if p["is_pinned"]][:max(0, pinned_n)]
    latest = sorted((p for p in posts if not p["is_pinned"]), key=lambda p: p["created_ts"], reverse=True)
    return pinned + latest[:max(0, latest_n)]


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def due_posts(posts: list, *, now: float, recheck_hours: float, cap: int) -> list:
    """Posts worth opening now: never read > grew > shrank > stale. Capped per scan."""
    scored = []
    for p in posts:
        cc, checked, last = p["comment_count"], p.get("checked_comment_count"), _ts(p.get("last_checked_at"))
        if p.get("last_error") and last and now - last < 1800:
            continue                                  # failed a moment ago: do not hammer it every cycle
        if checked is None:
            prio = (0, 0)
        elif cc > checked:
            prio = (1, -(cc - checked))
        elif cc < checked:
            prio = (2, 0)
        elif last is None or now - last > recheck_hours * 3600:
            prio = (3, last or 0)
        else:
            continue
        scored.append((prio, p["watch_rank"] or 99, p))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in scored[:cap]]


# ----------------------------------------------------------------------------- captcha cooldown
def is_captcha_error(exc: BaseException | str) -> bool:
    return "captcha" in str(exc).lower()


def captcha_cooldown_left(db: DB, now: float | None = None) -> float:
    """Seconds of 'do not open posts' still to go after TikTok showed its security check (0 = free to go)."""
    until = float(db.meta_get("captcha_until", 0) or 0)
    return max(0.0, until - (time.time() if now is None else now))


def start_captcha_cooldown(db: DB, now: float | None = None) -> dict:
    """TikTok walled us: stop opening posts for a while, longer each time it happens in a row
    (5, 10, 20, 40, then 60 min). More attempts while walled only deepen the wall. Reading the profile page
    (one cheap request) carries on, and the first post that opens cleanly ends the episode."""
    streak = int(db.meta_get("captcha_streak", 0) or 0) + 1
    minutes = min(config.CAPTCHA_COOLDOWN_MAX_MINUTES, config.CAPTCHA_COOLDOWN_MIN_MINUTES * 2 ** (streak - 1))
    db.meta_set("captcha_streak", streak)
    db.meta_set("captcha_until", (time.time() if now is None else now) + minutes * 60)
    db.log_event("captcha_cooldown", f"streak={streak} pause={minutes}min", "warn")
    return {"streak": streak, "minutes": minutes}


def clear_captcha_cooldown(db: DB) -> None:
    if int(db.meta_get("captcha_streak", 0) or 0):
        db.log_event("captcha_cleared", "a post opened cleanly again")
    db.meta_set("captcha_streak", 0)
    db.meta_set("captcha_until", 0)


def normalize_comments(pages: list) -> list:
    """comment-list payloads -> [{cid,text,username,digg_count,create_time,reply_count}], deduplicated."""
    out, seen = [], set()
    for payload in pages:
        for c in (payload or {}).get("comments") or []:
            cid = str(c.get("cid") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            user = c.get("user") or {}
            out.append({"cid": cid, "text": c.get("text") or "",
                        "username": user.get("unique_id") or user.get("nickname") or "?",
                        "digg_count": c.get("digg_count", 0), "create_time": c.get("create_time"),
                        "reply_count": c.get("reply_comment_total", 0), "screenshot": None,
                        # TikTok sends the first reply or two with the comment: enough to recognise our own
                        "reply_preview": [{"user": (r.get("user") or {}).get("unique_id"), "text": r.get("text") or ""}
                                          for r in (c.get("reply_comment") or [])[:3] if isinstance(r, dict)]})
    return out


class Capture:
    """Collects the page's own API responses while we browse."""
    def __init__(self) -> None:
        self.item_lists: list = []
        self.comment_pages: dict = {}

    def on_response(self, response) -> None:
        url = response.url
        try:
            if "/api/post/item_list/" in url:
                self.item_lists.append(response.json())
            elif "/api/comment/list/" in url and "/reply/" not in url:
                m = _AWEME.search(url)
                if m:
                    self.comment_pages.setdefault(m.group(1), []).append(response.json())
        except Exception:  # noqa: BLE001 - a body that cannot be read is simply not captured
            pass

    def pages(self, vid: str) -> list:
        return self.comment_pages.get(vid, [])


# ----------------------------------------------------------------------------- browser side
_SCROLL_PANEL_JS = """() => {
  const el = document.querySelector('[data-e2e="comment-level-1"]');
  if (!el) return 'no-list';
  let p = el.parentElement;
  while (p && p !== document.body) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight + 5) { p.scrollTop = p.scrollHeight; return 'scrolled'; }
    p = p.parentElement;
  }
  window.scrollTo(0, document.body.scrollHeight); return 'window';
}"""


_PANEL_BOX_JS = """() => {
  const el = document.querySelector('[data-e2e="comment-level-1"]');
  if (!el) return null;
  let p = el.parentElement;
  while (p && p !== document.body) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight + 5) {
      const r = p.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + Math.min(r.height, innerHeight - r.y) / 2];
    }
    p = p.parentElement;
  }
  return null;
}"""


def _load_more_comments(page, stalls: int) -> None:
    """Ask TikTok for the next comment page the way a person does: scroll the comment list. The list is virtualised
    inside a short scroll box, so the first try jumps to its end; when that brings nothing, real mouse-wheel turns
    over the box (a few more each time) do - measured live on a 152-comment post, where one jump alone stopped
    after the first 20 comments."""
    if stalls == 0:
        page.evaluate(_SCROLL_PANEL_JS)
        return
    box = page.evaluate(_PANEL_BOX_JS)
    if not box:
        page.evaluate(_SCROLL_PANEL_JS)
        return
    page.mouse.move(box[0], box[1])
    for _ in range(3 + stalls):
        page.mouse.wheel(0, 650)
        time.sleep(0.35)


def _wait_for(predicate, timeout: float, step: float = 0.4, page=None) -> bool:
    """Poll `predicate` until it is true or `timeout` passes. Pass `page` whenever the predicate looks at something the
    page's network events fill in (captured API responses, the URL): the browser connection only delivers those events
    while a browser call is running, so a plain time.sleep() would leave an already-arrived response unseen until the
    wait had timed out - the bot then gave up on pages that were sitting right there (worst on a slow server)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        if page is not None:
            try:
                page.wait_for_timeout(int(step * 1000))
                continue
            except Exception:  # noqa: BLE001
                pass
        time.sleep(step)
    return predicate()


def _grid_visible(page) -> bool:
    return bool(page.evaluate("() => !!document.querySelector('[data-e2e=\"user-post-item-list\"]')"))


def _overlay_open(page) -> bool:
    return "/video/" in page.url


def _close_overlay(s, profile_url: str) -> None:
    """Back from an opened post to the profile grid. The grid stays in the DOM underneath the overlay,
    so 'grid visible' proves nothing; the URL (/video/<id> while a post is open) is the real signal.
    Escape closes it - twice when the comment panel was used (confirmed live); history.back and finally
    the profile document are fallbacks."""
    page = s.page
    if not _overlay_open(page):
        return
    for _ in range(3):                           # 1st Escape only closes the comment panel / input focus
        page.keyboard.press("Escape")
        if _wait_for(lambda: not _overlay_open(page), 1.8, 0.2, page):
            return
    try:
        page.go_back(wait_until="commit", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    if _wait_for(lambda: not _overlay_open(page), 4, page=page):
        return
    s.goto(profile_url)                          # last resort: the profile document (never throttled so far)
    time.sleep(3)


_CLICK_TARGET_JS = """(sel) => { const a = document.querySelector(sel); if (!a) return {tile: 'missing'};
    const r = a.getBoundingClientRect(); const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return {box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
            vp: [innerWidth, innerHeight], scrollY: Math.round(scrollY), tileIsHit: !!hit && a.contains(hit),
            hit: hit ? hit.tagName + '|' + (hit.getAttribute('data-e2e') || '') + '|' + String(hit.className).slice(0, 40) : null,
            dialogs: document.querySelectorAll('[role=dialog]').length, url: location.pathname.slice(-24)}; }"""


def _open_tile(s, tile: str, vid: str, profile_url: str | None = None) -> None:
    """Click a grid tile so the post opens as an overlay (URL becomes /video/<id>, no document load).
    Tiles below the fold must be scrolled into view first - a click on off-screen coordinates is lost.

    Self-healing: when three clicks do not open it (seen intermittently: the grid left in an odd state after the
    previous post), take a diagnostic snapshot, reload the profile page once and try again before giving up."""
    page = s.page
    profile_url = profile_url or config.TARGET_PROFILE_URL
    info = None
    for round_ in range(2):
        loc = page.locator(tile).first
        for attempt in range(3):
            try:
                loc.scroll_into_view_if_needed(timeout=6000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.7)
            try:
                if attempt == 0 and round_ == 0:
                    s.click(tile, by="css")           # humanised first
                else:
                    loc.click(timeout=5000)           # then a plain trusted click
            except Exception:  # noqa: BLE001
                pass
            if _wait_for(lambda: f"/video/{vid}" in page.url, 6, page=page):
                return
        if round_ == 0:
            try:
                info = page.evaluate(_CLICK_TARGET_JS, tile)
                page.screenshot(path=str(config.SCREENSHOTS_DIR / f"scanfail_{vid}.png"))
            except Exception:  # noqa: BLE001
                pass
            s.goto(profile_url)                        # recovery: a clean grid
            time.sleep(4)
            if not _find_tile(s, tile, profile_url):
                break
    raise ScanError(f"the post {vid} did not open after clicking its tile (state before reload: {info})")


def _find_tile(s, tile: str, profile_url: str) -> bool:
    """Make the post's tile present in the DOM. The grid is virtualised and keeps its scroll position
    after an overlay closes, so start from the top and scroll down; reload the profile as the fallback."""
    page = s.page
    for reload_first in (False, True):
        if reload_first:
            s.goto(profile_url)
            time.sleep(4)
        page.evaluate("() => window.scrollTo(0, 0)")
        time.sleep(0.8)
        for _ in range(10):
            if page.locator(tile).count():
                return True
            page.mouse.wheel(0, 900)
            time.sleep(0.9)
    return False


def read_post_comments(s, post: dict, capture: Capture, profile_url: str, tm) -> list:
    """Open one post from the profile grid by clicking its tile; return all comments we could page through."""
    page = s.page
    vid = post["video_id"]
    # NOT cleared first: opening a post makes TikTok prefetch its neighbour's first comment page, and
    # the Capture object lives for this scan only, so whatever it holds for `vid` is fresh enough
    tile = f'a[href*="/video/{vid}"]'
    if not _find_tile(s, tile, profile_url):
        raise ScanError(f"tile for {vid} not found on the profile grid")
    _open_tile(s, tile, vid, profile_url)
    if not _wait_for(lambda: bool(capture.pages(vid)), 6, page=page):
        # panel closed in this layout: ask for it the way a person does
        try:
            s.click('[data-e2e="comment-icon"]', by="css")
        except Exception:  # noqa: BLE001
            pass
        _wait_for(lambda: bool(capture.pages(vid)), 10, page=page)
    if tm._captcha_present(page):
        raise ScanError("captcha wall appeared while opening a post")
    if not capture.pages(vid):
        raise ScanError(f"no comment data arrived for post {vid} (it reports {post['comment_count']} comments)")
    # pagination: scroll the comment panel until the API says there is nothing more
    if not page.evaluate("() => !!document.querySelector('[data-e2e=\"comment-level-1\"]')"):
        try:
            s.click('[data-e2e="comment-icon"]', by="css")
            time.sleep(1.0)
            tm._ensure_comments_tab(page)
        except Exception:  # noqa: BLE001
            pass
    stalls = 0
    for _ in range(MAX_COMMENT_PAGES * 3):              # attempts, not pages: a try that loads nothing is not a page
        pages = capture.pages(vid)
        last = pages[-1] or {}
        loaded = sum(len((p or {}).get("comments") or []) for p in pages)
        if len(pages) >= MAX_COMMENT_PAGES or not last.get("has_more") or loaded >= int(last.get("total") or 0):
            break
        n = len(pages)
        _load_more_comments(page, stalls)
        if _wait_for(lambda: len(capture.pages(vid)) > n, 8, page=page):
            stalls = 0
        else:
            stalls += 1
            if stalls >= 3:                              # three different scroll styles, nothing more arrives: done
                break
    comments = normalize_comments(capture.pages(vid))
    _close_overlay(s, profile_url)
    return comments


def scan(db: DB, *, headless: bool | None = None, accounts: list | None = None, progress=print,
         force: bool = False) -> dict:
    """One monitoring pass. Raises ScanError when no account could read the profile."""
    import sys
    from pathlib import Path
    from app.bfp_loader import ensure_bfp_on_path
    ensure_bfp_on_path()
    import bfp
    from app import tiktok_monitor as tm

    headless = config.BFP_HEADLESS if headless is None else headless
    handle = config.TARGET_PROFILE_URL.rstrip("/").split("@")[-1]
    latest_n = int(db.get_setting("watch_latest", config.WATCH_LATEST_POSTS))
    pinned_n = int(db.get_setting("watch_pinned", config.WATCH_PINNED_POSTS))
    errors = []
    for account in (accounts or config.account_chain()):
        try:
            with tm._use_account(account):
                return _scan_with(db, bfp, tm, account, handle, headless, latest_n, pinned_n, progress, force)
        except ScanError as exc:
            errors.append(f"{account}: {exc}")
            progress(f"scan via {account} failed: {exc}")
    raise ScanError("; ".join(errors) or "no account available")


def _scan_with(db, bfp, tm, account, handle, headless, latest_n, pinned_n, progress, force) -> dict:
    vault = bfp.ProfileVault(root=str(config.BFP_VAULT_DIR))
    profile = tm._get_or_create_profile(vault, account)
    capture = Capture()
    result = {"account": account, "posts_seen": 0, "watched": 0, "opened": 0, "comments_seen": 0, "new": 0,
              "free": 0, "pending_ai": 0, "errors": []}
    with bfp.Session(profile=profile, vault=vault, proxy=(config.BFP_PROXY or None), name=account,
                     backend=config.BFP_BACKEND, out_dir=str(config.BFP_RUNS_DIR / f"scan-{account}"),
                     headless=headless, hitl="off", block_resources=config.BROWSER_BLOCK_RESOURCES) as s:
        s.page.on("response", capture.on_response)
        goto = s.goto(config.TARGET_PROFILE_URL)
        if not goto.get("ok"):
            raise ScanError(f"could not open the profile: {str(goto.get('error'))[:120]}")
        if tm._captcha_present(s.page):
            raise ScanError("captcha wall on the profile page")
        if not _wait_for(lambda: bool(capture.item_lists), 15, page=s.page):
            s.page.reload(wait_until="domcontentloaded", timeout=30000)
            if not _wait_for(lambda: bool(capture.item_lists), 15, page=s.page):
                raise ScanError("the profile page did not list any posts")
        posts = parse_item_list(capture.item_lists, handle)
        watch = select_watch(posts, latest_n, pinned_n)
        result["posts_seen"], result["watched"] = len(posts), len(watch)
        for p in watch:
            db.upsert_post(p)
        db.set_watch_set([p["video_id"] for p in watch])
        for row in db.watched_posts():
            if row["comment_count"] == 0 and row.get("checked_comment_count") is None:
                db.mark_post_checked(row["video_id"], 0)          # nothing to read: no visit needed
        due = due_posts(db.watched_posts(), now=time.time(), recheck_hours=config.RECHECK_MAX_HOURS,
                        cap=config.MAX_VIDEO_OPENS_PER_SCAN if not force else 99)
        wait_s = captcha_cooldown_left(db)
        if wait_s > 0 and due and not force:              # TikTok showed a security check recently: let it cool down
            progress(f"profile read: {len(posts)} posts, {len(due)} to open - paused {wait_s / 60:.0f} more min "
                     f"(TikTok security check)")
            result["captcha_wait_s"] = round(wait_s)
            due = []
        else:
            progress(f"profile read: {len(posts)} posts, watching {len(watch)}, {len(due)} to open")
        failures = 0
        for p in due:
            try:
                comments = read_post_comments(s, p, capture, config.TARGET_PROFILE_URL, tm)
                first = p.get("checked_comment_count") is None
                stats = analysis.ingest_comments(db, p["video_id"], comments, first_scan=first)
                db.mark_post_checked(p["video_id"], p["comment_count"])
                clear_captcha_cooldown(db)
                result["opened"] += 1
                result["comments_seen"] += stats["seen"]
                result["new"] += stats["new"]
                result["free"] += stats["cache"] + stats["heuristic"] + stats["legacy"]
                result["pending_ai"] += stats["pending"]
                failures = 0
            except ScanError as exc:
                db.mark_post_checked(p["video_id"], None, error=str(exc))
                result["errors"].append(str(exc))
                if is_captcha_error(exc):
                    result["captcha"] = start_captcha_cooldown(db)
                    break                                  # no further posts this round; nothing is lost, it is retried
                failures += 1
                if failures >= 2:
                    raise
                if not _grid_visible(s.page):
                    s.goto(config.TARGET_PROFILE_URL)
            time.sleep(random.uniform(3.0, 6.5))
        s.finish()
    return result
