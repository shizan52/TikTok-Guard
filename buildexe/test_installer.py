"""Real tests of the BUILT Setup.exe - what a client does: install, start, update, remove.

    python buildexe/test_installer.py

Needs buildexe/build/installer/TikTok-Guard-Setup-<version>.exe (TG_SETUP overrides). It REALLY installs: shortcuts appear on this
user's desktop and Start menu and an "Apps & features" entry is written - and all of it is removed again by the uninstaller at the
end (the tests refuse to run when TikTok Guard is already installed on this machine, so nobody's real install is touched).
The data folder %LOCALAPPDATA%\\TikTok Guard is only touched when it did not exist before the tests (and is removed again).
Pictures of every wizard page go to buildexe/build/exe-test-shots/.
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import winreg
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(HERE))
import test_exe as T                                      # noqa: E402  (helpers: visible_windows, Running, processes_using ...)
from app import __version__                               # noqa: E402

VERSION = __version__
SETUP = Path(os.environ.get("TG_SETUP") or HERE / "build" / "installer" / f"TikTok-Guard-Setup-{VERSION}.exe")
STAGE = HERE / "build" / "stage" / "TikTok Guard"
SHOTS = T.SHOTS
APP_ID = "{7D4A3B9E-2C61-4F58-9E0A-6B1D84C5F273}_is1"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_ID
REAL_DATA = Path(os.environ["LOCALAPPDATA"]) / "TikTok Guard"
user32 = T.user32


def powershell(command: str) -> str:
    """Run one PowerShell command, output read as UTF-8 (paths with accents would break the console's own code page)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command],
                         capture_output=True, encoding="utf-8", errors="replace", creationflags=0x08000000)
    return out.stdout.strip()


def known_folder(name: str) -> Path:
    return Path(powershell(f"[Environment]::GetFolderPath('{name}')"))


DESKTOP, PROGRAMS = known_folder("Desktop"), known_folder("Programs")
GROUP = PROGRAMS / "TikTok Guard"


def shortcut_target(lnk: Path) -> str:
    return powershell(f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath")


def registry_entry() -> dict | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            out = {}
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, i)
                out[name] = value
            return out
    except FileNotFoundError:
        return None


def run_setup(*args, dest: Path | None = None, tasks="desktopicon", timeout=300) -> subprocess.CompletedProcess:
    log = Path(tempfile.gettempdir()) / f"tg-setup-{int(time.time() * 1000)}.log"
    cmd = [str(SETUP), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", *args]
    if dest is not None:
        cmd.append(f"/DIR={dest}")
    if tasks is not None:
        cmd.append(f"/TASKS={tasks}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=0x08000000)
    out.log = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    return out


def listing(root: Path) -> dict:
    return {str(f.relative_to(root)): f.stat().st_size for f in root.rglob("*") if f.is_file()}


def wait_for(condition, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def stop_everything_of_the_tests(folder: Path) -> None:
    """Close every program (the installed app, its browser window) that runs from or on `folder`."""
    for proc in T.processes_using(folder):
        T.kill_tree(proc["pid"])
    time.sleep(2)


def uninstall_silently(folder: Path) -> None:
    exe = folder / "unins000.exe"
    if exe.exists():
        subprocess.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], capture_output=True, timeout=120, creationflags=0x08000000)
        wait_for(lambda: not folder.exists() or not any(folder.iterdir()), 90, "the uninstaller to finish")


# -------------------------------------------------------------------------------------------- driving the real wizard
def wizard():
    return next(((h, v) for h, v in T.visible_windows().items() if v[1].startswith("Setup - TikTok Guard")), None)


def buttons(hwnd) -> dict:
    found = {}
    Enum = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(child, _):
        cls, text = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, cls, 128)
        user32.GetWindowTextW(child, text, 256)
        if user32.IsWindowVisible(child) and user32.IsWindowEnabled(child) and cls.value.endswith("Button"):      # TNewButton, not labels
            found[text.value.replace("&", "")] = (int(child), cls.value)
        return True
    user32.EnumChildWindows(hwnd, Enum(each), 0)
    return found


def click(child) -> None:
    user32.PostMessageW(child, 0x00F5, 0, 0)                  # BM_CLICK


def picture(name: str, hwnd) -> None:
    if not T.capture_window(hwnd, SHOTS / name):
        print("(no picture:", name, ")")


class TestTheInstaller(unittest.TestCase):
    """Tests run in order (01, 02 ...): each step builds on the one before. A failed install stops the rest."""
    dest = None
    installed = False
    data_existed = REAL_DATA.exists()

    @classmethod
    def setUpClass(cls):
        SHOTS.mkdir(parents=True, exist_ok=True)
        if not SETUP.is_file():
            raise unittest.SkipTest(f"no installer at {SETUP}: run `python buildexe/build.py installer`")
        if registry_entry() is not None or (DESKTOP / "TikTok Guard.lnk").exists() or GROUP.exists():
            raise unittest.SkipTest("TikTok Guard is already installed on this computer - these tests would replace it")
        cls.base = Path(tempfile.mkdtemp(prefix="tg setup ñ "))
        cls.dest = cls.base / "Programas" / "TikTok Guard"
        cls.addClassCleanup(cls.cleanup)

    @classmethod
    def cleanup(cls):
        """Whatever happened above (a failed assertion, an interrupted run), leave this computer as it was found."""
        stop_everything_of_the_tests(cls.base)
        entry = registry_entry()
        if entry is not None and Path(entry.get("InstallLocation", "").rstrip("\\")).is_relative_to(cls.base):
            uninstall_silently(Path(entry["InstallLocation"].rstrip("\\")))
            if registry_entry() is not None:                    # the uninstaller itself is gone: remove what it would have removed
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
                except OSError:
                    pass
                (DESKTOP / "TikTok Guard.lnk").unlink(missing_ok=True)
                shutil.rmtree(GROUP, ignore_errors=True)
        shutil.rmtree(cls.base, ignore_errors=True)
        if not cls.data_existed:
            stop_everything_of_the_tests(REAL_DATA)             # the app the wizard started, and its window
            shutil.rmtree(REAL_DATA, ignore_errors=True)

    def need_install(self):
        if not self.__class__.installed:
            self.skipTest("the install step did not succeed")

    # ---------------------------------------------------------------------------------------------------------- the file
    def test_01_the_setup_program_itself(self):
        import pefile
        pe = pefile.PE(str(SETUP))
        strings = {}
        for info in getattr(pe, "FileInfo", []):
            for entry in info:
                for table in getattr(entry, "StringTable", []):
                    strings.update({k.decode(): v.decode(errors="replace").strip() for k, v in table.entries.items()})   # Inno pads with spaces
        self.assertEqual(strings.get("ProductName"), "TikTok Guard")
        self.assertEqual(strings.get("FileVersion"), VERSION)
        self.assertIn(pefile.RESOURCE_TYPE["RT_GROUP_ICON"], {e.id for e in pe.DIRECTORY_ENTRY_RESOURCE.entries})
        manifest = ""
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if entry.id == pefile.RESOURCE_TYPE["RT_MANIFEST"]:
                data = entry.directory.entries[0].directory.entries[0].data.struct
                manifest = pe.get_data(data.OffsetToData, data.Size).decode("utf-8", "replace")
        self.assertNotIn("requireAdministrator", manifest, "installing must not ask for an administrator password")
        pe.close()
        self.assertLess(SETUP.stat().st_size, 80 * 1024 * 1024)

    # ---------------------------------------------------------------------------------------------------------- install
    def test_02_a_silent_install_puts_everything_in_place_without_asking_for_administrator_rights(self):
        out = run_setup(dest=self.dest)
        self.assertEqual(out.returncode, 0, out.log[-1500:])
        self.__class__.installed = True
        self.assertEqual(listing(self.dest).keys() - {"unins000.exe", "unins000.dat", "unins000.msg"}, listing(STAGE).keys(),
                         "exactly the tested files, nothing more, nothing missing")
        for rel, size in listing(STAGE).items():
            self.assertEqual((self.dest / rel).stat().st_size, size, rel)
        for name in ("TikTok Guard.exe", "TikTok Guard Diagnostics.exe", "README.txt", "unins000.exe"):
            self.assertTrue((self.dest / name).is_file(), name)
        built = hashlib_of(T.EXE_DIR / "TikTok Guard.exe")
        self.assertEqual(hashlib_of(self.dest / "TikTok Guard.exe"), built, "the installed exe is the tested exe")

    def test_03_shortcuts_and_the_apps_and_features_entry(self):
        self.need_install()
        exe = str(self.dest / "TikTok Guard.exe")
        self.assertTrue((DESKTOP / "TikTok Guard.lnk").is_file(), "desktop shortcut")
        self.assertEqual(shortcut_target(DESKTOP / "TikTok Guard.lnk").lower(), exe.lower())
        for name, target in (("TikTok Guard.lnk", "TikTok Guard.exe"), ("TikTok Guard Diagnostics.lnk", "TikTok Guard Diagnostics.exe"),
                             ("Read me.lnk", "README.txt")):
            self.assertTrue((GROUP / name).is_file(), f"Start menu: {name}")
            self.assertEqual(shortcut_target(GROUP / name).lower(), str(self.dest / target).lower())
        self.assertTrue((GROUP / "Uninstall TikTok Guard.lnk").is_file())
        entry = registry_entry()
        self.assertIsNotNone(entry, "the entry in Windows Settings -> Apps")
        self.assertEqual(entry["DisplayName"], "TikTok Guard")
        self.assertEqual(entry["DisplayVersion"], VERSION)
        self.assertEqual(Path(entry["InstallLocation"].rstrip("\\")), self.dest)
        self.assertIn("unins000.exe", entry["UninstallString"])
        self.assertIn("TikTok Guard.exe", entry["DisplayIcon"])

    def test_04_without_the_desktop_task_no_desktop_shortcut_is_made(self):
        self.need_install()
        (DESKTOP / "TikTok Guard.lnk").unlink()                                  # the client chose "no" on the shortcut question
        out = run_setup(dest=self.dest, tasks="!desktopicon")
        self.assertEqual(out.returncode, 0, out.log[-800:])
        self.assertFalse((DESKTOP / "TikTok Guard.lnk").exists())
        out = run_setup(dest=self.dest, tasks="desktopicon")                      # and back to the default for the next steps
        self.assertEqual(out.returncode, 0, out.log[-800:])
        self.assertTrue((DESKTOP / "TikTok Guard.lnk").exists())

    # ---------------------------------------------------------------------------------------------------------- the app
    def test_05_the_installed_program_passes_the_whole_exe_test_suite(self):
        self.need_install()
        out = subprocess.run([sys.executable, "-X", "utf8", "-W", "ignore", str(HERE / "test_exe.py")], capture_output=True, text=True,
                             env={**os.environ, "TG_EXE_DIR": str(self.dest)}, timeout=580)
        tail = (out.stdout + out.stderr)[-1800:]
        self.assertEqual(out.returncode, 0, tail)
        self.assertRegex(out.stderr + out.stdout, r"Ran \d+ tests")

    def test_06_a_running_app_is_never_replaced(self):
        self.need_install()
        home = T.new_home()
        before = hashlib_of(self.dest / "TikTok Guard.exe")
        app = T.Running(["--no-window", "--port", "0"], home, exe=self.dest / "TikTok Guard.exe")           # the INSTALLED program, running
        try:
            app.wait_for(r"Open this address in a browser", 60)
            (self.dest / "marker-from-the-test.txt").write_text("x")
            out = run_setup(dest=self.dest)
            self.assertNotEqual(out.returncode, 0, "setup must refuse while the app is running")
            self.assertIn("currently running", out.log, out.log[-800:])                 # what the client would read on screen
            self.assertEqual(hashlib_of(self.dest / "TikTok Guard.exe"), before)
            out = subprocess.run([str(self.dest / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], capture_output=True,
                                 timeout=60, creationflags=0x08000000)
            time.sleep(3)
            self.assertTrue((self.dest / "TikTok Guard.exe").exists(), "the uninstaller must not remove a running app either")
            self.assertIsNotNone(registry_entry())
        finally:
            app.stop()
            (self.dest / "marker-from-the-test.txt").unlink(missing_ok=True)
        time.sleep(1)

    def test_07_installing_a_newer_version_over_the_old_one_keeps_the_data_and_does_not_duplicate_anything(self):
        self.need_install()
        home = T.new_home("tg-keep-")
        (home / "data").mkdir()
        (home / "data" / "my-accounts.txt").write_text("precious")
        out = run_setup(dest=self.dest)
        self.assertEqual(out.returncode, 0, out.log[-800:])
        self.assertEqual((home / "data" / "my-accounts.txt").read_text(), "precious")
        self.assertEqual(sum(1 for _ in GROUP.glob("TikTok Guard.lnk")), 1)
        self.assertEqual(listing(self.dest).keys() - {"unins000.exe", "unins000.dat", "unins000.msg"}, listing(STAGE).keys())

    # ---------------------------------------------------------------------------------------------------------- uninstall
    def test_08_uninstalling_removes_the_program_and_the_shortcuts_but_keeps_the_clients_data(self):
        self.need_install()
        REAL_DATA.mkdir(parents=True, exist_ok=True)
        sentinel = REAL_DATA / "kept-after-uninstall.txt"
        sentinel.write_text("my accounts")
        try:
            uninstall_silently(self.dest)
            self.assertFalse(self.dest.exists() and any(self.dest.iterdir()), "the program folder is gone")
            self.assertFalse((DESKTOP / "TikTok Guard.lnk").exists())
            self.assertFalse(GROUP.exists(), "the Start menu group is gone")
            self.assertIsNone(registry_entry(), "no entry left in Apps & features")
            self.assertTrue(sentinel.exists(), "a silent uninstall never deletes the client's data")
        finally:
            sentinel.unlink(missing_ok=True)
        self.__class__.installed = False

    # ---------------------------------------------------------------------------------------------------------- the wizard
    def test_09_the_real_wizard_next_install_finish_opens_the_app_and_it_can_be_removed_again(self):
        dest = self.base / "Wizard install" / "TikTok Guard"
        proc = subprocess.Popen([str(SETUP), f"/DIR={dest}", f"/LOG={self.base / 'wizard.log'}"])
        shots, clicked, started_app = 0, [], None
        try:
            wait_for(wizard, 60, "the setup window")
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                found = wizard()
                if not found:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.5)
                    continue
                hwnd = found[0]
                btn = buttons(hwnd)
                page = next((p for p in ("Finish", "Install", "Next") if p in btn or any(k.startswith(p) for k in btn)), None)
                key = next((k for k in btn if k.startswith(page)), None) if page else None
                if key and key != (clicked[-1] if clicked else None):
                    shots += 1
                    time.sleep(1.0)
                    picture(f"8-setup-{shots}-{key.strip(' <>').lower()}.png", hwnd)
                    click(btn[key][0])
                    clicked.append(key)
                    if page == "Finish":
                        break
                time.sleep(0.7)
            self.assertEqual(clicked, ["Next", "Finish"], "two clicks: Next on the shortcut question, Finish when it is installed")
            wait_for(lambda: (dest / "TikTok Guard.exe").exists(), 30, "the program files")
            # "Start TikTok Guard now" is ticked on the last page: the app window opens by itself
            wait_for(lambda: any(v[1] == "TikTok Guard" for v in T.visible_windows().values()), 90, "the app window to open after Finish")
            window = next((h, v) for h, v in T.visible_windows().items() if v[1] == "TikTok Guard")
            time.sleep(4)
            picture("8-setup-4-the-app-after-finish.png", window[0])
            started_app = window[1][2]
            subprocess.run(["taskkill", "/PID", str(started_app)], capture_output=True, creationflags=0x08000000)          # the X of the window
            wait_for(lambda: not any(v[1] == "TikTok Guard" for v in T.visible_windows().values()), 60, "the app window to close")
            time.sleep(6)
            self.assertEqual([p for p in T.processes_using(dest)], [], "closing the window ended the program")
        finally:
            if proc.poll() is None:
                proc.kill()
            stop_everything_of_the_tests(dest)
            if not self.data_existed:
                stop_everything_of_the_tests(REAL_DATA)
        self.assertIsNotNone(registry_entry())
        time.sleep(2)
        uninstall_silently(dest)
        self.assertIsNone(registry_entry())
        self.assertFalse((DESKTOP / "TikTok Guard.lnk").exists())


def hashlib_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


if __name__ == "__main__":
    outcome = unittest.main(verbosity=2, exit=False).result
    passed = outcome.wasSuccessful() and not outcome.skipped and outcome.testsRun >= 9
    marker = HERE / "build" / "tests-passed.txt"            # `build.py package` delivers only a Setup that these tests passed for
    marker.unlink(missing_ok=True)
    if passed:
        marker.write_text(hashlib_of(SETUP), encoding="utf-8")
    sys.exit(0 if passed else 1)
