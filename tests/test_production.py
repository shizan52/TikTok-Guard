"""What makes the desktop app safe to leave running: nothing is lost when it is closed mid-send, a person can see what it is doing,
problems end up in a log, and a computer that is not ready says so."""
import json
import logging
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _helpers import make_ai, tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import health, logs
from app.controller import INTERRUPTED_ERROR
from app.service import Service

ROOT = pathlib.Path(__file__).resolve().parents[1]
BS = chr(92)


def make_approval(db, aid="a1", status="posting", **extra):
    db.create_approval({"id": aid, "cid": f"c-{aid}", "video_id": "v1", "video_url": "https://www.tiktok.com/@x/video/v1",
                        "username": "ana", "comment_text": "preco?", "reply": "Obrigado!", "status": status, "kind": "link_reply",
                        **extra})
    return aid


class TestAClosedAppLeavesNothingSpinning(unittest.TestCase):
    def test_a_card_that_was_being_sent_when_the_app_stopped_says_so_and_waits_for_a_person(self):
        db = tmp_db()
        make_approval(db, "a1", "posting")
        make_approval(db, "a2", "pending")
        make_approval(db, "a3", "posted")
        svc = Service(db, telegram=False, monitor=False)
        svc.start()
        try:
            a1 = db.get_approval("a1")
            self.assertEqual(a1["status"], "post_failed", "nobody is sending it any more")
            self.assertEqual(a1["last_error"], INTERRUPTED_ERROR)
            self.assertEqual(db.get_approval("a2")["status"], "pending", "other cards are left alone")
            self.assertEqual(db.get_approval("a3")["status"], "posted")
            view = svc.controller.approval("a1")
            self.assertTrue(view["interrupted"], "the screen shows 'not sure this was sent' with a link to look")
            self.assertTrue(view["can"]["post"], "a person may still send it again - after looking")
            self.assertIn("post_interrupted", [e["kind"] for e in db.recent_events(5)])
        finally:
            svc.stop()

    def test_an_ordinary_failure_is_not_called_interrupted(self):
        db = tmp_db()
        make_approval(db, "a1", "post_failed", last_error="the comment box did not appear")
        self.assertFalse(Service(db, telegram=False, monitor=False).controller.approval("a1")["interrupted"])

    def test_nothing_is_touched_when_nothing_was_interrupted(self):
        db = tmp_db()
        make_approval(db, "a1", "posted")
        self.assertEqual(Service(db, telegram=False, monitor=False).controller.recover_interrupted(), [])


class TestSeeingWhatIsHappening(unittest.TestCase):
    def setUp(self):
        self.db = tmp_db()
        self.svc = Service(self.db, telegram=False, monitor=False)
        self.ctl = self.svc.controller

    def test_an_idle_app_is_not_busy(self):
        self.assertFalse(self.svc.busy()["busy"])
        self.assertEqual(self.ctl.activity(), {"busy": False, "sending": [], "reading": None, "checking": []})

    def test_a_reply_on_its_way_shows_the_browsers_current_step_and_how_long_it_has_been(self):
        make_approval(self.db, "a1", "posting")
        self.svc._set_stage("a1", "Finding the comment...")
        activity = self.ctl.activity()
        self.assertTrue(activity["busy"])
        self.assertEqual(activity["sending"][0]["text"], "Finding the comment...")
        self.assertEqual(activity["sending"][0]["username"], "ana")
        self.assertGreaterEqual(activity["sending"][0]["seconds"], 0)
        self.assertEqual(self.ctl.approval("a1")["progress"]["text"], "Finding the comment...", "the card itself carries it too")
        self.assertTrue(self.svc.busy()["busy"])

    def test_a_queued_reply_with_no_step_yet_says_it_is_waiting_for_the_browser(self):
        make_approval(self.db, "a1", "posting")
        self.assertEqual(self.ctl.activity()["sending"][0]["text"], "Waiting for the browser...")

    def test_the_step_is_forgotten_when_the_send_ends_either_way(self):
        sent = []
        make_approval(self.db, "a1", "posting")
        self.svc._post_fn = lambda a: (sent.append(a["_stage"]), {"ok": False, "error": "x"})[1]
        self.svc._memory_check = lambda: (True, 9000.0)
        self.svc.do_post("a1")
        self.assertEqual(self.svc.send_stage("a1"), None)
        self.assertEqual(self.db.get_approval("a1")["status"], "post_failed")
        sent[0]("later call is harmless")                          # a stray callback after the end must not break anything

    def test_the_post_function_is_given_a_way_to_report_its_steps(self):
        seen = []
        make_approval(self.db, "a1", "posting")
        self.svc._memory_check = lambda: (True, 9000.0)

        def poster(a):
            a["_stage"]("Typing the reply...")
            seen.append(self.svc.send_stage("a1")["text"])
            return {"ok": True, "profile": "acc"}
        self.svc._post_fn = poster
        self.svc.do_post("a1")
        self.assertEqual(seen, ["Typing the reply..."])
        self.assertEqual(self.db.get_approval("a1")["status"], "posted")

    def test_a_post_being_read_is_activity_too(self):
        self.svc.analyzer.current = lambda: {"id": "j1", "finished": False, "message": "Reading comments... 14 of 26",
                                             "started_at": time.time() - 12, "progress": {"read": 14, "total": 26}, "url": "u"}
        reading = self.ctl.activity()["reading"]
        self.assertEqual(reading["message"], "Reading comments... 14 of 26")
        self.assertGreaterEqual(reading["seconds"], 12)
        self.assertTrue(self.svc.busy()["reading"])

    def test_the_worker_job_in_flight_is_known(self):
        started, release = threading.Event(), threading.Event()
        self.svc._memory_check = lambda: (True, 9000.0)
        self.svc.start()
        try:
            self.svc.run_background(lambda: (started.set(), release.wait(5)))
            self.assertTrue(started.wait(5))
            state = self.svc.busy()
            self.assertTrue(state["busy"])
            self.assertEqual(state["job"]["kind"], "call")
            release.set()
            end = time.monotonic() + 5
            while self.svc.busy()["busy"] and time.monotonic() < end:
                time.sleep(0.02)
            self.assertFalse(self.svc.busy()["busy"])
        finally:
            release.set()
            self.svc.stop()

    def test_stopping_waits_for_a_send_in_flight_but_only_as_long_as_it_was_told(self):
        release = threading.Event()
        self.svc.start()
        try:
            self.svc.run_background(lambda: release.wait(10))
            time.sleep(0.4)
            t0 = time.monotonic()
            self.svc.stop(wait=0.6)                               # the job runs on: stop gives up after 0.6 s
            self.assertLess(time.monotonic() - t0, 3)
            self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        finally:
            release.set()


class TestMemoryAndTheKeptBrowser(unittest.TestCase):
    def test_a_machine_that_looks_full_gets_the_kept_browser_back_before_a_job_is_refused(self):
        """The kept browser is the cheapest memory to give back: a send must not fail 'short of memory' because of it."""
        from app import tiktok_monitor as tm
        svc = Service(tmp_db(), telegram=False, monitor=False)
        readings = iter([(False, 500.0), (True, 1600.0)])
        svc._memory_check = lambda: next(readings)
        with mock.patch.object(tm, "warm_account", return_value="acc1"), mock.patch.object(tm, "close_warm") as close:
            ok, free = svc._memory_ok()
        self.assertTrue(ok)
        close.assert_called_once_with()

    def test_a_job_that_will_reuse_the_kept_browser_needs_no_new_memory_and_does_not_close_it(self):
        from app import tiktok_monitor as tm
        svc = Service(tmp_db(), telegram=False, monitor=False)
        svc._memory_check = lambda: (False, 500.0)             # the kept browser itself is what makes the machine look full
        with mock.patch.object(tm, "warm_account", return_value="acc1"), mock.patch.object(tm, "close_warm") as close:
            self.assertEqual(svc._memory_ok(reuse="acc1"), (True, 500.0))
        close.assert_not_called()

    def test_a_job_for_another_account_still_gets_the_kept_browser_closed_to_make_room(self):
        from app import tiktok_monitor as tm
        svc = Service(tmp_db(), telegram=False, monitor=False)
        readings = iter([(False, 500.0), (True, 1500.0)])
        svc._memory_check = lambda: next(readings)
        with mock.patch.object(tm, "warm_account", return_value="acc1"), mock.patch.object(tm, "close_warm") as close:
            self.assertEqual(svc._memory_ok(reuse="acc2"), (True, 1500.0))
        close.assert_called_once_with()

    def test_the_account_a_job_will_try_first_is_the_chosen_one_else_the_first_of_the_normal_order(self):
        from app import tiktok_monitor as tm
        svc = Service(tmp_db(), telegram=False, monitor=False)
        self.assertEqual(svc._first_account({"account": "acc9"}), "acc9")
        tm.reset_walls()
        self.addCleanup(tm.reset_walls)
        with mock.patch.object(config, "account_chain", lambda: ["a1", "a2"]):
            self.assertEqual(svc._first_account({"account": None}), "a1")
            tm.mark_walled("a1")
            self.assertEqual(svc._first_account({}), "a2", "an account TikTok is refusing is not the first any more")

    def test_the_desktop_app_uses_a_memory_threshold_made_for_a_personal_computer_and_puts_it_back(self):
        from app.desktop import DesktopApp
        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(config, "MIN_FREE_MB", 900):
            import os
            os.environ.pop("MIN_FREE_MB", None)
            app = DesktopApp(db=tmp_db(), port=0, token="t" * 32)
            self.assertEqual(config.MIN_FREE_MB, 400)
            app.stop()
            self.assertEqual(config.MIN_FREE_MB, 900)

    def test_a_threshold_the_person_set_themselves_is_respected(self):
        from app.desktop import DesktopApp
        with mock.patch.dict("os.environ", {"MIN_FREE_MB": "1200"}), mock.patch.object(config, "MIN_FREE_MB", 1200):
            app = DesktopApp(db=tmp_db(), port=0, token="t" * 32)
            self.assertEqual(config.MIN_FREE_MB, 1200)
            app.stop()

    def test_without_a_kept_browser_a_full_machine_is_still_refused(self):
        from app import tiktok_monitor as tm
        svc = Service(tmp_db(), telegram=False, monitor=False)
        svc._memory_check = lambda: (False, 500.0)
        with mock.patch.object(tm, "warm_account", return_value=None), mock.patch.object(tm, "close_warm") as close:
            self.assertEqual(svc._memory_ok(), (False, 500.0))
        close.assert_not_called()


class TestTheWatchdog(unittest.TestCase):
    def test_a_job_that_never_returns_gets_its_browser_closed_so_the_worker_is_free_again(self):
        db = tmp_db()
        svc = Service(db, telegram=False, monitor=False, watchdog_s=0.3)
        release = threading.Event()
        closed = []
        with mock.patch("app.orphans.close_stale_browsers", side_effect=lambda dirs: closed.append(1) or []), \
                mock.patch("app.orphans.our_profile_dirs", return_value=["x"]):
            svc.start()
            try:
                svc.run_background(lambda: release.wait(10))
                end = time.monotonic() + 5
                while not closed and time.monotonic() < end:
                    time.sleep(0.05)
                self.assertEqual(closed, [1], "its browser was closed")
                time.sleep(0.6)
                self.assertEqual(closed, [1], "once per job, not every tick")
                self.assertIn("job_stuck", [e["kind"] for e in db.recent_events(5)])
            finally:
                release.set()
                svc.stop()

    def test_a_job_that_finishes_in_time_is_left_alone(self):
        svc = Service(tmp_db(), telegram=False, monitor=False, watchdog_s=5)
        done = threading.Event()
        with mock.patch("app.orphans.close_stale_browsers") as closer:
            svc.start()
            try:
                svc.run_background(done.set)
                self.assertTrue(done.wait(5))
                time.sleep(0.4)
                closer.assert_not_called()
            finally:
                svc.stop()

    def test_without_a_watchdog_setting_there_is_no_watchdog_thread(self):
        svc = Service(tmp_db(), telegram=False, monitor=False)
        svc.start()
        try:
            self.assertNotIn("watchdog", [t.name for t in svc._threads])
        finally:
            svc.stop()

    def test_the_desktop_app_has_one_by_default(self):
        from app.desktop import DesktopApp
        app = DesktopApp(db=tmp_db(), port=0, token="t" * 32)
        try:
            self.assertEqual(app.svc.watchdog_s, 900.0)
        finally:
            app.stop()


class TestTheLogFile(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="logs-"))
        self._saved = config.LOG_DIR
        config.LOG_DIR = self.dir
        self._hooks = (sys.excepthook, threading.excepthook)
        self.addCleanup(self.restore)

    def restore(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_guard_file", False):
                root.removeHandler(h)
                h.close()
        sys.excepthook, threading.excepthook = self._hooks
        config.LOG_DIR = self._saved

    def test_secrets_never_reach_the_file(self):
        logs.setup()
        logging.getLogger("t").warning("open http://127.0.0.1:8765/api/status?token=abc123SECRET&x=1 with Bearer abc.def-ghi and "
                                       "sk-or-v1-0123456789abcdef and 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
        for h in logging.getLogger().handlers:
            h.flush()
        text = (self.dir / logs.FILE_NAME).read_text(encoding="utf-8")
        for secret in ("abc123SECRET", "abc.def-ghi", "0123456789abcdef", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"):
            self.assertNotIn(secret, text)
        self.assertIn("token=***", text)

    def test_every_line_says_when_how_bad_which_thread_and_who(self):
        logs.setup()
        logging.getLogger("tiktok_monitor").info("read acc1 123: post opened (+9.1s, 12.0s in all)")
        for h in logging.getLogger().handlers:
            h.flush()
        line = logs.tail(5)[-1]
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ INFO\s+\[MainThread\] tiktok_monitor: read acc1 123")

    def test_setting_up_twice_does_not_write_every_line_twice(self):
        logs.setup()
        logs.setup()
        logging.getLogger("t").warning("once")
        for h in logging.getLogger().handlers:
            h.flush()
        self.assertEqual(sum("once" in line for line in logs.tail(20)), 1)

    def test_an_uncaught_error_in_any_thread_ends_up_in_the_log_with_its_traceback(self):
        logs.setup()
        t = threading.Thread(target=lambda: 1 / 0, name="worker-x")
        with mock.patch.object(sys, "__excepthook__"):
            t.start()
            t.join()
        for h in logging.getLogger().handlers:
            h.flush()
        text = "\n".join(logs.tail(30))
        self.assertIn("uncaught error in thread worker-x", text)
        self.assertIn("ZeroDivisionError", text)

    def test_tail_masks_secrets_that_an_older_version_wrote_and_survives_a_missing_file(self):
        self.assertEqual(logs.tail(), [])
        (self.dir / logs.FILE_NAME).write_text("old line token=SECRET123\nnew line\n", encoding="utf-8")
        self.assertEqual(logs.tail(5), ["old line token=***", "new line"])
        self.assertEqual(logs.tail(1), ["new line"])


class TestIsTheComputerReady(unittest.TestCase):
    def status(self, items, cid):
        return next(i["status"] for i in items if i["id"] == cid)

    def test_a_ready_computer_has_nothing_to_fix(self):
        db = tmp_db()
        db.set_setting("company_context", "We open 9 to 6.")
        with mock.patch.object(config, "OPENROUTER_API_KEY", "sk-or-v1-real"), \
                mock.patch.object(health.sysguard, "enough_memory", return_value=(True, 8000.0)), \
                mock.patch.object(health.shutil, "disk_usage", return_value=mock.Mock(free=50 * 1024 ** 3)), \
                mock.patch.object(health.browsers, "find_browsers", return_value=[{"kind": "chrome", "name": "Google Chrome", "path": "c"}]), \
                mock.patch.object(health, "_internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "ok", "detail": "", "fix": ""}):
            items = health.run(db, accounts={"total": 2, "usable": 2, "ready": 1, "need_login": 0, "unchecked": 1})
        self.assertEqual(health.worst(items), "ok", health.report(items))

    def test_what_is_missing_is_named_with_the_way_to_fix_it(self):
        db = tmp_db()
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""), mock.patch.object(health.browsers, "find_browsers", return_value=[]):
            items = health.run(db, accounts={"total": 0, "usable": 0, "ready": 0, "need_login": 0, "unchecked": 0}, internet=False)
        self.assertEqual(self.status(items, "browser"), "fail")
        self.assertEqual(self.status(items, "accounts"), "fail")
        self.assertEqual(self.status(items, "ai_key"), "warn")
        self.assertEqual(self.status(items, "company"), "warn")
        self.assertEqual(health.worst(items), "fail")
        report = health.report(items)
        self.assertIn("Add account", report)
        self.assertIn("OpenRouter", report)
        for item in items:
            if item["status"] != "ok":
                self.assertTrue(item["fix"], f"{item['id']} says what is wrong but not what to do")

    def test_the_placeholder_key_from_the_example_file_does_not_count_as_a_key(self):
        with mock.patch.object(config, "OPENROUTER_API_KEY", "sk-or-v1-your-key-goes-here"):
            self.assertEqual(health._ai_key()["status"], "warn")

    def test_every_account_logged_out_is_a_warning_with_the_fix(self):
        item = health._accounts({"total": 2, "usable": 2, "ready": 0, "need_login": 2, "unchecked": 0})
        self.assertEqual(item["status"], "warn")
        self.assertIn("Log in again", item["fix"])

    def test_no_internet_is_a_warning_not_a_crash(self):
        with mock.patch.object(health.socket, "create_connection", side_effect=OSError("unreachable")):
            self.assertEqual(health._internet()["status"], "warn")

    def test_low_memory_and_disk_are_warnings(self):
        with mock.patch.object(health.sysguard, "enough_memory", return_value=(False, 310.0)):
            self.assertEqual(health._memory()["status"], "warn")
        with mock.patch.object(health.shutil, "disk_usage", return_value=mock.Mock(free=100 * 1048576)):
            self.assertEqual(health._disk()["status"], "warn")

    def test_an_unwritable_data_folder_is_a_failure(self):
        with mock.patch.object(health.tempfile, "NamedTemporaryFile", side_effect=PermissionError("denied")):
            self.assertEqual(health._data_writable()["status"], "fail")

    def test_the_summary_without_the_app_running_reads_the_registry(self):
        with mock.patch.object(config, "load_accounts", return_value=[{"name": "a", "session": "ok", "enabled": True},
                                                                       {"name": "b", "session": "expired", "enabled": True},
                                                                       {"name": "c", "enabled": False}]), \
                mock.patch.object(config, "registry_value", return_value="ok"):
            self.assertEqual(health.accounts_summary(), {"total": 4, "usable": 3, "ready": 2, "need_login": 1, "unchecked": 0})


class TestBrowsersAnEarlierRunLeftBehind(unittest.TestCase):
    MASTER = "F:" + BS + "spain" + BS + "network" + BS + "bfp_profiles" + BS + "master-brave-profile" + BS + "User Data"
    ACC1 = "F:" + BS + "spain" + BS + "data" + BS + "real" + BS + "real-account-1" + BS + "User Data"

    def proc(self, pid, name, folder, *flags, quote=True):
        folder_arg = f'--user-data-dir="{folder}"' if quote else f"--user-data-dir={folder}"
        return {"pid": pid, "name": name, "cmdline": f'"C:' + BS + f'Program Files' + BS + f'{name}" ' + " ".join(flags) + " " + folder_arg + " --no-first-run"}

    def test_the_folder_is_read_from_the_command_line_quoted_or_not(self):
        from app import orphans
        self.assertEqual(orphans.user_data_dir_of(self.proc(1, "brave.exe", self.MASTER)["cmdline"]), self.MASTER)
        self.assertEqual(orphans.user_data_dir_of(self.proc(1, "brave.exe", self.ACC1, quote=False)["cmdline"]), self.ACC1)
        self.assertIsNone(orphans.user_data_dir_of("brave.exe --no-first-run"))
        whole_arg_quoted = 'brave.exe "--user-data-dir=' + self.ACC1 + '" --no-first-run'      # how Windows shows it for a path with a space
        self.assertEqual(orphans.user_data_dir_of(whole_arg_quoted), self.ACC1)

    def test_only_automation_browsers_on_our_own_folders_are_chosen(self):
        from app import orphans
        ours = [self.MASTER, self.ACC1]
        processes = [
            self.proc(10, "brave.exe", self.MASTER, "--remote-debugging-port=9222"),                # left over: ours
            self.proc(11, "chrome.exe", self.ACC1, "--headless=new", "--remote-debugging-port=0"),   # left over: ours
            self.proc(20, "brave.exe", self.MASTER),                                                # the person's own window on that folder
            self.proc(21, "chrome.exe", "C:" + BS + "Users" + BS + "me" + BS + "AppData" + BS + "Chrome" + BS + "User Data", "--remote-debugging-port=9222"),
            self.proc(22, "msedge.exe", "F:" + BS + "spain" + BS + "data" + BS + "ui-profile"),     # the app's own window
            self.proc(30, "python.exe", self.MASTER, "--remote-debugging-port=1"),                  # not a browser
            {"pid": None, "name": "chrome.exe", "cmdline": ""},
        ]
        self.assertEqual([p["pid"] for p in orphans.select_stale(processes, ours)], [10, 11])

    def test_the_comparison_ignores_case_and_a_trailing_slash(self):
        from app import orphans
        p = self.proc(10, "chrome.exe", self.ACC1.upper() + BS, "--remote-debugging-port=9222")
        self.assertEqual(len(orphans.select_stale([p], [self.ACC1])), 1)

    def test_this_process_is_never_chosen(self):
        import os
        from app import orphans
        p = self.proc(os.getpid(), "chrome.exe", self.ACC1, "--remote-debugging-port=1")
        self.assertEqual(orphans.select_stale([p], [self.ACC1], own_pid=os.getpid()), [])

    def test_closing_leftovers_reports_what_it_closed_and_survives_a_failure(self):
        from app import orphans
        processes = [self.proc(10, "brave.exe", self.MASTER, "--remote-debugging-port=1"),
                     self.proc(11, "chrome.exe", self.ACC1, "--remote-debugging-port=2")]
        closed = orphans.close_stale_browsers([self.MASTER, self.ACC1], lister=lambda: processes, killer=lambda pid: pid == 11)
        self.assertEqual(closed, [11], "10 could not be closed: it is not reported as closed, and nothing raised")
        self.assertEqual(orphans.close_stale_browsers([self.MASTER], lister=lambda: [], killer=lambda pid: True), [])

    def test_a_system_that_cannot_be_asked_is_never_a_reason_not_to_start(self):
        from app import orphans
        with mock.patch.object(orphans.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertEqual(orphans.list_processes(), [])

    def test_the_desktop_app_cleans_up_at_start_and_a_failure_there_does_not_stop_it(self):
        from app.desktop import DesktopApp
        with mock.patch.object(Service, "do_maintenance"),                 mock.patch("app.orphans.close_stale_browsers", side_effect=RuntimeError("boom")) as clean:
            app = DesktopApp(db=tmp_db(), port=0, token="t" * 32)
            try:
                app.start()
            finally:
                app.stop()
        clean.assert_called_once()


class TestClosingTheAppMidSend(unittest.TestCase):
    def test_closing_the_window_while_a_reply_is_on_its_way_lets_it_finish_and_leaves_no_half_done_card(self):
        import importlib.util
        from app.desktop import DesktopApp
        started, release = threading.Event(), threading.Event()
        db = tmp_db()

        def poster(a):
            started.set()
            release.wait(20)
            return {"ok": True, "profile": "acc"}
        make_approval(db, "s1", "pending")
        patch = mock.patch.object(Service, "do_maintenance")              # the start-up tidy-up would touch the real profile folders
        patch.start()
        self.addCleanup(patch.stop)
        posting = mock.patch.object(config, "POSTING_ENABLED", True)      # this test needs posting ON, whatever the local .env says
        posting.start()
        self.addCleanup(posting.stop)
        app = DesktopApp(db=db, poster=poster, port=0, token="t" * 32, clean_leftovers=False)
        app.svc._memory_check = lambda: (True, 9000.0)
        app.start()
        try:
            app.controller.request_post("s1")
            self.assertTrue(started.wait(10))
            self.assertTrue(app.sending(), "the app knows a reply is on its way")
            from app import launcher
            said = []
            threading.Timer(1.0, release.set).start()                        # the reply finishes one second later
            t0 = time.monotonic()
            launcher.close_safely(app, wait_s=15, say=said.append)           # the window was closed
            self.assertGreaterEqual(time.monotonic() - t0, 0.8, "it waited for the reply")
            self.assertLess(time.monotonic() - t0, 10)
            self.assertEqual(db.get_approval("s1")["status"], "posted", "the reply went out and is recorded")
            self.assertIn("still being sent", said[0])
        finally:
            release.set()
            app.stop()


class TestTheDatabaseDoesNotKeepConnectionsOfFinishedThreads(unittest.TestCase):
    """Every HTTP request runs in a thread of its own, and every thread used to leave one open sqlite connection behind for good
    (60 dashboard requests = 61 connections); close() could not close them either, so the file stayed locked."""

    def test_a_finished_thread_leaves_no_connection_behind(self):
        import gc
        db = tmp_db()
        db.get_setting("x")
        baseline = len(db._conns)
        for _ in range(25):
            t = threading.Thread(target=lambda: db.get_setting("x"))
            t.start()
            t.join()
        gc.collect()
        self.assertEqual(len(db._conns), baseline)
        db.close()

    def test_close_closes_the_connections_other_threads_made_so_the_file_can_be_deleted(self):
        import os
        db = tmp_db()
        done = threading.Event()
        holder = threading.Thread(target=lambda: (db.get_setting("x"), done.set(), time.sleep(2)))      # a thread that is still alive
        holder.start()
        self.assertTrue(done.wait(5))
        db.close()
        os.remove(db.path)                                       # a locked file would raise PermissionError on Windows
        holder.join()

    def test_an_open_connection_still_works_for_its_own_thread_and_a_closed_database_can_be_used_again(self):
        db = tmp_db()
        db.set_setting("k", "v")
        db.close()
        self.assertEqual(db.get_setting("k"), "v", "a new connection is made on demand")
        db.close()


class TestTheLauncher(unittest.TestCase):
    def launcher(self):
        from app import launcher
        return launcher

    def test_closing_the_window_while_a_reply_is_being_sent_waits_for_it(self):
        launcher, said = self.launcher(), []
        desk = mock.Mock()
        desk.sending.return_value = True
        launcher.close_safely(desk, wait_s=123, say=said.append)
        desk.stop.assert_called_once_with(wait=123)
        self.assertIn("still being sent", said[0])

    def test_closing_an_idle_app_just_stops_it(self):
        launcher, said = self.launcher(), []
        desk = mock.Mock()
        desk.sending.return_value = False
        launcher.close_safely(desk, say=said.append)
        desk.stop.assert_called_once_with()
        self.assertEqual(said, [])

    def test_ctrl_c_while_waiting_quits_anyway_and_says_what_that_means(self):
        launcher, said = self.launcher(), []
        desk = mock.Mock()
        desk.sending.return_value = True
        desk.stop.side_effect = [KeyboardInterrupt(), None]
        launcher.close_safely(desk, say=said.append)
        self.assertEqual(desk.stop.call_count, 2)
        self.assertIn("may or may not have reached TikTok", said[-1])

    def test_check_prints_the_report_and_fails_only_when_something_essential_is_missing(self):
        launcher = self.launcher()
        ok = [{"id": "x", "title": "T", "status": "ok", "detail": "fine", "fix": ""}]
        bad = [{"id": "y", "title": "B", "status": "fail", "detail": "gone", "fix": "install it"}]
        with mock.patch.object(health, "run", return_value=ok), mock.patch.object(health, "accounts_summary", return_value={}), \
                mock.patch("builtins.print") as out:
            self.assertEqual(launcher.check(), 0)
        out.assert_called_once()
        with mock.patch.object(health, "run", return_value=bad), mock.patch.object(health, "accounts_summary", return_value={}), \
                mock.patch("builtins.print"):
            self.assertEqual(launcher.check(), 1)


if __name__ == "__main__":
    unittest.main()
