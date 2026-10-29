"""Add account from the desktop app: the fresh login window, automatic detection, smart registration, session health,
organising accounts. The browser is a fake window; nothing opens, nothing touches TikTok - offline."""
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from _helpers import ROOT  # noqa: F401

import config
from app import account_manager as am
from app import accounts as acc
from app import browsers
from app.events import EventBus

CHROME = {"kind": "chrome", "name": "Google Chrome", "path": r"C:\fake\chrome.exe", "configured": False}


class FakeWindow:
    """Stands in for the real browser window. It 'logs in' after `login_after` cookie polls."""

    def __init__(self, udd, *, login_after=2, who=None, closes_on_poll=None, identify_error=False):
        self.udd = pathlib.Path(udd)
        self.udd.mkdir(parents=True, exist_ok=True)
        (self.udd / "Cookies").write_text("session data")          # a saved profile is never empty
        self.login_after, self.who, self.closes_on_poll, self.identify_error = login_after, who, closes_on_poll, identify_error
        self.polls, self.closed, self.dead, self.identify_calls = 0, False, False, 0

    def alive(self):
        return not self.closed and not self.dead

    def has_login(self):
        self.polls += 1
        if self.closes_on_poll and self.polls >= self.closes_on_poll:
            self.dead = True                                        # the person closed the window
        return self.polls > self.login_after

    def identify(self):
        self.identify_calls += 1
        if self.identify_error:
            raise RuntimeError("page not ready")
        return dict(self.who or {})

    def close(self):
        self.closed = True


def who(username, region="BD", nick=None):
    return {"uniqueId": username, "nickName": nick or username.title(), "region": region}


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="acct-"))
        self._saved = (config.ACCOUNTS_DIR, config.ACCOUNTS_FILE, config.BFP_MASTER_USER_DATA_DIR,
                       config.REPLY_ACCOUNT_FIRST, config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES, browsers.choose)
        config.ACCOUNTS_DIR = self.tmp / "real"
        config.ACCOUNTS_FILE = self.tmp / "accounts.json"
        config.BFP_MASTER_USER_DATA_DIR = str(self.tmp / "master" / "User Data")
        config.REPLY_ACCOUNT_FIRST = ""
        config.BFP_PROFILE_NAME = "master-acct"
        config.BFP_PROFILE_NAMES = ["master-acct"]
        browsers.choose = lambda pref="auto", **kw: CHROME
        # No test may ever reach a real browser: a background identity check can outlive the `with` block of a test that
        # patches identify() itself, so the safe default is in place for the whole test (the tests' own patches nest inside)
        self._identify_default = mock.patch.object(acc, "identify", return_value={"ok": False, "who": None,
                                                                                  "error": "tests never start a browser"})
        self._identify_default.start()
        self.bus = EventBus()
        self.windows: list = []
        self.window_kwargs: dict = {}
        self.mgr = am.AccountManager(self.bus, launcher=self.launch, memory_check=lambda: (True, 8000.0),
                                     poll_s=0.01, settle_s=0, identify_timeout_s=0.2, login_timeout_s=5, sleep=lambda s: time.sleep(0.001))

    def tearDown(self):
        self.mgr.close()
        self._identify_default.stop()
        (config.ACCOUNTS_DIR, config.ACCOUNTS_FILE, config.BFP_MASTER_USER_DATA_DIR, config.REPLY_ACCOUNT_FIRST,
         config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES, browsers.choose) = self._saved

    # -- helpers
    def launch(self, exe, udd):
        w = FakeWindow(udd, **self.window_kwargs)
        self.windows.append(w)
        return w

    def registry(self) -> list:
        return config.load_accounts()

    def seed(self, name, username, **extra):
        udd = config.ACCOUNTS_DIR / name / "User Data"
        udd.mkdir(parents=True, exist_ok=True)
        (udd / "Cookies").write_text("old")
        entry = {"name": name, "user_data_dir": str(udd), "username": username, "region": "BD", "enabled": True,
                 "added": "2026-10-01 10:00:00", **extra}
        acc._write_registry(self.registry() + [entry])
        return entry

    def run_add(self, **kw) -> dict:
        self.mgr.start_add(**kw)
        return self.wait_done()

    def wait_done(self, timeout=10) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cur = self.mgr.current()
            if cur and cur["finished"]:
                return cur
            time.sleep(0.01)
        self.fail(f"the flow never finished: {self.mgr.current()}")

    def states(self) -> list:
        out = []
        for e in self.bus.since(0, limit=1000):
            if e["type"] == "account.flow" and (not out or out[-1] != e["data"]["flow"]["state"]):
                out.append(e["data"]["flow"]["state"])
        return out


class TestAddAccount(ManagerCase):
    def test_a_login_is_detected_saved_and_the_window_closed_by_itself(self):
        self.window_kwargs = dict(who=who("shop.one", nick="Shop One"))
        flow = self.run_add()
        self.assertEqual((flow["state"], flow["outcome"]), ("done", "added"))
        self.assertEqual(self.states(), ["starting", "window_open", "login_detected", "verifying", "saving", "done"])
        self.assertTrue(self.windows[0].closed, "closed gracefully by the app, nothing for the person to click")
        (entry,) = self.registry()
        self.assertEqual((entry["name"], entry["username"], entry["nickname"], entry["region"], entry["session"], entry["enabled"]),
                         ("real-account-1", "shop.one", "Shop One", "BD", "ok", True))
        self.assertEqual((entry["browser"], entry["browser_kind"], entry["source"]), (CHROME["path"], "chrome", "desktop"))
        self.assertTrue(pathlib.Path(entry["user_data_dir"]).exists())
        self.assertEqual(flow["result"]["account"]["label"], "@shop.one")
        self.assertIn("added", flow["message"])

    def test_nothing_is_saved_before_the_login_cookie_appears(self):
        self.window_kwargs = dict(who=who("late.login"), login_after=10**6)        # the person has not logged in yet
        self.mgr.start_add()
        time.sleep(0.2)
        cur = self.mgr.current()
        self.assertEqual((cur["state"], cur["waiting_for_you"], cur["finished"]), ("window_open", True, False))
        self.assertEqual(self.registry(), [])
        self.windows[0].login_after = 0                                              # ...and now they have
        self.assertEqual(self.wait_done()["outcome"], "added")

    def test_the_progress_message_tells_the_person_what_to_do(self):
        self.window_kwargs = dict(login_after=10**6)
        view = self.mgr.start_add(label="Second page")
        time.sleep(0.1)
        self.assertIn("Log in to TikTok", self.mgr.current()["message"])
        self.assertEqual(view["browser"], {"kind": "chrome", "name": "Google Chrome"})
        self.mgr.cancel()
        self.wait_done()

    def test_a_label_given_up_front_is_kept(self):
        self.window_kwargs = dict(who=who("shop.one"))
        self.run_add(label="  Main page  ")
        self.assertEqual(self.registry()[0]["label"], "Main page")

    def test_closing_the_window_before_logging_in_cancels_and_leaves_nothing(self):
        self.window_kwargs = dict(login_after=10**6, closes_on_poll=3)
        flow = self.run_add()
        self.assertEqual(flow["outcome"], "cancelled")
        self.assertIn("closed before a login", flow["message"])
        self.assertEqual(self.registry(), [])
        self.assertFalse((config.ACCOUNTS_DIR / "real-account-1").exists(), "the fresh folder is cleaned up")

    def test_cancel_from_the_app_closes_the_window_and_cleans_up(self):
        self.window_kwargs = dict(login_after=10**6)
        self.mgr.start_add()
        time.sleep(0.1)
        self.mgr.cancel()
        flow = self.wait_done()
        self.assertEqual(flow["outcome"], "cancelled")
        self.assertTrue(self.windows[0].closed)
        self.assertFalse((config.ACCOUNTS_DIR / "real-account-1").exists())

    def test_no_login_in_time_ends_with_a_timeout_and_nothing_saved(self):
        mgr = am.AccountManager(self.bus, launcher=self.launch, memory_check=lambda: (True, 1), poll_s=0.01, settle_s=0,
                                login_timeout_s=0.2, sleep=lambda s: time.sleep(0.01))
        self.window_kwargs = dict(login_after=10**6)
        try:
            mgr.start_add()
            end = time.monotonic() + 5
            while time.monotonic() < end and not (mgr.current() or {}).get("finished"):
                time.sleep(0.02)
            self.assertEqual(mgr.current()["outcome"], "timeout")
        finally:
            mgr.close()
        self.assertEqual(self.registry(), [])
        self.assertTrue(self.windows[0].closed)

    def test_a_browser_that_will_not_start_is_reported_and_the_slot_is_free_again(self):
        def broken(exe, udd):
            raise RuntimeError("could not start chromium")
        self.mgr._launch = broken
        flow = self.run_add()
        self.assertEqual((flow["state"], flow["outcome"]), ("failed", "failed"))
        self.assertIn("could not be opened", flow["message"])
        self.mgr._launch = self.launch
        self.window_kwargs = dict(who=who("fine.now"))
        self.assertEqual(self.run_add()["outcome"], "added")

    def test_only_one_login_window_at_a_time(self):
        self.window_kwargs = dict(login_after=10**6)
        self.mgr.start_add()
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.start_add()
        self.assertEqual(cm.exception.code, "busy")
        self.mgr.cancel()
        self.wait_done()
        self.window_kwargs = dict(who=who("x.y"))
        self.mgr.start_add()                                       # free again
        self.wait_done()

    def test_no_browser_and_low_memory_are_refused_with_a_reason(self):
        browsers.choose = lambda pref="auto", **kw: None
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.start_add()
        self.assertEqual(cm.exception.code, "no_browser")
        browsers.choose = lambda pref="auto", **kw: None
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.start_add(browser="chrome")
        self.assertIn("Chrome is not installed", cm.exception.message)
        browsers.choose = lambda pref="auto", **kw: CHROME
        self.mgr._memory = lambda: (False, 300.0)
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.start_add()
        self.assertEqual(cm.exception.code, "low_memory")
        self.assertIsNone(self.mgr.current(), "a refused request starts nothing")

    def test_the_account_numbers_skip_leftover_folders(self):
        (config.ACCOUNTS_DIR / "real-account-1" / "User Data").mkdir(parents=True)     # a stale folder nobody registered
        self.window_kwargs = dict(who=who("shop.one"))
        self.run_add()
        self.assertEqual(self.registry()[0]["name"], "real-account-2", "never reuse a folder: the window needs a fresh profile")

    def test_the_login_window_gets_a_folder_of_its_own_never_the_persons_browser(self):
        seen = []
        self.mgr._launch = lambda exe, udd: (seen.append((exe, pathlib.Path(udd))), self.launch(exe, udd))[1]
        self.window_kwargs = dict(who=who("a.b"))
        self.run_add()
        exe, udd = seen[0]
        self.assertEqual(exe, CHROME["path"])
        self.assertEqual(udd.parent.parent, config.ACCOUNTS_DIR, "inside the app's own accounts folder")


class TestSmartRegistration(ManagerCase):
    def test_the_same_user_while_the_account_works_is_a_duplicate_and_changes_nothing(self):
        old = self.seed("real-account-1", "shop.one", session="ok", label="Main")
        self.window_kwargs = dict(who=who("Shop.One"))                 # TikTok handles are case-insensitive
        flow = self.run_add()
        self.assertEqual(flow["outcome"], "duplicate")
        self.assertEqual(flow["result"]["duplicate_of"], "real-account-1")
        self.assertIn("already added", flow["message"])
        (entry,) = self.registry()
        self.assertEqual((entry["user_data_dir"], entry["label"]), (old["user_data_dir"], "Main"))
        self.assertFalse((config.ACCOUNTS_DIR / "real-account-2").exists(), "the redundant new profile is deleted")
        self.assertTrue(pathlib.Path(old["user_data_dir"]).exists(), "the working one is untouched")

    def test_the_same_user_after_the_session_expired_refreshes_that_account(self):
        old = self.seed("real-account-1", "shop.one", session="expired", label="Main", enabled=False)
        self.seed("real-account-2", "other.user", session="ok")
        self.window_kwargs = dict(who=who("shop.one", region="PY"))
        flow = self.run_add()
        self.assertEqual(flow["outcome"], "refreshed")
        a, b = self.registry()
        self.assertEqual([a["name"], b["name"]], ["real-account-1", "real-account-2"], "same name, same position")
        self.assertEqual((a["label"], a["enabled"], a["session"], a["region"]), ("Main", False, "ok", "PY"))
        self.assertNotEqual(a["user_data_dir"], old["user_data_dir"])
        self.assertTrue(pathlib.Path(a["user_data_dir"]).exists())
        self.assertFalse(pathlib.Path(old["user_data_dir"]).exists(), "the dead profile folder is removed")
        self.assertEqual(len(self.registry()), 2)

    def test_log_in_again_for_a_named_account_refreshes_it_even_if_it_looked_fine(self):
        old = self.seed("real-account-1", "shop.one", session="ok")
        self.window_kwargs = dict(who=who("shop.one"))
        flow = self.run_add(replace="real-account-1")
        self.assertEqual(flow["outcome"], "refreshed")
        self.assertFalse(pathlib.Path(old["user_data_dir"]).exists())

    def test_log_in_again_as_a_different_user_adds_a_new_account_and_keeps_the_old(self):
        old = self.seed("real-account-1", "shop.one", session="expired")
        self.window_kwargs = dict(who=who("someone.else"))
        flow = self.run_add(replace="real-account-1")
        self.assertEqual(flow["outcome"], "added")
        names = [a["name"] for a in self.registry()]
        self.assertEqual(names, ["real-account-1", "real-account-2"])
        self.assertEqual(self.registry()[0]["user_data_dir"], old["user_data_dir"], "untouched")

    def test_the_pages_own_master_account_is_never_added_twice(self):
        acc._write_registry([], master_username="Master.Page")
        self.window_kwargs = dict(who=who("master.page"))
        flow = self.run_add()
        self.assertEqual(flow["outcome"], "duplicate")
        self.assertEqual(flow["result"]["duplicate_of"], "master-acct")
        self.assertEqual(self.registry(), [])
        self.assertFalse((config.ACCOUNTS_DIR / "real-account-1").exists())

    def test_a_login_whose_user_cannot_be_read_is_still_saved_and_looked_up_afterwards(self):
        self.window_kwargs = dict(who=None)                            # TikTok page never answers who
        looked_up = threading.Event()

        def fake_identify(udd, browser=None):
            looked_up.set()
            return {"ok": True, "who": who("found.later", region="PY")}
        with mock.patch.object(acc, "identify", fake_identify):
            flow = self.run_add()
            self.assertEqual(flow["outcome"], "added")
            self.assertTrue(flow["result"]["account"]["identity_pending"])
            self.assertTrue(looked_up.wait(5), "the missing identity is fetched automatically")
            end = time.monotonic() + 5
            while time.monotonic() < end and not self.registry()[0].get("username"):
                time.sleep(0.02)
        entry = self.registry()[0]
        self.assertEqual((entry["username"], entry["region"], entry["session"]), ("found.later", "PY", "ok"))

    def test_closing_the_window_by_hand_after_logging_in_still_saves_the_session(self):
        self.window_kwargs = dict(who=who("quick.closer"), login_after=1, closes_on_poll=2)
        mgr = am.AccountManager(self.bus, launcher=self.launch, memory_check=lambda: (True, 1), poll_s=0.01, settle_s=0.3,
                                identify_timeout_s=0.2, login_timeout_s=5, sleep=lambda s: time.sleep(0.01))
        with mock.patch.object(acc, "identify", lambda udd, browser=None: {"ok": True, "who": who("quick.closer")}):
            try:
                mgr.start_add()
                end = time.monotonic() + 5
                while time.monotonic() < end and not (mgr.current() or {}).get("finished"):
                    time.sleep(0.02)
                self.assertEqual(mgr.current()["outcome"], "added")
            finally:
                mgr.close()
        self.assertEqual(len(self.registry()), 1)
        self.assertTrue(pathlib.Path(self.registry()[0]["user_data_dir"]).exists())

    def test_the_new_account_goes_to_the_end_of_the_order(self):
        self.seed("real-account-1", "a.a")
        self.seed("real-account-2", "b.b")
        self.window_kwargs = dict(who=who("c.c"))
        self.run_add()
        self.assertEqual([a["name"] for a in self.registry()], ["real-account-1", "real-account-2", "real-account-3"])
        self.assertEqual(config.account_chain()[-1], "real-account-3")


class TestHealth(ManagerCase):
    def checked(self, result, **seed):
        self.seed("real-account-1", "shop.one", **seed)
        with mock.patch.object(acc, "identify", return_value=result) as ident:
            out = self.mgr.check("real-account-1")
        return out, ident

    def test_a_working_session_is_marked_ok_with_the_fresh_identity(self):
        out, ident = self.checked({"ok": True, "who": who("shop.one", region="PY")}, session="expired", browser=r"C:\b\brave.exe")
        self.assertEqual((out["ok"], out["session"], out["username"], out["region"]), (True, "ok", "shop.one", "PY"))
        self.assertEqual(self.registry()[0]["session"], "ok")
        self.assertEqual(self.registry()[0]["region"], "PY")
        self.assertEqual(ident.call_args.args[1], r"C:\b\brave.exe", "checked with the browser that created the profile")

    def test_a_page_with_no_session_cookie_means_the_session_expired(self):
        out, _ = self.checked({"ok": True, "logged_in": False, "who": None}, session="ok")
        self.assertEqual((out["ok"], out["session"]), (True, "expired"))
        self.assertEqual(self.registry()[0]["session"], "expired")
        self.assertEqual(self.registry()[0]["enabled"], True, "checking never switches an account off by itself")

    def test_a_page_that_names_nobody_while_the_cookie_is_still_there_is_never_called_expired(self):
        """Measured on a real, working account: TikTok sometimes serves its guest page. That must not send anyone to log in
        again, and must not overwrite what is already known."""
        out, ident = self.checked({"ok": True, "logged_in": None, "who": None}, session="ok")
        self.assertEqual((out["ok"], out["session"], out["unconfirmed"]), (True, "unknown", True))
        self.assertEqual(self.registry()[0]["session"], "ok", "the stored state is untouched")
        self.assertEqual(ident.call_count, 2, "a second, fresh browser is tried before giving up")
        event = [e for e in self.bus.since(0) if e["type"] == "account.checked"][-1]
        self.assertEqual((event["level"], event["data"]["unconfirmed"]), ("warn", True))

    def test_the_second_browser_often_gets_it_right(self):
        self.seed("real-account-1", "shop.one", session="expired")
        answers = [{"ok": True, "logged_in": None, "who": None}, {"ok": True, "logged_in": True, "who": who("shop.one")}]
        with mock.patch.object(acc, "identify", side_effect=answers) as ident:
            out = self.mgr.check("real-account-1")
        self.assertEqual((out["session"], ident.call_count), ("ok", 2))
        self.assertEqual(self.registry()[0]["session"], "ok")

    def test_a_clear_answer_is_not_asked_twice(self):
        _out, ident = self.checked({"ok": True, "logged_in": True, "who": who("shop.one")})
        self.assertEqual(ident.call_count, 1)
        _out, ident = self.checked({"ok": True, "logged_in": False, "who": None})
        self.assertEqual(ident.call_count, 1)

    def test_a_check_that_could_not_run_changes_nothing(self):
        out, _ = self.checked({"ok": False, "who": None, "error": "browser did not start"}, session="ok")
        self.assertEqual((out["ok"], out["session"], out["error"]), (False, "unknown", "browser did not start"))
        self.assertEqual(self.registry()[0]["session"], "ok", "a failed check is not evidence of a logged-out account")

    def test_a_check_that_finds_a_user_already_registered_elsewhere_disables_the_copy(self):
        self.seed("real-account-2", "shop.two", session="ok")
        self.seed("real-account-1", "placeholder", session="ok")
        with mock.patch.object(acc, "identify", return_value={"ok": True, "who": who("shop.two")}):
            out = self.mgr.check("real-account-1")
        self.assertEqual(out["duplicate_of"], "real-account-2")
        by_name = {a["name"]: a for a in self.registry()}
        self.assertFalse(by_name["real-account-1"]["enabled"], "the copy is switched off")
        self.assertTrue(by_name["real-account-2"]["enabled"], "the original is not")

    def test_the_master_account_is_checked_and_remembered_in_the_registry(self):
        udd = pathlib.Path(config.BFP_MASTER_USER_DATA_DIR)
        udd.mkdir(parents=True)
        with mock.patch.object(acc, "identify", return_value={"ok": True, "who": who("the.page")}):
            out = self.mgr.check("master-acct")
        self.assertEqual((out["session"], out["username"]), ("ok", "the.page"))
        self.assertEqual(config.registry_value("master_username"), "the.page")
        self.assertEqual(config.registry_value("master_session"), "ok")

    def test_a_check_does_not_occupy_the_services_browser_thread(self):
        """A check can take a minute when TikTok is slow; scans and posts must not wait for it."""
        calls = []
        self.mgr._worker = lambda fn, *a: (calls.append(1), fn(*a))[1]
        self.seed("real-account-1", "shop.one")
        with mock.patch.object(acc, "identify", return_value={"ok": True, "logged_in": True, "who": who("shop.one")}) as ident:
            self.mgr.check("real-account-1")
        self.assertEqual(calls, [], "the worker was not used")
        ident.assert_called_once()

    def test_the_list_says_which_accounts_tiktok_just_refused(self):
        self.seed("real-account-1", "shop.one")
        self.seed("real-account-2", "shop.two")
        self.mgr._rest_fn = lambda name: 240 if name == "real-account-1" else 0
        rows = {a["name"]: a["resting_seconds"] for a in self.mgr.list()["accounts"]}
        self.assertEqual((rows["real-account-1"], rows["real-account-2"]), (240, 0))

    def test_a_broken_rest_lookup_never_breaks_the_list(self):
        self.seed("real-account-1", "shop.one")

        def broken(name):
            raise RuntimeError("no engine")
        self.mgr._rest_fn = broken
        self.assertEqual({a["resting_seconds"] for a in self.mgr.list()["accounts"]}, {0})

    def test_a_check_first_asks_for_the_kept_browser_of_that_account_to_be_closed(self):
        """A browser kept open for the next reply holds the account's folder; the check would find it in use."""
        order = []
        self.mgr._release_fn = lambda name: order.append(("release", name))
        self.seed("real-account-1", "shop.one")
        with mock.patch.object(acc, "identify", side_effect=lambda *a, **k: (order.append(("identify",)),
                                                                              {"ok": True, "logged_in": True, "who": who("shop.one")})[1]):
            self.mgr.check("real-account-1")
        self.assertEqual(order, [("release", "real-account-1"), ("identify",)])

    def test_removing_an_account_first_lets_go_of_its_kept_browser(self):
        order = []
        self.mgr._release_fn = lambda name: order.append(("release", name))
        self.mgr._worker = lambda fn, *a: (order.append(("delete folder",)), fn(*a))[1]
        self.seed("real-account-1", "shop.one")
        self.mgr.remove("real-account-1")
        self.assertEqual(order, [("release", "real-account-1"), ("delete folder",)])

    def test_a_release_that_fails_never_stops_the_check(self):
        def broken(name):
            raise RuntimeError("the worker is gone")
        self.mgr._release_fn = broken
        self.seed("real-account-1", "shop.one")
        with mock.patch.object(acc, "identify", return_value={"ok": True, "logged_in": True, "who": who("shop.one")}):
            self.assertEqual(self.mgr.check("real-account-1")["session"], "ok")

    def test_a_check_will_not_open_a_browser_on_a_full_machine(self):
        self.seed("real-account-1", "shop.one")
        self.mgr._memory = lambda: (False, 250.0)
        with mock.patch.object(acc, "identify") as ident:
            with self.assertRaises(am.AccountError) as cm:
                self.mgr.check("real-account-1")
        self.assertEqual(cm.exception.code, "low_memory")
        ident.assert_not_called()

    def test_a_new_account_is_only_called_logged_in_once_who_it_is_is_known(self):
        self.window_kwargs = dict(who=None)                           # the identity could not be read
        flow = self.run_add()
        self.assertEqual(flow["result"]["account"]["session"], "unknown")
        self.assertEqual(self.registry()[0]["session"], "unknown")

    def test_checking_a_missing_account_or_folder_is_a_clear_error(self):
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.check("nope")
        self.assertEqual(cm.exception.code, "not_found")
        acc._write_registry([{"name": "real-account-1", "user_data_dir": str(self.tmp / "gone"), "username": "x"}])
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.check("real-account-1")
        self.assertEqual(cm.exception.code, "no_profile")

    def test_a_post_that_finds_an_account_logged_out_is_remembered_and_announced(self):
        self.seed("real-account-1", "shop.one", session="ok")
        self.assertTrue(acc.record_session("real-account-1", "expired"))
        self.assertEqual(self.registry()[0]["session"], "expired")
        self.assertFalse(acc.record_session("real-account-1", "expired"), "no change, no noise")
        event = [e for e in self.bus.since(0) if e["type"] == "account.session"][0]
        self.assertEqual((event["data"], event["level"]), ({"name": "real-account-1", "session": "expired"}, "warn"))
        self.assertTrue(acc.record_session("real-account-1", "ok"))

    def test_sessions_of_unknown_names_are_ignored_and_the_master_is_remembered_separately(self):
        self.assertFalse(acc.record_session("some-test-name", "expired"))
        self.assertFalse(config.ACCOUNTS_FILE.exists() and "some-test-name" in config.ACCOUNTS_FILE.read_text())
        self.assertTrue(acc.record_session("master-acct", "expired"))
        self.assertEqual(config.registry_value("master_session"), "expired")

    def test_the_engine_reports_every_attempt_to_the_account_registry(self):
        from app import tiktok_monitor as tm
        self.seed("real-account-1", "shop.one", session="ok")
        with mock.patch.object(tm, "_pace_posts"), mock.patch.object(tm, "_mark_post_attempt"):
            tm._run_chain(lambda a: {"ok": False, "error": "not logged in -- run login", "profile": a}, ["real-account-1"])
        self.assertEqual(self.registry()[0]["session"], "expired")
        with mock.patch.object(tm, "_pace_posts"), mock.patch.object(tm, "_mark_post_attempt"):
            tm._run_chain(lambda a: {"ok": True, "profile": a}, ["real-account-1"])
        self.assertEqual(self.registry()[0]["session"], "ok")
        with mock.patch.object(tm, "_pace_posts"), mock.patch.object(tm, "_mark_post_attempt"):
            tm._run_chain(lambda a: {"ok": False, "error": "comment box not found", "profile": a}, ["real-account-1"])
        self.assertEqual(self.registry()[0]["session"], "ok", "a failure about the target says nothing about the login")


class TestOrganising(ManagerCase):
    def setUp(self):
        super().setUp()
        for i, user in enumerate(("a.a", "b.b", "c.c"), start=1):
            self.seed(f"real-account-{i}", user, session="ok")

    def names(self):
        return [a["name"] for a in self.registry()]

    def test_list_shows_the_master_first_then_every_account_with_its_position(self):
        out = self.mgr.list()
        rows = {r["name"]: r for r in out["accounts"]}
        self.assertTrue(rows["master-acct"]["is_master"])
        self.assertEqual([r["name"] for r in out["accounts"]], ["master-acct", "real-account-1", "real-account-2", "real-account-3"])
        self.assertEqual([r["position"] for r in out["accounts"]], [1, 2, 3, 4])
        self.assertEqual((rows["real-account-1"]["label"], rows["real-account-1"]["session"], rows["real-account-1"]["folder_exists"]),
                         ("@a.a", "ok", True))
        self.assertEqual(rows["master-acct"]["session"], "unknown")
        self.assertIsNone(out["flow"])
        self.assertEqual(out["chain"], ["master-acct", "real-account-1", "real-account-2", "real-account-3"])

    def test_rename(self):
        self.assertEqual(self.mgr.set_label("real-account-2", " Second shop ")["label"], "Second shop")
        for bad in ("", "   ", "x" * 61):
            with self.assertRaises(am.AccountError):
                self.mgr.set_label("real-account-2", bad)

    def test_disabled_accounts_leave_the_posting_order(self):
        self.mgr.set_enabled("real-account-2", False)
        self.assertNotIn("real-account-2", config.account_chain())
        self.assertFalse([r for r in self.mgr.list()["accounts"] if r["name"] == "real-account-2"][0]["enabled"])
        self.mgr.set_enabled("real-account-2", True)
        self.assertIn("real-account-2", config.account_chain())

    def test_the_preferred_account_answers_first_and_survives_in_the_registry(self):
        self.mgr.set_first("real-account-3")
        self.assertEqual(config.account_chain()[0], "real-account-3")
        self.assertEqual(self.mgr.list()["first"], "real-account-3")
        self.assertTrue([r for r in self.mgr.list()["accounts"] if r["name"] == "real-account-3"][0]["preferred"])
        self.mgr.set_first(None)
        self.assertEqual(config.account_chain()[0], "master-acct")

    def test_the_env_setting_still_wins_over_the_apps_choice(self):
        self.mgr.set_first("real-account-3")
        config.REPLY_ACCOUNT_FIRST = "real-account-1"
        self.assertEqual(config.account_chain()[0], "real-account-1")

    def test_a_disabled_or_unknown_account_cannot_be_the_preferred_one(self):
        self.mgr.set_enabled("real-account-2", False)
        with self.assertRaises(am.AccountError):
            self.mgr.set_first("real-account-2")
        with self.assertRaises(am.AccountError) as cm:
            self.mgr.set_first("ghost")
        self.assertEqual(cm.exception.code, "not_found")

    def test_disabling_the_preferred_account_clears_the_preference(self):
        self.mgr.set_first("real-account-2")
        self.mgr.set_enabled("real-account-2", False)
        self.assertIsNone(config.registry_value("first"))

    def test_reorder(self):
        self.mgr.reorder(["real-account-3", "real-account-1"])
        self.assertEqual(self.names(), ["real-account-3", "real-account-1", "real-account-2"], "unlisted ones follow")
        self.assertEqual(config.account_chain()[1:], ["real-account-3", "real-account-1", "real-account-2"])
        with self.assertRaises(am.AccountError):
            self.mgr.reorder(["real-account-1", "ghost"])
        self.assertEqual(self.names()[0], "real-account-3", "a refused order changes nothing")

    def test_remove_forgets_the_account_and_deletes_only_its_folder(self):
        keep = pathlib.Path(self.registry()[0]["user_data_dir"])
        gone = pathlib.Path(self.registry()[1]["user_data_dir"])
        self.mgr.set_first("real-account-2")
        out = self.mgr.remove("real-account-2")
        self.assertEqual((out["removed"], out["folder_deleted"]), (True, True))
        self.assertEqual(self.names(), ["real-account-1", "real-account-3"])
        self.assertFalse(gone.parent.exists(), "the whole account folder is gone")
        self.assertTrue(keep.exists(), "the others are untouched")
        self.assertIsNone(config.registry_value("first"), "no preference left pointing at a removed account")

    def test_removing_an_account_whose_folder_a_background_check_still_holds_closes_that_browser_first(self):
        """Seen with the packaged exe: remove right after "Add account" -> the automatic check's headless browser still ran on the
        folder, Windows refused to delete it and the cookies stayed on the disk although the account was forgotten."""
        gone = pathlib.Path(self.registry()[1]["user_data_dir"])
        order, real_remove = [], acc.remove_profile_folder

        def remover(udd, attempts=6):
            order.append(("delete", attempts))
            return real_remove(udd, attempts)

        def closer(dirs):
            order.append(("close", dirs))
            return [4242]
        with mock.patch.object(acc, "remove_profile_folder", side_effect=remover), \
                mock.patch.object(am.orphans, "close_stale_browsers", side_effect=closer):
            out = self.mgr.remove("real-account-2")
        self.assertEqual(order, [("close", [str(gone)]), ("delete", 3)], "the browser is closed BEFORE the folder is deleted, quickly")
        self.assertTrue(out["folder_deleted"])
        self.assertFalse(gone.parent.exists())

    def test_a_folder_that_stays_locked_is_marked_and_deleted_in_the_background_as_soon_as_it_is_free(self):
        """The check's own browser handles (a log file, a lock file) are held by this very program for a while after the browser
        was closed: the folder cannot go yet - but it must go, with its cookies."""
        gone = pathlib.Path(self.registry()[1]["user_data_dir"])
        real_remove, calls = acc.remove_profile_folder, []

        def held_for_a_while(udd, attempts=6):
            calls.append(1)
            return False if len(calls) <= 2 else real_remove(udd, attempts)
        self.mgr._removal_retry_s = 0.01
        with mock.patch.object(acc, "remove_profile_folder", side_effect=held_for_a_while), \
                mock.patch.object(am.orphans, "close_stale_browsers", return_value=[]):
            out = self.mgr.remove("real-account-2")
            self.assertEqual((out["removed"], out["folder_deleted"], out["cleanup_pending"]), (True, False, True))
            self.assertTrue((gone.parent / acc.PENDING_REMOVAL).exists(), "marked, so that the next start finishes it too")
            end = time.monotonic() + 5
            while gone.parent.exists() and time.monotonic() < end:
                time.sleep(0.02)
        self.assertFalse(gone.parent.exists(), "deleted by the background retry")

    def test_a_marked_folder_is_deleted_at_the_next_start_and_only_a_marked_one(self):
        marked, plain = config.ACCOUNTS_DIR / "real-account-8", config.ACCOUNTS_DIR / "real-account-9"
        for folder in (marked, plain):
            (folder / "User Data").mkdir(parents=True)
            (folder / "User Data" / "Cookies").write_text("x")
        self.assertTrue(acc.mark_for_removal(marked / "User Data"))
        self.assertEqual(self.mgr.finish_pending_removals(), ["real-account-8"])
        self.assertFalse(marked.exists())
        self.assertTrue(plain.exists(), "an unmarked folder (even one nobody registered) is never touched")

    def test_the_marker_is_refused_outside_the_accounts_folder(self):
        outside = self.tmp / "my-documents" / "User Data"
        outside.mkdir(parents=True)
        self.assertFalse(acc.mark_for_removal(outside))
        self.assertFalse((outside.parent / acc.PENDING_REMOVAL).exists())
        self.assertFalse(acc.mark_for_removal(config.BFP_MASTER_USER_DATA_DIR))

    def test_a_folder_that_nothing_holds_is_simply_deleted(self):
        with mock.patch.object(am.orphans, "close_stale_browsers", return_value=[]) as closer:
            out = self.mgr.remove("real-account-2")
        self.assertEqual((out["folder_deleted"], out["cleanup_pending"]), (True, False))
        closer.assert_called_once()

    def test_the_master_account_cannot_be_removed_changed_or_relogged_from_here(self):
        for call in (lambda: self.mgr.remove("master-acct"), lambda: self.mgr.set_label("master-acct", "x"),
                     lambda: self.mgr.set_enabled("master-acct", False), lambda: self.mgr.start_add(replace="master-acct")):
            with self.assertRaises(am.AccountError) as cm:
                call()
            self.assertEqual(cm.exception.code, "master")

    def test_unknown_accounts_are_a_not_found(self):
        for call in (lambda: self.mgr.remove("ghost"), lambda: self.mgr.set_label("ghost", "x"),
                     lambda: self.mgr.start_add(replace="ghost")):
            with self.assertRaises(am.AccountError) as cm:
                call()
            self.assertEqual(cm.exception.code, "not_found")

    def test_check_all_reports_each_account_as_an_event(self):
        udd = pathlib.Path(config.BFP_MASTER_USER_DATA_DIR)
        udd.mkdir(parents=True)
        with mock.patch.object(acc, "identify", return_value={"ok": True, "who": who("someone")}):
            self.assertTrue(self.mgr.check_all()["started"])
            end = time.monotonic() + 5
            while time.monotonic() < end and len([e for e in self.bus.since(0) if e["type"] == "account.checked"]) < 4:
                time.sleep(0.02)
        checked = [e["data"]["name"] for e in self.bus.since(0) if e["type"] == "account.checked"]
        self.assertEqual(sorted(checked), ["master-acct", "real-account-1", "real-account-2", "real-account-3"])


class FakePage:
    def __init__(self, answers=None):
        self.answers, self.gotos, self.calls = list(answers or []), [], 0
        self.browser_info = {"webSocketDebuggerUrl": "ws://fake"}

    def goto(self, url, **kw):
        self.gotos.append(url)

    def evaluate(self, js):
        self.calls += 1
        if not self.answers:
            return "{}"
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class FakeBrowser:
    created: list = []

    def __init__(self, profile=None, executable=None, headless=True, **kw):
        self.executable, self.headless, self.driver, self.stopped, self.started = executable, headless, FakePage(), False, False
        FakeBrowser.created.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class TestReadingWhoIsLoggedIn(unittest.TestCase):
    """The bug found on a real account: TikTok names the user a few seconds late - and sometimes serves a guest page."""

    def read(self, answers, wait_s=10.0, step_s=1.0):
        page, naps, now = FakePage(answers), [], [0.0]
        out = acc.read_identity(page, wait_s, step_s, sleep=lambda s: (naps.append(s), now.__setitem__(0, now[0] + s)),
                                clock=lambda: now[0])
        return out, page, naps

    def test_it_waits_for_the_user_to_appear_instead_of_reading_once(self):
        empty, mine = "{}", json.dumps({"uniqueId": "shop.one", "nickName": "Shop", "region": "BD"})
        out, page, naps = self.read([empty, '{"err": "x"}', empty, mine])
        self.assertEqual(out["uniqueId"], "shop.one")
        self.assertEqual((page.calls, len(naps)), (4, 3), "asked again and again until the page named the user")

    def test_it_gives_up_after_the_time_is_up(self):
        out, page, naps = self.read([], wait_s=5.0, step_s=2.0)
        self.assertEqual(out, {})
        self.assertGreaterEqual(page.calls, 3)
        self.assertLessEqual(sum(naps), 6.0)

    def test_a_page_that_fails_or_answers_nonsense_is_just_not_ready_yet(self):
        mine = json.dumps({"uniqueId": "ok.user"})
        out, _p, _n = self.read([RuntimeError("detached"), "not json at all", None, mine])
        self.assertEqual(out["uniqueId"], "ok.user")


class TestIdentify(unittest.TestCase):
    def setUp(self):
        FakeBrowser.created = []
        self._patches = [mock.patch.object(acc.bfp, "CDPBrowser", FakeBrowser), mock.patch.object(acc.time, "sleep")]
        for p in self._patches:
            p.start()
        self.cookies = [{"name": "sessionid", "value": "v", "domain": ".tiktok.com"}]
        self._cookie_patch = mock.patch.object(acc, "_cookies", lambda ws: self.cookies)
        self._cookie_patch.start()

    def tearDown(self):
        self._cookie_patch.stop()
        for p in self._patches:
            p.stop()

    def run_identify(self, reads, browser=None):
        with mock.patch.object(acc, "read_identity", side_effect=reads) as ri:
            return acc.identify(r"C:\x\folder", browser), ri

    def test_a_user_found_on_a_later_load_is_a_logged_in_account(self):
        res, ri = self.run_identify([{}, {"uniqueId": "shop.one", "region": "BD"}])
        self.assertEqual((res["ok"], res["logged_in"], res["who"]["uniqueId"]), (True, True, "shop.one"))
        self.assertEqual(len(FakeBrowser.created[0].driver.gotos), 2, "the page was loaded again after a guest page")
        self.assertTrue(FakeBrowser.created[0].stopped)

    def test_no_user_but_the_cookie_still_there_is_unconfirmed_not_logged_out(self):
        res, _ri = self.run_identify([{}] * acc.IDENTIFY_ATTEMPTS)
        self.assertEqual((res["ok"], res["logged_in"], res["who"]), (True, None, None))
        self.assertEqual(len(FakeBrowser.created[0].driver.gotos), acc.IDENTIFY_ATTEMPTS)

    def test_no_user_and_no_cookie_is_logged_out(self):
        self.cookies = [{"name": "ttwid", "value": "v", "domain": ".tiktok.com"}]       # a visitor cookie, not a session
        res, _ri = self.run_identify([{}] * acc.IDENTIFY_ATTEMPTS)
        self.assertEqual((res["ok"], res["logged_in"], res["who"]), (True, False, None))

    def test_the_browser_that_made_the_profile_is_the_one_that_opens_it(self):
        self.run_identify([{"uniqueId": "x"}], browser=r"C:\chrome\chrome.exe")
        self.assertEqual(FakeBrowser.created[0].executable, r"C:\chrome\chrome.exe")
        self.assertTrue(FakeBrowser.created[0].headless)

    def test_a_browser_that_cannot_start_is_a_failed_check_not_a_verdict(self):
        class Broken(FakeBrowser):
            def start(self):
                raise RuntimeError("no devtools endpoint")
        with mock.patch.object(acc.bfp, "CDPBrowser", Broken):
            res = acc.identify(r"C:\x\folder")
        self.assertEqual((res["ok"], res["who"]), (False, None))
        self.assertIn("no devtools endpoint", res["error"])

    def test_a_crash_while_reading_still_stops_the_browser(self):
        with mock.patch.object(acc, "read_identity", side_effect=RuntimeError("tab crashed")):
            res = acc.identify(r"C:\x\folder")
        self.assertEqual(res["ok"], False)
        self.assertTrue(FakeBrowser.created[0].stopped, "no headless browser is left running")

    def test_the_process_wide_profile_variable_is_never_touched_so_it_can_run_beside_the_worker(self):
        class Watching(FakeBrowser):
            def __init__(self, *a, **k):
                self.env_seen = acc.os.environ.get("BFP_USER_DATA_DIR")
                super().__init__(*a, **k)
                self.kw = k
        before = acc.os.environ.get("BFP_USER_DATA_DIR")
        with mock.patch.object(acc.bfp, "CDPBrowser", Watching), mock.patch.object(acc, "read_identity", return_value={"uniqueId": "x"}):
            acc.identify(r"C:\x\folder", r"C:\b\brave.exe")
        b = FakeBrowser.created[-1]
        self.assertEqual(b.env_seen, before, "the environment variable was not set for the launch")
        self.assertEqual(acc.os.environ.get("BFP_USER_DATA_DIR"), before)
        self.assertEqual(str(b.kw["user_data_dir"]), r"C:\x\folder", "the folder is handed over explicitly")
        self.assertIs(b.preserve_real_identity, True, "the profile keeps its natural identity")

    def test_the_whole_check_is_bounded_in_time(self):
        clock = [0.0]
        real_monotonic = acc.time.monotonic
        with mock.patch.object(acc.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(acc, "read_identity", side_effect=lambda *a, **k: (clock.__setitem__(0, clock[0] + 9), {})[1]):
            res = acc.identify(r"C:\x\folder", max_s=20.0)
        self.assertEqual((res["ok"], res["logged_in"]), (True, None))
        self.assertLessEqual(len(FakeBrowser.created[-1].driver.gotos), 2, "stopped loading pages when the time budget ran out")
        self.assertTrue(FakeBrowser.created[-1].stopped)
        self.assertIsNotNone(real_monotonic)


class TestRegistryIsReadReliably(ManagerCase):
    """Found by a flaky test: a reader that meets the writer's file swap used to see NO accounts at all."""

    def test_a_read_that_collides_with_the_writer_is_retried_not_taken_for_an_empty_list(self):
        self.seed("real-account-1", "a.a")
        real = pathlib.Path.read_text
        fails = [PermissionError("sharing violation"), PermissionError("sharing violation")]

        def flaky(self_path, *a, **k):
            if self_path == config.ACCOUNTS_FILE and fails:
                raise fails.pop(0)
            return real(self_path, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", flaky), mock.patch("time.sleep"):
            self.assertEqual([a["name"] for a in config.load_accounts()], ["real-account-1"])
        self.assertEqual(fails, [], "both collisions happened and were survived")

    def test_a_registry_that_stays_unreadable_ends_as_empty_without_hanging(self):
        self.seed("real-account-1", "a.a")
        real = pathlib.Path.read_text

        def always(self_path, *a, **k):
            if self_path == config.ACCOUNTS_FILE:
                raise PermissionError("locked")
            return real(self_path, *a, **k)
        with mock.patch.object(pathlib.Path, "read_text", always), mock.patch("time.sleep") as nap:
            self.assertEqual(config.load_accounts(), [])
            self.assertEqual(config.registry_value("first", "none"), "none")
        self.assertLessEqual(nap.call_count, 12, "bounded retries")

    def test_no_registry_at_all_is_an_instant_empty_answer(self):
        self.assertFalse(config.ACCOUNTS_FILE.exists())
        with mock.patch("time.sleep") as nap:
            self.assertEqual((config.load_accounts(), config.registry_value("x")), ([], None))
        nap.assert_not_called()

    def test_a_corrupt_registry_is_empty_not_an_exception(self):
        config.ACCOUNTS_FILE.write_text("{not json", encoding="utf-8")
        with mock.patch("time.sleep"):
            self.assertEqual(config.load_accounts(), [])
        config.ACCOUNTS_FILE.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(config.load_accounts(), [], "valid JSON of the wrong shape")

    def test_a_write_that_collides_with_a_reader_is_retried(self):
        calls = []
        real = acc.os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError("the file is in use")
            return real(src, dst)
        with mock.patch.object(acc.os, "replace", flaky), mock.patch.object(acc.time, "sleep"):
            acc._write_registry([{"name": "real-account-1", "user_data_dir": "x"}])
        self.assertEqual(len(calls), 3)
        self.assertEqual([a["name"] for a in config.load_accounts()], ["real-account-1"])

    def test_a_write_that_never_succeeds_says_so_instead_of_silently_losing_the_change(self):
        with mock.patch.object(acc.os, "replace", side_effect=PermissionError("locked")), mock.patch.object(acc.time, "sleep"):
            with self.assertRaises(PermissionError):
                acc._write_registry([{"name": "real-account-1", "user_data_dir": "x"}])

    def test_concurrent_readers_and_writers_never_see_a_half_empty_list(self):
        for i in range(1, 4):
            self.seed(f"real-account-{i}", f"user{i}")
        stop, seen_bad = threading.Event(), []

        def reader():
            while not stop.is_set():
                if len(config.load_accounts()) != 3:
                    seen_bad.append(1)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            for n in range(60):
                acc.record_session(f"real-account-{1 + n % 3}", "expired" if n % 2 == 0 else "ok")
        finally:
            stop.set()
            for t in threads:
                t.join(5)
        self.assertEqual(len(seen_bad), 0, "a reader saw a list that was not complete")


class TestFolderSafety(ManagerCase):
    def test_only_folders_inside_the_accounts_directory_can_be_deleted(self):
        outside = self.tmp / "my-documents" / "User Data"
        outside.mkdir(parents=True)
        (outside / "precious.txt").write_text("keep me")
        self.assertFalse(acc.remove_profile_folder(outside))
        self.assertFalse(acc.remove_profile_folder(config.ACCOUNTS_DIR), "never the whole accounts folder")
        self.assertFalse(acc.remove_profile_folder(config.BFP_MASTER_USER_DATA_DIR), "never the master folder")
        self.assertFalse(acc.remove_profile_folder(self.tmp / "real" / ".." / "my-documents"), "no path tricks")
        self.assertTrue((outside / "precious.txt").exists())

    def test_a_folder_inside_is_removed_as_a_whole_and_a_missing_one_is_fine(self):
        udd = config.ACCOUNTS_DIR / "real-account-9" / "User Data"
        udd.mkdir(parents=True)
        (udd / "Cookies").write_text("x")
        self.assertTrue(acc.remove_profile_folder(udd))
        self.assertFalse((config.ACCOUNTS_DIR / "real-account-9").exists())
        self.assertTrue(acc.remove_profile_folder(udd), "already gone")


if __name__ == "__main__":
    unittest.main()
