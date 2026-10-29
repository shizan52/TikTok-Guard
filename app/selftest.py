"""`TikTok Guard.exe --self-test`: does THIS copy, on THIS computer, really work? (Not just "are the files there".)

Each step is something that has gone wrong on someone's computer before; each says in plain words what it found and what to do:

  program files     the screen's page and the libraries the program needs are all present
  data folder       the folder the program keeps its settings in can be written to
  database          a database can be created, written and read back
  web browser       Chrome, Edge or Brave is installed
  automation        a REAL browser is started, driven and closed (the same way the app reads and replies to comments)
  the app           the app's own server starts, answers, serves its screen and stops
  internet          tiktok.com can be reached

Nothing is sent to TikTok: the browser only opens a page that is part of the test itself.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import config
from app import browsers, health


def _item(cid: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def _remove(folder: Path) -> None:
    """Delete a throw-away folder. Windows keeps a file locked for a moment after a program that used it has ended: try again."""
    for _ in range(10):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.3)


def _program_files() -> dict:
    page = config.RESOURCE_DIR / "app" / "web" / "desktop.html"
    missing = [str(p) for p in (page, config.RESOURCE_DIR / "app" / "web" / "console.html") if not p.is_file()]
    if missing:
        return _item("files", "Program files", "fail", f"These files of the program are missing: {', '.join(missing)}.",
                     "Download TikTok Guard again and unpack the WHOLE folder (not only the .exe).")
    libs = []
    try:
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        import bfp  # noqa: F401
        import greenlet  # noqa: F401
        import playwright.sync_api  # noqa: F401
        libs = ["bfp", "playwright", "greenlet"]
        driver = Path(sys.modules["playwright"].__file__).parent / "driver"
        if not (driver / ("node.exe" if sys.platform == "win32" else "node")).exists():
            return _item("files", "Program files", "fail", "The browser driver that comes with the program is missing.",
                         "Download TikTok Guard again and unpack the WHOLE folder.")
    except Exception as exc:  # noqa: BLE001
        return _item("files", "Program files", "fail", f"A library of the program cannot be loaded ({type(exc).__name__}: {str(exc)[:120]}).",
                     "Download TikTok Guard again and unpack the WHOLE folder.")
    from app import __version__
    return _item("files", "Program files", "ok", f"Version {__version__}; the screen and {', '.join(libs)} are present"
                 + (" (packaged)." if config.FROZEN else "."))


def _database() -> dict:
    from app.db import DB
    folder = Path(tempfile.mkdtemp(prefix="tg-selftest-db-"))
    db = None
    try:
        db = DB(folder / "test.db")
        db.set_setting("selftest", "ok")
        if db.get_setting("selftest") != "ok":
            return _item("database", "Database", "fail", "A value written to the database could not be read back.", "Free some disk space and try again.")
        return _item("database", "Database", "ok", "A database can be created, written and read.")
    except Exception as exc:  # noqa: BLE001
        return _item("database", "Database", "fail", f"The database does not work ({type(exc).__name__}: {str(exc)[:120]}).",
                     "Check that the disk is not full or write-protected.")
    finally:
        if db is not None:
            db.close()
        _remove(folder)


def _automation() -> dict:
    """Start a real browser through the program's own engine, load a page, run a script, close it."""
    chosen = browsers.choose("auto")
    if chosen is None:
        return _item("automation", "Browser automation", "fail", "No browser to drive.", "Install Google Chrome, Microsoft Edge or Brave.")
    folder = Path(tempfile.mkdtemp(prefix="tg-selftest-browser-"))
    saved = {k: os.environ.get(k) for k in ("BFP_USER_DATA_DIR", "BFP_PATCHED_CHROMIUM")}
    started = time.monotonic()
    try:
        from app import tiktok_monitor as tm          # puts bfp on the path
        import bfp
        os.environ["BFP_USER_DATA_DIR"] = str(folder / "profile")
        os.environ["BFP_PATCHED_CHROMIUM"] = chosen["path"]
        vault = bfp.ProfileVault(root=str(folder / "vault"))
        profile = vault.new(name="selftest", kind="desktop", country="US")
        with bfp.Session(profile=profile, vault=vault, proxy=None, name="selftest", backend=config.BFP_BACKEND,
                         out_dir=str(folder / "run"), headless=True, hitl="off") as s:
            s.page.goto("data:text/html,<title>self-test</title><body>ok</body>", wait_until="domcontentloaded", timeout=30000)
            answer, title = s.page.evaluate("() => 20 + 22"), s.page.title()
            s.finish()
        if answer != 42 or title != "self-test":
            return _item("automation", "Browser automation", "fail", f"The browser started but answered wrongly ({answer!r}, {title!r}).",
                         "Update the browser and try again.")
        return _item("automation", "Browser automation", "ok",
                     f"{chosen['name']} was started, driven and closed in {time.monotonic() - started:.1f} s.")
    except Exception as exc:  # noqa: BLE001
        return _item("automation", "Browser automation", "fail",
                     f"{chosen['name']} could not be driven ({type(exc).__name__}: {str(exc)[:160]}).",
                     "Close every window of that browser and try again; if it keeps failing, install Google Chrome.")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _remove(folder)


def _the_app() -> dict:
    """The app's own server: start it on a free port with a throw-away database, ask it three things, stop it."""
    from app.db import DB
    from app.desktop import DesktopApp
    folder = Path(tempfile.mkdtemp(prefix="tg-selftest-app-"))
    app = db = None
    try:
        db = DB(folder / "app.db")
        app = DesktopApp(db=db, port=0, token="selftest-" + "x" * 24, clean_leftovers=False, backups=False, maintenance=False)
        base = app.start()

        def get(path: str, token: bool = False):
            req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {app.token_value}"} if token else {})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        ping = json.loads(get("/api/ping"))
        page = get("/").decode("utf-8", "replace")
        health_state = json.loads(get("/api/health", token=True))
        if ping.get("app") != "tiktok-guard" or "TikTok Guard" not in page or not health_state.get("items"):
            return _item("app", "The app", "fail", "The app's server started but did not answer as expected.", "Start the program again.")
        return _item("app", "The app", "ok", "The app's server started, answered, showed its screen and stopped.")
    except Exception as exc:  # noqa: BLE001
        return _item("app", "The app", "fail", f"The app's server did not work ({type(exc).__name__}: {str(exc)[:160]}).",
                     "Check that no firewall or security program blocks the program from using localhost.")
    finally:
        if app is not None:
            try:
                app.stop()
            except Exception:  # noqa: BLE001
                pass
        if db is not None:
            db.close()
        _remove(folder)


def run() -> list:
    """All the steps. Browser-dependent ones are skipped (not failed twice) when the browser itself is missing."""
    items = [_program_files(), health._data_writable(), _database(), health._browsers()]
    items.append(_automation() if items[-1]["status"] == "ok" else
                 _item("automation", "Browser automation", "fail", "Skipped: no browser.", "Install Google Chrome, Microsoft Edge or Brave."))
    items.append(_the_app())
    items.append(health._internet())
    return items
