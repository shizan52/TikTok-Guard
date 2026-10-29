"""The warm browser: kept open between jobs so the next one needs no page load - and never kept when it should not be."""
import threading
import types
import unittest
from unittest import mock

from _helpers import tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import tiktok_monitor as tm
from app.service import Service

PROFILE = "https://www.tiktok.com/@some.shop"


class FakePage:
    def __init__(self, url=PROFILE):
        self.url, self.closed, self.listeners = url, False, []

    def is_closed(self):
        return self.closed

    def evaluate(self, js, *args):
        if self.closed:
            raise RuntimeError("the browser is gone")
        return 1

    def on(self, event, fn):
        self.listeners.append(fn)

    def remove_listener(self, event, fn):
        self.listeners.remove(fn)


class FakeSession:
    made: list = []

    def __init__(self, **kw):
        self.kw, self.page, self.finished = kw, FakePage(), 0
        FakeSession.made.append(self)

    def start(self):
        return self

    def finish(self, *a, **k):
        self.finished += 1
        self.page.closed = True

    def close(self, *a, **k):
        self.page.closed = True


class WarmCase(unittest.TestCase):
    def setUp(self):
        FakeSession.made = []
        tm.close_warm()
        self._warm = tm.WARM_SECONDS
        tm.WARM_SECONDS = 300
        patch = mock.patch.object(tm.bfp, "Session", FakeSession)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.restore)

    def restore(self):
        tm.close_warm()
        tm.WARM_SECONDS = self._warm

    def op(self, account="acc1", *, keep=True, fail=False, video="111"):
        """One operation the way the engine runs it."""
        with tm._browser(None, None, account, kind="read", video_id=video, headless=True, hitl="off") as s:
            s.listen(lambda r: None)
            s.finish()                                         # every operation ends with this - it must not close a kept browser
            if fail:
                raise RuntimeError("boom")
            s.keep = keep
            return s


class TestKeepingAndReusing(WarmCase):
    def test_an_operation_that_ended_well_leaves_the_browser_open_for_the_next(self):
        first = self.op()
        self.assertFalse(first.reused)
        self.assertEqual(tm.warm_account(), "acc1")
        self.assertEqual(FakeSession.made[0].finished, 0, "kept, not closed")
        second = self.op()
        self.assertTrue(second.reused)
        self.assertEqual(len(FakeSession.made), 1, "the same browser served both")

    def test_the_listeners_of_an_operation_go_away_with_it(self):
        self.op()
        self.assertEqual(FakeSession.made[0].page.listeners, [], "else every job would add another listener to the page")

    def test_with_the_setting_off_every_job_closes_its_browser_as_before(self):
        tm.WARM_SECONDS = 0
        self.op()
        self.assertIsNone(tm.warm_account())
        self.assertEqual(FakeSession.made[0].finished, 1)

    def test_an_operation_that_did_not_end_well_closes_the_browser(self):
        self.op(keep=False)
        self.assertIsNone(tm.warm_account())
        self.assertEqual(FakeSession.made[0].finished, 1)

    def test_an_error_closes_the_browser_and_is_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            self.op(fail=True)
        self.assertIsNone(tm.warm_account())
        self.assertEqual(FakeSession.made[0].finished, 1)

    def test_another_account_closes_the_kept_browser_first(self):
        self.op("acc1")
        other = self.op("acc2")
        self.assertFalse(other.reused)
        self.assertEqual(FakeSession.made[0].finished, 1, "one browser at a time: acc1's was closed")
        self.assertEqual(tm.warm_account(), "acc2")

    def test_a_browser_that_died_in_the_meantime_is_not_reused(self):
        self.op()
        FakeSession.made[0].page.closed = True                 # the person killed it, or it crashed
        again = self.op()
        self.assertFalse(again.reused)
        self.assertEqual(len(FakeSession.made), 2)

    def test_a_kept_browser_is_only_used_by_the_thread_that_made_it(self):
        self.op()
        seen = []
        t = threading.Thread(target=lambda: seen.append(self.op().reused))
        t.start()
        t.join()
        self.assertEqual(seen, [False], "browser objects belong to one thread")


class TestLettingGo(WarmCase):
    def test_idle_browsers_expire_and_busy_ones_do_not(self):
        self.op()
        self.assertFalse(tm.expire_warm(), "just used: kept")
        with mock.patch.object(tm.time, "monotonic", return_value=tm.time.monotonic() + 301):
            self.assertTrue(tm.expire_warm())
        self.assertIsNone(tm.warm_account())
        self.assertEqual(FakeSession.made[0].finished, 1)

    def test_close_warm_can_be_aimed_at_one_account(self):
        self.op("acc1")
        self.assertFalse(tm.close_warm("acc2"))
        self.assertEqual(tm.warm_account(), "acc1")
        self.assertTrue(tm.close_warm("acc1"))
        self.assertIsNone(tm.warm_account())


class TestStandingOnThePage(unittest.TestCase):
    class _S:
        def __init__(self, url):
            self.page, self.urls = FakePage(url), []

        def goto(self, url):
            self.urls.append(url)
            self.page.url = url
            return {"ok": True}

    def test_the_owners_page_and_a_post_opened_over_it_count_as_standing_on_it(self):
        for url in (PROFILE, PROFILE + "/", PROFILE + "/video/123", PROFILE.upper()):
            self.assertTrue(tm._standing_on(self._S(url), PROFILE), url)
        for url in ("https://www.tiktok.com/@other.shop", "https://www.tiktok.com/", "about:blank", PROFILE + "x"):
            self.assertFalse(tm._standing_on(self._S(url), PROFILE), url)

    def test_a_kept_browser_opens_the_next_post_without_loading_the_profile_again(self):
        s = self._S(PROFILE + "/video/999")                    # an earlier post is still open over the grid
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_back_to_grid") as back, mock.patch.object(tm, "_open_tile_fast", return_value=True) as click:
            goto, via_tile = tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)
        self.assertEqual((goto, via_tile), ({"ok": True}, True))
        self.assertEqual(s.urls, [], "no page load: TikTok counts those")
        back.assert_called_once()
        click.assert_called_once()

    def test_a_reply_under_the_post_that_is_already_open_navigates_nowhere(self):
        s = self._S(PROFILE + "/video/111")
        with mock.patch.object(tm, "_find_tile_fast") as find, mock.patch.object(tm, "_back_to_grid") as back, \
                mock.patch.object(tm, "_open_tile_fast", return_value=True) as click:
            goto, via_tile = tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE, keep_open=True)
        self.assertEqual((goto, via_tile), ({"ok": True}, True))
        for untouched in (find, back, click):
            untouched.assert_not_called()
        self.assertEqual(s.urls, [])

    def test_a_read_always_reopens_the_post_because_it_needs_a_fresh_comment_list(self):
        s = self._S(PROFILE + "/video/111")
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_back_to_grid") as back, mock.patch.object(tm, "_open_tile_fast", return_value=True) as click:
            tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)             # keep_open is off for reads
        back.assert_called_once()
        click.assert_called_once()

    def test_a_different_post_than_the_open_one_is_opened_even_for_a_reply(self):
        s = self._S(PROFILE + "/video/999")
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_back_to_grid") as back, mock.patch.object(tm, "_open_tile_fast", return_value=True) as click:
            tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE, keep_open=True)
        back.assert_called_once()
        click.assert_called_once()

    def test_a_fresh_browser_loads_the_profile_once(self):
        s = self._S("about:blank")
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_back_to_grid"), mock.patch.object(tm, "_open_tile_fast", return_value=True):
            tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)
        self.assertEqual(s.urls, [PROFILE])


class _TileSession:
    """A page where a click opens the post under the pointer - or, when `shifts`, the WRONG one (the grid moved)."""

    def __init__(self, shifts=0, never=False):
        self.shifts, self.never, self.clicks, self.backs = shifts, never, 0, 0
        self.page = types.SimpleNamespace(url=PROFILE, wait_for_timeout=lambda ms: None,
                                          locator=lambda sel: types.SimpleNamespace(first=types.SimpleNamespace(
                                              scroll_into_view_if_needed=lambda timeout=0: None, click=lambda timeout=0: self.opens())))

    def opens(self):
        self.clicks += 1
        if self.never:
            return
        if self.shifts > 0:
            self.shifts -= 1
            self.page.url = PROFILE + "/video/999"             # another post opened: the grid shifted under the click
        else:
            self.page.url = PROFILE + "/video/111"

    def click(self, sel, by="css"):
        self.opens()


class _GridPage:
    """A profile grid like TikTok's: virtualised - the page always holds `window` tiles; scrolling swaps which ones."""

    def __init__(self, total=120, window=30, step=8, target=None, empty=False, menu_traps_wheel=False, wheel_dead=False):
        self.total, self.window, self.step, self.target, self.empty, self.offset, self.wheels = total, window, step, target, empty, 0, 0
        self.over_menu, self.menu_traps_wheel, self.wheel_dead = True, menu_traps_wheel, wheel_dead     # the pointer starts over the side menu
        self.mouse = types.SimpleNamespace(wheel=self.wheel, move=self.move)

    def move(self, x, y):
        self.over_menu = x < 250

    def hrefs(self):
        if self.empty:
            return []
        return [f"/@shop/video/{1000 + i}" for i in range(self.offset, min(self.total, self.offset + self.window))]

    def wheel(self, dx, dy):
        self.wheels += 1
        if self.wheel_dead or (self.menu_traps_wheel and self.over_menu):
            return                                              # the wheel scrolled nothing
        self.offset = min(self.total - self.window, self.offset + self.step)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, *args):
        if "scrollBy" in js:                                     # the page scrolled directly
            self.offset = min(self.total - self.window, self.offset + self.step)
            return None
        if "innerWidth" in js:
            return [1350, 700]
        if js.strip() == "() => scrollY":
            return self.offset * 100
        if "scrollHeight" in js:                                 # is the page scrolled to its bottom?
            return self.offset >= self.total - self.window
        return self.hrefs() if "map(" in js else len(self.hrefs())

    def screenshot(self, path=None, **kw):
        self.shots = getattr(self, "shots", 0) + 1

    def locator(self, sel):
        want = f"/video/{self.target}" if self.target else None
        return types.SimpleNamespace(count=lambda: int(bool(want) and any(h.endswith(want) for h in self.hrefs())))


class TestFindingATileOnAVirtualisedGrid(unittest.TestCase):
    class Sess:
        def __init__(self, page):
            self.page, self.urls = page, []

        def goto(self, url):
            self.urls.append(url)
            return {"ok": True}

    def find(self, page):
        s = self.Sess(page)
        return tm._find_tile_fast(s, 'a[href*="/video/1099"]', PROFILE), s

    def test_a_post_far_down_the_grid_is_found_although_the_number_of_tiles_never_grows(self):
        page = _GridPage(target=1099)                            # 100 posts down; the page only ever holds 30 tiles
        found, s = self.find(page)
        self.assertTrue(found)
        self.assertGreater(page.wheels, 5, "it really scrolled")
        self.assertEqual(s.urls, [], "and never reloaded the profile page")

    def test_a_wheel_that_the_fixed_side_menu_swallows_does_not_strand_the_scan_at_the_first_tiles(self):
        """Seen live: with the pointer over TikTok's side menu the wheel scrolled nothing and the scan ended with 19 tiles."""
        page = _GridPage(target=1099, menu_traps_wheel=True)
        found, s = self.find(page)
        self.assertTrue(found)
        self.assertFalse(page.over_menu, "the pointer was moved onto the grid first")
        self.assertEqual(s.urls, [])

    def test_a_wheel_that_does_nothing_at_all_is_replaced_by_scrolling_the_page(self):
        page = _GridPage(target=1099, wheel_dead=True)
        found, _ = self.find(page)
        self.assertTrue(found, "the page was scrolled directly when the wheel did not move it")

    def test_a_post_that_is_not_on_the_grid_ends_at_the_bottom_without_a_reload(self):
        page = _GridPage(target=None)
        found, s = self.find(page)
        self.assertFalse(found)
        self.assertEqual(s.urls, [], "a complete scan is never followed by another page load")
        self.assertLessEqual(page.wheels, 40)
        self.assertEqual(page.shots, 1, "a picture is kept of what the page looked like")

    def test_tiktok_fetching_more_tiles_slowly_is_not_mistaken_for_the_end_of_the_grid(self):
        """Seen live: after a scroll the same tiles stay for a moment, then more arrive."""
        page = _GridPage(target=1099)
        real_wheel = page.wheel
        calls = {"n": 0}

        def slow_wheel(dx, dy):                                  # the first four scrolls bring nothing new yet
            calls["n"] += 1
            if calls["n"] > 4:
                real_wheel(dx, dy)
            page.wheels += 0
        page.mouse = types.SimpleNamespace(wheel=slow_wheel, move=page.move)
        found, _ = self.find(page)
        self.assertTrue(found, "it waited for the slow fetch instead of giving up after three unchanged scrolls")

    def test_a_page_that_errors_while_busy_is_looked_at_again_not_given_up_on(self):
        page = _GridPage(target=1099)
        real = page.evaluate
        flaky = {"n": 0}

        def evaluate(js, *a):
            flaky["n"] += 1
            if flaky["n"] % 7 == 0:                              # "Execution context was destroyed" - the page is navigating
                raise RuntimeError("Execution context was destroyed")
            return real(js, *a)
        page.evaluate = evaluate
        with mock.patch.object(tm.time, "sleep"):
            found, s = self.find(page)
        self.assertTrue(found)
        self.assertEqual(s.urls, [])

    def test_a_page_where_nothing_rendered_is_reloaded_once(self):
        page = _GridPage(empty=True)
        with mock.patch("app.tiktok_watch._wait_for", return_value=False):
            found, s = self.find(page)
        self.assertFalse(found)
        self.assertEqual(s.urls, [PROFILE], "one reload for an empty page")


class TestOpeningATile(unittest.TestCase):
    def open(self, s):
        with mock.patch.object(tm, "_back_to_grid", side_effect=lambda sess, url: setattr(sess.page, "url", PROFILE) or setattr(sess, "backs", sess.backs + 1)):
            return tm._open_tile_fast(s, 'a[href*="/video/111"]', "111", PROFILE, wait_s=0.05)

    def test_a_click_that_opens_the_post_is_enough(self):
        s = _TileSession()
        self.assertTrue(self.open(s))
        self.assertEqual((s.clicks, s.backs), (1, 0))

    def test_a_wrong_post_is_closed_before_the_click_is_repeated_not_clicked_through(self):
        s = _TileSession(shifts=1)
        self.assertTrue(self.open(s))
        self.assertEqual((s.clicks, s.backs), (2, 1), "one wrong post was closed, then the right one opened")

    def test_it_gives_up_after_a_few_tries_instead_of_reloading_the_page(self):
        s = _TileSession(never=True)
        self.assertFalse(self.open(s))
        self.assertEqual(s.clicks, 3)

    def test_an_error_while_searching_the_grid_is_a_wall_for_this_account_and_never_a_direct_load(self):
        class Sess(_TileSession):
            urls = []

            def goto(self, url):
                self.urls.append(url)
                return {"ok": True}
        s = Sess()
        s.page.url = PROFILE
        with mock.patch.object(tm, "_find_tile_fast", side_effect=RuntimeError("Execution context was destroyed")), \
                mock.patch.object(tm, "_captcha_present", return_value=False), mock.patch.object(tm, "_snapshot"):
            goto, via_tile = tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)
        self.assertEqual((goto["ok"], goto["blocked"], via_tile), (False, True, False))
        self.assertIn("could not be read", goto["error"])
        self.assertEqual(Sess.urls, [], "the address would only meet a captcha or a 403")

    def test_a_post_that_is_not_on_the_owners_grid_at_all_is_loaded_by_its_address_as_a_last_resort(self):
        class Sess(_TileSession):
            urls = []

            def goto(self, url):
                self.urls.append(url)
                return {"ok": True}
        s = Sess()
        s.page.url = PROFILE
        with mock.patch.object(tm, "_find_tile_fast", return_value=False), mock.patch.object(tm, "_captcha_present", return_value=False):
            goto, via_tile = tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)
        self.assertEqual((goto, via_tile), ({"ok": True}, False))
        self.assertEqual(Sess.urls, [PROFILE + "/video/111"])

    def test_a_post_whose_tile_will_not_open_is_a_wall_for_this_account_not_a_page_load(self):
        class Sess(_TileSession):
            urls = []

            def goto(self, url):
                self.urls.append(url)
                return {"ok": True}
        s = Sess(never=True)
        s.page.url = PROFILE
        with mock.patch.object(tm, "_find_tile_fast", return_value=True), mock.patch.object(tm, "_captcha_present", return_value=False), \
                mock.patch.object(tm, "_open_tile_fast", return_value=False):
            goto, via_tile = tm._open_post(s, "111", PROFILE + "/video/111", profile_url=PROFILE)
        self.assertEqual((goto["ok"], goto["blocked"], via_tile), (False, True, False))
        self.assertEqual(Sess.urls, [], "nothing was loaded afterwards: the next account is tried instead")


class TestTheAppAroundIt(unittest.TestCase):
    def test_the_service_closes_the_kept_browser_of_an_account_through_its_worker(self):
        svc = Service(tmp_db(), telegram=False, monitor=False)
        ran = []
        with mock.patch.object(tm, "warm_account", return_value="acc1"), mock.patch.object(tm, "close_warm") as close, \
                mock.patch.object(svc, "call_in_worker", side_effect=lambda fn, *a, **k: (ran.append(a), fn(*a))[1]):
            svc._release_browser("acc1")
            svc._release_browser("acc2")                       # not the kept one: nothing to do
        self.assertEqual(ran, [("acc1",)])
        close.assert_called_once_with("acc1")

    def test_the_worker_lets_idle_browsers_expire_only_when_the_feature_is_on(self):
        svc = Service(tmp_db(), telegram=False, monitor=False)
        with mock.patch.object(tm, "expire_warm") as expire:
            tm.WARM_SECONDS = 0
            svc._expire_warm()
            expire.assert_not_called()
            tm.WARM_SECONDS = 120
            try:
                svc._expire_warm()
            finally:
                tm.WARM_SECONDS = 0
            expire.assert_called_once()

    def test_stopping_the_service_closes_the_kept_browser_on_the_worker_thread(self):
        svc = Service(tmp_db(), telegram=False, monitor=False)
        svc.start()
        try:
            with mock.patch.object(tm, "warm_account", return_value="acc1"), mock.patch.object(tm, "close_warm") as close:
                svc.stop()
            close.assert_called_once_with()
        finally:
            svc.stop_event.set()

    def test_the_desktop_app_turns_the_feature_on_and_a_stop_puts_it_back(self):
        from app.desktop import DesktopApp
        with mock.patch.dict("os.environ", {"WARM_SESSION_SECONDS": "240"}):
            tm.WARM_SECONDS = 0
            app = DesktopApp(db=tmp_db(), port=0, token="t" * 32)
            self.assertEqual(tm.WARM_SECONDS, 240)
            app.stop()
        self.assertEqual(tm.WARM_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
