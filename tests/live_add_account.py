"""LIVE check of "Add account" with REAL browser windows - run by hand, never part of the automatic suite:

    python tests/live_add_account.py [chrome] [brave] [edge]        (default: every installed browser)

For each browser it runs three real scenarios against the backend's HTTP API:
  1. a login: the fresh window opens, nothing is saved while nobody has logged in, then the TikTok session cookie
     appears (put there by this script - it cannot type a password for you), the app DETECTS it, lets the session
     settle, closes the window itself and registers the account; a background session check then runs headless;
  2. the app's Cancel button closes the window and leaves no folder;
  3. the person closing the window by hand cancels and leaves no folder.
It uses a throw-away accounts registry (your real accounts are not touched) and switches Telegram OFF (a second
poller would fight the production bot). Windows are visible for a short while: do not type in them.
"""
import http.client
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="live-add-"))
config.ACCOUNTS_DIR, config.ACCOUNTS_FILE = TMP / "real", TMP / "accounts.json"
config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES = "live-master", ["live-master"]
config.BFP_MASTER_USER_DATA_DIR = str(TMP / "master" / "User Data")
config.TELEGRAM_BOT_TOKEN = config.TELEGRAM_CHAT_ID = ""             # never poll the production bot's Telegram
config.REPLY_ACCOUNT_FIRST = ""

from app import account_manager as am  # noqa: E402
from app import accounts as acc  # noqa: E402
from app import api, browsers  # noqa: E402
from app.db import DB  # noqa: E402
from app.service import Service  # noqa: E402

TOKEN = "live-test-token-" * 3
T0 = time.monotonic()


def say(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


class Live:
    def __init__(self) -> None:
        self.windows: list = []
        db = DB(TMP / "bot.db")
        self.svc = Service(db, scanner=lambda d: {"opened": 0, "new": 0}, api=False)
        self.svc.accounts._launch = self.launch
        self.svc.accounts.poll_s, self.svc.accounts._settle_s, self.svc.accounts.identify_timeout_s = 1.0, 3, 12
        self.svc.start()
        self.server = api.ApiServer(self.svc.controller, self.svc.events, self.svc.accounts, self.svc, host="127.0.0.1",
                                    port=0, token=TOKEN).start()

    def launch(self, exe, udd):
        say(f"launching {exe}")
        w = am.RealWindow.launch(exe, udd)
        self.windows.append(w)
        return w

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=120)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, data

    def flow(self):
        return self.call("GET", "/api/accounts/add")[1]["flow"]

    def wait_state(self, states, timeout):
        end, seen = time.monotonic() + timeout, []
        while time.monotonic() < end:
            f = self.flow()
            if f and (not seen or seen[-1] != f["state"]):
                seen.append(f["state"])
                say(f"flow state: {f['state']:<14} {f['message']}")
            if f and f["state"] in states:
                return f
            time.sleep(0.5)
        raise AssertionError(f"timed out waiting for {states}; saw {seen}")

    def accounts(self):
        return [a for a in self.call("GET", "/api/accounts")[1]["accounts"] if not a["is_master"]]

    def stop(self):
        self.server.stop()
        self.svc.stop()
        for w in self.windows:
            try:
                w.browser.stop()
            except Exception:  # noqa: BLE001
                pass


def plant_login_cookie(window) -> None:
    """What a real login leaves behind, planted through the browser's own debugging socket."""
    ws = acc.bfp.WSClient(window.ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.setCookies", "params": {"cookies": [
            {"name": "sessionid", "value": "live-test-session-value", "domain": ".tiktok.com", "path": "/",
             "secure": True, "httpOnly": True, "expires": time.time() + 86400}]}}))
        for _ in range(20):
            if json.loads(ws.recv(timeout=10)).get("id") == 1:
                return
    finally:
        ws.close()


def scenario_login(live: Live, kind: str) -> None:
    say(f"--- {kind}: a login ---")
    code, flow = live.call("POST", "/api/accounts/add", {"browser": kind, "label": f"live {kind}"})
    assert code == 202, (code, flow)
    live.wait_state(("window_open",), 90)
    window = live.windows[-1]
    assert window.alive(), "the window should be open"
    time.sleep(4)
    assert live.accounts() == [], "nothing may be saved before a login"
    assert live.flow()["state"] == "window_open", "still waiting for the person"
    say("window is open, nothing saved yet - now the TikTok session cookie appears (as after a real login)")
    plant_login_cookie(window)
    done = live.wait_state(("done", "failed"), 150)
    assert done["outcome"] == "added", done
    assert not window.alive(), "the app must have closed the window itself"
    (acct,) = live.accounts()
    say(f"registered: {acct['name']} label={acct['label']!r} browser={acct['browser_kind']} session={acct['session']}")
    assert acct["browser_kind"] == kind and acct["folder_exists"]
    assert acct["session"] == "unknown", "who logged in could not be read, so the account must not be called 'logged in' yet"
    folder = Path(config.load_accounts()[0]["user_data_dir"])
    assert (folder / "Local State").exists(), "a real profile folder was written"
    say("waiting for the automatic background session check (headless, in its own thread) ...")
    end = time.monotonic() + 150
    events = lambda: [e for e in live.svc.events.since(0) if e["type"] == "account.checked"]
    while time.monotonic() < end and not events():
        time.sleep(1)
    assert events(), "the background check never reported"
    result = events()[-1]["data"]
    after = live.accounts()[0]
    say(f"the check said: session={result.get('session')} unconfirmed={result.get('unconfirmed', False)}; registry now: "
        f"session={after['session']} username={after['username']}")
    assert after["session"] in ("expired", "unknown"), \
        "TikTok does not accept the planted cookie: the check may say expired (cookie dropped) or unknown (could not confirm), never ok"
    code, removed = live.call("DELETE", f"/api/accounts/{acct['name']}")
    assert code == 200 and removed["folder_deleted"] and not folder.exists(), (code, removed)
    say(f"{kind}: LOGIN SCENARIO PASSED")


def scenario_cancel(live: Live, kind: str) -> None:
    say(f"--- {kind}: Cancel in the app ---")
    live.call("POST", "/api/accounts/add", {"browser": kind})
    live.wait_state(("window_open",), 90)
    window = live.windows[-1]
    folder = Path(window.browser.user_data_dir).parent
    live.call("POST", "/api/accounts/add/cancel", {})
    done = live.wait_state(("done", "failed"), 60)
    assert done["outcome"] == "cancelled", done
    time.sleep(1)
    assert not window.alive() and not folder.exists() and live.accounts() == [], "window closed, folder gone, nothing saved"
    say(f"{kind}: CANCEL SCENARIO PASSED")


def scenario_closed_by_hand(live: Live, kind: str) -> None:
    say(f"--- {kind}: the person closes the window ---")
    live.call("POST", "/api/accounts/add", {"browser": kind})
    live.wait_state(("window_open",), 90)
    window = live.windows[-1]
    folder = Path(window.browser.user_data_dir).parent
    window.browser.proc.terminate()
    done = live.wait_state(("done", "failed"), 60)
    assert done["outcome"] == "cancelled" and "closed" in done["message"], done
    time.sleep(1)
    assert not folder.exists() and live.accounts() == [], "folder gone, nothing saved"
    say(f"{kind}: CLOSED-BY-HAND SCENARIO PASSED")


def main() -> int:
    wanted = [a.lower() for a in sys.argv[1:]] or [b["kind"] for b in browsers.find_browsers() if b["kind"] in browsers.KINDS]
    live, failed = Live(), []
    try:
        for kind in wanted:
            for scenario in (scenario_login, scenario_cancel, scenario_closed_by_hand):
                for left_over in live.accounts():             # a failed scenario must not poison the next one
                    live.call("DELETE", f"/api/accounts/{left_over['name']}")
                try:
                    scenario(live, kind)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{kind}/{scenario.__name__}: {type(exc).__name__}: {exc}")
                    say(f"FAILED: {failed[-1]}")
                    try:
                        live.call("POST", "/api/accounts/add/cancel", {})
                        time.sleep(3)
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        live.stop()
    say("ALL PASSED" if not failed else "FAILURES:\n  " + "\n  ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
