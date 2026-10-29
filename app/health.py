"""Is this computer ready to run the app? Plain checks a person can act on - shown on the Home page when something is missing,
in Help -> Diagnostics, and by `python app.py --check`.

Each check: {id, title, status: ok | warn | fail, detail, fix}. `fail` = the app cannot do its job (no browser, no account);
`warn` = it works but something will disappoint (no AI key, no company information, low disk space, no internet).
"""
import importlib.util
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import config
from app import browsers, sysguard

PLACEHOLDER_KEYS = ("", "sk-or-v1-your-key-goes-here")


def _check(cid: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def _browser_engine() -> dict:
    try:
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        have = importlib.util.find_spec("bfp") is not None
    except Exception:  # noqa: BLE001
        have = False
    engines = [n for n in ("playwright", "patchright") if importlib.util.find_spec(n) is not None]
    if not have or not engines:
        return _check("engine", "Browser automation", "fail",
                      "The browser automation library is missing." if engines or have else
                      "Neither the bfp helper nor patchright/playwright is installed.",
                      "Install the requirements:  pip install -r requirements.txt")
    return _check("engine", "Browser automation", "ok", f"{engines[0]} is installed.")


def _browsers(found=None) -> dict:
    found = browsers.find_browsers() if found is None else found
    if not found:
        return _check("browser", "A web browser", "fail", "No Chrome, Edge, Brave or Chromium was found.",
                      "Install Google Chrome (or Microsoft Edge / Brave) and start the app again.")
    return _check("browser", "A web browser", "ok", ", ".join(sorted({b["name"] for b in found})) + " found.")


def _ai_key(db=None) -> dict:
    key = config.OPENROUTER_API_KEY or ""
    if key in PLACEHOLDER_KEYS:
        return _check("ai_key", "AI key", "warn", "No OpenRouter key is set, so the AI cannot judge comments or write drafts.",
                      "Open Controls and paste your OpenRouter key (or put it in the .env file).")
    return _check("ai_key", "AI key", "ok", "An OpenRouter key is set.")


def _accounts(summary: dict | None) -> dict:
    if summary is None:
        return _check("accounts", "TikTok accounts", "warn", "Accounts could not be read.", "Open the Accounts page.")
    if not summary.get("usable"):
        return _check("accounts", "TikTok accounts", "fail", "There is no TikTok account yet - nothing can be read or sent.",
                      "Open Accounts and press Add account.")
    if not summary.get("ready") and not summary.get("unchecked"):
        return _check("accounts", "TikTok accounts", "warn", "Every account needs to log in again.",
                      "Open Accounts and press Log in again on one of them.")
    return _check("accounts", "TikTok accounts", "ok",
                  f"{summary['usable']} account(s): {summary.get('ready', 0)} logged in, {summary.get('unchecked', 0)} not checked yet.")


def _company_info(db) -> dict:
    if not (db.get_setting("company_context", "") or "").strip():
        return _check("company", "Company information", "warn",
                      "No company information is saved, so every customer question gets the safe \"we will confirm\" reply.",
                      "Open AI & Brand and write what customers ask about (address, hours, shipping, prices).")
    return _check("company", "Company information", "ok", "Saved - questions can be answered from it.")


def _disk() -> dict:
    try:
        free_mb = shutil.disk_usage(config.DATA_DIR).free / 1048576
    except OSError:
        return _check("disk", "Disk space", "warn", "The free space could not be measured.")
    if free_mb < 500:
        return _check("disk", "Disk space", "warn", f"Only {free_mb:.0f} MB free on the drive of the app's data folder.",
                      "Free some space; the app keeps screenshots and a small database there.")
    return _check("disk", "Disk space", "ok", f"{free_mb / 1024:.1f} GB free.")


def _memory() -> dict:
    ok, free = sysguard.enough_memory(config.MIN_FREE_MB)
    if not ok:
        return _check("memory", "Memory", "warn", f"Only {free:.0f} MB of memory is free; a browser needs about {config.MIN_FREE_MB} MB.",
                      "Close other programs (many browser tabs) and try again.")
    return _check("memory", "Memory", "ok", "Enough free memory." if free is None else f"{free / 1024:.1f} GB free.")


def _data_writable() -> dict:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, prefix=".write-test-", delete=True):
            pass
        return _check("data", "Data folder", "ok", f"{config.DATA_DIR} can be written to.")
    except OSError as exc:
        return _check("data", "Data folder", "fail", f"The data folder cannot be written to ({exc}).",
                      "Move the app to a folder you own (not Program Files) or fix its permissions.")


def _internet(host: str = "www.tiktok.com", timeout: float = 2.5) -> dict:
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            pass
        return _check("internet", "Internet / TikTok", "ok", "TikTok can be reached.")
    except OSError:
        return _check("internet", "Internet / TikTok", "warn", "tiktok.com cannot be reached right now.",
                      "Check the internet connection (or a VPN / firewall) and try again.")


def _python() -> dict:
    if sys.version_info < (3, 10):
        return _check("python", "Python", "fail", f"Python {sys.version_info.major}.{sys.version_info.minor} is too old.",
                      "Install Python 3.10 or newer.")
    return _check("python", "Python", "ok", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.")


def accounts_summary() -> dict:
    """The same numbers as the dashboard, straight from the account registry (for `--check`, when the app is not running)."""
    rows = [{"enabled": a.get("enabled", True), "session": a.get("session") or "unknown"} for a in config.load_accounts()]
    if config.master_enabled():
        rows.append({"enabled": True, "session": config.registry_value("master_session") or "unknown"})   # the page's own account
    usable = [r for r in rows if r["enabled"]]
    return {"total": len(rows), "usable": len(usable), "ready": sum(r["session"] == "ok" for r in usable),
            "need_login": sum(r["session"] == "expired" for r in usable),
            "unchecked": sum(r["session"] not in ("ok", "expired") for r in usable)}


def run(db=None, *, accounts: dict | None = None, internet: bool = True, found_browsers=None) -> list:
    """All the checks, most important first. `accounts` is the dashboard's {total, usable, ready, need_login, unchecked}."""
    items = [_python(), _data_writable(), _browser_engine(), _browsers(found_browsers), _accounts(accounts), _ai_key(db)]
    if db is not None:
        items.append(_company_info(db))
    items += [_disk(), _memory()]
    if internet:
        items.append(_internet())
    return items


def worst(items: list) -> str:
    states = {i["status"] for i in items}
    return "fail" if "fail" in states else "warn" if "warn" in states else "ok"


def report(items: list) -> str:
    mark = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = []
    for i in items:
        lines.append(f"{mark[i['status']]} {i['title']}: {i['detail']}")
        if i["status"] != "ok" and i.get("fix"):
            lines.append(f"       -> {i['fix']}")
    return "\n".join(lines)
