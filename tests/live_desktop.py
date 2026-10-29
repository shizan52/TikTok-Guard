"""LIVE check of the desktop app against REAL TikTok and the REAL AI, driven through its REAL screen - and with nothing posted:

    python tests/live_desktop.py [post_url] [--account NAME] [--analyze-only]

  * the post's comments are read by the real browser reader (an account of yours, read-only, like the watcher does),
  * the real AI picks out the negative ones and drafts a reply for one,
  * "Send" goes through the real posting engine but in DRY-RUN: the reply is typed into the real comment box, a screenshot is
    taken, the box is emptied again and NOTHING is sent (that screenshot is shown as the "proof").
It uses a throw-away database (your real data is not touched) and never polls Telegram.
--account NAME reads AND replies with that one account (otherwise "Auto": the first account of the normal order)."""
import json
import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import tiktok_monitor as tm  # noqa: E402
from app.db import DB  # noqa: E402
from app.desktop import DesktopApp  # noqa: E402
from ui_support import UI  # noqa: E402

TOKEN = "live-desktop-token-" * 2
import logging  # noqa: E402
logging.basicConfig(level=logging.INFO, format="        engine: %(message)s", stream=sys.stdout)
logging.getLogger("tiktok_monitor").setLevel(logging.INFO)
ARGS = sys.argv[1:]
ACCOUNT = ARGS[ARGS.index("--account") + 1] if "--account" in ARGS else ""
ANALYZE_ONLY = "--analyze-only" in ARGS          # read + judge only (no reply flow): to look at how comments of any post are judged
URL = next((a for a in ARGS if a.startswith("http")), "https://www.tiktok.com/@examplebrand/video/7100000000000000001")
T0 = time.monotonic()
OUT = pathlib.Path(tempfile.mkdtemp(prefix="livedesk-shots-"))


def say(msg):
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


def pick(selector: str, value: str) -> str:
    """JS that chooses `value` in a <select> the way a person does (and tells the page)."""
    return ("(() => { const s = document.querySelector(%s); s.value = %s; s.dispatchEvent(new Event('change', {bubbles: true})); return s.value; })()"
            % (json.dumps(selector), json.dumps(value)))


def dry_poster(a: dict) -> dict:
    """The real engine, stopped one click before the end. Never sends."""
    res = tm.post_link_comment(a["video_url"], a["reply"], cid=a["cid"] if a["kind"] == "link_reply" else None,
                               account=a.get("account") or None, dry_run=True, stage=a.get("_stage"),
                               comment_text=a.get("comment_text") if a["kind"] == "link_reply" else None)
    say(f"dry-run result: ok={res.get('ok')} account={res.get('profile')} error={res.get('error')}")
    if res.get("ok") and res.get("before_screenshot"):
        name = f"proof_{a['video_id']}_{a['cid']}.png"
        shutil.copy(res["before_screenshot"], config.SCREENSHOTS_DIR / name)
        return {"ok": True, "profile": res["profile"], "visibility": "public", "publish_response": {"body": {"comment": {"cid": "dry-run"}}}}
    return {"ok": False, "error": res.get("error") or "the dry run failed"}


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="livedesk-"))
    desk = DesktopApp(db=DB(tmp / "bot.db"), poster=dry_poster, host="127.0.0.1", port=0, token=TOKEN)
    ui = None
    try:
        base = desk.start()
        say(f"app running at {base} (Telegram off: {not desk.svc.telegram_enabled}, monitoring: {desk.svc.monitoring}); posting={config.POSTING_ENABLED}")
        ui = UI()
        ui.open(f"{base}/#token={TOKEN}")
        ui.wait("document.querySelector('#url-input') && document.querySelector('#stats .stat')", 20, "home")
        ui.shot(OUT / "live_1_home.png")
        say(f"home loaded: {ui.texts('#stats .stat')}")
        ui.fill("#url-input", URL)
        if ACCOUNT:
            ui.wait("document.querySelector('#read-account').options.length > 1", 15, "accounts in the picker")
            say(f"reading with: {ui.ev(pick('#read-account', ACCOUNT))}")
        ui.click("#analyze-btn")
        say("analysing the real post ...")
        last = ""
        end = time.monotonic() + 300
        while time.monotonic() < end:
            if ui.exists("[data-role=results]") or ui.exists("[data-role=job-error]"):
                break
            msg = ui.text("[data-role=progress] b")
            if msg and msg != last:
                last = msg
                say(f"progress: {msg}")
            time.sleep(1)
        if ui.exists("[data-role=job-error]"):
            say("ANALYSIS FAILED: " + ui.text("[data-role=job-error]"))
            ui.shot(OUT / "live_error.png")
            return 1
        ui.wait("document.querySelector('[data-role=results]')", 5)
        time.sleep(1)
        header = ui.text("[data-role=results]").split("\n")
        say("results: " + " | ".join(h for h in header[:8] if h))
        tabs = ui.texts("[data-role=results] .tab")
        say(f"tabs: {tabs}")
        ui.shot(OUT / "live_2_results.png")
        if ANALYZE_ONLY:
            ui.click("[data-filter=all]")
            time.sleep(0.6)
            for t in ui.texts("#comment-list .cc")[:40]:
                say("  * " + " / ".join(x for x in t.split(chr(10)) if x)[:230])
            ui.shot(OUT / "live_2b_all.png")
            return 0
        cards = ui.count("#comment-list .cc")
        say(f"{cards} comment card(s) under the current filter")
        for t in ui.texts("#comment-list .cc")[:5]:
            say("  - " + " / ".join(x for x in t.split("\n") if x)[:200])
        if not cards:
            ui.click("[data-filter=all]")
            time.sleep(0.5)
            cards = ui.count("#comment-list .cc")
            say(f"(no negatives) showing all: {cards} comments")
        if not cards:
            return 1
        ui.click("#comment-list .cc [data-act=reply]")
        ui.wait("document.querySelector('[data-role=composer]')", 10, "composer")
        say("asking the real AI for a reply ...")
        ui.click("#comment-list .cc [data-act=ai]")
        ui.wait("document.querySelector('#comment-list [data-role=text]').value.length > 5 || document.querySelector('#comment-list .status-line.bad')", 90, "AI reply")
        text = ui.ev("document.querySelector('#comment-list [data-role=text]').value")
        say(f"AI reply: {text!r}")
        if not text:
            say("AI draft failed: " + ui.text("#comment-list .status-line"))
            ui.shot(OUT / "live_ai_failed.png")
            return 1
        if ACCOUNT:
            say(f"replying as: {ui.ev(pick('#comment-list [data-role=account]', ACCOUNT))}")
            time.sleep(1)
        ui.shot(OUT / "live_3_draft.png")
        say("pressing Send - the REAL engine in DRY-RUN (nothing will be sent) ...")
        ui.click("#comment-list [data-act=send]")
        ui.wait("document.querySelector('#comment-list .status-line.ok') || document.querySelector('#comment-list .status-line.bad')", 240, "send result")
        status = ui.text("#comment-list .status-line")
        say(f"status shown to the person: {status!r}")
        time.sleep(1.5)
        ui.shot(OUT / "live_4_sent.png")
        ok = bool(ui.exists("#comment-list .status-line.ok"))
        say(f"proof image shown: {ui.exists('#comment-list .proof img')}")
        say(f"kept browser after the first reply: {tm.warm_account()}")
        # more replies under the same post: the kept browser should make them quick (no page load at all)
        for n in (1, 2):
            if ui.count("#comment-list .cc") <= n:
                break
            t0 = time.monotonic()
            ui.click("#comment-list .cc [data-act=reply]", n)
            ui.wait(f"document.querySelectorAll('#comment-list [data-role=composer]').length === {n + 1}", 10, "composer")
            ui.fill("#comment-list [data-role=text]", "Obrigado pela pergunta! Vamos confirmar essa informacao.", n)
            ui.click("#comment-list [data-act=send]", n)
            seen = set()
            end = time.monotonic() + 200
            while time.monotonic() < end and ui.count("#comment-list .status-line.ok") < n + 1 and ui.count("#comment-list .status-line.bad") == 0:
                strip = ui.text("[data-role=activity-sending] small")
                if strip and strip not in seen:
                    seen.add(strip)
                    say(f"  sidebar: {strip}")
                time.sleep(0.5)
            good = ui.count("#comment-list .status-line.ok") >= n + 1
            say(f"reply #{n + 1} under the same post: {time.monotonic() - t0:.1f}s ok={good}")
            ui.shot(OUT / f"live_5_reply{n + 1}.png")
            ok = ok and good
        say("LIVE CHECK " + ("PASSED (nothing was posted)" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        if ui:
            ui.close()
        desk.stop()
        say(f"after stopping the app: kept browser = {tm.warm_account()}")
        say(f"screenshots: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
