"""Support for testing the desktop app in a REAL browser: a fake world (no TikTok, no posting, no Telegram, throw-away data) the
app runs against, and a small driver that opens the app's page in headless Chrome/Edge and clicks through it."""
import base64
import json
import pathlib
import struct
import tempfile
import time
import zlib
from unittest import mock

from _helpers import make_ai, tmp_db  # noqa: F401  (also puts the project on sys.path)

import config
from app import accounts as acc
from app import browsers
from app.desktop import DesktopApp

TOKEN = "ui-test-token-" * 3


def make_png(path: pathlib.Path, w: int = 420, h: int = 300) -> None:
    """A real PNG (a soft gradient with a 'reply posted' bar) - stands in for the proof screenshot."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            if 30 <= y <= 80 and 24 <= x <= w - 24:
                rows += bytes((255, 255, 255))
            else:
                rows += bytes((int(40 + 90 * x / w), int(30 + 60 * y / h), int(120 + 100 * y / h)))
    raw = zlib.compress(bytes(rows))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


COMMENTS = [
    {"cid": "7000000000000000001", "text": "É golpe, nunca mais compro nessa loja!", "username": "ana.silva", "digg_count": 12, "create_time": int(time.time()) - 3600, "reply_count": 2},
    {"cid": "7000000000000000002", "text": "Meu pedido está atrasado há 3 semanas, péssimo atendimento", "username": "bruno_costa", "digg_count": 4, "create_time": int(time.time()) - 7200, "reply_count": 0},
    {"cid": "7000000000000000003", "text": "Vocês entregam no Brasil?", "username": "carla.m", "digg_count": 1, "create_time": int(time.time()) - 9000, "reply_count": 0},
    {"cid": "7000000000000000004", "text": "Amei, lindo demais 😍", "username": "dani", "digg_count": 30, "create_time": int(time.time()) - 12000, "reply_count": 0},
    {"cid": "7000000000000000005", "text": "kkkkkk", "username": "edu_games", "digg_count": 0, "create_time": int(time.time()) - 15000, "reply_count": 0},
    {"cid": "7000000000000000006", "text": "Produto de péssima qualidade, veio quebrado. Vergonha!", "username": "fabio77", "digg_count": 7, "create_time": int(time.time()) - 20000, "reply_count": 1},
]
POST_URL = "https://www.tiktok.com/@examplebrand/video/7100000000000000001"


def classify_line(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("golpe", "fraude", "scam", "vergonha")):
        return "NEG|pt|3|scam|accuses the shop of fraud"
    if any(w in t for w in ("atras", "péssim", "pessim", "quebrado", "demora")):
        return "NEG|pt|2|service|complains about the service"
    if "?" in t:
        return "Q|pt|0|other|asks about delivery"
    return "OK|pt|0|other|"


def responder(model, system, user):
    if "screen comments" in system:
        lines = []
        for row in user.splitlines():
            n, _, text = row.partition(". ")
            lines.append(f"{n}|{classify_line(text).split('|', 1)[1]}" if False else f"{n}|{classify_line(text)}")
        return {"content": "\n".join(lines)}
    return {"content": "Sentimos muito pelo transtorno! Fale com a gente por mensagem direta, por favor."}


class World:
    """Everything the app needs, faked: comments to read, a poster that 'sends', two accounts, throw-away config and data."""

    def __init__(self, *, comments=None, reader_error=None, post_results=None):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="uiworld-"))
        self._saved = {k: getattr(config, k) for k in (
            "ACCOUNTS_DIR", "ACCOUNTS_FILE", "BFP_MASTER_USER_DATA_DIR", "REPLY_ACCOUNT_FIRST", "BFP_PROFILE_NAME", "BFP_PROFILE_NAMES",
            "POSTING_ENABLED", "LINK_COMMENTS_PER_DAY", "SCREENSHOTS_DIR", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENROUTER_API_KEY",
            "BRAND_NAME", "API_TOKEN_FILE", "TARGET_PROFILE_URL")}
        self._choose = browsers.choose
        config.ACCOUNTS_DIR, config.ACCOUNTS_FILE = self.tmp / "real", self.tmp / "accounts.json"
        config.BFP_MASTER_USER_DATA_DIR = str(self.tmp / "master" / "User Data")
        config.REPLY_ACCOUNT_FIRST, config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES = "", "page-account", ["page-account"]
        config.POSTING_ENABLED, config.LINK_COMMENTS_PER_DAY = True, 20
        config.SCREENSHOTS_DIR = self.tmp / "shots"
        config.SCREENSHOTS_DIR.mkdir()
        config.API_TOKEN_FILE = self.tmp / "api_token.txt"
        config.OPENROUTER_API_KEY, config.BRAND_NAME = "sk-or-v1-test-key-0123456789abcdef", "Example Store"
        config.TARGET_PROFILE_URL = "https://www.tiktok.com/@examplebrand"
        browsers.choose = lambda pref="auto", **kw: {"kind": "chrome", "name": "Google Chrome", "path": r"C:\fake\chrome.exe", "configured": False}
        self._identify = acc_identify_patch()
        self.comments = list(COMMENTS if comments is None else comments)
        self.reader_error = reader_error
        self.post_results = list(post_results or [])
        self.reader_calls, self.posted, self.windows = [], [], []
        self.hold = None
        self.window_kwargs: dict = {}
        self.db = tmp_db()
        self.ai = make_ai(self.db, responder)
        self.seed_account("real-account-1", "alpha.shop", session="ok")
        self.seed_account("real-account-2", "beta.shop", session="expired")
        self._net = mock.patch("app.health._internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "ok",
                                                                    "detail": "TikTok can be reached.", "fix": ""})
        self._net.start()                                          # no test depends on this computer's network
        self.verify_result = {"ok": True, "count": 1, "preview": []}       # what "check on TikTok" finds (a test sets it)
        self.verify_hold = None
        self.app = DesktopApp(db=self.db, ai=self.ai, reader=self.reader, poster=self.poster, host="127.0.0.1", port=0, token=TOKEN,
                              clean_leftovers=False, verifier=self.verifier, backups=False, maintenance=False)
        self.app.svc._memory_check = lambda: (True, 8000.0)
        am = self.app.svc.accounts
        am._launch, am.poll_s, am._settle_s, am.identify_timeout_s, am._login_timeout_s = self.launch, 0.05, 0, 0.3, 30
        am._sleep = lambda s: time.sleep(0.01)
        self.url = ""

    # -- fakes
    def reader(self, url, *, account=None, max_comments=240, progress=None, should_stop=None, stage=None):
        if stage:
            stage("Opening the post with the first account...")
        self.reader_calls.append({"url": url, "account": account, "max": max_comments})
        if self.reader_error:
            return {"ok": False, "error": self.reader_error, "captcha": "captcha" in self.reader_error}
        for i in range(0, len(self.comments) + 1, 2):
            if progress:
                progress(i, len(self.comments))
            time.sleep(0.15)
        return {"ok": True, "profile": account or "page-account", "logged_in": True, "comments": self.comments, "total": len(self.comments),
                "truncated": False, "description": "Unboxing the new phones - tag a friend who needs one!", "author": "examplebrand"}

    def verifier(self, a):
        if a.get("_stage"):
            a["_stage"]("Looking at the replies...")
        if self.verify_hold is not None:
            self.verify_hold.wait(30)
        time.sleep(0.3)
        return dict(self.verify_result)

    def poster(self, a):
        self.posted.append(dict(a))
        result = self.post_results.pop(0) if self.post_results else {"ok": True}
        if a.get("_stage"):                                    # what the real engine does: tell the screen each step
            a["_stage"]("Finding the comment...")
        if self.hold is not None:                              # a test that wants to look at the screen mid-send
            self.hold.wait(30)
        time.sleep(0.4)
        if result.get("ok"):
            # the file names the real engine uses: a new comment on a post vs a reply to a comment
            name = f"proof_comment_{a['video_id']}.png" if a.get("kind") == "link_post" else f"proof_{a['video_id']}_{a['cid']}.png"
            make_png(config.SCREENSHOTS_DIR / name)
            return {"ok": True, "profile": a.get("account") or "page-account", "visibility": result.get("visibility", "public"),
                    "publish_response": {"body": {"comment": {"cid": "999"}}}}
        return {"ok": False, "error": result.get("error", "TikTok did not answer")}

    def launch(self, exe, udd):
        from test_account_manager import FakeWindow
        w = FakeWindow(udd, **self.window_kwargs)
        self.windows.append(w)
        return w

    def seed_account(self, name, username, **extra):
        udd = config.ACCOUNTS_DIR / name / "User Data"
        udd.mkdir(parents=True, exist_ok=True)
        (udd / "Cookies").write_text("x")
        acc._write_registry(config.load_accounts() + [{"name": name, "user_data_dir": str(udd), "username": username, "region": "BD", "enabled": True,
                                                       "added": "2026-10-01 10:00:00", **extra}])

    def start(self) -> str:
        self.url = f"{self.app.start()}/#token={TOKEN}"
        return self.url

    def stop(self):
        self.app.stop()
        self._net.stop()
        self._identify.stop()
        browsers.choose = self._choose
        for k, v in self._saved.items():
            setattr(config, k, v)


def acc_identify_patch():
    from unittest import mock
    p = mock.patch.object(acc, "identify", return_value={"ok": False, "who": None, "error": "no browser in tests"})
    p.start()
    return p


# ------------------------------------------------------------------------------------------------ the browser driver
class UI:
    def __init__(self, width: int = 1360, height: int = 900) -> None:
        import bfp
        installed = {b["kind"]: b for b in browsers.find_browsers()}          # the REAL ones (World fakes browsers.choose)
        chosen = installed.get("chrome") or installed.get("edge") or installed.get("brave")
        if chosen is None:
            raise RuntimeError("no Chrome / Edge / Brave installed")
        self.tmp = tempfile.mkdtemp(prefix="uidrv-")
        self.browser = bfp.CDPBrowser(bfp.ProfileFactory().desktop(), executable=chosen["path"], headless=True,
                                      user_data_dir=pathlib.Path(self.tmp), extra_args=[f"--window-size={width},{height}"])
        self.browser.preserve_real_identity = True
        self.browser.start()
        self.page = self.browser.driver
        self.size = (width, height)
        self.resize(width, height)

    def resize(self, w, h):
        self.page.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)

    def open(self, url):
        self.page.goto(url)
        self.wait("document.readyState === 'complete'", 15)

    def ev(self, js):
        return self.page.evaluate(js)

    def wait(self, js, timeout=12.0, msg=""):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                last = self.ev(f"(() => {{ try {{ return !!({js}); }} catch (e) {{ return false; }} }})()")
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            if last is True:
                return True
            time.sleep(0.15)
        shot = ""
        try:                                                   # a picture of the page at the moment of the failure, for whoever investigates
            shot = self.shot(pathlib.Path(tempfile.gettempdir()) / f"ui-failure-{int(time.time())}.png")
        except Exception:  # noqa: BLE001
            pass
        raise AssertionError(f"timed out waiting for: {msg or js}  (last: {last}); page text: {self.text('body')[:600]!r}; screenshot: {shot}")

    def exists(self, sel):
        return bool(self.ev(f"!!document.querySelector({json.dumps(sel)})"))

    def count(self, sel):
        return int(self.ev(f"document.querySelectorAll({json.dumps(sel)}).length"))

    def text(self, sel):
        return self.ev(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); return e ? e.innerText : ''; }})()") or ""

    def texts(self, sel):
        return self.ev(f"[...document.querySelectorAll({json.dumps(sel)})].map(e => e.innerText)") or []

    def click(self, sel, nth=0):
        ok = self.ev(f"(() => {{ const e = document.querySelectorAll({json.dumps(sel)})[{nth}]; if (!e) return false; e.click(); return true; }})()")
        if not ok:
            raise AssertionError(f"nothing to click: {sel} [{nth}]; page text: {self.text('body')[:300]!r}")

    def fill(self, sel, text, nth=0):
        ok = self.ev(f"(() => {{ const e = document.querySelectorAll({json.dumps(sel)})[{nth}]; if (!e) return false; e.focus(); e.value = {json.dumps(text)};"
                     " e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); return true; })()")
        if not ok:
            raise AssertionError(f"nothing to fill: {sel}")

    def select(self, sel, value):
        self.ev(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(value)}; e.dispatchEvent(new Event('change', {{bubbles: true}})); }})()")

    def shot(self, path, full=False):
        self.page.screenshot(path, full=full)
        return path

    def close(self):
        try:
            self.browser.stop()
        except Exception:  # noqa: BLE001
            pass
