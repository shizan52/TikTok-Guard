"""Import a TikTok session from a normal, already-logged-in browser.

Google's sign-in flow outright refuses the automated browser ("Couldn't sign
you in -- this browser or app may not be secure", confirmed live against
Playwright's bundled "Chrome for Testing" build) -- that block is permanent
and by design, not something a stealth patch fixes. The reliable alternative:
log in once in a REAL, normal browser (already done -- via Google, there),
export that session's cookies, and hand them to this bot instead of ever
driving a login form through automation.

Expects the JSON export format of the "Cookie-Editor" browser extension
(https://cookie-editor.com/, Chrome/Edge/Brave/Firefox) -- a plain list of
cookie objects. Converts it to Playwright's storage_state format.
"""
import json
from pathlib import Path

_SAME_SITE_MAP = {
    "strict": "Strict", "lax": "Lax", "no_restriction": "None",
    "none": "None", "unspecified": "Lax",
}


def _convert(raw_cookies: list) -> dict:
    cookies = []
    for c in raw_cookies:
        same_site = _SAME_SITE_MAP.get(str(c.get("sameSite", "")).lower(), "Lax")
        expires = -1
        if not c.get("session") and c.get("expirationDate"):
            expires = float(c["expirationDate"])
        cookies.append({
            "name": c["name"], "value": c["value"],
            "domain": c["domain"], "path": c.get("path", "/"),
            "expires": expires,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": same_site,
        })
    return {"cookies": cookies, "origins": []}


def import_cookie_editor_file(src_path, dest_state_file, *, localstorage_path=None,
                               origin: str = "https://www.tiktok.com") -> dict:
    """localstorage_path, if given, points at a JSON dump shaped
    {"localStorage": [[key, value], ...], "indexedDB": {...}} (produced by the
    DevTools snippet that reads `Object.entries(localStorage)` on a real,
    already-logged-in tab). Merging this in matters: cookies alone prove WHO
    is logged in, but TikTok's own "Ticket Guard" write-signing mechanism
    (the tt-ticket-guard-* request headers, confirmed live 2026-10-06 via a
    HAR of a successful manual reply) is computed from an EC keypair + ts_sign
    ticket that TikTok's JS stores in localStorage under the
    "security-sdk/..." keys -- NOT in any cookie. Without it, a cookie-only
    session can read everything (no signature needed) but every write
    silently no-ops (HTTP 200, empty body) because the signature TikTok's JS
    computes locally never matches a real, registered key. Importing the
    donor browser's localStorage wholesale (not just those two keys) keeps
    the whole profile internally consistent rather than cherry-picking only
    the keys understood today.
    """
    raw = json.loads(Path(src_path).read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]  # tolerate a full storage_state-shaped export too
    state = _convert(raw)

    ls_entries = []
    if localstorage_path:
        ls_raw = json.loads(Path(localstorage_path).read_text(encoding="utf-8-sig"))
        ls_entries = ls_raw.get("localStorage", []) if isinstance(ls_raw, dict) else []
        if ls_entries:
            state["origins"] = [{
                "origin": origin,
                "localStorage": [{"name": k, "value": v} for k, v in ls_entries],
            }]

    dest_state_file = Path(dest_state_file)
    dest_state_file.parent.mkdir(parents=True, exist_ok=True)
    dest_state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    names = {c["name"] for c in state["cookies"]}
    session_names = sorted(names & {"sessionid", "sid_tt", "sid_guard"})
    has_ticket_guard_key = any(
        isinstance(k, str) and k.startswith("security-sdk/") for k, _ in ls_entries
    )
    return {"saved_to": str(dest_state_file), "cookie_count": len(state["cookies"]),
            "has_session_cookie": bool(session_names), "session_cookie_names": session_names,
            "localstorage_entry_count": len(ls_entries),
            "has_ticket_guard_key": has_ticket_guard_key}
