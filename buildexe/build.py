"""Build, check and package TikTok Guard for Windows.

    python buildexe/build.py build        PyInstaller -> buildexe/build/dist/TikTok Guard/   (+ the checks below)
    python buildexe/build.py scan         look for secrets / personal data / forbidden files in the build (inside the archives too)
    python buildexe/build.py installer    Setup.exe (Inno Setup, buildexe/installer.iss) from the build -> buildexe/build/installer/
    python buildexe/build.py package      production/: Setup.exe + README + checksums, and the portable ZIP in production/portable/

Order: build -> scan -> `python buildexe/test_exe.py` -> installer -> `python buildexe/test_installer.py` -> package.
Nothing here talks to TikTok. Inno Setup is looked for in buildexe/tools/InnoSetup, then in the usual install folders.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
BUILD = HERE / "build"
DIST = BUILD / "dist" / "TikTok Guard"
PRODUCTION = PROJECT / "production"
BFP_DIR = Path(os.environ.get("BFP_DIR", r"..\bfp"))
sys.path.insert(0, str(PROJECT))

SECRET_KEY_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CHAT_ID", "COOKIE")
SECRET_PATTERNS = {
    "OpenRouter key": re.compile(rb"sk-or-v1-[A-Za-z0-9]{20,}"),
    "Telegram bot token": re.compile(rb"\b\d{8,}:[A-Za-z0-9_-]{30,}"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
FORBIDDEN_NAMES = {".env", "accounts.json", "api_token.txt", "bot.db", "pending_approvals.json", "seen_comments.json", "desktop.log"}
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".log")                                    # databases and logs
FORBIDDEN_DIRS = {"data", "network", "bfp_runs", "tests", "demo_output", "output", "screenshots", "deploy", "graphify-out", "patchright",
                  "ui-profile", "backups", "logs"}                                                       # our own folders; never a library's


def version() -> str:
    from app import __version__
    return __version__


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# ------------------------------------------------------------------------------------------------------------------ build
def stop_copies_running_from(folder: Path) -> None:
    """A copy of the exe still running from the old build (a test that was interrupted) holds its DLLs: Windows cannot delete them."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
         "Select-Object ProcessId, Name, ExecutablePath | ConvertTo-Json -Compress"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60, creationflags=0x08000000).stdout.strip()
    needle = str(folder).lower()
    for proc in (json.loads(out) if out else []):
        if needle in (proc.get("ExecutablePath") or "").lower() and proc["ProcessId"] != os.getpid():
            print(f"stopping {proc['Name']} (pid {proc['ProcessId']}) that still runs from {folder}")
            subprocess.run(["taskkill", "/PID", str(proc["ProcessId"]), "/T", "/F"], capture_output=True)
    time.sleep(1)


def build() -> int:
    os.environ["TG_VERSION"] = version()
    sys.path.insert(0, str(HERE))
    import make_icon
    make_icon.main()
    stop_copies_running_from(BUILD)
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    started = time.monotonic()
    cmd = [sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(BUILD / "dist"),
           "--workpath", str(BUILD / "work"), str(HERE / "tiktok_guard.spec")]
    print("running:", " ".join(cmd))
    code = subprocess.run(cmd, cwd=PROJECT).returncode
    if code != 0:
        print(f"PyInstaller failed (exit {code})")
        return code
    print(f"\nbuilt in {time.monotonic() - started:.0f} s -> {DIST}")
    return report_structure()


def report_structure() -> int:
    problems = []
    for need in ("TikTok Guard.exe", "TikTok Guard Diagnostics.exe", "_internal/client_defaults.env", "_internal/app/web/desktop.html",
                 "_internal/app/web/console.html", "_internal/playwright/driver/node.exe"):
        if not (DIST / need).exists():
            problems.append(f"missing: {need}")
    size = folder_size(DIST)
    print(f"size of the app folder: {human(size)}")
    biggest = sorted((f for f in DIST.rglob("*") if f.is_file()), key=lambda f: -f.stat().st_size)[:8]
    for f in biggest:
        print(f"   {human(f.stat().st_size):>9}  {f.relative_to(DIST)}")
    for p in problems:
        print("PROBLEM", p)
    return 1 if problems else 0


# ------------------------------------------------------------------------------------------------------------------- scan
def secret_values() -> dict:
    """The real secrets of THIS machine (from .env and the API token file): what must never be inside the build."""
    found = {}
    env = PROJECT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                value = value.split(" #")[0].strip().strip("'\"")
                if len(value) >= 6 and any(w in key.upper() for w in SECRET_KEY_WORDS):
                    found[f".env {key.strip()}"] = value.encode()
    token = PROJECT / "data" / "api_token.txt"
    if token.exists() and token.read_text(errors="replace").strip():
        found["data/api_token.txt"] = token.read_text(errors="replace").strip().encode()
    return found


def archive_blobs(exe: Path):
    """Everything inside an exe's own archive (PyInstaller PKG), the compiled modules (PYZ) included: -> (name, bytes)."""
    import marshal
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    reader = CArchiveReader(str(exe))
    for name in reader.toc:
        try:
            data = reader.extract(name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, bytes):
            yield f"{exe.name}!{name}", data
        if name.endswith(".pyz") or name.startswith("PYZ"):
            with tempfile.TemporaryDirectory() as tmp:
                pyz = Path(tmp) / "x.pyz"
                pyz.write_bytes(data)
                z = ZlibArchiveReader(str(pyz))
                for mod in z.toc:
                    try:
                        code = z.extract(mod)
                        yield f"{exe.name}!PYZ!{mod}", (code if isinstance(code, bytes) else marshal.dumps(code))
                    except Exception:  # noqa: BLE001
                        continue


def all_blobs(root: Path):
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix == ".exe" and f.parent == root:
            yield from archive_blobs(f)
        elif f.suffix == ".zip" or f.name == "base_library.zip":
            try:
                with zipfile.ZipFile(f) as z:
                    for info in z.infolist():
                        yield f"{f.name}!{info.filename}", z.read(info)
            except zipfile.BadZipFile:
                pass
        if f.stat().st_size < 400 * 1024 * 1024:
            yield str(f.relative_to(root)), f.read_bytes()


def scan(root: Path = DIST) -> int:
    if not root.exists():
        print(f"nothing to scan: {root}")
        return 2
    secrets = secret_values()
    print(f"scanning {root} for {len(secrets)} secret value(s) of this machine and {len(SECRET_PATTERNS)} secret patterns ...")
    problems, blobs = [], 0
    for rel in (str(f.relative_to(root)) for f in root.rglob("*") if f.is_file() or f.is_dir()):
        parts = Path(rel).parts
        name = parts[-1]
        if name in FORBIDDEN_NAMES:
            problems.append(f"forbidden file: {rel}")
        if any(p in FORBIDDEN_DIRS for p in parts[:2]):                       # at the top of the app folder, never deeper (a library may have its own)
            problems.append(f"forbidden folder content: {rel}")
        if name.lower().endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"forbidden file type: {rel}")
    for name, blob in all_blobs(root):
        blobs += 1
        for label, value in secrets.items():
            if value in blob:
                problems.append(f"SECRET {label} found in {name}")
        for label, pattern in SECRET_PATTERNS.items():
            m = pattern.search(blob)
            if m and not (b"xxxxxxxx" in m.group(0) or b"AAxxxx" in m.group(0)):          # the placeholders of .env.example
                problems.append(f"{label} pattern found in {name}")
    print(f"looked inside {blobs} files / archive members")
    for p in problems:
        print("PROBLEM", p)
    if not problems:
        print("clean: no secrets, no data, no accounts, no screenshots, no databases in the build")
    return 1 if problems else 0


# ------------------------------------------------------------------------------------------------------------------ installer
STAGE = BUILD / "stage" / "TikTok Guard"
INSTALLER_DIR = BUILD / "installer"


def find_iscc() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for path in (HERE / "tools" / "InnoSetup" / "ISCC.exe", local / "Programs" / "Inno Setup 6" / "ISCC.exe",
                 Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
                 Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe"):
        if path.is_file():
            return path
    return None


def client_readme() -> str:
    return (HERE / "client_README.txt").read_text(encoding="utf-8").replace("{VERSION}", version())


def installer() -> int:
    """The folder that is installed = the tested build + the README; Inno Setup packs it into one Setup.exe."""
    if not (DIST / "TikTok Guard.exe").exists():
        print("build first")
        return 2
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup (ISCC.exe) not found - see buildexe/tools or https://jrsoftware.org/isinfo.php")
        return 2
    ver = version()
    if STAGE.parent.exists():
        shutil.rmtree(STAGE.parent)
    shutil.copytree(DIST, STAGE)
    (STAGE / "README.txt").write_text(client_readme(), encoding="utf-8")
    if INSTALLER_DIR.exists():
        shutil.rmtree(INSTALLER_DIR)
    INSTALLER_DIR.mkdir(parents=True)
    started = time.monotonic()
    cmd = [str(iscc), "/Q", f"/DSourceDir={STAGE}", f"/DAppVersion={ver}", f"/DOutputDir={INSTALLER_DIR}", str(HERE / "installer.iss")]
    print("running:", " ".join(cmd))
    code = subprocess.run(cmd, cwd=HERE).returncode
    (BUILD / "tests-passed.txt").unlink(missing_ok=True)               # a new Setup has not been tested yet
    setup = INSTALLER_DIR / f"TikTok-Guard-Setup-{ver}.exe"
    if code != 0 or not setup.is_file():
        print(f"Inno Setup failed (exit {code})")
        return code or 1
    print(f"built in {time.monotonic() - started:.0f} s -> {setup}  ({human(setup.stat().st_size)})")
    return 0


# --------------------------------------------------------------------------------------------------------------- package
def package() -> int:
    ver = version()
    setup = INSTALLER_DIR / f"TikTok-Guard-Setup-{ver}.exe"
    if not (DIST / "TikTok Guard.exe").exists() or not setup.is_file() or not (STAGE / "README.txt").is_file():
        print("build and installer first")
        return 2
    if scan() != 0:
        print("refusing to package a build that did not pass the scan")
        return 1
    marker = BUILD / "tests-passed.txt"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != sha256(setup):
        print("refusing to package: test_installer.py has not passed for this exact Setup file - run it (and test_exe.py) first")
        return 1
    if setup.stat().st_mtime < (DIST / "TikTok Guard.exe").stat().st_mtime:
        print("the installer is older than the build - run `installer` again")
        return 1
    if PRODUCTION.exists():
        shutil.rmtree(PRODUCTION)
    (PRODUCTION / "portable").mkdir(parents=True)
    shutil.copy2(setup, PRODUCTION / setup.name)
    (PRODUCTION / "README.txt").write_text(client_readme(), encoding="utf-8")
    zip_path = PRODUCTION / "portable" / f"TikTok-Guard-{ver}-portable.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(STAGE.rglob("*")):
            if f.is_file():
                z.write(f, Path("TikTok Guard") / f.relative_to(STAGE))
    delivered = [PRODUCTION / setup.name, zip_path]
    lines = [f"SHA-256 checksums - TikTok Guard {ver}", ""]
    lines += [f"{sha256(f)}  {f.relative_to(PRODUCTION).as_posix()}" for f in delivered]
    lines += ["", "Check on Windows:   certutil -hashfile <file> SHA256   (or in PowerShell: Get-FileHash <file>)"]
    (PRODUCTION / "CHECKSUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    import PyInstaller
    info = {
        "product": "TikTok Guard", "version": ver, "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0], "pyinstaller": PyInstaller.__version__, "inno setup": str(find_iscc()),
        "bfp.py sha256": sha256(BFP_DIR / "bfp.py"),
        "installed size": human(folder_size(STAGE)), "setup": human((PRODUCTION / setup.name).stat().st_size),
        "setup sha256": sha256(PRODUCTION / setup.name), "portable zip": human(zip_path.stat().st_size),
    }
    (PRODUCTION / "BUILD-INFO.txt").write_text("\n".join(f"{k}: {v}" for k, v in info.items()) + "\n", encoding="utf-8")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    commands = {"build": build, "scan": scan, "installer": installer, "package": package}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    sys.exit(commands[sys.argv[1]]())
