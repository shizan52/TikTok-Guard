"""TikTok guard - the desktop application's launcher (`python app.py`, or the packaged `TikTok Guard.exe`).

    TikTok Guard.exe                 open the app (a window of its own; closing it stops everything)
    TikTok Guard.exe --no-window     just run it and print the address (open it in any browser)
    TikTok Guard.exe --check         check that this computer is ready (browser, accounts, AI key ...) and exit
    TikTok Guard.exe --self-test     really start a browser and the app and look at both - for support - and exit
    TikTok Guard.exe --version

Paste the link of any TikTok post on the home page: the app reads its comments, shows the negative ones, writes a reply
with the AI if you want, sends it from the account you choose and shows the screenshot as proof. Nothing runs in the
background: no monitoring, no notifications, no Telegram - the app does what you ask, when you ask.

The window is a Chromium "app window" (Edge, Chrome or Brave, whichever is installed) with a profile folder of its own
(data/ui-profile) - it is not your normal browser, it has no address bar, and your own browsing is never touched.

The packaged exe has NO console window. So everything a person must know is shown in a message box (a reply that is still
being sent, a start-up that failed), everything else goes to data/logs/desktop.log, and the command-line options above attach to
the console they were started from (or, started by a double-click, write their report to a file and show it in a message box).
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import config
from app import browsers

UI_PROFILE = config.DATA_DIR / "ui-profile"
CLOSE_WAIT_S = 600                  # how long a reply that is being sent may keep the app alive after its window was closed
LOG = logging.getLogger("desktop")
# The window opens in the browser the person most likely uses (the "Add account" window prefers Chrome too); Edge ships with every
# Windows 10/11, so there is always something to fall back on. Any Chromium does the job.
WINDOW_PREFERENCE = ("chrome", "edge", "brave", "chromium")
NO_WINDOW = 0x08000000                                           # CREATE_NO_WINDOW: a child process must not flash a console
TITLE = "TikTok Guard"
APP_MUTEX = "TikTokGuardRunning"                                 # the installer (Inno Setup's AppMutex) will not replace or remove a running app
_MUTEXES: list = []                                              # kept for the life of the process: the mutex goes when the program does


# ------------------------------------------------------------------------------------------------ talking to the person
class _LogStream:
    """What stdout/stderr become when there is no console: lines go to the log (where a person can find them)."""

    def __init__(self, level: int) -> None:
        self.level, self._buf, self.encoding = level, "", "utf-8"
        self._busy = threading.local()

    def _log(self, line: str) -> None:
        # Python's last-resort log handler writes to sys.stderr - which is this stream. With no log file to take the line (it could
        # not be opened) that would log, write, log ... until the stack is gone: a line that arrives while one is being logged is dropped.
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            logging.getLogger("console").log(self.level, line)
        finally:
            self._busy.on = False

    def write(self, text) -> int:
        self._buf += str(text)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log(line.rstrip())
        return len(str(text))

    def flush(self) -> None:
        if self._buf.strip():
            self._log(self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False


def attach_console() -> bool:
    """A windowless exe started from a terminal: write to THAT terminal. -> was there one?"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):                 # ATTACH_PARENT_PROCESS
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def prepare_streams(*, wants_console: bool) -> bool:
    """Make print() and friends safe when there is no console (a windowless exe). -> is there a console to write to?"""
    if sys.stdout is not None and sys.stderr is not None:
        for stream in (sys.stdout, sys.stderr):                  # piped to another program (or a file): lines must arrive when printed,
            try:                                                 # not when a buffer fills - the address of the app is printed once
                stream.reconfigure(line_buffering=True)
            except Exception:  # noqa: BLE001
                pass
        return True
    if wants_console and attach_console():
        return True
    sys.stdout, sys.stderr = _LogStream(logging.INFO), _LogStream(logging.WARNING)
    return False


def alert(text: str, *, kind: str = "info", block: bool = True) -> None:
    """A message box (Windows) - the only way a windowless program can speak. Elsewhere: print. `block=False`: do not wait for OK."""
    def show() -> None:
        try:
            if os.environ.get("TIKTOK_GUARD_NO_DIALOG"):                  # automated runs: nobody is there to click OK
                print(f"{TITLE}: {text}")
            elif sys.platform == "win32":
                import ctypes
                flags = {"info": 0x40, "warn": 0x30, "error": 0x10}.get(kind, 0x40) | 0x10000 | 0x40000   # + foreground, topmost
                ctypes.windll.user32.MessageBoxW(0, text, TITLE, flags)
            else:
                print(f"{TITLE}: {text}")
        except Exception:  # noqa: BLE001
            print(f"{TITLE}: {text}")
    if block:
        show()
    else:
        threading.Thread(target=show, name="message-box", daemon=True).start()


def hold_app_mutex(name: str = APP_MUTEX):
    """Say "this program is running" to the installer/uninstaller. -> the mutex handle (None where there are no mutexes)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        create = ctypes.windll.kernel32.CreateMutexW
        create.restype = ctypes.c_void_p
        handle = create(None, False, name)
        _MUTEXES.append(handle)
        return handle
    except Exception:  # noqa: BLE001
        return None


def make_teller(has_console: bool):
    """The function that says something to the person: on the console when there is one, else in a message box."""
    def tell(text: str) -> None:
        LOG.info(text)
        if has_console:
            print(text)
        else:
            alert(text, kind="warn", block=False)
    return tell


# ------------------------------------------------------------------------------------------------ the app window
def is_running(port: int, host: str = "127.0.0.1") -> bool:
    """Is a TikTok guard backend already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=1.5) as resp:
            return json.loads(resp.read()).get("app") == "tiktok-guard"
    except Exception:  # noqa: BLE001
        return False


def window_command(url: str, exe: str, profile_dir: Path) -> list:
    """The command that opens `url` as an app window (no tabs, no address bar) on a profile folder of its own."""
    # --disable-sync and msImplicitSignin: a fresh Edge profile signs in with the person's Windows account by itself and then announces
    # "we are now syncing your browsing data across all your devices" - on the person's screen, with their e-mail address, and the
    # address of this window (it carries the app's token) on its way into their Microsoft account's history.
    return [exe, f"--app={url}", f"--user-data-dir={profile_dir}", "--window-size=1360,900", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-features=Translate,msImplicitSignin", "--lang=en-US"]


def pick_window_browser(installed: list) -> dict | None:
    for kind in WINDOW_PREFERENCE:
        for b in installed:
            if b["kind"] == kind:
                return b
    return installed[0] if installed else None


def open_window(url: str, *, installed: list | None = None):
    """-> the browser process whose end means 'the person closed the window', or None when no browser was found."""
    chosen = pick_window_browser(browsers.find_browsers() if installed is None else installed)
    if chosen is None:
        return None
    UI_PROFILE.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(window_command(url, chosen["path"], UI_PROFILE))


def window_processes(processes: list, profile_dir=UI_PROFILE) -> list:
    """Of `processes` ([{pid, name, cmdline}]) those that belong to the app's own window (its profile folder)."""
    from app import orphans
    want = orphans._norm(str(profile_dir))
    return [p for p in processes if (orphans.user_data_dir_of(p.get("cmdline") or "") or "") and
            orphans._norm(orphans.user_data_dir_of(p["cmdline"])) == want]


def wait_until_window_gone(*, lister=None, sleep=time.sleep, poll_s: float = 3.0, first_wait_s: float = 3.0) -> None:
    """The browser handed our window to a browser that was already running on the app's window folder (a process of an earlier
    run that was never closed), so its process ended at once and tells nothing. The window is gone when no browser process
    stands on that folder any more."""
    from app import orphans
    lister = lister or orphans.list_processes
    sleep(first_wait_s)
    while window_processes(lister()):
        sleep(poll_s)


def close_safely(desk, *, wait_s: float = CLOSE_WAIT_S, say=print) -> None:
    """Stop the app - but never in the middle of sending a reply: if one is on its way, wait for it (Ctrl+C = stop anyway)."""
    try:
        if desk.sending():
            say("A reply is still being sent - TikTok Guard will close by itself as soon as it has finished "
                "(Ctrl+C in a console quits anyway).")
            LOG.info("window closed while a reply was being sent: waiting up to %ds", wait_s)
            desk.stop(wait=wait_s)
        else:
            desk.stop()
    except KeyboardInterrupt:
        say("Quitting now - the reply that was being sent may or may not have reached TikTok.")
        LOG.warning("quit while a reply was being sent")
        desk.stop()


# ------------------------------------------------------------------------------------------------ command-line reports
def report_to_person(title: str, text: str, *, has_console: bool) -> None:
    """A report (--check, --self-test): on the console when there is one; otherwise in a file and a message box."""
    if has_console:
        print(text)
        return
    path = config.ROOT / "last-report.txt"
    try:
        path.write_text(f"{title}\n{'=' * len(title)}\n{text}\n", encoding="utf-8")
    except OSError:
        pass
    alert(f"{title}\n\n{text[:1500]}\n\n(saved in {path})", kind="info")


def check(*, has_console: bool = True) -> int:
    """`--check`: is this computer ready? 0 = yes (or only warnings), 1 = something essential is missing."""
    from app import health
    items = health.run(None, accounts=health.accounts_summary())
    report_to_person("TikTok Guard - check", health.report(items), has_console=has_console)
    return 1 if health.worst(items) == "fail" else 0


def self_test(*, has_console: bool = True) -> int:
    """`--self-test`: start a real browser and the app itself and look at both. 0 = all good, 1 = something failed."""
    from app import health, selftest
    items = selftest.run()
    report_to_person("TikTok Guard - self-test", health.report(items), has_console=has_console)
    return 1 if health.worst(items) == "fail" else 0


def diagnostics(*, wait: bool = True) -> int:
    """`TikTok Guard Diagnostics.exe` (a console program, so a double-click shows a window): everything support needs to know,
    on screen and in one file (last-report.txt) the person can send. 0 = nothing essential failed."""
    import platform
    from app import __version__, health, logs, selftest
    prepare_streams(wants_console=True)
    logs.setup()
    print(f"TikTok Guard {__version__} - diagnostics. This takes about half a minute; nothing is sent to TikTok ...\n")
    tests = selftest.run()
    ready = health.run(None, accounts=health.accounts_summary())
    text = "\n".join([f"TikTok Guard {__version__}", f"{platform.platform()}  |  Python {sys.version.split()[0]}  |  packaged: {config.FROZEN}",
                      f"Data folder: {config.ROOT}", "", "SELF-TEST", health.report(tests), "", "IS THIS COMPUTER READY?", health.report(ready),
                      "", "LOG (last 40 lines)", *logs.tail(40)])
    path = config.ROOT / "last-report.txt"
    try:
        path.write_text(text + "\n", encoding="utf-8")
        saved = f"\nThis report was saved as:\n  {path}\nSend that file to support if something does not work."
    except OSError:
        saved = ""
    print(text + "\n" + saved)
    if wait and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window ...")
        except (EOFError, KeyboardInterrupt):
            pass
    return 1 if health.worst(tests) == "fail" else 0              # "no account yet" is a fact about the person's setup, not a defect


# ------------------------------------------------------------------------------------------------ the app
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="TikTok guard - desktop application")
    ap.add_argument("--no-window", action="store_true", help="do not open the app window; print the address instead")
    ap.add_argument("--port", type=int, default=None, help=f"port of the local API (default {config.API_PORT})")
    ap.add_argument("--check", action="store_true", help="check that this computer is ready and exit")
    ap.add_argument("--self-test", action="store_true", help="start a real browser and the app and look at both, then exit")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    return ap


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    one_shot = args.check or args.self_test or args.version
    has_console = prepare_streams(wants_console=one_shot)
    from app import logs, __version__
    logs.setup()
    if args.version:
        report_to_person(TITLE, f"TikTok Guard {__version__}", has_console=has_console)
        return 0
    try:
        if args.check:
            return check(has_console=has_console)
        if args.self_test:
            return self_test(has_console=has_console)
        return run(args, has_console=has_console)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - a windowless program that fails silently is the worst kind
        LOG.exception("TikTok Guard could not run")
        alert(f"TikTok Guard could not start.\n\n{type(exc).__name__}: {str(exc)[:400]}\n\n"
              f"Details are in the log file:\n{logs.path()}", kind="error")
        return 1


def run(args, *, has_console: bool = True) -> int:
    port = config.API_PORT if args.port is None else args.port
    from app import __version__
    LOG.info("starting TikTok Guard %s (python %s, port %s, frozen=%s, home=%s)", __version__, sys.version.split()[0], port,
             config.FROZEN, config.ROOT)
    from app import api
    # already open? then just bring up another window on it (two copies would fight over the same accounts)
    if port and is_running(port):
        url = f"http://127.0.0.1:{port}/#token={api.load_token()}"
        print(f"TikTok guard is already running - opening a window on it ({url.split('#')[0]})")
        if not args.no_window and open_window(url) is None:
            webbrowser.open(url)
        return 0

    from app.desktop import DesktopApp
    try:
        desk = DesktopApp(port=port)
        base = desk.start()
    except OSError:                                       # the port belongs to another program: take any free one
        desk = DesktopApp(port=0)
        base = desk.start()
    hold_app_mutex()
    url = f"{base}/#token={desk.token_value}"
    print(f"TikTok guard is running at {base}/")
    window = None if args.no_window else open_window(url)
    tell = make_teller(has_console)
    try:
        if args.no_window or window is None:
            if not args.no_window:
                tell("No Edge, Chrome or Brave was found for the app window - opening your default browser instead.")
                webbrowser.open(url)
            print(f"Open this address in a browser:  {url}\nPress Ctrl+C to stop.")
            while True:
                time.sleep(1)
        else:
            print("The app window is open. Close it to stop the app.")
            started = time.monotonic()
            window.wait()
            if time.monotonic() - started < 3:           # the browser handed the window to an already running instance
                LOG.info("the window was handed to a browser that was already running: waiting for it to go")
                wait_until_window_gone()
    except KeyboardInterrupt:
        pass
    finally:
        close_safely(desk, say=tell)
        LOG.info("stopped")
        print("Stopped.")
    return 0
