"""What makes the program packageable as an exe a client can double-click: paths that work from a read-only bundle, defaults that
ship with the build, no phantom account on a fresh install, a launcher that can speak without a console, and a self-test."""
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _helpers import tmp_db  # noqa: F401  (also sets sys.path)

import config
from app import health, launcher, selftest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_frozen(code: str, *, home: pathlib.Path, bundle: pathlib.Path, extra_env: dict | None = None) -> dict:
    """Run `code` in a fresh interpreter that BELIEVES it is a PyInstaller exe (sys.frozen, sys._MEIPASS) - the only honest way to test
    config.py's module-level logic - with a clean environment (nothing of this machine's .env or settings leaks in)."""
    env = {k: v for k, v in os.environ.items() if k in ("SystemRoot", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "COMSPEC")}
    env.update({"TIKTOK_GUARD_HOME": str(home), "PYTHONUTF8": "1", **(extra_env or {})})
    prelude = (f"import sys; sys.frozen = True; sys._MEIPASS = {str(bundle)!r}; sys.path.insert(0, {str(ROOT)!r}); "
               "import urllib.request as _u\n"
               "def _no_network(*a, **k): raise OSError('the network must not be touched at import time')\n"
               "_u.urlopen = _no_network\n")
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", prelude + code], capture_output=True, text=True, env=env, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr[-1500:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestAFrozenBuildKeepsItsDataInTheUsersFolder(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="tg-home-"))
        self.bundle = pathlib.Path(tempfile.mkdtemp(prefix="tg-bundle-"))
        (self.bundle / "client_defaults.env").write_text(
            "BRAND_NAME=Client Brand\nTARGET_PROFILE_URL=https://www.tiktok.com/@client\nBFP_HEADLESS=true\nPOSTING_ENABLED=true\n", encoding="utf-8")

    def probe(self, extra="", **kw):
        return run_frozen(
            "import config, json\n" + extra +
            "print(json.dumps({'frozen': config.FROZEN, 'root': str(config.ROOT), 'data': str(config.DATA_DIR), 'db': str(config.DB_FILE),"
            " 'shots': str(config.SCREENSHOTS_DIR), 'resource': str(config.RESOURCE_DIR), 'brand': config.BRAND_NAME,"
            " 'profile': config.TARGET_PROFILE_URL, 'headless': config.BFP_HEADLESS, 'posting': config.POSTING_ENABLED,"
            " 'master': config.master_enabled(), 'chain': config.account_chain(),"
            " 'data_exists': config.DATA_DIR.is_dir(), 'network_exists': config.NETWORK_DIR.is_dir()}))",
            home=self.home, bundle=self.bundle, **kw)

    def test_everything_the_person_creates_goes_to_their_own_folder_never_into_the_program_folder(self):
        got = self.probe()
        self.assertTrue(got["frozen"])
        self.assertEqual(pathlib.Path(got["root"]), self.home)
        for key in ("data", "db", "shots"):
            self.assertTrue(pathlib.Path(got[key]).is_relative_to(self.home), f"{key}: {got[key]}")
        self.assertEqual(pathlib.Path(got["resource"]), self.bundle, "the program's own files are read from the bundle")
        self.assertTrue(got["data_exists"] and got["network_exists"], "the folders are created on first start")

    def test_the_build_ships_its_defaults_and_the_persons_own_settings_win(self):
        got = self.probe()
        self.assertEqual((got["brand"], got["profile"], got["headless"], got["posting"]),
                         ("Client Brand", "https://www.tiktok.com/@client", True, True))
        (self.home / ".env").write_text("BRAND_NAME=Their Own Name\nPOSTING_ENABLED=false\n", encoding="utf-8")
        got = self.probe()
        self.assertEqual(got["brand"], "Their Own Name", "a .env in their folder overrides the build's default")
        self.assertFalse(got["posting"])
        self.assertTrue(got["headless"], "what they did not set keeps the build's default")

    def test_a_fresh_install_has_no_phantom_master_account(self):
        got = self.probe()
        self.assertFalse(got["master"])
        self.assertNotIn("tiktok-monitor", got["chain"], "the default master name is not an account nobody can log in to")
        self.assertEqual(got["chain"], [])

    def test_a_master_folder_in_the_env_file_brings_the_master_back(self):
        (self.home / ".env").write_text(f"BFP_USER_DATA_DIR={self.home / 'brave'}\nBFP_PROFILE_NAME=page-account\n", encoding="utf-8")
        got = self.probe()
        self.assertTrue(got["master"])
        self.assertEqual(got["chain"], ["page-account"])

    def test_importing_the_settings_never_touches_the_network(self):
        """The country lookup used to run (5 s timeout) every time the program started, and sent the person's address to a third
        party before they had done anything. The probe's urlopen raises: the import must still work."""
        got = self.probe()
        self.assertTrue(got["frozen"])

    def test_the_country_is_looked_up_the_first_time_it_is_needed_and_remembered(self):
        got = run_frozen(
            "import config, json, urllib.request as u\n"
            "calls = []\n"
            "class R:\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): return False\n"
            "    def read(self): return b'{\"countryCode\": \"BR\"}'\n"
            "u.urlopen = lambda *a, **k: (calls.append(1), R())[1]\n"
            "first, second = config.BFP_PROFILE_COUNTRY, config.BFP_PROFILE_COUNTRY\n"
            "print(json.dumps({'first': first, 'second': second, 'lookups': len(calls)}))",
            home=self.home, bundle=self.bundle)
        self.assertEqual(got, {"first": "BR", "second": "BR", "lookups": 1})

    def test_a_country_set_in_the_env_file_is_used_without_any_lookup(self):
        got = run_frozen("import config, json\nprint(json.dumps({'c': config.BFP_PROFILE_COUNTRY}))", home=self.home, bundle=self.bundle,
                         extra_env={"BFP_PROFILE_COUNTRY": "PY"})
        self.assertEqual(got["c"], "PY")


class TestTheMasterAccountIsOptionalForTheDesktopApp(unittest.TestCase):
    def test_the_server_always_has_its_master_the_desktop_app_only_when_a_folder_is_configured(self):
        with mock.patch.object(config, "MASTER_OPTIONAL", False), mock.patch.object(config, "BFP_MASTER_USER_DATA_DIR", ""):
            self.assertTrue(config.master_enabled())
        with mock.patch.object(config, "MASTER_OPTIONAL", True), mock.patch.object(config, "BFP_MASTER_USER_DATA_DIR", ""):
            self.assertFalse(config.master_enabled())
        with mock.patch.object(config, "MASTER_OPTIONAL", True), mock.patch.object(config, "BFP_MASTER_USER_DATA_DIR", "C:/profile"):
            self.assertTrue(config.master_enabled())

    def test_the_account_chain_leaves_out_a_master_that_does_not_exist(self):
        with mock.patch.object(config, "MASTER_OPTIONAL", True), mock.patch.object(config, "BFP_MASTER_USER_DATA_DIR", ""), \
                mock.patch.object(config, "BFP_PROFILE_NAME", "page"), mock.patch.object(config, "BFP_PROFILE_NAMES", ["page", "extra"]), \
                mock.patch.object(config, "load_accounts", return_value=[{"name": "real-account-1", "enabled": True}]):
            self.assertEqual(config.account_chain(), ["extra", "real-account-1"])

    def test_the_account_summary_does_not_count_a_master_that_does_not_exist(self):
        with mock.patch.object(config, "MASTER_OPTIONAL", True), mock.patch.object(config, "BFP_MASTER_USER_DATA_DIR", ""), \
                mock.patch.object(config, "load_accounts", return_value=[]):
            self.assertEqual(health.accounts_summary()["usable"], 0, "so a fresh install is told to add an account")

    def test_the_desktop_app_makes_the_master_optional_and_puts_it_back(self):
        from app.desktop import DesktopApp
        with mock.patch.object(config, "MASTER_OPTIONAL", False):
            app = DesktopApp(db=tmp_db(), port=0, token="t" * 32, clean_leftovers=False, backups=False, maintenance=False)
            self.assertTrue(config.MASTER_OPTIONAL)
            app.stop()
            self.assertFalse(config.MASTER_OPTIONAL)


class TestTheLauncherCanSpeakWithoutAConsole(unittest.TestCase):
    def test_a_missing_console_never_breaks_print(self):
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            self.assertFalse(launcher.prepare_streams(wants_console=False))
            print("this must not raise")
            sys.stderr.write("neither must this\n")

    def test_what_would_have_gone_to_the_console_goes_to_the_log(self):
        stream = launcher._LogStream(20)
        with self.assertLogs("console", "INFO") as logged:
            stream.write("first line\nsecond ")
            stream.write("line\n\n")
            stream.write("unfinished")
            stream.flush()
        self.assertEqual([r.getMessage() for r in logged.records], ["first line", "second line", "unfinished"])

    def test_a_log_that_cannot_take_the_line_does_not_make_the_stream_log_itself_forever(self):
        """With no handler on the "console" logger Python's last-resort handler writes to sys.stderr - which IS the stream."""
        stream = launcher._LogStream(logging.WARNING)
        with mock.patch.object(logging.root, "handlers", []), mock.patch.object(logging, "lastResort", logging.StreamHandler(stream)), \
                mock.patch.object(sys, "stderr", stream):
            stream.write("nobody is listening\n")                       # RecursionError before the guard
            stream.write("and again\n")
            stream.flush()

    def test_a_real_console_is_left_alone(self):
        before = (sys.stdout, sys.stderr)
        self.assertTrue(launcher.prepare_streams(wants_console=True))
        self.assertEqual((sys.stdout, sys.stderr), before)

    def test_without_a_console_the_person_is_told_in_a_message_box_and_a_console_gets_text(self):
        with mock.patch.object(launcher, "alert") as alert, mock.patch("builtins.print") as shown:
            launcher.make_teller(True)("A reply is still being sent")
            shown.assert_called_once_with("A reply is still being sent")
            alert.assert_not_called()
            launcher.make_teller(False)("A reply is still being sent")
        alert.assert_called_once()
        self.assertEqual(alert.call_args.kwargs["block"], False, "the message box must not hold the shutdown up")

    def test_a_message_box_that_cannot_be_shown_falls_back_to_print(self):
        with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch("builtins.print") as shown:
            launcher.alert("hello")
        shown.assert_called_once_with("TikTok Guard: hello")

    def test_a_non_blocking_message_box_runs_in_its_own_thread(self):
        shown = threading.Event()
        with mock.patch.object(launcher.sys, "platform", "linux"), mock.patch("builtins.print", side_effect=lambda *a: shown.set()):
            launcher.alert("hello", block=False)
            self.assertTrue(shown.wait(3))

    def test_a_report_without_a_console_is_written_to_a_file_and_shown_in_a_box(self):
        home = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(config, "ROOT", home), mock.patch.object(launcher, "alert") as alert:
            launcher.report_to_person("TikTok Guard - check", "[ OK ] Python", has_console=False)
        self.assertIn("[ OK ] Python", (home / "last-report.txt").read_text(encoding="utf-8"))
        self.assertIn("last-report.txt", alert.call_args.args[0])

    def test_a_report_with_a_console_is_just_printed(self):
        with mock.patch("builtins.print") as shown, mock.patch.object(launcher, "alert") as alert:
            launcher.report_to_person("t", "text", has_console=True)
        shown.assert_called_once_with("text")
        alert.assert_not_called()


class TestTheLauncherOptions(unittest.TestCase):
    def setUp(self):
        patch = mock.patch("app.logs.setup")               # main() starts the log file: never the real data/logs one, never twice
        patch.start()
        self.addCleanup(patch.stop)

    def test_version_prints_the_version(self):
        from app import __version__
        with mock.patch("builtins.print") as shown:
            self.assertEqual(launcher.main(["--version"]), 0)
        shown.assert_called_with(f"TikTok Guard {__version__}")

    def test_the_self_test_exit_code_says_whether_anything_failed(self):
        ok = [{"id": "a", "title": "A", "status": "ok", "detail": "", "fix": ""}]
        bad = [{"id": "b", "title": "B", "status": "fail", "detail": "broken", "fix": "fix it"}]
        with mock.patch.object(selftest, "run", return_value=ok), mock.patch("builtins.print"):
            self.assertEqual(launcher.main(["--self-test"]), 0)
        with mock.patch.object(selftest, "run", return_value=bad), mock.patch("builtins.print") as shown:
            self.assertEqual(launcher.main(["--self-test"]), 1)
        self.assertIn("fix it", shown.call_args.args[0])

    def test_a_start_that_fails_is_shown_to_the_person_with_the_log_file_not_swallowed(self):
        with mock.patch.object(launcher, "run", side_effect=RuntimeError("the port is blocked")), \
                mock.patch.object(launcher, "alert") as alert, self.assertLogs("desktop", "ERROR"):
            self.assertEqual(launcher.main([]), 1)
        text = alert.call_args.args[0]
        self.assertIn("could not start", text)
        self.assertIn("RuntimeError: the port is blocked", text)
        self.assertIn("desktop.log", text)
        self.assertEqual(alert.call_args.kwargs["kind"], "error")

    def test_every_run_gets_its_streams_prepared_and_a_one_shot_option_may_attach_to_the_console(self):
        with mock.patch.object(launcher, "prepare_streams", return_value=False) as prep, mock.patch.object(launcher, "check", return_value=0) as check:
            launcher.main(["--check"])
        prep.assert_called_once_with(wants_console=True)
        check.assert_called_once_with(has_console=False)
        with mock.patch.object(launcher, "prepare_streams", return_value=True) as prep, mock.patch.object(launcher, "run", return_value=0):
            launcher.main(["--no-window"])
        prep.assert_called_once_with(wants_console=False)


class TestTheInstallerCanTellTheAppIsRunning(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows mutexes")
    def test_a_running_app_holds_a_named_mutex_the_installer_can_see(self):
        import ctypes
        name = f"TikTokGuardTest{os.getpid()}{int(time.time())}"
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        self.assertFalse(kernel32.OpenMutexW(0x00100000, False, name), "not there before")      # SYNCHRONIZE
        self.assertTrue(launcher.hold_app_mutex(name))
        found = kernel32.OpenMutexW(0x00100000, False, name)
        self.assertTrue(found, "an installer asking 'is it running?' finds it")
        kernel32.CloseHandle(ctypes.c_void_p(found))

    def test_the_name_is_the_one_the_installer_script_checks(self):
        script = (ROOT / "buildexe" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn(f"AppMutex={launcher.APP_MUTEX}", script)


class TestTheBrowserTheClientActuallyUses(unittest.TestCase):
    CHROME = {"kind": "chrome", "name": "Google Chrome", "path": "C:/c/chrome.exe"}
    EDGE = {"kind": "edge", "name": "Microsoft Edge", "path": "C:/e/msedge.exe"}
    BRAVE = {"kind": "brave", "name": "Brave", "path": "C:/b/brave.exe"}

    def test_a_client_with_chrome_gets_chrome_for_the_app_window_even_though_edge_is_always_there(self):
        self.assertEqual(launcher.pick_window_browser([self.EDGE, self.CHROME]), self.CHROME)
        self.assertEqual(launcher.pick_window_browser([self.EDGE, self.BRAVE, self.CHROME]), self.CHROME)

    def test_without_chrome_it_falls_back_to_edge_then_brave(self):
        self.assertEqual(launcher.pick_window_browser([self.BRAVE, self.EDGE]), self.EDGE)
        self.assertEqual(launcher.pick_window_browser([self.BRAVE]), self.BRAVE)
        self.assertIsNone(launcher.pick_window_browser([]))

    def test_a_machine_with_only_chrome_uses_chrome_for_the_window_and_for_adding_accounts(self):
        from app import browsers
        only_chrome = {"PROGRAMFILES": "C:/pf", "PROGRAMFILES(X86)": "C:/pf86", "LOCALAPPDATA": "C:/la"}
        exists = lambda path: path.replace("\\", "/") == "C:/pf/Google/Chrome/Application/chrome.exe"
        with mock.patch.object(browsers, "_registry_paths", return_value=[]):
            installed = browsers.find_browsers(env=only_chrome, platform="win32", exists=exists)
            self.assertEqual([b["kind"] for b in installed], ["chrome"])
            self.assertEqual(browsers.choose("auto", env=only_chrome, platform="win32", exists=exists)["kind"], "chrome")
        self.assertEqual(launcher.pick_window_browser(installed)["kind"], "chrome")
        self.assertIn("--app=http://127.0.0.1:1/", launcher.window_command("http://127.0.0.1:1/", installed[0]["path"], pathlib.Path("C:/x")))


class TestTheAppWindowStaysPrivate(unittest.TestCase):
    def test_the_window_never_signs_in_or_syncs_with_the_persons_microsoft_account(self):
        """Seen on a real screen: a fresh Edge profile signed in by itself and announced 'we are now syncing your browsing data'."""
        command = launcher.window_command("http://127.0.0.1:8765/#token=abc", "C:/edge/msedge.exe", pathlib.Path("C:/x/ui-profile"))
        self.assertIn("--disable-sync", command)
        features = [a for a in command if a.startswith("--disable-features=")]
        self.assertEqual(len(features), 1, "two --disable-features flags: the last one silently wins")
        self.assertIn("msImplicitSignin", features[0])
        self.assertIn("--app=http://127.0.0.1:8765/#token=abc", command)
        self.assertTrue(any(a.startswith("--user-data-dir=") and "ui-profile" in a for a in command), "its own profile, never the person's")


class TestAWindowThatWasHandedOver(unittest.TestCase):
    PROFILE = pathlib.Path("C:/Users/x/AppData/Local/TikTok Guard/data/ui-profile")

    def proc(self, pid, folder):
        return {"pid": pid, "name": "msedge.exe", "cmdline": f'"msedge.exe" --app=http://127.0.0.1:8765 --user-data-dir="{folder}" --lang=en-US'}

    def test_only_browsers_standing_on_the_apps_own_window_folder_count(self):
        ours = self.proc(1, self.PROFILE)
        others = [self.proc(2, "C:/Users/x/AppData/Local/Microsoft/Edge/User Data"), {"pid": 3, "name": "python.exe", "cmdline": "python"}]
        self.assertEqual(launcher.window_processes([ours, *others], self.PROFILE), [ours])

    def test_waiting_ends_when_the_last_such_process_is_gone_and_not_before(self):
        states = iter([[self.proc(1, launcher.UI_PROFILE)], [self.proc(1, launcher.UI_PROFILE)], []])
        naps = []
        launcher.wait_until_window_gone(lister=lambda: next(states), sleep=naps.append, poll_s=2, first_wait_s=1)
        self.assertEqual(naps, [1, 2, 2], "a short first wait, then a look every few seconds until nothing is left")


class TestTheBundledLibrariesAreFoundNowhereElse(unittest.TestCase):
    def test_a_packaged_build_uses_the_bfp_inside_itself_and_never_goes_looking(self):
        from app import bfp_loader
        fake = mock.Mock(__file__=str(pathlib.Path(tempfile.gettempdir()) / "bundle" / "bfp.py"))
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.dict(sys.modules, {"bfp": fake}), \
                mock.patch.object(bfp_loader.os.environ, "get", side_effect=AssertionError("must not look at the environment")):
            self.assertEqual(bfp_loader.ensure_bfp_on_path(), pathlib.Path(fake.__file__).resolve().parent)


class TestProcessesWithAccentedFoldersAreRecognised(unittest.TestCase):
    """Brazilian (and many other) Windows user names have accents: a data folder under a user called "Usuário" must still be recognised."""

    @unittest.skipUnless(sys.platform == "win32", "Windows process listing")
    def test_a_browser_standing_on_a_folder_with_accents_is_found_by_that_folder(self):
        from app import orphans
        folder = pathlib.Path(tempfile.mkdtemp(prefix="Usuário ção ")) / "User Data"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={folder}", "--remote-debugging-port=1"])
        try:
            deadline, found = time.monotonic() + 30, []
            while time.monotonic() < deadline and not found:
                found = [p for p in orphans.list_processes() if p["pid"] == child.pid]
                time.sleep(0.5)
            self.assertTrue(found, "the process is listed")
            self.assertEqual(orphans.user_data_dir_of(found[0]["cmdline"]), str(folder))
            self.assertEqual([p["pid"] for p in launcher.window_processes(found, folder)], [child.pid],
                             "and recognised as standing on the app's own window folder")
            as_a_browser = [{**found[0], "name": "chrome.exe"}]                      # (only browsers are ever closed)
            self.assertEqual(orphans.select_stale(as_a_browser, [str(folder)], own_pid=os.getpid()), as_a_browser,
                             "a leftover automation browser on the person's own folder is found, whatever the accents")
        finally:
            child.kill()
            child.wait()


class TestTheAppDoesNotDependOnPath(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows tools")
    def test_system_tools_are_found_by_their_full_path_whatever_is_in_path(self):
        from app import orphans
        for name in ("powershell", "taskkill"):
            path = orphans.system_tool(name)
            self.assertTrue(os.path.isabs(path) and os.path.exists(path), path)

    @unittest.skipUnless(sys.platform == "win32", "Windows process listing")
    def test_processes_can_be_listed_from_a_bare_environment(self):
        """The packaged app can be started with almost nothing in PATH; it used to answer "could not list the running processes"."""
        from app import orphans
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--user-data-dir=C:/some/folder", "--remote-debugging-port=1"])
        try:
            with mock.patch.dict(os.environ, {"PATH": "C:/nonexistent"}):
                deadline, found = time.monotonic() + 30, []
                while time.monotonic() < deadline and not found:
                    found = [r for r in orphans.list_processes() if r["pid"] == child.pid]
                    time.sleep(0.5)
            self.assertTrue(found, "listed although PATH holds nothing useful")
        finally:
            child.kill()
            child.wait()


class TestNothingFlashesAConsoleWindow(unittest.TestCase):
    def test_every_helper_program_the_app_starts_is_started_without_a_window(self):
        from app import orphans
        self.assertEqual(orphans.NO_WINDOW, 0x08000000 if sys.platform == "win32" else 0)
        with mock.patch.object(orphans.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", returncode=0)
            orphans.list_processes()
            orphans.kill(123)
        for call in run.call_args_list:
            if sys.platform == "win32":
                self.assertEqual(call.kwargs.get("creationflags"), 0x08000000, call.args)


class TestAutomatedRunsNeverWaitForSomeoneToClickOK(unittest.TestCase):
    def test_with_the_no_dialog_switch_a_message_is_printed_not_shown(self):
        with mock.patch.dict(os.environ, {"TIKTOK_GUARD_NO_DIALOG": "1"}), mock.patch("builtins.print") as shown:
            launcher.alert("hello", kind="error")
        shown.assert_called_once_with("TikTok Guard: hello")

    def load_entry(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("tg_entry", ROOT / "buildexe" / "entry.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_start_that_fails_before_the_launcher_exists_is_still_reported_and_leaves_a_crash_file(self):
        entry, temp = self.load_entry(), tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"TIKTOK_GUARD_NO_DIALOG": "1", "TEMP": temp}), mock.patch.object(sys, "stderr") as err:
            try:
                raise PermissionError("cannot write to the data folder")
            except PermissionError as exc:
                self.assertEqual(entry.fatal(exc), 1)
        shown = "".join(str(c.args[0]) for c in err.write.call_args_list) or "".join(str(c) for c in err.method_calls)
        self.assertIn("could not start", shown)
        self.assertIn("PermissionError: cannot write to the data folder", shown)
        self.assertIn("PermissionError", pathlib.Path(temp, "TikTok Guard crash.txt").read_text(encoding="utf-8"))

    def test_the_entry_point_turns_any_import_time_error_into_that_report(self):
        entry = self.load_entry()
        with mock.patch.dict(sys.modules, {"app.launcher": None}), mock.patch.object(entry, "fatal", return_value=1) as fatal:
            self.assertEqual(entry.entry(), 1)                         # `from app.launcher import main` raises ImportError
        fatal.assert_called_once()

    def test_the_console_exe_runs_the_diagnostics_and_the_other_runs_the_app(self):
        entry = self.load_entry()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", "C:/x/TikTok Guard Diagnostics.exe"), \
                mock.patch.object(launcher, "diagnostics", return_value=7), mock.patch.object(launcher, "main", return_value=8):
            self.assertEqual(entry.entry(), 7)
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", "C:/x/TikTok Guard.exe"), \
                mock.patch.object(launcher, "diagnostics", return_value=7), mock.patch.object(launcher, "main", return_value=8):
            self.assertEqual(entry.entry(), 8)


class TestTheDiagnosticsReport(unittest.TestCase):
    def run_it(self, items):
        home = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(config, "ROOT", home), mock.patch.object(selftest, "run", return_value=items), \
                mock.patch.object(health, "run", return_value=[]), mock.patch.object(health, "accounts_summary", return_value={}), \
                mock.patch("app.logs.setup"), mock.patch("app.logs.tail", return_value=["a log line token=***"]), \
                mock.patch("builtins.print") as shown:
            code = launcher.diagnostics(wait=False)
        return code, home, "\n".join(str(c.args[0]) for c in shown.call_args_list if c.args)

    def test_everything_support_needs_is_on_screen_and_in_one_file(self):
        ok = [{"id": "a", "title": "Database", "status": "ok", "detail": "fine", "fix": ""}]
        code, home, shown = self.run_it(ok)
        saved = (home / "last-report.txt").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        for text in (shown, saved):
            for part in ("TikTok Guard 1.0.0", "SELF-TEST", "Database", "IS THIS COMPUTER READY?", "LOG (last 40 lines)", "a log line token=***"):
                self.assertIn(part, text)
        self.assertIn("last-report.txt", shown, "the person is told where the file is")

    def test_no_account_yet_is_not_a_defect_of_the_program(self):
        ok = [{"id": "a", "title": "Database", "status": "ok", "detail": "fine", "fix": ""}]
        home = pathlib.Path(tempfile.mkdtemp())
        no_account = [{"id": "accounts", "title": "TikTok accounts", "status": "fail", "detail": "none yet", "fix": "Add account"}]
        with mock.patch.object(config, "ROOT", home), mock.patch.object(selftest, "run", return_value=ok),                 mock.patch.object(health, "run", return_value=no_account), mock.patch.object(health, "accounts_summary", return_value={}),                 mock.patch("app.logs.setup"), mock.patch("builtins.print"):
            self.assertEqual(launcher.diagnostics(wait=False), 0)
        self.assertIn("Add account", (home / "last-report.txt").read_text(encoding="utf-8"), "but it is in the report")

    def test_a_failed_step_makes_the_exit_code_one(self):
        bad = [{"id": "b", "title": "Browser automation", "status": "fail", "detail": "cannot drive", "fix": "install Chrome"}]
        code, home, shown = self.run_it(bad)
        self.assertEqual(code, 1)
        self.assertIn("install Chrome", shown)


class TestTheSelfTest(unittest.TestCase):
    def test_the_program_files_check_passes_here_and_fails_clearly_when_the_screen_is_missing(self):
        self.assertEqual(selftest._program_files()["status"], "ok")
        empty = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(config, "RESOURCE_DIR", empty):
            got = selftest._program_files()
        self.assertEqual(got["status"], "fail")
        self.assertIn("desktop.html", got["detail"])
        self.assertIn("WHOLE folder", got["fix"])

    def test_the_database_check_works_here(self):
        self.assertEqual(selftest._database()["status"], "ok")

    def test_the_steps_leave_no_temporary_folders_behind(self):
        """An open database file made Windows refuse to delete its folder: every self-test left a pile of them in TEMP."""
        def folders():
            return set(pathlib.Path(tempfile.gettempdir()).glob("tg-selftest-*"))
        before = folders()
        selftest._database()
        selftest._the_app()
        self.assertEqual(folders() - before, set())

    def test_without_a_browser_the_automation_step_says_what_to_install(self):
        with mock.patch.object(selftest.browsers, "choose", return_value=None):
            got = selftest._automation()
        self.assertEqual(got["status"], "fail")
        self.assertIn("Chrome", got["fix"])

    def test_a_browser_that_cannot_be_driven_is_reported_and_the_environment_is_put_back(self):
        os.environ.pop("BFP_USER_DATA_DIR", None)
        with mock.patch.object(selftest.browsers, "choose", return_value={"name": "Fake", "path": "C:/nope/browser.exe", "kind": "chrome"}):
            got = selftest._automation()
        self.assertEqual(got["status"], "fail")
        self.assertIn("could not be driven", got["detail"])
        self.assertNotIn("BFP_USER_DATA_DIR", os.environ, "the test must not leave its settings behind")

    def test_the_app_step_starts_the_real_server_and_stops_it(self):
        got = selftest._the_app()
        self.assertEqual((got["status"], got["detail"]), ("ok", "The app's server started, answered, showed its screen and stopped."))

    def test_a_missing_browser_skips_the_automation_step_instead_of_failing_it_twice(self):
        no_browser = {"id": "browser", "title": "A web browser", "status": "fail", "detail": "none", "fix": "install"}
        with mock.patch.object(health, "_browsers", return_value=no_browser), mock.patch.object(selftest, "_automation") as drive, \
                mock.patch.object(selftest, "_the_app", return_value={"id": "app", "title": "x", "status": "ok", "detail": "", "fix": ""}), \
                mock.patch.object(health, "_internet", return_value={"id": "internet", "title": "x", "status": "ok", "detail": "", "fix": ""}):
            items = selftest.run()
        drive.assert_not_called()
        self.assertIn("Skipped", [i for i in items if i["id"] == "automation"][0]["detail"])

    @unittest.skipUnless(any(b["kind"] in ("chrome", "edge", "brave") for b in __import__("app.browsers", fromlist=["x"]).find_browsers()),
                         "needs Chrome, Edge or Brave")
    @unittest.skipUnless(__import__("importlib.util", fromlist=["x"]).find_spec("patchright"),
                         "needs the browser engine (bfp + patchright), which is not part of this repository")
    def test_a_real_browser_is_started_driven_and_closed(self):
        got = selftest._automation()
        self.assertEqual(got["status"], "ok", got["detail"])
        self.assertIn("started, driven and closed", got["detail"])


if __name__ == "__main__":
    unittest.main()
