"""TikTok accounts for the desktop app: add one by logging in to a fresh browser window, keep them healthy, organise them.

"Add account" (start_add) in short
  1. a FRESH window of the person's Chrome / Brave / Edge opens on TikTok's login page, on a profile folder of its own
     (never the person's own browser profile) - they log in the way they always do;
  2. nothing automated touches that window while they type: only a read-only cookie poll runs in the background;
  3. the moment TikTok's session cookie appears the login is DETECTED, the session gets a few seconds to settle, the
     app reads who logged in (@username, region) and closes the window itself - nothing to click or confirm;
  4. then the decisions a person would otherwise have to make: a brand-new account is added; the same user logging in
     again REFRESHES the account that had expired (no duplicate); the same user while that account still works is
     refused as a duplicate; the page's own (master) account is never added twice;
  5. a window closed before the login finished simply cancels it and leaves nothing behind.

Everything reports through the EventBus (`account.flow`, `accounts.changed`, `account.session`, `account.checked`), so
the UI can show progress without polling. Browser work that must not collide with a scan or a post (a session check, a
folder removal) goes through `worker`, which the service points at its single browser thread.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import config
from app import accounts as acc
from app import browsers, orphans, sysguard
from app.events import EventBus

LOGIN_URL = acc.LOGIN_URL
ACTIVE = ("starting", "window_open", "login_detected", "verifying", "saving")
STATE_TEXT = {
    "starting": "Opening a fresh browser window...",
    "window_open": "Log in to TikTok in the window that just opened.",
    "login_detected": "Login detected - letting the session settle...",
    "verifying": "Checking which account logged in...",
    "saving": "Saving the account and closing the window...",
}


class AccountError(Exception):
    """A request the manager refuses; `code` is machine-readable (busy, no_browser, not_found, master ...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class RealWindow:
    """A visible Chromium window on a fresh profile folder. No automation client stays attached while the person logs
    in: only browser-level cookie reads (the same quiet approach as app.accounts.add_account)."""

    def __init__(self, browser, ws_url: str) -> None:
        self.browser, self.ws_url = browser, ws_url

    @classmethod
    def launch(cls, exe: str, user_data_dir: Path) -> "RealWindow":
        bfp = acc.bfp
        # The folder is passed explicitly, NOT through the BFP_USER_DATA_DIR environment variable: that variable is
        # process-wide and the service's browser thread flips it while scanning - this thread must never race it.
        browser = bfp.CDPBrowser(bfp.ProfileFactory().desktop(), executable=exe, headless=False,
                                 user_data_dir=user_data_dir, extra_args=["--start-maximized"])
        # ...but then bfp would lay a synthetic identity (random user-agent, timezone ...) over the window. The login must
        # happen under the browser's NATURAL identity - the one every later scan/post uses - so switch that off, exactly as
        # bfp's real-profile mode does.
        browser.preserve_real_identity = True
        saved = os.environ.get("BFP_LOW_PRIORITY")
        os.environ["BFP_LOW_PRIORITY"] = "0"            # a window a person types into must not be sluggish
        try:
            browser.start()
        finally:
            if saved is None:
                os.environ.pop("BFP_LOW_PRIORITY", None)
            else:
                os.environ["BFP_LOW_PRIORITY"] = saved
        driver = browser.driver
        ws_url = driver.browser_info["webSocketDebuggerUrl"]
        driver.send("Page.navigate", url=LOGIN_URL)
        time.sleep(2)
        driver.close()                                   # no CDP client attached while the person logs in
        return cls(browser, ws_url)

    def alive(self) -> bool:
        return self.browser.proc is not None and self.browser.proc.poll() is None

    def has_login(self) -> bool:
        return acc._logged_in(self.ws_url)

    def identify(self) -> dict:
        """{uniqueId, nickName, region} of whoever is logged in ({} until the page can tell)."""
        page = acc.bfp.CDPDriver(self.browser.port, process=self.browser.proc).connect()
        try:
            who = acc.read_identity(page, 6.0)                            # the page the person is on after logging in
            if not who:
                page.goto("https://www.tiktok.com/", timeout=15.0)       # one attempt must never hold the person up long
                who = acc.read_identity(page, 12.0)
            return who
        finally:
            page.close()

    def close(self) -> None:
        self.browser.stop()                              # graceful: cookies / localStorage / IndexedDB flush to disk


class Flow:
    def __init__(self, reserved: str, browser: dict, replace: str | None, label: str | None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.reserved, self.browser, self.replace, self.label = reserved, browser, replace, label
        self.state, self.message = "starting", STATE_TEXT["starting"]
        self.outcome: str | None = None
        self.result: dict = {}
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.cancel = threading.Event()

    @property
    def finished(self) -> bool:
        return self.state not in ACTIVE

    def view(self) -> dict:
        return {"id": self.id, "state": self.state, "message": self.message, "finished": self.finished,
                "outcome": self.outcome, "result": self.result, "replace": self.replace,
                "browser": {k: self.browser.get(k) for k in ("kind", "name")}, "started_at": self.started_at,
                "updated_at": self.updated_at, "waiting_for_you": self.state == "window_open"}


class AccountManager:
    def __init__(self, bus: EventBus | None = None, *, launcher=None, worker=None, memory_check=None,
                 poll_s: float = 2.0, settle_s: float | None = None, identify_timeout_s: float = 30.0,
                 login_timeout_s: float | None = None, sleep=time.sleep, release=None, rest=None) -> None:
        """`release(name)` closes a browser that is being kept open for account `name` (the service points it at its warm
        browser): an account's folder can only be used by one browser, so a check or a removal asks for it first."""
        self.bus = bus or EventBus()
        self._release_fn = release
        self._rest_fn = rest                              # rest(name) -> seconds TikTok's refusal of that account may last
        self._launch = launcher or RealWindow.launch
        self._worker = worker or (lambda fn, *a, **k: fn(*a, **k))
        self._removal_retry_s, self._removal_tries = 5.0, 60          # a folder that is still held is tried again every 5 s for 5 minutes
        self._memory = memory_check or (lambda: sysguard.enough_memory(config.MIN_FREE_MB))
        self.poll_s, self.identify_timeout_s, self._sleep = poll_s, identify_timeout_s, sleep
        self._settle_s, self._login_timeout_s = settle_s, login_timeout_s
        self._lock = threading.RLock()
        self._active: Flow | None = None
        self._last: Flow | None = None
        acc.SESSION_LISTENERS.append(self._on_session)

    def close(self) -> None:
        """Stop listening for session changes (tests, shutdown) and ask an open login window to stop."""
        if self._on_session in acc.SESSION_LISTENERS:
            acc.SESSION_LISTENERS.remove(self._on_session)
        self.cancel()

    # ------------------------------------------------------------------ events / views
    def _emit(self, type: str, data: dict | None = None, level: str = "info") -> None:
        self.bus.publish(type, data, level)

    def _set(self, flow: Flow, state: str, message: str | None = None) -> None:
        flow.state, flow.message, flow.updated_at = state, message or STATE_TEXT.get(state, state), time.time()
        self._emit("account.flow", {"flow": flow.view()})

    def _on_session(self, name: str, state: str) -> None:
        self._emit("account.session", {"name": name, "session": state}, "warn" if state == "expired" else "info")
        self._emit("accounts.changed", {})

    @staticmethod
    def _view(entry: dict, *, master: bool = False, position: int | None = None, first: str | None = None) -> dict:
        name = entry["name"]
        return {"name": name, "label": entry.get("label") or (f"@{entry['username']}" if entry.get("username") else name),
                "username": entry.get("username"), "nickname": entry.get("nickname"), "region": entry.get("region"),
                "enabled": entry.get("enabled", True), "is_master": master,
                "session": entry.get("session") or "unknown", "session_checked_at": entry.get("session_checked_at"),
                "added": entry.get("added"), "browser_kind": entry.get("browser_kind"),
                "position": position, "preferred": first == name,
                "folder_exists": bool(entry.get("user_data_dir")) and Path(entry["user_data_dir"]).exists()}

    def list(self) -> dict:
        with acc._REG_LOCK:
            chain, first = config.account_chain(), config.REPLY_ACCOUNT_FIRST or config.registry_value("first")
            master = {"name": config.BFP_PROFILE_NAME, "username": config.registry_value("master_username"),
                      "session": config.registry_value("master_session"),
                      "session_checked_at": config.registry_value("master_session_checked_at"),
                      "user_data_dir": config.BFP_MASTER_USER_DATA_DIR}
            rows = [self._view(master, master=True, position=chain.index(master["name"]) + 1 if master["name"] in chain else None,
                               first=first)] if config.master_enabled() else []
            for a in config.load_accounts():
                rows.append(self._view(a, position=chain.index(a["name"]) + 1 if a["name"] in chain else None, first=first))
        for row in rows:
            try:
                row["resting_seconds"] = int(self._rest_fn(row["name"])) if self._rest_fn else 0
            except Exception:  # noqa: BLE001
                row["resting_seconds"] = 0
        return {"accounts": rows, "chain": chain, "first": first if first in chain else None,
                "flow": self.current(), "browsers": browsers.find_browsers(),
                "browser_preference": config.BROWSER_PREFERENCE}

    def _entry(self, name: str) -> tuple:
        """(entry dict, is_master) or raises not_found."""
        if name == config.BFP_PROFILE_NAME and config.master_enabled():
            return ({"name": name, "user_data_dir": config.BFP_MASTER_USER_DATA_DIR,
                     "username": config.registry_value("master_username")}, True)
        for a in config.load_accounts():
            if a["name"] == name:
                return a, False
        raise AccountError("not_found", f"There is no account called '{name}'.")

    # ------------------------------------------------------------------ add / re-login
    def current(self) -> dict | None:
        with self._lock:
            flow = self._active or self._last
            return flow.view() if flow else None

    def _reserve_name(self) -> str:
        taken = {a["name"] for a in config.load_accounts()}
        n = 1
        while f"real-account-{n}" in taken or (Path(config.ACCOUNTS_DIR) / f"real-account-{n}").exists():
            n += 1                                   # a leftover folder is never reused: the window needs a FRESH profile
        return f"real-account-{n}"

    def start_add(self, *, browser: str | None = None, replace: str | None = None, label: str | None = None) -> dict:
        """Open the fresh login window. Returns at once; progress arrives as `account.flow` events."""
        with self._lock:
            if self._active is not None:
                raise AccountError("busy", "A login window is already open - finish it or cancel it first.")
            if replace:
                _entry, is_master = self._entry(replace)
                if is_master:
                    raise AccountError("master", "The page's own account is set in .env; log it in again from there.")
            chosen = browsers.choose(browser or config.BROWSER_PREFERENCE)
            if chosen is None:
                want = browser or config.BROWSER_PREFERENCE
                raise AccountError("no_browser", "No supported browser was found (install Chrome, Brave or Edge)."
                                   if want in ("", "auto") else f"{want.title()} is not installed on this computer.")
            ok, free = self._memory()
            if not ok:
                raise AccountError("low_memory", f"Not enough free memory to open a browser right now ({free:.0f} MB free).")
            flow = Flow(self._reserve_name(), chosen, replace, (label or "").strip() or None)
            self._active = flow
        self._emit("account.flow", {"flow": flow.view()})
        threading.Thread(target=self._run, args=(flow,), name=f"add-account-{flow.id}", daemon=True).start()
        return flow.view()

    def cancel(self) -> dict | None:
        with self._lock:
            flow = self._active
        if flow is None:
            return self.current()
        flow.cancel.set()
        return flow.view()

    def _wait(self, flow: Flow, window, seconds: float) -> str | None:
        """Sleep `seconds` in small steps. -> 'cancelled' | 'closed' | None (the time simply passed)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if flow.cancel.is_set():
                return "cancelled"
            if not window.alive():
                return "closed"
            self._sleep(min(self.poll_s, max(0.0, end - time.monotonic())) or 0.01)
        return None

    def _run(self, flow: Flow) -> None:
        udd = Path(config.ACCOUNTS_DIR) / flow.reserved / "User Data"
        held: dict = {}                                  # the open window, so `finally` can always close it
        outcome, message, result = "failed", "The login did not finish.", {}
        try:
            outcome, message, result = self._execute(flow, udd, held)
        except Exception as exc:  # noqa: BLE001 - whatever happened, the flow ends and nothing stays half-done
            outcome, message, result = "failed", f"{type(exc).__name__}: {str(exc)[:160]}", {}
        finally:
            if held.get("window") is not None:
                try:
                    held["window"].close()
                except Exception:  # noqa: BLE001
                    pass
        if outcome not in ("added", "refreshed"):
            acc.remove_profile_folder(udd)               # a cancelled / failed / duplicate login leaves no folder behind
        self._finish(flow, outcome, message, result)

    def _execute(self, flow: Flow, udd: Path, held: dict) -> tuple:
        """The login itself. -> (outcome, message, result)."""
        try:
            window = held["window"] = self._launch(flow.browser["path"], udd)
        except Exception as exc:  # noqa: BLE001
            return "failed", f"The browser window could not be opened: {str(exc)[:160]}", {}
        self._set(flow, "window_open")
        login_timeout = self._login_timeout_s if self._login_timeout_s is not None else config.ACCOUNT_LOGIN_TIMEOUT_S
        deadline = time.monotonic() + login_timeout
        detected = False
        while time.monotonic() < deadline:
            if flow.cancel.is_set():
                return "cancelled", "Cancelled.", {}
            if not window.alive():
                return "cancelled", "The window was closed before a login was detected.", {}
            try:
                detected = window.has_login()
            except Exception:  # noqa: BLE001
                detected = False
            if detected:
                break
            self._sleep(self.poll_s)
        if not detected:
            return "timeout", f"No login within {int(login_timeout // 60)} minutes - nothing was saved.", {}

        self._set(flow, "login_detected")
        settle = self._settle_s if self._settle_s is not None else config.ACCOUNT_SETTLE_S
        ended = self._wait(flow, window, settle)
        if ended == "cancelled":
            return "cancelled", "Cancelled.", {}
        who: dict = {}
        if ended != "closed":
            self._set(flow, "verifying")
            end = time.monotonic() + self.identify_timeout_s
            while time.monotonic() < end and not who:
                if flow.cancel.is_set():
                    return "cancelled", "Cancelled.", {}
                if not window.alive():
                    break                                # closed by hand: the session is saved, who it is is looked up later
                try:
                    who = window.identify()
                except Exception:  # noqa: BLE001
                    who = {}
                if not who:
                    self._sleep(3.0)
        self._set(flow, "saving")
        held["window"] = None
        try:
            window.close()                               # graceful: the session reaches the disk before it is registered
        except Exception:  # noqa: BLE001
            pass
        result = self._register(flow, udd, who)
        return result.pop("outcome"), result.pop("message"), result

    def _finish(self, flow: Flow, outcome: str, message: str, result: dict | None = None) -> None:
        flow.outcome, flow.result = outcome, result or {}
        with self._lock:                                 # free the slot BEFORE announcing the end: a client may start the next one
            if self._active is flow:
                self._active, self._last = None, flow
        self._set(flow, "failed" if outcome == "failed" else "done", message)
        self._emit("accounts.changed", {})

    # ------------------------------------------------------------------ the decisions after a login
    def _register(self, flow: Flow, udd: Path, who: dict) -> dict:
        """What to do with a login that just finished. -> {outcome, message, account?, duplicate_of?}"""
        username = (who.get("uniqueId") or "").strip() or None
        low = username.lower() if username else None
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with acc._REG_LOCK:
            accounts = config.load_accounts()
            master_user = config.registry_value("master_username")
            if low and master_user and master_user.lower() == low:
                return {"outcome": "duplicate", "duplicate_of": config.BFP_PROFILE_NAME, "username": username,
                        "message": f"@{username} is the page's own account, which is already set up - nothing was added."}
            same = next((a for a in accounts if low and (a.get("username") or "").lower() == low), None)
            target = same
            if flow.replace:
                wanted = next((a for a in accounts if a["name"] == flow.replace), None)
                if wanted and (not low or not wanted.get("username") or wanted["username"].lower() == low):
                    target = wanted                      # "log in again" for exactly this account
            if target is not None and flow.replace != target["name"] and target.get("session") != "expired":
                return {"outcome": "duplicate", "duplicate_of": target["name"], "username": username,
                        "message": f"@{username} is already added as '{target['name']}' and its session works. "
                                   "Use 'Log in again' on that account if it needs a new login."}
            # "ok" only when who logged in is KNOWN; a cookie alone is not proof (TikTok may not honour it) - the background
            # check that follows an unreadable identity settles it
            fields = {"user_data_dir": str(udd), "browser": flow.browser["path"], "browser_kind": flow.browser["kind"],
                      "session": "ok" if username else "unknown", "session_checked_at": now}
            if username:
                fields.update(username=username, nickname=who.get("nickName"), region=who.get("region"))
            stale_dir = None
            if target is not None:                       # refresh: the account keeps its name, position and settings
                old_dir = target.get("user_data_dir")
                target.update(fields)
                acc._write_registry(accounts)
                if old_dir and Path(old_dir).resolve() != Path(udd).resolve():
                    stale_dir = old_dir
                outcome, message = "refreshed", f"'{target['name']}' (@{username or '?'}) was logged in again."
                entry = target
            else:
                entry = {"name": flow.reserved, "enabled": True, "added": now, "source": "desktop", **fields}
                if flow.label:
                    entry["label"] = flow.label
                acc._write_registry(accounts + [entry])
                outcome = "added"
                message = (f"Account @{username} added." if username else
                           "Account added - its @username will be read in a moment.")
        if stale_dir:                                     # the dead profile goes AFTER the registry lock is released: a slow
            self._release(entry["name"])                  # (a kept browser still standing on the old folder is closed first)
            acc.remove_profile_folder(stale_dir)          # delete must not hold up the scan that reads the account list
        self._emit("accounts.changed", {})
        if not username:                                  # logged in, but who is not known yet: look it up in the background
            threading.Thread(target=self._safe_check, args=(entry["name"],), daemon=True).start()
        view = self._view(entry)
        view["identity_pending"] = not username
        return {"outcome": outcome, "message": message, "account": view, "username": username}

    def _safe_check(self, name: str) -> None:
        try:
            self.check(name)
        except Exception:  # noqa: BLE001
            pass

    def _release(self, name: str) -> None:
        if self._release_fn is not None:
            try:
                self._release_fn(name)
            except Exception:  # noqa: BLE001 - a browser that cannot be released only makes the next step wait or fail visibly
                pass

    # ------------------------------------------------------------------ health
    def check(self, name: str) -> dict:
        """Is this account still logged in? Opens its folder headless and reads who is there.

        Runs in the CALLING thread, not on the service's browser thread: a check can take a minute when TikTok is slow, and
        that thread must stay free for scans and posts. Two things keep it safe anyway - bfp's per-folder lock makes it wait
        for a scan or post that is using this very profile, and the memory guard refuses to start a browser on a full
        machine."""
        entry, is_master = self._entry(name)
        if not entry.get("user_data_dir") or not Path(entry["user_data_dir"]).exists():
            raise AccountError("no_profile", f"The saved browser folder of '{name}' is missing - log in again.")
        ok, free = self._memory()
        if not ok:
            raise AccountError("low_memory", f"Not enough free memory to open a browser right now ({free:.0f} MB free).")
        self._release(name)                                   # the kept browser of this account would hold its folder
        res = acc.identify(entry["user_data_dir"], entry.get("browser"))
        if res.get("ok") and not res.get("who") and res.get("logged_in") is None:
            # the cookie is there but the page named nobody (TikTok's guest page): a fresh browser very often gets it right
            res = acc.identify(entry["user_data_dir"], entry.get("browser"))
        out = {"name": name, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if not res["ok"]:
            out.update(ok=False, session="unknown", error=res.get("error"))
            self._emit("account.checked", out, "warn")
            return out
        who = res["who"]
        if who:
            session = "ok"
        elif res.get("logged_in") is False:
            session = "expired"                              # TikTok's session cookie is gone: really logged out
        else:
            session = "unknown"                              # cookie present, page named nobody: do not claim anything
            out["unconfirmed"] = True
        out.update(ok=True, session=session, username=(who or {}).get("uniqueId"), region=(who or {}).get("region"))
        if session == "unknown":                             # an unconfirmed check never overwrites what is already known
            self._emit("account.checked", out, "warn")
            return out
        with acc._REG_LOCK:
            accounts = config.load_accounts()
            if is_master:
                extra = {"master_session": out["session"], "master_session_checked_at": out["checked_at"]}
                if who:
                    extra["master_username"] = who["uniqueId"]
                acc._write_registry(accounts, **extra)
            else:
                row = next((a for a in accounts if a["name"] == name), None)
                if row is not None:
                    row["session"], row["session_checked_at"] = out["session"], out["checked_at"]
                    if who:
                        row.update(username=who["uniqueId"], nickname=who.get("nickName"), region=who.get("region"))
                        clash = next((a["name"] for a in accounts if a["name"] != name
                                      and (a.get("username") or "").lower() == who["uniqueId"].lower()), None)
                        master_user = config.registry_value("master_username")
                        if clash or (master_user and master_user.lower() == who["uniqueId"].lower()):
                            out["duplicate_of"] = clash or config.BFP_PROFILE_NAME
                            row["enabled"] = False       # two entries for one user would only waste its reply budget
                    acc._write_registry(accounts)
        self._emit("account.checked", out, "info" if out["session"] == "ok" else "warn")
        self._emit("accounts.changed", {})
        return out

    def check_all(self) -> dict:
        """Check every account one after another in the background; results arrive as `account.checked` events."""
        names = ([config.BFP_PROFILE_NAME] if config.master_enabled() else []) + [a["name"] for a in config.load_accounts()]

        def run():
            for n in names:
                try:
                    self.check(n)
                except AccountError as exc:
                    self._emit("account.checked", {"name": n, "ok": False, "error": exc.message}, "warn")
                except Exception as exc:  # noqa: BLE001
                    self._emit("account.checked", {"name": n, "ok": False, "error": str(exc)[:160]}, "warn")
        threading.Thread(target=run, name="check-accounts", daemon=True).start()
        return {"started": True, "accounts": names}

    # ------------------------------------------------------------------ organise
    def _update(self, name: str, **fields) -> dict:
        entry, is_master = self._entry(name)
        if is_master:
            raise AccountError("master", "The page's own account is configured in .env; it cannot be changed here.")
        with acc._REG_LOCK:
            accounts = config.load_accounts()
            row = next(a for a in accounts if a["name"] == name)
            row.update({k: v for k, v in fields.items() if v is not None})
            acc._write_registry(accounts)
        self._emit("accounts.changed", {})
        return self._view(row)

    def set_label(self, name: str, label: str) -> dict:
        label = (label or "").strip()
        if not label or len(label) > 60:
            raise AccountError("bad_request", "The name must be 1-60 characters.")
        return self._update(name, label=label)

    def set_enabled(self, name: str, enabled: bool) -> dict:
        view = self._update(name, enabled=bool(enabled))
        if not enabled and config.registry_value("first") == name:
            self.set_first(None)
        return view

    def set_first(self, name: str | None) -> dict:
        """The account that answers first (the page's own account is the usual choice). None = the normal order."""
        if name is not None:
            self._entry(name)
            if name not in config.account_chain():
                raise AccountError("bad_request", f"'{name}' is disabled - enable it first.")
        with acc._REG_LOCK:
            acc._write_registry(config.load_accounts(), first=name)
        self._emit("accounts.changed", {})
        return {"first": name}

    def reorder(self, names: list) -> dict:
        """Put the registered accounts in this order (the master stays where .env puts it; names not listed keep their
        relative order after the listed ones)."""
        with acc._REG_LOCK:
            accounts = config.load_accounts()
            by_name = {a["name"]: a for a in accounts}
            unknown = [n for n in names if n not in by_name]
            if unknown:
                raise AccountError("bad_request", f"Unknown account(s): {', '.join(unknown)}.")
            ordered = [by_name[n] for n in dict.fromkeys(names)] + [a for a in accounts if a["name"] not in names]
            acc._write_registry(ordered)
        self._emit("accounts.changed", {})
        return {"order": [a["name"] for a in ordered]}

    def remove(self, name: str) -> dict:
        """Forget an account and delete its saved browser folder. The page's own account cannot be removed here."""
        entry, is_master = self._entry(name)
        if is_master:
            raise AccountError("master", "The page's own account is configured in .env; it cannot be removed here.")
        with self._lock:
            if self._active is not None and self._active.replace == name:
                raise AccountError("busy", "A login for this account is in progress.")
        with acc._REG_LOCK:
            accounts = [a for a in config.load_accounts() if a["name"] != name]
            extra = {"first": None} if config.registry_value("first") == name else {}
            acc._write_registry(accounts, **extra)
        # the folder goes through the browser thread: a scan or post that is using it right now finishes first
        self._release(name)
        folder = entry.get("user_data_dir") or ""
        # The automatic session check that follows "Add account" runs a headless browser of its own on this folder - for up to a
        # minute or two - and Windows will not delete a folder a program is using. (Anything of the worker - a send, a kept browser -
        # is over by now: `_release` went through the worker.) That browser is one this app started on this very folder: close it.
        closed = orphans.close_stale_browsers([folder]) if folder else []
        if closed:
            time.sleep(1.0)
        deleted = bool(self._worker(acc.remove_profile_folder, folder, 3 if closed else 6))
        if not deleted and folder and acc.mark_for_removal(folder):
            # still held (the check's own browser handles live in this very program for a little longer): finish it in the
            # background, and at the next start if the program is closed first
            threading.Thread(target=self._finish_removal, args=(folder,), name="finish-removal", daemon=True).start()
        self._emit("accounts.changed", {})
        return {"removed": True, "name": name, "folder_deleted": deleted, "cleanup_pending": bool(folder) and not deleted}

    def _finish_removal(self, folder: str) -> None:
        for _ in range(self._removal_tries):
            time.sleep(self._removal_retry_s)
            if acc.remove_profile_folder(folder):
                self._emit("accounts.changed", {})
                return

    def finish_pending_removals(self) -> list:
        """At start: folders of accounts removed earlier that could not be deleted then."""
        return acc.finish_pending_removals()
