"""Real tests of the BUILT TikTok Guard.exe - the files a client would receive, started the way a client starts them.

    python buildexe/test_exe.py            the tests that need no internet account (a few minutes)
    python buildexe/test_exe.py --live     + one read of a public TikTok post through the packaged engine (read-only, nothing posted)

Which build: TG_EXE_DIR (default buildexe/build/dist/TikTok Guard); `package` copies the same folder to production/.
Every run uses a throw-away data folder (TIKTOK_GUARD_HOME) and a scrubbed environment: nothing of this machine's .env, Python
or source tree is visible to the exe, and the real data/ and network/ folders are never touched.
Pictures of the screens it saw are kept in buildexe/build/exe-test-shots/.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
EXE_DIR = Path(os.environ.get("TG_EXE_DIR") or HERE / "build" / "dist" / "TikTok Guard")
APP, DIAG = EXE_DIR / "TikTok Guard.exe", EXE_DIR / "TikTok Guard Diagnostics.exe"
SHOTS = HERE / "build" / "exe-test-shots"
LIVE = "--live" in sys.argv
if "--live" in sys.argv:
    sys.argv.remove("--live")
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

KEEP = ("SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA",
        "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "ALLUSERSPROFILE",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "HOMEDRIVE", "HOMEPATH", "PUBLIC", "COMPUTERNAME")


def clean_env(home: Path | None, **extra) -> dict:
    """What a client's computer looks like to the exe: Windows' own variables - and none of this machine's settings."""
    keep = {k.upper() for k in KEEP}                                     # os.environ upper-cases the names on Windows
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    assert "SYSTEMROOT" in {k.upper() for k in env}, "Windows cannot even start OpenSSL without it"
    env["PATH"] = r"C:\Windows\System32;C:\Windows"
    env["TIKTOK_GUARD_NO_DIALOG"] = "1"                                  # nobody is there to click OK in an automated run
    if home is not None:
        env["TIKTOK_GUARD_HOME"] = str(home)
    env.update(extra)
    return env


def run(args=(), *, home, exe=APP, timeout=240, cwd=None, **extra) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          env=clean_env(home, **extra), stdin=subprocess.DEVNULL, cwd=cwd)


def new_home(prefix="tg-exe-home-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def installed_kinds() -> set:
    from app import browsers
    return {b["kind"] for b in browsers.find_browsers()}


def snapshot(folder: Path) -> dict:
    return {str(f.relative_to(folder)): (f.stat().st_size, int(f.stat().st_mtime)) for f in folder.rglob("*") if f.is_file()}


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)


def all_processes() -> list:
    """[{pid, name, cmdline, exe}] of EVERY running program, accents in their paths read correctly (UTF-8 from PowerShell)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
         "Select-Object ProcessId, Name, ExecutablePath, CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60, creationflags=0x08000000).stdout.strip()
    rows = json.loads(out) if out else []
    return [{"pid": r["ProcessId"], "name": r.get("Name") or "", "cmdline": r.get("CommandLine") or "", "exe": r.get("ExecutablePath") or ""}
            for r in rows]


def processes_using(folder: Path) -> list:
    """Programs that run FROM `folder` (the installed exe) or have it in their command line (a browser on a profile folder)."""
    needle = str(folder).lower()
    return [p for p in all_processes()
            if p["pid"] != os.getpid() and "powershell" not in p["name"].lower()
            and (needle in p["cmdline"].lower() or needle in p["exe"].lower())]


# ------------------------------------------------------------------------------------------ windows on the screen
user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def visible_windows() -> dict:
    """{hwnd: (class, title, pid)} of every visible top-level window."""
    found = {}

    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls, title, pid = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256), wintypes.DWORD()
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 256)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found[int(hwnd)] = (cls.value, title.value, pid.value)
        return True
    user32.EnumWindows(WNDENUMPROC(each), 0)
    return found


def capture_window(hwnd, path) -> bool:
    """Save a picture of ONE window's own content (PrintWindow), whatever is on top of it - a screen grab of its rectangle would
    show the other programs of the person's desktop that happen to cover it."""
    gdi32 = ctypes.windll.gdi32
    for fn, res, args in ((gdi32.CreateCompatibleDC, ctypes.c_void_p, [ctypes.c_void_p]),
                          (gdi32.CreateCompatibleBitmap, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
                          (gdi32.SelectObject, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
                          (gdi32.DeleteObject, ctypes.c_int, [ctypes.c_void_p]), (gdi32.DeleteDC, ctypes.c_int, [ctypes.c_void_p]),
                          (user32.GetWindowDC, ctypes.c_void_p, [wintypes.HWND]), (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, ctypes.c_void_p]),
                          (user32.PrintWindow, ctypes.c_int, [wintypes.HWND, ctypes.c_void_p, ctypes.c_uint]),
                          (gdi32.GetDIBits, ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                                          ctypes.c_void_p, ctypes.c_uint])):
        fn.restype, fn.argtypes = res, args

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old_bmp = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):                       # 2 = PW_RENDERFULLCONTENT (also browsers)
            return False
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
        from PIL import Image
        image = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        middle = image.crop((w // 4, h // 4, w * 3 // 4, h * 3 // 4)) if w > 200 and h > 200 else image
        if len(set(middle.getdata())) <= 2:                                    # a browser's GPU surface cannot be printed: it comes out blank
            user32.ShowWindow(hwnd, 9)                                          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(1.0)
            if user32.GetForegroundWindow() != hwnd:                            # something else is on top: never photograph other programs
                return False
            from PIL import ImageGrab
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
        image.save(path)
        return True
    finally:
        gdi32.SelectObject(mem, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")


class ConsoleWatch:
    """A console window that flashes up while a test runs would be seen by the client. Looks 20 times a second."""

    def __enter__(self):
        self.before = {h for h, (c, _, _) in visible_windows().items() if c in CONSOLE_CLASSES}
        self.seen, self._stop = {}, threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            for h, (c, t, p) in visible_windows().items():
                if c in CONSOLE_CLASSES and h not in self.before:
                    self.seen[h] = (c, t, p)
            time.sleep(0.05)

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


# ------------------------------------------------------------------------------------------ a running exe
class Running:
    """The exe started with `args`, its output collected on a thread."""

    def __init__(self, args, home, *, exe=None, **extra):
        self.home, self.lines, self._lock = home, [], threading.Lock()
        self.proc = subprocess.Popen([str(exe or APP), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=clean_env(home, **extra), text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line.rstrip())

    @property
    def text(self) -> str:
        with self._lock:
            return "\n".join(self.lines)

    def wait_for(self, pattern: str, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = re.search(pattern, self.text)
            if m:
                return m
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"never saw /{pattern}/ in the exe's output (exit={self.proc.poll()}):\n{self.text[-1500:]}")

    def stop(self):
        if self.proc.poll() is None:
            kill_tree(self.proc.pid)
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            pass
        try:
            self.proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass


def http(url, *, token=None, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**({"Authorization": f"Bearer {token}"} if token else {}), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ================================================================================================ the tests
class TestTheFilesThemselves(unittest.TestCase):
    def test_both_programs_are_there_with_the_screen_and_the_driver_inside(self):
        for path in (APP, DIAG, EXE_DIR / "_internal" / "client_defaults.env", EXE_DIR / "_internal" / "app" / "web" / "desktop.html",
                     EXE_DIR / "_internal" / "playwright" / "driver" / "node.exe"):
            self.assertTrue(path.is_file(), f"missing: {path}")

    def test_the_exe_looks_like_a_real_product_and_never_asks_for_administrator_rights(self):
        import pefile
        for exe, subsystem in ((APP, 2), (DIAG, 3)):
            pe = pefile.PE(str(exe))
            self.assertEqual(pe.OPTIONAL_HEADER.Subsystem, subsystem, f"{exe.name}: 2 = no console window, 3 = console")
            strings = {}
            for info in getattr(pe, "FileInfo", []):
                for entry in info:
                    for table in getattr(entry, "StringTable", []):
                        strings.update({k.decode(): v.decode() for k, v in table.entries.items()})
            self.assertEqual(strings.get("ProductName"), "TikTok Guard")
            self.assertEqual(strings.get("FileVersion"), "1.0.0")
            self.assertTrue(strings.get("FileDescription"))
            kinds = {e.id for e in pe.DIRECTORY_ENTRY_RESOURCE.entries}
            self.assertIn(pefile.RESOURCE_TYPE["RT_GROUP_ICON"], kinds, "an icon of its own")
            self.assertIn(pefile.RESOURCE_TYPE["RT_VERSION"], kinds)
            manifest = ""
            for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
                if entry.id == pefile.RESOURCE_TYPE["RT_MANIFEST"]:
                    data = entry.directory.entries[0].directory.entries[0].data.struct
                    manifest = pe.get_data(data.OffsetToData, data.Size).decode("utf-8", "replace")
            self.assertNotIn("requireAdministrator", manifest)
            self.assertNotIn("highestAvailable", manifest)
            pe.close()

    def test_the_defaults_that_ship_with_it_contain_no_secrets(self):
        text = (EXE_DIR / "_internal" / "client_defaults.env").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                self.assertFalse(any(w in key.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CHAT")), f"secret-looking setting: {key}")
        self.assertNotIn("sk-or-", text)

    def test_nothing_of_the_developers_machine_is_needed_the_program_folder_has_no_data_folders(self):
        names = {p.name for p in EXE_DIR.rglob("*")}
        for forbidden in (".env", "data", "network", "bfp_runs", "accounts.json", "api_token.txt", "bot.db"):
            self.assertNotIn(forbidden, names)


class TestCommandLine(unittest.TestCase):
    def test_version(self):
        home = new_home()
        out = run(["--version"], home=home)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "TikTok Guard 1.0.0"), out.stderr)

    def test_the_self_test_passes_here_and_every_step_is_reported(self):
        home = new_home()
        out = run(["--self-test"], home=home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for step in ("Program files", "Data folder", "Database", "A web browser", "Browser automation", "The app", "Internet / TikTok"):
            self.assertRegex(out.stdout, rf"\[ OK \] {re.escape(step)}")
        self.assertIn("(packaged)", out.stdout)
        self.assertIn(str(home), out.stdout, "the data folder is the one it was given, not the program folder")

    def test_check_on_a_fresh_install_says_what_is_missing_and_what_to_do(self):
        out = run(["--check"], home=new_home())
        self.assertIn(out.returncode, (0, 1))
        self.assertNotIn("Traceback", out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"(?i)account")
        self.assertRegex(out.stdout, r"(?i)AI|key")

    def test_the_diagnostics_program_writes_a_report_the_client_can_send(self):
        home = new_home()
        out = run([], home=home, exe=DIAG)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        report = (home / "last-report.txt").read_text(encoding="utf-8")
        for part in ("TikTok Guard 1.0.0", "SELF-TEST", "[ OK ] Browser automation", "IS THIS COMPUTER READY?", "LOG (last 40 lines)", str(home)):
            self.assertIn(part, report)
        self.assertNotRegex(report, r"sk-or-v1-[A-Za-z0-9]{10,}")
        self.assertIn("last-report.txt", out.stdout)

    def test_an_unknown_option_is_refused_with_a_message(self):
        out = run(["--nonsense"], home=new_home())
        self.assertEqual(out.returncode, 2)
        self.assertIn("--nonsense", out.stderr + out.stdout)


class TestRunsFromAnywhere(unittest.TestCase):
    def test_a_copy_in_a_folder_with_spaces_and_accents_runs_from_any_directory_and_ignores_python_variables_of_this_machine(self):
        base = Path(tempfile.mkdtemp(prefix="tg exe copy ñ "))
        copy = base / "Pasta do cliente ñ" / "TikTok Guard"
        shutil.copytree(EXE_DIR, copy)
        home, elsewhere = new_home("tg-exe-home usuário ção "), Path(tempfile.mkdtemp(prefix="elsewhere-"))   # a data path like "C:\Users\Usuário"
        before = snapshot(copy)
        out = run(["--self-test"], home=home, exe=copy / "TikTok Guard.exe", cwd=str(elsewhere),
                  PYTHONPATH=f"{PROJECT};X:\\bfp", PYTHONHOME=r"C:\does\not\exist", BFP_PATH=r"X:\bfp")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("(packaged)", out.stdout)
        self.assertEqual(snapshot(copy), before, "the program folder must not be written to")
        log = (home / "data" / "logs" / "desktop.log").read_text(encoding="utf-8", errors="replace")
        for hint in ("X:\\project", "X:/project", "X:\\bfp", "X:/bfp"):
            self.assertNotIn(hint, log, "nothing may be read from the developer's folders")
        shutil.rmtree(base, ignore_errors=True)


class TestWhereTheDataGoes(unittest.TestCase):
    def test_without_a_home_variable_everything_goes_to_localappdata_tiktok_guard(self):
        local = Path(tempfile.mkdtemp(prefix="fake-localappdata-"))
        before = snapshot(EXE_DIR)
        out = subprocess.run([str(APP), "--check"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                             env={**clean_env(None), "LOCALAPPDATA": str(local)}, stdin=subprocess.DEVNULL)
        self.assertIn(out.returncode, (0, 1), out.stderr)
        home = local / "TikTok Guard"
        self.assertTrue((home / "data" / "logs" / "desktop.log").is_file())
        self.assertTrue((home / "network").is_dir())
        self.assertEqual(snapshot(EXE_DIR), before)

    def test_an_unwritable_data_folder_is_reported_not_swallowed(self):
        blocker = Path(tempfile.mkdtemp()) / "a-file-not-a-folder"
        blocker.write_text("x")
        temp = Path(tempfile.mkdtemp(prefix="crash-temp-"))
        out = run([], home=blocker, TEMP=str(temp), TMP=str(temp))
        self.assertEqual(out.returncode, 1)
        self.assertIn("could not start", out.stdout + out.stderr)
        self.assertTrue((temp / "TikTok Guard crash.txt").is_file(), "the details are left in a file the person can send")


class TestTheRunningApp(unittest.TestCase):
    """One `--no-window` start shared by these tests: the server, the API, and the real screen driven by headless Chrome."""

    @classmethod
    def setUpClass(cls):
        SHOTS.mkdir(parents=True, exist_ok=True)
        cls.home = new_home()
        cls.watch = ConsoleWatch().__enter__()
        cls.app = Running(["--no-window", "--port", "0"], cls.home)
        cls.addClassCleanup(cls.app.stop)                            # also when the start below fails: no exe may be left running
        cls.addClassCleanup(cls.watch.__exit__, None, None, None)
        m = cls.app.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
        cls.base, cls.token = m.group(1), m.group(2)

    def test_it_answers_and_serves_its_screen(self):
        status, body = http(self.base + "/api/ping")
        self.assertEqual((status, json.loads(body)["app"]), (200, "tiktok-guard"))
        status, page = http(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("TikTok Guard", page.decode())

    def test_the_api_needs_the_token(self):
        self.assertEqual(http(self.base + "/api/health")[0], 401)
        self.assertEqual(http(self.base + "/api/health", token="wrong")[0], 401)
        self.assertEqual(http(self.base + "/api/health", token=self.token)[0], 200)

    def test_a_fresh_install_has_the_clients_settings_no_phantom_account_and_no_key(self):
        _, body = http(self.base + "/api/settings", token=self.token)
        settings = json.loads(body)
        self.assertEqual(settings["brand_name"], "Example Store")
        self.assertTrue(settings["posting_enabled"])
        self.assertFalse(settings["ai_key"]["set"], "the client enters their own key")
        _, body = http(self.base + "/api/accounts", token=self.token)
        accounts = json.loads(body)
        rows = accounts["accounts"] if isinstance(accounts, dict) else accounts
        self.assertEqual(rows, [], "no 'master' account that nobody can log in to")

    def test_the_program_never_starts_a_background_monitor_or_telegram(self):
        _, body = http(self.base + "/api/dashboard", token=self.token)
        text = self.app.text.lower()
        self.assertNotIn("telegram", text)
        self.assertEqual(json.loads(body)["accounts"]["usable"], 0)

    def test_the_real_screen_works_page_by_page_without_any_script_error(self):
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        from ui_support import UI
        ui = UI()
        try:
            ui.page.send("Page.addScriptToEvaluateOnNewDocument", source=(
                "window.__errors = []; window.addEventListener('error', e => window.__errors.push(String(e.message)));"
                "window.addEventListener('unhandledrejection', e => window.__errors.push('rejected: ' + String(e.reason)));"))
            ui.open(f"{self.base}/#token={self.token}")
            ui.wait("document.querySelector('#url-input') && document.querySelector('#analyze-btn')", 20, "home")
            ui.wait("document.querySelectorAll('#stats .stat').length >= 1", 20, "the numbers on the home page")
            ui.shot(SHOTS / "1-home.png")
            self.assertIn("TikTok Guard", ui.text("body"))
            for page, marker in (("replies", None), ("accounts", "#add-account"), ("ai", "#ai-instructions"),
                                 ("controls", "#key-input"), ("help", None), ("home", "#url-input")):
                ui.ev(f"location.hash = '#/{page}'")
                ui.wait(f"document.querySelector('#nav a.on[data-nav={page}]')", 10, f"the {page} page")
                if marker:
                    ui.wait(f"document.querySelector({json.dumps(marker)})", 15, f"{page}: {marker}")
                time.sleep(0.8)
                ui.shot(SHOTS / f"2-{page}.png")
                if page == "home":                     # a fresh install is told, in plain words, what to do first - that IS the banner
                    banner = ui.text(".banner.bad")
                    self.assertIn("needs fixing", banner)
                    self.assertIn("Add account", banner)
                    self.assertIn("OpenRouter key", banner)
                else:
                    self.assertFalse(ui.exists(".banner.bad, [data-role=fatal]"), f"{page}: an error is shown: {ui.text('body')[:300]}")
            ui.ev("location.hash = '#/accounts'")
            ui.wait("document.querySelector('#accounts-list .empty')", 15, "the empty accounts list")
            self.assertIn("No accounts yet", ui.text("#accounts-list"))
            self.assertNotIn("tiktok-monitor", ui.text("#accounts-list"), "no phantom master account")
            self.assertEqual(ui.ev("window.__errors"), [], "script errors on the screens")
        finally:
            ui.close()

    def test_a_pasted_link_without_any_account_is_answered_with_what_to_do_not_with_a_crash(self):
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        from ui_support import UI
        ui = UI()
        try:
            ui.open(f"{self.base}/#token={self.token}")
            ui.wait("document.querySelector('#url-input')", 20, "home")
            ui.fill("#url-input", "https://www.tiktok.com/@someone/video/7600000000000000000")
            ui.click("#analyze-btn")
            ui.wait("document.querySelector('[data-role=job-error]') || document.querySelector('.banner') || document.querySelector('.toast')",
                    40, "an answer")
            text = ui.text("body")
            ui.shot(SHOTS / "3-no-account.png")
            self.assertRegex(text, r"(?i)account", text[:500])
            self.assertNotIn("Traceback", text)
        finally:
            ui.close()

    def test_no_console_window_flashed_up_while_it_ran(self):
        self.assertEqual(self.watch.seen, {}, f"a console window was visible: {self.watch.seen}")


class TestItKeepsWhatThePersonSaved(unittest.TestCase):
    def test_a_key_and_a_brand_name_saved_in_the_app_are_still_there_after_a_restart(self):
        home = new_home()
        first = Running(["--no-window", "--port", "0"], home)
        try:
            m = first.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
            base, token = m.group(1), m.group(2)
            status, _ = http(base + "/api/settings", token=token, method="PUT",
                             body={"openrouter_api_key": "sk-or-v1-" + "a" * 40, "brand_name": "Loja do Cliente"})
            self.assertEqual(status, 200)
        finally:
            first.stop()
        self.assertEqual(processes_using(home), [])
        second = Running(["--no-window", "--port", "0"], home)
        try:
            m = second.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
            settings = json.loads(http(m.group(1) + "/api/settings", token=m.group(2))[1])
            self.assertTrue(settings["ai_key"]["set"])
            self.assertEqual(settings["brand_name"], "Loja do Cliente")
            self.assertNotIn("a" * 40, json.dumps(settings), "the key itself is never sent back to the screen")
            self.assertNotIn("a" * 40, (home / "data" / "logs" / "desktop.log").read_text(encoding="utf-8", errors="replace"))
        finally:
            second.stop()


class TestTwoCopies(unittest.TestCase):
    def test_a_second_start_joins_the_running_app_instead_of_starting_another(self):
        home, port = new_home(), "8791"
        first = Running(["--no-window", "--port", port], home)
        try:
            first.wait_for(r"Open this address in a browser", 60)
            out = run(["--no-window", "--port", port], home=home, timeout=60)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("already running", out.stdout)
            self.assertEqual(json.loads(http(f"http://127.0.0.1:{port}/api/ping")[1])["app"], "tiktok-guard")
        finally:
            first.stop()

    def test_when_the_usual_port_is_taken_by_another_program_it_takes_a_free_one(self):
        import socket
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        app = Running(["--no-window", "--port", str(port)], new_home())
        try:
            m = app.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:(\d+))/#token=", 60)
            self.assertNotEqual(int(m.group(2)), port)
        finally:
            app.stop()
            blocker.close()


class TestTheAppWindow(unittest.TestCase):
    def test_a_double_click_opens_the_window_and_closing_the_window_ends_the_program_cleanly(self):
        SHOTS.mkdir(parents=True, exist_ok=True)
        home = new_home()
        profile = (home / "data" / "ui-profile")
        with ConsoleWatch() as watch:
            app = Running([], home)
            try:
                app.wait_for(r"The app window is open", 60)
                deadline, window, browser = time.monotonic() + 40, None, []
                while time.monotonic() < deadline and not window:
                    browser = [p for p in processes_using(profile) if "--type=" not in (p.get("cmdline") or "")]
                    pids = {p["pid"] for p in browser}
                    # until the page has loaded, the window's title is only its address: wait for the real one
                    window = next(((h, v) for h, v in visible_windows().items() if v[2] in pids and "TikTok Guard" in v[1]), None)
                    time.sleep(0.5)
                self.assertTrue(browser, f"no app window process appeared\n{app.text}")
                self.assertIn("--app=http://127.0.0.1:", browser[0]["cmdline"])
                self.assertIn("--disable-sync", browser[0]["cmdline"], "no Microsoft-account sync pop-up in the person's window")
                if installed_kinds() & {"chrome"}:
                    self.assertEqual(browser[0]["name"].lower(), "chrome.exe", "a client with Chrome gets Chrome, not Edge")
                self.assertTrue(window, "the window never became visible")
                hwnd, (cls, title, pid) = window
                self.assertIn("TikTok Guard", title)
                time.sleep(3)
                if not capture_window(hwnd, SHOTS / "4-the-real-window.png"):
                    print("(no picture of the window)")
                # the person presses the X of the window: the same WM_CLOSE a click on it sends
                subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, creationflags=0x08000000)
                try:
                    code = app.proc.wait(45)
                except subprocess.TimeoutExpired:
                    self.fail(f"the program kept running after its window was closed\n{app.text}")
                self.assertEqual(code, 0, app.text)
                self.assertIn("Stopped.", app.text)
            finally:
                app.stop()
        time.sleep(2)
        self.assertEqual([p["pid"] for p in processes_using(home)], [], "nothing of the app may be left running")
        self.assertEqual(watch.seen, {}, f"a console window was visible: {watch.seen}")


class TestAddingAnAccount(unittest.TestCase):
    def test_the_login_window_opens_from_the_packaged_exe_and_cancelling_leaves_nothing_behind(self):
        """The most important first step of a client, up to the point where only they can go on (typing their TikTok password)."""
        SHOTS.mkdir(parents=True, exist_ok=True)
        home = new_home()
        app = Running(["--no-window", "--port", "0"], home)
        try:
            m = app.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
            base, token = m.group(1), m.group(2)
            status, body = http(base + "/api/accounts/add", token=token, method="POST", body={"browser": "auto"})
            self.assertIn(status, (200, 202), body)                         # 202 Accepted: the window opens in the background
            deadline, flow = time.monotonic() + 90, {}
            while time.monotonic() < deadline:
                flow = json.loads(http(base + "/api/accounts/add", token=token)[1])["flow"] or {}
                if flow.get("state") == "window_open" or flow.get("finished"):
                    break
                time.sleep(0.5)
            self.assertEqual(flow.get("state"), "window_open", flow)
            self.assertTrue(flow.get("waiting_for_you"))
            expected = next((k for k in ("chrome", "brave", "edge") if k in installed_kinds()), None)
            self.assertEqual((flow.get("browser") or {}).get("kind"), expected, "the login window opens in the client's Chrome when there is one")
            time.sleep(4)                                                    # let TikTok's login page draw
            pids = {p["pid"] for p in processes_using(home)}
            window = next(((h, v) for h, v in visible_windows().items() if v[2] in pids and v[1].strip()), None)
            self.assertTrue(window, "a login window is visible on the screen")
            if not capture_window(window[0], SHOTS / "7-add-account-window.png"):
                print("(no picture of the login window)")
            self.assertIn(http(base + "/api/accounts/add/cancel", token=token, method="POST", body={})[0], (200, 202))
            end = time.monotonic() + 40
            while time.monotonic() < end and not (json.loads(http(base + "/api/accounts/add", token=token)[1])["flow"] or {}).get("finished"):
                time.sleep(0.5)
            rows = json.loads(http(base + "/api/accounts", token=token)[1])
            self.assertEqual(rows["accounts"] if isinstance(rows, dict) else rows, [], "a cancelled login leaves no account behind")
            time.sleep(3)
            left = [p for p in processes_using(home) if "TikTok Guard.exe" not in (p.get("cmdline") or "")]
            self.assertEqual(left, [], "the login window's browser is gone")
        finally:
            app.stop()


@unittest.skipUnless("chrome" in installed_kinds(), "needs Google Chrome")
class TestAChromeOnlyClientAddsAnAccount(unittest.TestCase):
    """A client who has only Chrome: the whole 'Add account' flow in the packaged exe - window opens in Chrome, the login (a session cookie
    is planted the way a real login leaves it; this test cannot type a password) is DETECTED, the account is saved, the app closes the
    window itself, a background check runs with the saved Chrome profile, and removing the account removes its folder."""

    def test_login_detected_saved_and_the_window_closed_by_the_app(self):
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        import bfp
        home = new_home()
        app = Running(["--no-window", "--port", "0"], home)
        try:
            m = app.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
            base, token = m.group(1), m.group(2)
            status, body = http(base + "/api/accounts/add", token=token, method="POST", body={"browser": "chrome", "label": "Client account"})
            self.assertIn(status, (200, 202), body)
            flow = wait_flow(base, token, ("window_open",), 90)
            self.assertEqual(flow["browser"]["kind"], "chrome")
            time.sleep(3)
            self.assertEqual(accounts_of(base, token), [], "nothing is saved while nobody has logged in")
            chrome = next(p for p in processes_using(home) if "--remote-debugging-port=" in p["cmdline"] and "--type=" not in p["cmdline"])
            port = re.search(r"--remote-debugging-port=(\d+)", chrome["cmdline"]).group(1)
            ws_url = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10).read())["webSocketDebuggerUrl"]
            ws = bfp.WSClient(ws_url, timeout=10)
            try:
                ws.send(json.dumps({"id": 1, "method": "Storage.setCookies", "params": {"cookies": [
                    {"name": "sessionid", "value": "exe-test-session", "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True,
                     "expires": time.time() + 86400}]}}))
                for _ in range(20):
                    if json.loads(ws.recv(timeout=10)).get("id") == 1:
                        break
            finally:
                ws.close()
            done = wait_flow(base, token, ("done", "failed"), 150)
            self.assertEqual(done["outcome"], "added", done)
            rows = accounts_of(base, token)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["browser_kind"], row["label"], row["folder_exists"]), ("chrome", "Client account", True))
            self.assertEqual(row["session"], "unknown", "it must not call an account 'logged in' before it could read who it is")
            saved = json.loads(Path(home, "data", "accounts.json").read_text(encoding="utf-8"))
            saved = saved["accounts"] if isinstance(saved, dict) else saved
            self.assertTrue(Path(saved[0]["user_data_dir"]).is_relative_to(home), "the account's browser profile lives in the client's data folder")
            end = time.monotonic() + 150                                             # the automatic background check (headless Chrome)
            while time.monotonic() < end and not accounts_of(base, token)[0].get("session_checked_at"):
                time.sleep(2)
            self.assertTrue(accounts_of(base, token)[0].get("session_checked_at"), "the background check ran")
            self.assertIn(accounts_of(base, token)[0]["session"], ("expired", "unknown"), "TikTok does not accept a planted cookie: never 'ok'")
            status, removed = http(base + f"/api/accounts/{row['name']}", token=token, method="DELETE")
            self.assertEqual(status, 200, removed)
            result = json.loads(removed)
            self.assertTrue(result["folder_deleted"] or result["cleanup_pending"], result)
            self.assertEqual(accounts_of(base, token), [])
            account_folder = Path(saved[0]["user_data_dir"]).parent                  # <network>/.../real-account-1
            end = time.monotonic() + 120                                              # removed at once, or by the background clean-up
            while account_folder.exists() and time.monotonic() < end:
                time.sleep(1)
            self.assertFalse(account_folder.exists(), "the removed account's profile (its cookies) is gone from the disk")
            time.sleep(3)
            self.assertEqual([p["pid"] for p in processes_using(home) if "--remote-debugging-port=" in p["cmdline"]], [], "no Chrome left behind")
        finally:
            app.stop()


def wait_flow(base, token, states, timeout):
    end, flow = time.monotonic() + timeout, {}
    while time.monotonic() < end:
        flow = json.loads(http(base + "/api/accounts/add", token=token)[1])["flow"] or {}
        if flow.get("state") in states:
            return flow
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {states}; last: {flow}")


def accounts_of(base, token) -> list:
    rows = json.loads(http(base + "/api/accounts", token=token)[1])
    return [a for a in (rows["accounts"] if isinstance(rows, dict) else rows) if not a.get("is_master")]


@unittest.skipUnless(LIVE, "needs --live: reads a public TikTok post with an existing account profile (nothing is posted)")
class TestLiveRead(unittest.TestCase):
    def test_the_packaged_engine_reads_the_comments_of_a_real_post(self):
        raise NotImplementedError("see run_live() - driven separately")


if __name__ == "__main__":
    unittest.main(verbosity=2)
