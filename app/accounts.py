"""Extra TikTok accounts for the post_reply() fallback chain.

Each account is its own saved real-Brave profile folder (the same real-identity
mode as the master profile in .env's BFP_USER_DATA_DIR): a person logs in ONCE
in a normal, visible Brave window, the folder is kept, and from then on the bot
reuses that folder exactly as it is - cookies, localStorage, IndexedDB native.
Nothing is injected into it and no identity is layered on top.

add_account() deliberately keeps automation out of the login itself: it launches
the browser, navigates to the login page, then DISCONNECTS its own CDP client.
While the person logs in, the only thing touching the browser is a read-only
browser-level Storage.getCookies poll (no page attach, no Runtime.enable).
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import config

from app.bfp_loader import ensure_bfp_on_path  # noqa: E402

ensure_bfp_on_path()
import bfp  # noqa: E402

LOGIN_URL = "https://www.tiktok.com/login"
_SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")

_WHO_JS = r"""(() => { try {
  const d = JSON.parse(document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__').textContent);
  const ctx = (d['__DEFAULT_SCOPE__'] || {})['webapp.app-context'] || {};
  const u = ctx.user || {};
  return JSON.stringify({uniqueId: u.uniqueId || null, nickName: u.nickName || null,
                         region: ctx.region || u.region || null});
} catch (e) { return JSON.stringify({err: String(e).slice(0, 80)}); } })()"""


def _cookies(ws_url: str) -> list:
    """All cookies of the default browser context, over a throwaway browser-level socket."""
    ws = bfp.WSClient(ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        for _ in range(20):
            frame = json.loads(ws.recv(timeout=10))
            if frame.get("id") == 1:
                return (frame.get("result") or {}).get("cookies") or []
    finally:
        ws.close()
    return []


def has_login_cookie(cookies: list) -> bool:
    """Is one of TikTok's session cookies present (with a value) among these browser cookies?"""
    return any(c.get("name") in _SESSION_COOKIES and c.get("value")
               and "tiktok.com" in c.get("domain", "") for c in cookies)


def _logged_in(ws_url: str) -> bool:
    try:
        return has_login_cookie(_cookies(ws_url))
    except Exception:  # noqa: BLE001
        return False


def _write_registry(accounts: list, **extra) -> None:
    with _REG_LOCK:
        try:
            data = json.loads(config.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        data["accounts"] = accounts
        data.update(extra)
        tmp = config.ACCOUNTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(8):                     # a reader holding the file for a moment makes Windows refuse the swap
            try:
                os.replace(tmp, config.ACCOUNTS_FILE)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))


IDENTIFY_ATTEMPTS = 3                          # page loads a session check makes before it gives up naming the user
_REG_LOCK = config.REGISTRY_LOCK              # every reader AND writer of data/accounts.json in this process takes it
SESSION_LISTENERS: list = []                  # fn(account_name, "ok" | "expired"), called when a stored state changes


def read_identity(page, wait_s: float = 12.0, step_s: float = 1.5, *, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The page's own app-context names the logged-in user - but a few seconds AFTER the page loaded (measured: a fixed
    3 s wait read nothing from an account that works), so ask repeatedly until it does or `wait_s` has passed.
    -> {uniqueId, nickName, region} or {}."""
    end = clock() + wait_s
    while True:
        try:
            who = json.loads(page.evaluate(_WHO_JS) or "{}")
        except Exception:  # noqa: BLE001
            who = {}
        if who.get("uniqueId"):
            return who
        if clock() >= end:
            return {}
        sleep(step_s)


def identify(user_data_dir: str, browser_path: str | None = None, *, max_s: float = 40.0) -> dict:
    """Which TikTok user is logged in inside this saved folder? Launches it headless and reads the page's own app-context.
    Safe to run in its own thread: the folder is passed explicitly (the BFP_USER_DATA_DIR environment variable is
    process-wide and the service's browser thread flips it), and bfp's per-folder lock makes it wait for a scan or a post that
    is using the same profile. `max_s` bounds the whole check (a slow TikTok must not keep a browser open for minutes).
    -> {"ok": True, "logged_in": True,  "who": {uniqueId, nickName, region}}   logged in, and who
       {"ok": True, "logged_in": False, "who": None}   the page loaded and TikTok's session cookie is gone: logged out
       {"ok": True, "logged_in": None,  "who": None}   cookie still there but the page named nobody: could not confirm
       {"ok": False, "error": "..."}                   the check itself failed (browser did not start ...)"""
    exe = browser_path or os.environ.get("BFP_PATCHED_CHROMIUM", "")
    deadline = time.monotonic() + max_s
    try:
        browser = bfp.CDPBrowser(bfp.ProfileFactory().desktop(), executable=exe, headless=True,
                                 user_data_dir=Path(user_data_dir))
        # an explicit folder would make bfp lay a synthetic identity over the profile; every scan/post uses the browser's
        # NATURAL one, so the check must too (the same switch bfp's real-profile mode flips)
        browser.preserve_real_identity = True
        browser.start()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "who": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        page = browser.driver
        # TikTok sometimes serves the guest version of its page even to a valid session (measured: 2 of 3 loads of a working
        # account, then the 3rd named the user), so a page that names nobody is loaded again before anything is concluded
        for _attempt in range(IDENTIFY_ATTEMPTS):
            left = deadline - time.monotonic()
            if left < 6.0:
                break
            page.goto("https://www.tiktok.com/", timeout=min(20.0, left))
            who = read_identity(page, min(8.0, max(1.0, deadline - time.monotonic())))
            if who:
                return {"ok": True, "logged_in": True, "who": who}
        # No user on the page. Only a missing session cookie proves nobody is logged in; a cookie that is still there but
        # a page that will not name its user is "could not confirm", never "expired" (that would send a person to log in
        # again for an account that works).
        has_cookie = has_login_cookie(_cookies(page.browser_info["webSocketDebuggerUrl"]))
        return {"ok": True, "logged_in": None if has_cookie else False, "who": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "who": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        browser.stop()


def _identify(user_data_dir: str, browser_path: str | None = None) -> dict:
    """{} if nobody is logged in / unreadable (the form refresh_identity() and the CLI use)."""
    return identify(user_data_dir, browser_path).get("who") or {}


def record_session(name: str, state: str) -> bool:
    """A scan / post attempt just learned whether `name`'s TikTok session works ('ok' | 'expired'). Remembered in the
    registry (only when it changed) and announced to SESSION_LISTENERS, so the desktop app can offer 'log in again'.
    Names that are not registered accounts (or the master) are ignored. -> True when something changed."""
    with _REG_LOCK:
        try:
            data = json.loads(config.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        accounts = [a for a in data.get("accounts", []) if a.get("name") and a.get("user_data_dir")]
        entry = next((a for a in accounts if a["name"] == name), None)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if entry is not None:
            if entry.get("session") == state:
                return False
            entry["session"], entry["session_checked_at"] = state, now
            _write_registry(accounts)
        elif name == config.BFP_PROFILE_NAME:
            if data.get("master_session") == state:
                return False
            _write_registry(accounts, master_session=state, master_session_checked_at=now)
        else:
            return False
    for fn in list(SESSION_LISTENERS):
        try:
            fn(name, state)
        except Exception:  # noqa: BLE001
            pass
    return True


def remove_profile_folder(user_data_dir: str | os.PathLike, attempts: int = 6) -> bool:
    """Delete a saved account folder (<ACCOUNTS_DIR>/<name>/ with its 'User Data'). Only ever inside
    config.ACCOUNTS_DIR: the master folder and anything outside are refused. True when it is gone."""
    root = Path(config.ACCOUNTS_DIR).resolve()
    try:
        rel = Path(user_data_dir).resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    if not rel.parts:
        return False
    top = root / rel.parts[0]
    for attempt in range(attempts):                # a browser that just closed can hold files for a moment (Windows)
        shutil.rmtree(top, ignore_errors=True)
        if not top.exists():
            return True
        time.sleep(0.5 * (attempt + 1))
    return not top.exists()


PENDING_REMOVAL = ".remove-me"            # a marker inside an account folder: "this account was removed - delete me as soon as nobody holds me"


def _account_top(user_data_dir: str | os.PathLike) -> Path | None:
    """<ACCOUNTS_DIR>/<name> of a folder inside ACCOUNTS_DIR; None for anything outside it (the master folder, a person's files)."""
    root = Path(config.ACCOUNTS_DIR).resolve()
    try:
        rel = Path(user_data_dir).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return root / rel.parts[0] if rel.parts else None


def mark_for_removal(user_data_dir: str | os.PathLike) -> bool:
    """Leave the marker (see PENDING_REMOVAL) so that a folder that could not be deleted now is deleted later - by the retry that follows,
    or at the next start of the app. A removed account's cookies must never stay on the disk."""
    top = _account_top(user_data_dir)
    if top is None or not top.exists():
        return False
    try:
        (top / PENDING_REMOVAL).write_text("removed by the person; delete when free\n", encoding="utf-8")
        return True
    except OSError:
        return False


def finish_pending_removals() -> list:
    """Delete every account folder that carries the marker. -> the names of the folders that are gone."""
    root = Path(config.ACCOUNTS_DIR)
    done = []
    try:
        folders = [d for d in root.iterdir() if d.is_dir() and (d / PENDING_REMOVAL).exists()]
    except OSError:
        return done
    for top in folders:
        if remove_profile_folder(top / "User Data"):
            done.append(top.name)
    return done


def refresh_identity(name: str) -> dict:
    """Fill in username/region of a registered account (e.g. when its window was
    closed by hand before add_account could ask), and disable it if it turns out
    to be the same TikTok user as the master account or another registered one."""
    accounts = config.load_accounts()
    acct = next((a for a in accounts if a["name"] == name), None)
    if acct is None:
        raise KeyError(name)
    who = _identify(acct["user_data_dir"], acct.get("browser"))
    if not who:
        return {"name": name, "error": "no logged-in TikTok user found in this folder"}
    try:
        master = json.loads(config.ACCOUNTS_FILE.read_text(encoding="utf-8")).get("master_username")
    except Exception:  # noqa: BLE001
        master = None
    if not master and config.BFP_MASTER_USER_DATA_DIR:
        master = _identify(config.BFP_MASTER_USER_DATA_DIR).get("uniqueId")
    acct.update(username=who.get("uniqueId"), nickname=who.get("nickName"), region=who.get("region"))
    others = {a.get("username") for a in accounts if a["name"] != name}
    dup = acct["username"] == master or acct["username"] in others
    acct["enabled"] = not dup
    _write_registry(accounts, master_username=master)
    return {"name": name, "username": acct["username"], "region": acct["region"],
            "master_username": master, "duplicate": dup}


def next_name() -> str:
    taken = {a["name"] for a in config.load_accounts()}
    n = 1
    while f"real-account-{n}" in taken:
        n += 1
    return f"real-account-{n}"


def add_account(name: str | None = None, *, done_flag: Path | None = None,
                timeout_s: float = 1800.0) -> dict:
    """Open a visible real-Brave window on a fresh saved folder, wait for the
    person to log in, then close it gracefully and register the account.

    Finishes when the login cookie is present AND either `done_flag` appears
    or the person closes the window (or timeout_s passes). Never registers an
    account that is not logged in.
    """
    name = name or next_name()
    udd = config.ACCOUNTS_DIR / name / "User Data"
    exe = os.environ.get("BFP_PATCHED_CHROMIUM", "")
    if not exe or not Path(exe).exists():
        raise RuntimeError("BFP_PATCHED_CHROMIUM must point at the real browser (brave.exe)")
    if done_flag:
        done_flag.unlink(missing_ok=True)

    saved_env = os.environ.get("BFP_USER_DATA_DIR")
    os.environ["BFP_USER_DATA_DIR"] = str(udd)       # real-identity mode for bfp.CDPBrowser
    try:
        browser = bfp.CDPBrowser(bfp.ProfileFactory().desktop(), executable=exe, headless=False,
                                 extra_args=["--start-maximized"])
    finally:
        if saved_env is None:
            os.environ.pop("BFP_USER_DATA_DIR", None)
        else:
            os.environ["BFP_USER_DATA_DIR"] = saved_env
    saved_lp = os.environ.get("BFP_LOW_PRIORITY")
    os.environ["BFP_LOW_PRIORITY"] = "0"                  # a window a person is typing into must not be sluggish
    try:
        browser.start()
    finally:
        if saved_lp is None:
            os.environ.pop("BFP_LOW_PRIORITY", None)
        else:
            os.environ["BFP_LOW_PRIORITY"] = saved_lp
    result: dict = {"name": name, "user_data_dir": str(udd), "logged_in": False}
    try:
        d = browser.driver
        ws_url = d.browser_info["webSocketDebuggerUrl"]
        d.send("Page.navigate", url=LOGIN_URL)
        time.sleep(2)
        d.close()                                    # no CDP client attached while the person logs in
        print(f"[{name}] window is open - log in to TikTok now (use phone/email/username).", flush=True)

        deadline = time.monotonic() + timeout_s
        seen_login = False
        announced = False
        while time.monotonic() < deadline:
            if browser.proc.poll() is not None:
                print(f"[{name}] window was closed", flush=True)
                break
            if _logged_in(ws_url):
                seen_login = True
                if not announced:
                    print(f"[{name}] LOGIN DETECTED - waiting for your confirmation to save", flush=True)
                    announced = True
                if done_flag and done_flag.exists():
                    break
            time.sleep(3)
        result["logged_in"] = seen_login

        # who is it? (needs a page attach, so only now that the login is over)
        if seen_login and browser.proc.poll() is None:
            try:
                page = bfp.CDPDriver(browser.port, process=browser.proc).connect()
                who = json.loads(page.evaluate(_WHO_JS) or "{}")
                if not who.get("uniqueId"):
                    page.goto("https://www.tiktok.com/")
                    time.sleep(3)
                    who = json.loads(page.evaluate(_WHO_JS) or "{}")
                page.close()
                result.update(username=who.get("uniqueId"), nickname=who.get("nickName"),
                              region=who.get("region"))
            except Exception as exc:  # noqa: BLE001
                result["who_error"] = str(exc)[:120]
    finally:
        browser.stop()                               # graceful: lets cookies/localStorage flush to disk

    if result["logged_in"]:
        accounts = config.load_accounts()
        dup = next((a for a in accounts if result.get("username") and a.get("username") == result["username"]
                    and a["name"] != name), None)
        result["duplicate_of"] = dup["name"] if dup else None
        entry = {"name": name, "user_data_dir": str(udd), "username": result.get("username"),
                 "nickname": result.get("nickname"), "region": result.get("region"),
                 "added": time.strftime("%Y-%m-%d %H:%M:%S"), "enabled": not dup}
        accounts = [a for a in accounts if a["name"] != name] + [entry]
        _write_registry(accounts)
        result["registered"] = True
        if not result.get("username"):               # window closed by hand before we could ask
            try:
                result["identity"] = refresh_identity(name)
            except Exception as exc:  # noqa: BLE001
                result["identity"] = {"error": str(exc)[:120]}
    else:
        result["registered"] = False
    print(f"[{name}] result: {json.dumps(result, ensure_ascii=False)}", flush=True)
    return result
