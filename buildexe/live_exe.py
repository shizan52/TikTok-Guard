"""LIVE, READ-ONLY check of the BUILT exe against real TikTok and the real AI - through its real screen. NOTHING IS POSTED.

    python buildexe/live_exe.py [post_url] [--account NAME]

* a throw-away data folder; only the accounts list (data/accounts.json) of this machine is copied into it, so an existing logged-in
  profile can be used by the packaged engine (the profile folders themselves stay where they are)
* the OpenRouter key of this machine's .env is handed to the exe through its ENVIRONMENT (never stored in the exe or its data)
* the exe is started like a client starts it (scrubbed environment, no source tree, no .env of the project), `Analyze` is pressed,
  the post's comments are read by the packaged browser engine, judged by the AI, and the screen is photographed
* "Send" is never pressed. Needs ~1-3 minutes and costs one profile-page load of the account (TikTok limits those: give an account
  ten minutes between runs).
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_exe as T  # noqa: E402

PROJECT = T.PROJECT
ARGS = sys.argv[1:]
ACCOUNT = ARGS[ARGS.index("--account") + 1] if "--account" in ARGS else "real-account-3"
URL = next((a for a in ARGS if a.startswith("http")),
           "https://www.tiktok.com/@examplebrand/video/7100000000000000002?is_from_webapp=1&sender_device=pc&web_id=7100000000000000005")
T0 = time.monotonic()


def say(msg):
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


def env_value(name: str) -> str:
    for line in (PROJECT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].split(" #")[0].strip().strip("'\"")
    return ""


def main() -> int:
    T.SHOTS.mkdir(parents=True, exist_ok=True)
    registry = json.loads((PROJECT / "data" / "accounts.json").read_text(encoding="utf-8"))
    rows = registry["accounts"] if isinstance(registry, dict) else registry
    mine = next((a for a in rows if a["name"] == ACCOUNT), None)
    if mine is None:
        say(f"no account called {ACCOUNT} in this machine's registry: {[a['name'] for a in rows]}")
        return 2
    holders = T.processes_using(Path(mine["user_data_dir"]).parent)
    if holders:
        say(f"the profile of {ACCOUNT} is in use by {[(p['pid'], p['name']) for p in holders]} - close it first")
        return 2
    key = "" if "--no-key" in ARGS else env_value("OPENROUTER_API_KEY")          # --no-key: a client who has not pasted an AI key yet
    home = T.new_home("tg-exe-live-")
    (home / "data").mkdir(parents=True)
    shutil.copy(PROJECT / "data" / "accounts.json", home / "data" / "accounts.json")
    say(f"throw-away data folder {home}; account {ACCOUNT} (@{mine.get('username')}); AI key present: {bool(key)}")

    # The profiles of this machine's registry were logged in with the browser of its .env (Brave): another browser cannot read their
    # encrypted cookies. (An account added IN the app remembers its own browser - a client never needs this.)
    browser = env_value("BFP_PATCHED_CHROMIUM")
    say(f"browser of these profiles: {browser or '(none configured: the exe chooses)'}")
    app = T.Running(["--no-window", "--port", "0"], home, **({"OPENROUTER_API_KEY": key} if key else {}),
                    **({"BFP_PATCHED_CHROMIUM": browser} if browser else {}))
    ui = None
    try:
        m = app.wait_for(r"Open this address in a browser:\s+(http://127\.0\.0\.1:\d+)/#token=(\S+)", 60)
        base, token = m.group(1), m.group(2)
        say(f"packaged app is running at {base}")
        settings = json.loads(T.http(base + "/api/settings", token=token)[1])
        say(f"AI key seen by the app: {settings['ai_key']['set']}; posting enabled: {settings['posting_enabled']} (it will not be used)")
        from app.bfp_loader import ensure_bfp_on_path
        ensure_bfp_on_path()
        from ui_support import UI
        ui = UI()
        ui.open(f"{base}/#token={token}")
        ui.wait("document.querySelector('#url-input') && document.querySelector('#stats .stat')", 20, "home")
        ui.wait("document.querySelector('#read-account').options.length > 1", 20, "the account picker")
        say("accounts offered: " + str(ui.ev("[...document.querySelector('#read-account').options].map(o => o.value)")))
        ui.fill("#url-input", URL)
        ui.ev("(() => { const s = document.querySelector('#read-account'); s.value = %s; s.dispatchEvent(new Event('change', {bubbles: true})); })()"
              % json.dumps(ACCOUNT))
        ui.click("#analyze-btn")
        say("analysing the real post with the packaged engine ...")
        last, end = "", time.monotonic() + 420
        while time.monotonic() < end:
            if ui.exists("[data-role=results]") or ui.exists("[data-role=job-error]"):
                break
            msg = ui.text("[data-role=progress] b")
            if msg and msg != last:
                last = msg
                say(f"progress: {msg}")
            time.sleep(1)
        ui.shot(T.SHOTS / "5-live-result.png")
        if ui.exists("[data-role=job-error]"):
            say("ANALYSIS FAILED: " + ui.text("[data-role=job-error]"))
            return 1
        if not ui.exists("[data-role=results]"):
            say("no result within 7 minutes")
            return 1
        time.sleep(1)
        header = [h for h in ui.text("[data-role=results]").split("\n") if h]
        say("results: " + " | ".join(header[:10]))
        ui.click("[data-filter=all]")
        time.sleep(0.8)
        cards = ui.texts("#comment-list .cc")
        say(f"{len(cards)} comment card(s) on screen; first ones:")
        for t in cards[:4]:
            say("   * " + " / ".join(x for x in t.split("\n") if x)[:200])
        ui.shot(T.SHOTS / "6-live-all-comments.png")
        ok = len(cards) > 0
        say("how the screen describes the judging: " + " | ".join(h for h in header[:12] if h and ("negative" in h.lower() or "AI" in h or "key" in h.lower()))[:300])
        log = (home / "data" / "logs" / "desktop.log").read_text(encoding="utf-8", errors="replace")
        if key and key in log:
            say("PROBLEM: the AI key is in the log")
            ok = False
        say("log lines about the read: " + " | ".join(l[24:150] for l in log.splitlines() if "tiktok_monitor" in l)[:600])
        errors = [l for l in log.splitlines() if " ERROR " in l or " CRITICAL " in l]
        say(f"errors in the exe's log: {len(errors)}" + (": " + errors[0][:200] if errors else ""))
        say("RESULT: " + ("OK - the packaged exe read a real post, judged it with the AI and showed it on screen" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        if ui is not None:
            ui.close()
        app.stop()
        time.sleep(2)
        left = T.processes_using(home) + T.processes_using(Path(mine["user_data_dir"]).parent)
        say(f"processes left behind after stopping: {[(p['pid'], p['name']) for p in left]}")


if __name__ == "__main__":
    sys.exit(main())
