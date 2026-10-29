"""The desktop app's local API and the service wiring behind it: security, status, live events, approvals, link comments,
settings, accounts - through a REAL HTTP server on a real socket (no browser, no Telegram, no TikTok)."""
import http.client
import json
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from _helpers import FakeTG, comment, make_ai, tmp_db  # noqa: F401
from test_account_manager import CHROME, FakeWindow, who

import config
from app import accounts as acc
from app import analysis, api, browsers, links, notifier
from app.service import Service

TOKEN = "t" * 40
VID, CID = "7695930429047688468", "7234567890123456789"
POST = f"https://www.tiktok.com/@shop/video/{VID}"


class ApiCase(unittest.TestCase):
    start_service = False

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="api-"))
        self._saved = (config.ACCOUNTS_DIR, config.ACCOUNTS_FILE, config.BFP_MASTER_USER_DATA_DIR, config.REPLY_ACCOUNT_FIRST,
                       config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES, config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED,
                       config.LINK_COMMENTS_PER_DAY, config.SCREENSHOTS_DIR, browsers.choose, config.API_ALLOWED_ORIGINS)
        config.ACCOUNTS_DIR, config.ACCOUNTS_FILE = self.tmp / "real", self.tmp / "accounts.json"
        config.BFP_MASTER_USER_DATA_DIR = str(self.tmp / "master" / "User Data")
        config.REPLY_ACCOUNT_FIRST, config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES = "", "master-acct", ["master-acct"]
        config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED, config.LINK_COMMENTS_PER_DAY = "4242", True, 10
        config.SCREENSHOTS_DIR = self.tmp / "shots"
        config.SCREENSHOTS_DIR.mkdir()
        config.API_ALLOWED_ORIGINS = []
        browsers.choose = lambda pref="auto", **kw: CHROME
        self._identify = mock.patch.object(acc, "identify", return_value={"ok": False, "who": None, "error": "no browser"})
        self._identify.start()
        self.db = tmp_db()
        self.tg = FakeTG()
        self.ai = make_ai(self.db, [{"content": "Sorry about that! Please DM us your order number."}])
        self.svc = Service(self.db, tg=self.tg, ai=self.ai, scanner=lambda db: {"opened": 0, "new": 0},
                           poster=lambda a: {"ok": True, "profile": "master-acct"}, api=False)
        self.svc._memory_check = lambda: (True, 8000.0)
        self.windows: list = []
        self.window_kwargs: dict = {}
        self.svc.accounts._launch = self.launch
        self.svc.accounts.poll_s, self.svc.accounts._settle_s = 0.01, 0
        self.svc.accounts.identify_timeout_s, self.svc.accounts._login_timeout_s = 0.2, 5
        self.svc.accounts._sleep = lambda s: time.sleep(0.001)
        if self.start_service:
            self.svc.start()
        else:
            self.svc.accounts._worker = lambda fn, *a: fn(*a)
        self.server = api.ApiServer(self.svc.controller, self.svc.events, self.svc.accounts, self.svc,
                                    host="127.0.0.1", port=0, token=TOKEN).start()

    def tearDown(self):
        self.server.stop()
        self.svc.stop()
        self._identify.stop()
        (config.ACCOUNTS_DIR, config.ACCOUNTS_FILE, config.BFP_MASTER_USER_DATA_DIR, config.REPLY_ACCOUNT_FIRST,
         config.BFP_PROFILE_NAME, config.BFP_PROFILE_NAMES, config.TELEGRAM_CHAT_ID, config.POSTING_ENABLED,
         config.LINK_COMMENTS_PER_DAY, config.SCREENSHOTS_DIR, browsers.choose, config.API_ALLOWED_ORIGINS) = self._saved

    # -- helpers
    def launch(self, exe, udd):
        w = FakeWindow(udd, **self.window_kwargs)
        self.windows.append(w)
        return w

    def call(self, method, path, body=None, *, token=TOKEN, headers=None, raw_body=None):
        """-> (status, parsed JSON or None, response headers)"""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        hdrs = dict(headers or {})
        if token:
            hdrs.setdefault("Authorization", f"Bearer {token}")
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                parsed = json.loads(payload) if payload else None
            except ValueError:
                parsed = None
            return resp.status, parsed, dict(resp.getheaders())
        finally:
            conn.close()

    def ok(self, method, path, body=None, status=200):
        code, data, _ = self.call(method, path, body)
        self.assertEqual(code, status, f"{method} {path} -> {code} {data}")
        return data

    def err(self, method, path, body=None, status=400, code=None):
        got, data, _ = self.call(method, path, body)
        self.assertEqual(got, status, f"{method} {path} -> {got} {data}")
        if code:
            self.assertEqual(data["error"]["code"], code)
        return data["error"]

    def make_alert(self, cid="c1", text="É golpe, nunca mais!"):
        self.db.upsert_post({"video_id": "v1", "url": "https://www.tiktok.com/@x/video/v1", "description": "iPhone 17",
                             "is_pinned": 0, "comment_count": 3})
        self.db.set_watch_set(["v1"])
        analysis.ingest_comments(self.db, "v1", [comment(cid, text)], first_scan=False)
        self.db.update_comment(cid, status="negative", sentiment="NEG", lang="pt", severity=3, category="scam",
                               reason="accuses the shop of fraud", source="ai")
        notifier.notify_pending(self.db, self.ai, self.tg, "en", include_questions=False, burst=5)
        return self.db.list_approvals(("pending",))[0]

    def seed_account(self, name, username, **extra):
        udd = config.ACCOUNTS_DIR / name / "User Data"
        udd.mkdir(parents=True, exist_ok=True)
        (udd / "Cookies").write_text("x")
        acc._write_registry(config.load_accounts() + [{"name": name, "user_data_dir": str(udd), "username": username,
                                                       "enabled": True, "added": "2026-10-01 10:00:00", **extra}])

    def wait_flow(self, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            flow = self.ok("GET", "/api/accounts/add")["flow"]
            if flow and flow["finished"]:
                return flow
            time.sleep(0.02)
        self.fail("the add flow never finished")


class TestSecurity(ApiCase):
    def test_ping_is_open_everything_else_needs_the_token(self):
        self.assertEqual(self.call("GET", "/api/ping", token=None)[:2], (200, {"ok": True, "app": "tiktok-guard", "api": 1}))
        for path in ("/api/status", "/api/accounts", "/api/approvals", "/api/settings", "/api/events"):
            code, data, _ = self.call("GET", path, token=None)
            self.assertEqual((code, data["error"]["code"]), (401, "unauthorized"), path)
        self.assertEqual(self.call("POST", "/api/accounts/add", {}, token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/status", token="wrong" * 8)[0], 401)
        self.assertEqual(self.call("GET", "/api/status")[0], 200)

    def test_a_token_in_the_url_works_for_reading_only(self):
        self.assertEqual(self.call("GET", f"/api/status?token={TOKEN}", token=None)[0], 200)
        self.assertEqual(self.call("POST", f"/api/accounts/add/cancel?token={TOKEN}", {}, token=None)[0], 401,
                         "a URL token never authorises a change")

    def test_a_request_for_another_host_name_is_refused_dns_rebinding(self):
        code, data, _ = self.call("GET", "/api/status", headers={"Host": "evil.example.com"})
        self.assertEqual((code, data["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.call("GET", "/api/ping", token=None, headers={"Host": "attacker.test:8765"})[0], 403)
        self.assertEqual(self.call("GET", "/api/status", headers={"Host": f"localhost:{self.server.port}"})[0], 200)

    def test_only_local_origins_may_call_it_from_a_browser(self):
        for origin in ("https://evil.example.com", "http://localhost.evil.com", "http://127.0.0.1.evil.io"):
            self.assertEqual(self.call("GET", "/api/status", headers={"Origin": origin})[0], 403, origin)
        for origin in ("http://localhost:5173", "http://127.0.0.1:3000", "null", "tauri://localhost", "app://shell"):
            code, _data, headers = self.call("GET", "/api/status", headers={"Origin": origin})
            self.assertEqual(code, 200, origin)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)
        self.assertNotIn("Access-Control-Allow-Origin", self.call("GET", "/api/status")[2], "no Origin, no CORS header")

    def test_an_origin_can_be_allowed_in_the_settings(self):
        config.API_ALLOWED_ORIGINS = ["https://my.desktop.shell"]
        self.assertEqual(self.call("GET", "/api/status", headers={"Origin": "https://my.desktop.shell"})[0], 200)

    def test_the_cors_preflight_answers_without_a_token_but_only_for_local_origins(self):
        code, _d, h = self.call("OPTIONS", "/api/accounts/add", token=None,
                                headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
        self.assertEqual(code, 204)
        self.assertIn("POST", h["Access-Control-Allow-Methods"])
        self.assertIn("Authorization", h["Access-Control-Allow-Headers"])
        self.assertEqual(self.call("OPTIONS", "/api/accounts/add", token=None, headers={"Origin": "https://evil.example.com"})[0], 403)

    def test_bad_requests_get_clear_errors(self):
        self.err("GET", "/api/nope", status=404, code="not_found")
        self.err("DELETE", "/api/status", status=405, code="method_not_allowed")
        code, data, _ = self.call("POST", "/api/approvals/x/reply", raw_body=b"{not json")
        self.assertEqual((code, data["error"]["code"]), (400, "bad_request"))
        code, data, _ = self.call("POST", "/api/approvals/x/reply", raw_body=b"[1,2]")
        self.assertEqual((code, data["error"]["code"]), (400, "bad_request"))
        code, data, _ = self.call("POST", "/api/approvals/x/reply", raw_body=b"x" * 1_100_000)
        self.assertEqual((code, data["error"]["code"]), (413, "too_large"))
        self.assertEqual(self.call("GET", "/api/status")[0], 200, "the server is fine afterwards")

    def test_screenshots_are_served_only_from_their_folder(self):
        (config.SCREENSHOTS_DIR / "proof_1.png").write_bytes(b"\x89PNG fake")
        (self.tmp / "secret.png").write_bytes(b"\x89PNG secret")
        (self.tmp / "accounts.json").write_text("{}")
        code, _d, h = self.call("GET", "/api/screenshots/proof_1.png")
        self.assertEqual((code, h["Content-Type"]), (200, "image/png"))
        for bad in ("../secret.png", "%2e%2e/secret.png", "..%5csecret.png", "proof_1.png/../../secret.png", "accounts.json",
                    "nope.png", "a%00.png", ".."):
            self.assertEqual(self.call("GET", f"/api/screenshots/{bad}")[0], 404, bad)
        self.assertEqual(self.call("GET", "/api/screenshots/proof_1.png", token=None)[0], 401)

    def test_no_other_program_can_take_over_the_port(self):
        with self.assertRaises(OSError):
            api.ApiServer(self.svc.controller, self.svc.events, self.svc.accounts, host="127.0.0.1", port=self.server.port,
                          token=TOKEN).start()
        self.assertEqual(self.call("GET", "/api/ping", token=None)[0], 200, "the first server is unaffected")

    def test_the_pages_need_no_token_and_embed_none(self):
        for path, marker in (("/", "<title>TikTok Guard</title>"), ("/app", "<title>TikTok Guard</title>"),
                             ("/console", "backend console")):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            html = resp.read().decode()
            self.assertEqual(resp.status, 200, path)
            self.assertIn(marker, html, path)
            self.assertNotIn(TOKEN, html, path)
            self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
            self.assertIn("img-src 'self' data: blob:", resp.getheader("Content-Security-Policy"), "proof screenshots load")
            self.assertNotIn("innerHTML", html, f"{path}: untrusted TikTok text is only ever inserted as text")
            self.assertNotIn("EventSource", html.replace("EventSource(\"/api/events/stream", ""), path) if path != "/console" else None

    def test_the_app_page_has_no_notifications_or_background_machinery(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/")
        html = conn.getresponse().read().decode()
        for forbidden in ("Notification.", "new Notification", "serviceWorker", "EventSource", "setInterval", "/api/events"):
            self.assertNotIn(forbidden, html, f"the desktop page must not use {forbidden}: no notifications, no polling in the background")

    def test_a_browser_that_drops_its_connection_does_not_make_the_console_noisy(self):
        import contextlib
        import io
        import struct
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                s = socket.create_connection(("127.0.0.1", self.server.port), timeout=3)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))      # close = a hard reset
                s.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n")                      # ... in the middle of a request
                s.close()
            time.sleep(0.5)
        self.assertEqual(err.getvalue(), "", "no traceback for a client that simply went away")
        self.assertEqual(self.call("GET", "/api/ping", token=None)[0], 200, "the server is fine")

    def test_the_token_is_created_once_and_kept(self):
        saved = (config.API_TOKEN, config.API_TOKEN_FILE)
        config.API_TOKEN, config.API_TOKEN_FILE = "", self.tmp / "api_token.txt"
        try:
            first = api.load_token()
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(api.load_token(), first)
            self.assertEqual(config.API_TOKEN_FILE.read_text().strip(), first)
            config.API_TOKEN = "from-the-env-file-" * 2
            self.assertEqual(api.load_token(), config.API_TOKEN, ".env wins")
        finally:
            config.API_TOKEN, config.API_TOKEN_FILE = saved


class TestStatusAndEvents(ApiCase):
    def test_status_describes_the_whole_backend(self):
        s = self.ok("GET", "/api/status")
        self.assertEqual(s["api"]["version"], 1)
        self.assertTrue(s["monitoring"]["always_on"])
        self.assertEqual((s["posting_enabled"], s["waiting"], s["telegram_enabled"]), (True, 0, True))
        self.assertEqual((s["ai"]["daily_requests"], s["ai"]["used"]), (config.AI_DAILY_REQUESTS, 0))
        self.assertEqual(s["link_comments"], {"today": 0, "per_day": 10})
        self.assertIn("mb", s["database"])

    def test_waiting_cards_and_problems_show_up_in_status(self):
        self.make_alert()
        self.db.log_event("scan_failed", "TikTok said no", "error")
        s = self.ok("GET", "/api/status")
        self.assertEqual(s["waiting"], 1)
        self.assertEqual(s["recent_problems"][0]["kind"], "scan_failed")

    def test_polling_starts_from_now_and_then_returns_what_happened(self):
        head = self.ok("GET", "/api/events")
        self.assertEqual(head["events"], [])
        self.make_alert()
        got = self.ok("GET", f"/api/events?since={head['last_id']}")
        self.assertIn("approval.created", [e["type"] for e in got["events"]])
        self.assertEqual(got["last_id"], got["events"][-1]["id"])
        self.assertEqual(self.ok("GET", f"/api/events?since={got['last_id']}")["events"], [], "nothing new")

    def test_long_polling_waits_for_the_next_event(self):
        head = self.ok("GET", "/api/events")["last_id"]
        threading.Timer(0.3, lambda: self.svc.events.publish("custom", {"n": 1})).start()
        t0 = time.monotonic()
        got = self.ok("GET", f"/api/events?since={head}&timeout=5")
        self.assertEqual([e["type"] for e in got["events"]], ["custom"])
        self.assertLess(time.monotonic() - t0, 3)

    def test_database_changes_become_events(self):
        a = self.make_alert()
        self.db.update_approval(a["id"], status="rejected")
        self.db.set_setting("watch_latest", "3")
        self.db.log_event("something", "detail", "warn")
        events = self.svc.events.since(0)
        by_type = {}
        for e in events:
            by_type.setdefault(e["type"], []).append(e)
        self.assertEqual(by_type["approval.created"][0]["data"]["id"], a["id"])
        self.assertEqual(by_type["approval.updated"][-1]["data"]["status"], "rejected")
        self.assertEqual(by_type["setting.changed"][0]["data"], {"key": "watch_latest"})
        self.assertEqual((by_type["log"][-1]["level"], by_type["log"][-1]["data"]["kind"]), ("warn", "something"))

    def stream(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", path, headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})
        return conn, conn.getresponse()

    @staticmethod
    def read_event(resp):
        """-> (id, type, data) of the next event in a text/event-stream."""
        fields = {}
        while True:
            line = resp.readline().decode("utf-8").rstrip("\r\n")
            if line == "":
                if "event" in fields:
                    return fields.get("id"), fields["event"], json.loads(fields["data"])
                continue
            if line.startswith(":") or line.startswith("retry"):
                continue
            k, _, v = line.partition(": ")
            fields[k] = v

    def test_the_stream_delivers_events_as_they_happen(self):
        conn, resp = self.stream("/api/events/stream")
        try:
            self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/event-stream; charset=utf-8"))
            self.svc.events.publish("account.flow", {"flow": {"state": "window_open"}})
            eid, etype, data = self.read_event(resp)
            self.assertEqual((etype, data["data"]["flow"]["state"]), ("account.flow", "window_open"))
            self.assertEqual(int(eid), data["id"])
        finally:
            conn.close()

    def test_a_reconnecting_client_gets_what_it_missed(self):
        first = self.svc.events.publish("one")
        self.svc.events.publish("two")
        self.svc.events.publish("three")
        conn, resp = self.stream("/api/events/stream", headers={"Last-Event-ID": str(first["id"])})
        try:
            self.assertEqual(self.read_event(resp)[1], "two")
            self.assertEqual(self.read_event(resp)[1], "three")
        finally:
            conn.close()

    def test_the_stream_takes_the_token_from_the_url_for_the_browsers_eventsource(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", f"/api/events/stream?token={TOKEN}")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        conn.close()
        self.assertEqual(self.call("GET", "/api/events/stream", token=None)[0], 401)

    def test_stopping_the_server_ends_open_streams_promptly(self):
        conn, resp = self.stream("/api/events/stream")
        t0 = time.monotonic()
        threading.Thread(target=self.server.stop).start()
        data = resp.read()                                # returns when the server closes the stream
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIsInstance(data, bytes)
        conn.close()


class TestApprovals(ApiCase):
    def test_a_waiting_card_is_listed_with_everything_a_screen_needs(self):
        a = self.make_alert()
        listing = self.ok("GET", "/api/approvals")["approvals"]
        self.assertEqual([x["id"] for x in listing], [a["id"]])
        v = self.ok("GET", f"/api/approvals/{a['id']}")
        self.assertEqual((v["kind"], v["status"], v["reply"]), ("alert", "pending", "Sorry about that! Please DM us your order number."))
        self.assertEqual((v["comment"]["text"], v["comment"]["severity"], v["comment"]["category"]),
                         ("É golpe, nunca mais!", 3, "scam"))
        self.assertEqual(v["post"]["description"], "iPhone 17")
        self.assertEqual(v["can"], {"post": True, "edit": True, "ignore": True, "regenerate": True, "choose_account": True})

    def test_filtering_by_status_and_kind(self):
        a = self.make_alert()
        link = self.ok("POST", "/api/comments/link", {"link": POST, "text": "hi"}, status=201)
        self.assertEqual({x["id"] for x in self.ok("GET", "/api/approvals")["approvals"]}, {a["id"], link["id"]})
        self.assertEqual([x["id"] for x in self.ok("GET", "/api/approvals?kind=link")["approvals"]], [link["id"]])
        self.assertEqual([x["id"] for x in self.ok("GET", "/api/approvals?kind=alert")["approvals"]], [a["id"]])
        self.ok("POST", f"/api/approvals/{a['id']}/ignore")
        self.assertEqual([x["id"] for x in self.ok("GET", "/api/approvals?status=rejected")["approvals"]], [a["id"]])

    def test_editing_the_text_shortens_it_and_keeps_telegram_in_step(self):
        a = self.make_alert()
        self.db.update_approval(a["id"], message_id=77)
        v = self.ok("POST", f"/api/approvals/{a['id']}/reply", {"text": "Obrigado pelo aviso, vamos resolver!"})
        self.assertEqual((v["reply"], v["shortened"]), ("Obrigado pelo aviso, vamos resolver!", False))
        self.assertIn("Obrigado pelo aviso", self.tg.edits[-1]["text"], "the Telegram card shows the new text")
        long = self.ok("POST", f"/api/approvals/{a['id']}/reply", {"text": "palavra " * 40})
        self.assertTrue(long["shortened"])
        self.assertLessEqual(len(long["reply"]), 150)
        self.err("POST", f"/api/approvals/{a['id']}/reply", {"text": "   "}, 400, "bad_request")
        self.err("POST", f"/api/approvals/{a['id']}/reply", {}, 400, "bad_request")

    def test_regenerate_asks_the_ai_and_stops_at_the_limit(self):
        a = self.make_alert()
        for n in range(1, 4):
            self.assertEqual(self.ok("POST", f"/api/approvals/{a['id']}/regenerate")["regen_count"], n)
        self.err("POST", f"/api/approvals/{a['id']}/regenerate", None, 429, "regen_limit")

    def test_a_failing_ai_is_reported_as_a_bad_gateway(self):
        a = self.make_alert()
        self.svc.controller.ai = make_ai(self.db, [{"content": None, "http": 500, "error": "boom"}])
        self.err("POST", f"/api/approvals/{a['id']}/regenerate", None, 502, "ai_failed")

    def test_post_queues_the_job_and_telegram_sees_it(self):
        a = self.make_alert()
        self.db.update_approval(a["id"], message_id=77)
        v = self.ok("POST", f"/api/approvals/{a['id']}/post")
        self.assertEqual((v["status"], v["post_attempts"]), ("posting", 1))
        self.assertEqual(self.svc._jobs.qsize(), 1)
        self.assertIn("Posting to TikTok", self.tg.edits[-1]["text"])
        self.err("POST", f"/api/approvals/{a['id']}/post", None, 409, "already_handled")
        self.assertEqual(self.svc._jobs.qsize(), 1, "never queued twice")

    def test_with_posting_off_post_only_marks_it_ready(self):
        config.POSTING_ENABLED = False
        a = self.make_alert()
        self.assertEqual(self.ok("POST", f"/api/approvals/{a['id']}/post")["status"], "approved")
        self.assertEqual(self.svc._jobs.qsize(), 0)

    def test_post_without_text_is_refused(self):
        a = self.make_alert()
        self.db.update_approval(a["id"], reply=None)
        self.err("POST", f"/api/approvals/{a['id']}/post", None, 409, "no_reply")

    def test_ignore_and_unknown_cards(self):
        a = self.make_alert()
        self.assertEqual(self.ok("POST", f"/api/approvals/{a['id']}/ignore")["status"], "rejected")
        self.err("POST", f"/api/approvals/{a['id']}/ignore", None, 409, "already_handled")
        self.err("GET", "/api/approvals/doesnotexist", None, 404, "not_found")
        self.err("POST", "/api/approvals/doesnotexist/post", None, 404, "not_found")

    def test_a_posted_card_exposes_its_proof_screenshot(self):
        a = self.make_alert()
        self.db.update_approval(a["id"], status="posted", posted_by="master-acct")
        (config.SCREENSHOTS_DIR / f"proof_v1_{a['cid']}.png").write_bytes(b"\x89PNG fake")
        self.assertEqual(self.ok("GET", f"/api/approvals/{a['id']}")["proof_file"], f"proof_v1_{a['cid']}.png")
        self.assertEqual(self.call("GET", f"/api/screenshots/proof_v1_{a['cid']}.png")[0], 200)


class TestLinkComments(ApiCase):
    def test_preview_tells_what_a_link_points_to_without_creating_anything(self):
        p = self.ok("POST", "/api/links/preview", {"link": f"{POST}?comment_id={CID}"})
        self.assertEqual((p["kind"], p["video_id"], p["comment_id"], p["username"]), ("comment", VID, CID, "shop"))
        self.assertEqual(self.ok("GET", "/api/approvals")["approvals"], [])

    def test_create_a_card_then_write_post(self):
        v = self.ok("POST", "/api/comments/link", {"link": POST, "text": "Great video!"}, status=201)
        self.assertEqual((v["kind"], v["status"], v["reply"], v["target"]["video_id"]), ("link_post", "pending", "Great video!", VID))
        self.assertTrue(v["can"]["choose_account"])
        self.ok("POST", f"/api/approvals/{v['id']}/reply", {"text": "Obrigado!"})
        self.assertEqual(self.ok("POST", f"/api/approvals/{v['id']}/post")["status"], "posting")

    def test_a_comment_link_makes_a_reply_card(self):
        v = self.ok("POST", "/api/comments/link", {"link": f"{POST}?comment_id={CID}"}, status=201)
        self.assertEqual((v["kind"], v["target"]["comment_id"]), ("link_reply", CID))
        self.assertIsNone(v["reply"])
        self.assertFalse(v["can"]["post"], "no text yet")

    def test_a_second_card_for_the_same_target_names_the_open_one(self):
        first = self.ok("POST", "/api/comments/link", {"link": POST}, status=201)
        error = self.err("POST", "/api/comments/link", {"link": POST}, 409, "link_already_open")
        self.assertEqual(error["existing_id"], first["id"])

    def test_bad_links_are_unprocessable_with_a_reason(self):
        for link, code in (("https://example.com/x", "link_not_tiktok"), ("https://www.tiktok.com/@shop", "link_no_post"),
                           ("", "link_no_link")):
            self.err("POST", "/api/comments/link", {"link": link}, 422, code)

    def test_a_short_link_is_expanded_through_the_controller(self):
        self.svc.controller.link_fetch = lambda url: (301, f"{POST}?comment_id={CID}")
        v = self.ok("POST", "/api/comments/link", {"link": "https://vm.tiktok.com/ZMabc/", "text": "nice"}, status=201)
        self.assertEqual((v["kind"], v["target"]["comment_id"]), ("link_reply", CID))

    def test_the_account_of_a_link_comment_is_chosen_and_validated(self):
        self.seed_account("real-account-1", "a.a")
        v = self.ok("POST", "/api/comments/link", {"link": POST, "text": "hi", "account": "real-account-1"}, status=201)
        self.assertEqual((v["account"], v["account_label"]), ("real-account-1", "@a.a"))
        self.assertEqual(self.ok("POST", f"/api/approvals/{v['id']}/account", {"account": None})["account"], None)
        self.err("POST", f"/api/approvals/{v['id']}/account", {"account": "ghost"}, 400, "bad_request")
        self.err("POST", "/api/comments/link", {"link": f"{POST}x", "account": "ghost"}, 400, "bad_request")

    def test_an_alert_card_can_choose_its_account_too(self):
        """The desktop app lets the person pick the account for ANY reply."""
        self.seed_account("real-account-1", "a.a")
        a = self.make_alert()
        self.assertEqual(self.ok("POST", f"/api/approvals/{a['id']}/account", {"account": "real-account-1"})["account"], "real-account-1")
        self.assertEqual(self.db.get_approval(a["id"])["account"], "real-account-1")
        self.ok("POST", f"/api/approvals/{a['id']}/account", {"account": None})
        self.assertIsNone(self.db.get_approval(a["id"])["account"])
        self.err("POST", f"/api/approvals/{a['id']}/account", {"account": "ghost"}, 400, "bad_request")

    def test_the_daily_cap_blocks_the_post_with_a_clear_code(self):
        config.LINK_COMMENTS_PER_DAY = 1
        done = self.ok("POST", "/api/comments/link", {"link": f"{POST}1", "text": "one"}, status=201)
        self.db.update_approval(done["id"], status="posted", posted_by="m", posted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        v = self.ok("POST", "/api/comments/link", {"link": POST, "text": "two"}, status=201)
        error = self.err("POST", f"/api/approvals/{v['id']}/post", None, 429, "cap_reached")
        self.assertEqual(error["limit"], 1)

    def test_ai_writes_the_link_comment_from_a_brief(self):
        v = self.ok("POST", "/api/comments/link", {"link": POST}, status=201)
        self.err("POST", f"/api/approvals/{v['id']}/regenerate", {}, 400, "bad_request")
        got = self.ok("POST", f"/api/approvals/{v['id']}/regenerate", {"brief": "thank them and invite them to the store"})
        self.assertEqual(got["reply"], "Sorry about that! Please DM us your order number.")
        self.assertIn("invite them to the store", self.ai.calls[-1]["user"])


class TestSettings(ApiCase):
    def test_settings_show_defaults_and_limits(self):
        s = self.ok("GET", "/api/settings")
        self.assertTrue(s["ai_instructions"]["is_default"])
        self.assertEqual((s["company_context"]["value"], s["company_context"]["chars"]), ("", 0))
        self.assertEqual((s["watch"], s["notify_questions"]), ({"latest": 5, "pinned": 2}, True))
        self.assertEqual((s["ai_budget"]["max"], s["posting_enabled"]), (1000, True))

    def test_a_partial_update_changes_only_what_it_names(self):
        s = self.ok("PUT", "/api/settings", {"ai_instructions": "Be formal.", "watch_latest": 3, "notify_questions": False,
                                             "ai_daily_requests": 25})
        self.assertEqual((s["ai_instructions"]["value"], s["ai_instructions"]["is_default"]), ("Be formal.", False))
        self.assertEqual((s["watch"], s["notify_questions"], s["ai_budget"]["daily_requests"]), ({"latest": 3, "pinned": 2}, False, 25))
        self.assertEqual(self.db.get_setting("ai_instructions"), "Be formal.", "the Telegram side reads the same setting")

    def test_nothing_is_applied_unless_everything_is_valid(self):
        before = self.ok("GET", "/api/settings")
        for bad in ({"ai_instructions": "ok", "watch_latest": 99}, {"ai_instructions": "x" * (config.AI_INSTRUCTIONS_MAX_CHARS + 1)},
                    {"ai_daily_requests": -1}, {"ai_daily_requests": "40"}, {"notify_questions": "yes"}, {"watch_pinned": True},
                    {"ai_instructions": "ok", "bogus": 1}, {"ai_instructions": "  "}):
            self.err("PUT", "/api/settings", bad, 400, "bad_request")
        self.assertEqual(self.ok("GET", "/api/settings"), before)

    def test_company_context_replace_append_undo_reset(self):
        self.ok("PUT", "/api/settings", {"company_context": "We are a mall in Central City."})
        s = self.ok("PUT", "/api/settings", {"company_context": "Open Monday to Saturday.", "company_context_append": True})
        self.assertEqual(s["company_context"]["value"], "We are a mall in Central City.\n\nOpen Monday to Saturday.")
        self.assertTrue(s["company_context"]["can_undo"])
        s = self.ok("POST", "/api/settings/company-context/undo")
        self.assertEqual(s["company_context"]["value"], "We are a mall in Central City.")
        s = self.ok("POST", "/api/settings/company-context/reset")
        self.assertEqual(s["company_context"]["value"], "")
        s = self.ok("POST", "/api/settings/company-context/undo")            # a reset is itself undoable
        self.assertEqual(s["company_context"]["value"], "We are a mall in Central City.")
        self.err("POST", "/api/settings/company-context/undo", None, 400, "bad_request")      # nothing older left
        self.err("PUT", "/api/settings", {"company_context": "x" * (config.AI_CONTEXT_MAX_CHARS + 1)}, 400, "bad_request")

    def test_ai_instructions_reset_to_the_default(self):
        self.ok("PUT", "/api/settings", {"ai_instructions": "Custom."})
        s = self.ok("POST", "/api/settings/ai-instructions/reset")
        self.assertTrue(s["ai_instructions"]["is_default"])
        self.assertTrue(self.ok("POST", "/api/settings/ai-instructions/undo")["ai_instructions"]["value"] == "Custom.")

    def test_try_the_ai_with_a_sample_comment_and_backfill_and_cleanup(self):
        self.assertEqual(self.ok("POST", "/api/ai/test", {"text": "O produto veio com defeito"})["reply"],
                         "Sorry about that! Please DM us your order number.")
        self.err("POST", "/api/ai/test", {"text": " "}, 400, "bad_request")
        self.assertEqual(self.ok("POST", "/api/backfill", {"days": 30})["queued"], 0)
        self.err("POST", "/api/backfill", {"days": 0}, 400, "bad_request")
        self.err("POST", "/api/backfill", {"days": "7"}, 400, "bad_request")
        self.assertTrue(self.ok("POST", "/api/maintenance/cleanup", status=202)["started"])
        end = time.monotonic() + 5
        while time.monotonic() < end and not [e for e in self.svc.events.since(0) if e["type"] == "cleanup.done"]:
            time.sleep(0.02)
        self.assertTrue([e for e in self.svc.events.since(0) if e["type"] == "cleanup.done"])

    def test_posts_are_listed(self):
        self.make_alert()
        posts = self.ok("GET", "/api/posts")["posts"]
        self.assertEqual((posts[0]["video_id"], posts[0]["comment_count"], posts[0]["pinned"]), ("v1", 3, False))


class TestAccountsOverHttp(ApiCase):
    def test_the_account_list_and_the_browsers_to_choose_from(self):
        self.seed_account("real-account-1", "a.a", session="ok")
        d = self.ok("GET", "/api/accounts")
        self.assertEqual([a["name"] for a in d["accounts"]], ["master-acct", "real-account-1"])
        self.assertEqual(self.ok("GET", "/api/accounts/real-account-1")["label"], "@a.a")
        self.err("GET", "/api/accounts/ghost", None, 404, "not_found")
        self.assertIn("browsers", self.ok("GET", "/api/browsers"))

    def test_add_an_account_end_to_end_with_live_progress(self):
        self.window_kwargs = dict(who=who("shop.one", nick="Shop One"), login_after=10**6)     # the person has not logged in yet
        head = self.ok("GET", "/api/events")["last_id"]
        flow = self.ok("POST", "/api/accounts/add", {"browser": "chrome", "label": "Main page"}, status=202)
        self.assertIn(flow["state"], ("starting", "window_open"))
        end = time.monotonic() + 5
        while time.monotonic() < end and self.ok("GET", "/api/accounts/add")["flow"]["state"] != "window_open":
            time.sleep(0.01)
        current = self.ok("GET", "/api/accounts/add")["flow"]
        self.assertEqual((current["state"], current["waiting_for_you"]), ("window_open", True))
        self.err("POST", "/api/accounts/add", {}, 409, "busy")                 # one window at a time
        self.windows[0].login_after = 0                                         # ...now they log in
        done = self.wait_flow()
        self.assertEqual((done["state"], done["outcome"]), ("done", "added"))
        self.assertEqual(done["result"]["account"]["username"], "shop.one")
        states = [e["data"]["flow"]["state"] for e in self.ok("GET", f"/api/events?since={head}")["events"]
                  if e["type"] == "account.flow"]
        self.assertEqual([s for i, s in enumerate(states) if i == 0 or states[i - 1] != s][-6:],
                         ["starting", "window_open", "login_detected", "verifying", "saving", "done"])
        listed = self.ok("GET", "/api/accounts")["accounts"]
        self.assertEqual([(a["name"], a["label"], a["session"]) for a in listed if not a["is_master"]],
                         [("real-account-1", "Main page", "ok")])
        self.assertEqual(self.windows[0].closed, True)

    def test_cancel_a_login_window(self):
        self.window_kwargs = dict(login_after=10**6)
        self.ok("POST", "/api/accounts/add", {}, status=202)
        time.sleep(0.1)
        self.ok("POST", "/api/accounts/add/cancel", {})
        self.assertEqual(self.wait_flow()["outcome"], "cancelled")
        self.assertEqual(self.ok("GET", "/api/accounts")["accounts"][1:], [])

    def test_a_missing_browser_is_a_clean_error(self):
        browsers.choose = lambda pref="auto", **kw: None
        self.err("POST", "/api/accounts/add", {"browser": "chrome"}, 422, "no_browser")

    def test_organise_accounts(self):
        for i, u in enumerate(("a.a", "b.b", "c.c"), start=1):
            self.seed_account(f"real-account-{i}", u, session="ok")
        self.assertEqual(self.ok("PATCH", "/api/accounts/real-account-2", {"label": "Second", "enabled": False})["enabled"], False)
        self.assertNotIn("real-account-2", config.account_chain())
        self.err("PATCH", "/api/accounts/real-account-2", {}, 400, "bad_request")
        self.err("PATCH", "/api/accounts/real-account-2", {"label": ""}, 400, "bad_request")
        self.assertEqual(self.ok("PUT", "/api/accounts/order", {"names": ["real-account-3", "real-account-1"]})["order"],
                         ["real-account-3", "real-account-1", "real-account-2"])
        self.err("PUT", "/api/accounts/order", {"names": "nope"}, 400, "bad_request")
        self.assertEqual(self.ok("PUT", "/api/accounts/first", {"name": "real-account-1"})["first"], "real-account-1")
        self.assertEqual(config.account_chain()[0], "real-account-1")
        self.ok("PUT", "/api/accounts/first", {"name": None})
        removed = self.ok("DELETE", "/api/accounts/real-account-3")
        self.assertEqual((removed["removed"], removed["folder_deleted"]), (True, True))
        self.err("DELETE", "/api/accounts/real-account-3", None, 404, "not_found")

    def test_the_page_account_is_protected(self):
        self.err("DELETE", "/api/accounts/master-acct", None, 409, "master")
        self.err("PATCH", "/api/accounts/master-acct", {"enabled": False}, 409, "master")
        self.err("POST", "/api/accounts/master-acct/relogin", {}, 409, "master")

    def test_relogin_refreshes_the_account_in_place(self):
        self.seed_account("real-account-1", "shop.one", session="expired")
        self.window_kwargs = dict(who=who("shop.one"))
        self.ok("POST", "/api/accounts/real-account-1/relogin", {}, status=202)
        self.assertEqual(self.wait_flow()["outcome"], "refreshed")
        self.assertEqual(self.ok("GET", "/api/accounts/real-account-1")["session"], "ok")

    def test_check_one_and_check_all(self):
        self.seed_account("real-account-1", "a.a", session="ok")
        with mock.patch.object(acc, "identify", return_value={"ok": True, "logged_in": False, "who": None}):
            r = self.ok("POST", "/api/accounts/real-account-1/check")
        self.assertEqual((r["ok"], r["session"]), (True, "expired"))
        self.assertEqual(self.ok("GET", "/api/accounts/real-account-1")["session"], "expired")
        pathlib.Path(config.BFP_MASTER_USER_DATA_DIR).mkdir(parents=True)
        with mock.patch.object(acc, "identify", return_value={"ok": True, "who": who("a.a")}):
            self.assertTrue(self.ok("POST", "/api/accounts/check-all", status=202)["started"])
            end = time.monotonic() + 5
            while time.monotonic() < end and len([e for e in self.svc.events.since(0) if e["type"] == "account.checked"]) < 3:
                time.sleep(0.02)
        self.assertGreaterEqual(len([e for e in self.svc.events.since(0) if e["type"] == "account.checked"]), 3)

    def test_a_session_found_expired_while_posting_reaches_the_app_as_an_event(self):
        self.seed_account("real-account-1", "a.a", session="ok")
        head = self.svc.events.last_id()
        acc.record_session("real-account-1", "expired")
        got = [e for e in self.svc.events.since(head) if e["type"] == "account.session"]
        self.assertEqual((got[0]["data"], got[0]["level"]), ({"name": "real-account-1", "session": "expired"}, "warn"))
        self.assertEqual(self.ok("GET", "/api/accounts/real-account-1")["session"], "expired")


class TestWhatIsGoingOnAndWhetherItIsReady(ApiCase):
    def posting_card(self, **extra):
        self.db.create_approval({"id": "p1", "cid": "c-p1", "video_id": "v1", "video_url": "https://www.tiktok.com/@x/video/v1",
                                 "username": "ana", "comment_text": "preco?", "reply": "Obrigado!", "status": "posting",
                                 "kind": "link_reply", **extra})
        return "p1"

    def test_an_idle_app_reports_nothing_in_flight(self):
        self.assertEqual(self.ok("GET", "/api/activity"), {"busy": False, "sending": [], "reading": None, "checking": []})

    def test_a_reply_on_its_way_is_visible_with_the_browsers_current_step(self):
        aid = self.posting_card()
        self.svc._set_stage(aid, "Finding the comment...")
        got = self.ok("GET", "/api/activity")
        self.assertTrue(got["busy"])
        self.assertEqual((got["sending"][0]["id"], got["sending"][0]["text"], got["sending"][0]["username"]),
                         (aid, "Finding the comment...", "ana"))
        card = self.ok("GET", f"/api/approvals/{aid}")
        self.assertEqual(card["progress"]["text"], "Finding the comment...")
        self.assertIn("seconds", card["progress"])

    def test_a_card_that_is_not_being_sent_has_no_progress(self):
        aid = self.posting_card()
        self.db.update_approval(aid, status="pending")
        self.assertNotIn("progress", self.ok("GET", f"/api/approvals/{aid}"))

    def test_a_card_left_posting_by_a_stopped_app_is_marked_interrupted_not_spinning(self):
        aid = self.posting_card()
        self.svc.controller.recover_interrupted()
        card = self.ok("GET", f"/api/approvals/{aid}")
        self.assertEqual(card["status"], "post_failed")
        self.assertTrue(card["interrupted"])
        self.assertIn("not known whether it reached TikTok", card["error"])

    def test_health_names_what_is_missing_and_what_to_do(self):
        none = {"total": 0, "usable": 0, "ready": 0, "need_login": 0, "unchecked": 0}
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""), \
                mock.patch.object(self.svc.controller, "_accounts_summary", return_value=none):
            got = self.ok("GET", "/api/health")
        by_id = {i["id"]: i for i in got["items"]}
        self.assertEqual(by_id["ai_key"]["status"], "warn")
        self.assertIn("OpenRouter", by_id["ai_key"]["fix"])
        self.assertEqual(by_id["accounts"]["status"], "fail", "no account registered in this fixture")
        self.assertEqual(got["status"], "fail")
        self.assertNotIn("internet", by_id, "the slow network check only runs when asked for")

    def test_the_network_check_runs_only_when_asked_for(self):
        with mock.patch("app.health._internet", return_value={"id": "internet", "title": "Internet / TikTok", "status": "warn",
                                                              "detail": "tiktok.com cannot be reached right now.", "fix": "x"}) as net:
            self.ok("GET", "/api/health")
            net.assert_not_called()
            got = self.ok("GET", "/api/health?net=1")
        net.assert_called_once()
        self.assertIn("internet", [i["id"] for i in got["items"]])

    def test_diagnostics_gather_what_a_helper_needs_and_hide_secrets(self):
        self.db.log_event("post_interrupted", "a1: the app stopped while it was being sent", "warn")
        self.db.log_event("maintenance", "freed 0 MB", "info")
        none = {"total": 0, "usable": 0, "ready": 0, "need_login": 0, "unchecked": 0}
        with mock.patch("app.logs.tail", return_value=["2026-10-15 INFO x: a line of the log"]), \
                mock.patch.object(self.svc.controller, "_accounts_summary", return_value=none), \
                mock.patch("app.health._internet", return_value={"id": "internet", "title": "t", "status": "ok", "detail": "", "fix": ""}):
            got = self.ok("GET", "/api/diagnostics")
        self.assertEqual(set(got), {"app", "health", "problems", "log", "backups"})
        self.assertIn("python", got["app"])
        self.assertEqual([p["kind"] for p in got["problems"]], ["post_interrupted"], "only warnings and errors are listed")
        self.assertEqual(got["health"]["status"], "fail")

    def test_these_endpoints_need_the_token_like_everything_else(self):
        for path in ("/api/activity", "/api/health", "/api/diagnostics"):
            self.assertEqual(self.call("GET", path, token=None)[0], 401, path)


class TestTheRunningService(ApiCase):
    start_service = True

    def test_browser_work_is_handed_to_the_services_one_browser_thread(self):
        names = []
        self.assertEqual(self.svc.call_in_worker(lambda: (names.append(threading.current_thread().name), 42)[1]), 42)
        self.assertEqual(names, ["worker"])
        with self.assertRaises(ZeroDivisionError):
            self.svc.call_in_worker(lambda: 1 / 0)

    def test_a_slow_session_check_never_holds_up_the_services_browser_thread(self):
        """A check can take a minute when TikTok is slow; scans and posts (the worker's job) must not wait behind it."""
        self.seed_account("real-account-1", "a.a")
        started, release, result = threading.Event(), threading.Event(), {}

        def slow(udd, browser=None):
            started.set()
            release.wait(10)
            return {"ok": True, "logged_in": True, "who": who("a.a")}

        def request():
            with mock.patch.object(acc, "identify", slow):
                result["r"] = self.call("POST", "/api/accounts/real-account-1/check")
        t = threading.Thread(target=request)
        t.start()
        try:
            self.assertTrue(started.wait(5), "the check is running")
            t0 = time.monotonic()
            self.assertEqual(self.svc.call_in_worker(lambda: "free", timeout=3), "free")
            self.assertLess(time.monotonic() - t0, 1.5, "the worker answered at once while the check was still going")
        finally:
            release.set()
            t.join(10)
        self.assertEqual((result["r"][0], result["r"][1]["session"]), (200, "ok"))

    def test_removing_an_account_goes_through_the_worker_too(self):
        self.seed_account("real-account-1", "a.a")
        seen = []
        real = acc.remove_profile_folder
        with mock.patch.object(acc, "remove_profile_folder", lambda p, attempts=6: (seen.append(threading.current_thread().name), real(p, attempts))[1]):
            self.ok("DELETE", "/api/accounts/real-account-1")
        self.assertEqual(seen, ["worker"])

    def test_an_actual_post_travels_the_whole_way_and_the_app_hears_about_it(self):
        a = self.make_alert()
        head = self.svc.events.last_id()
        self.ok("POST", f"/api/approvals/{a['id']}/post")
        end = time.monotonic() + 8
        while time.monotonic() < end and self.ok("GET", f"/api/approvals/{a['id']}")["status"] != "posted":
            time.sleep(0.05)
        v = self.ok("GET", f"/api/approvals/{a['id']}")
        self.assertEqual((v["status"], v["posted_by"]), ("posted", "master-acct"))
        statuses = [e["data"].get("status") for e in self.svc.events.since(head) if e["type"] == "approval.updated"]
        self.assertEqual([s for s in statuses if s][:2], ["posting", "posted"])


class TestServiceWithoutTelegram(unittest.TestCase):
    def setUp(self):
        self._saved = (config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.API_ENABLED, config.API_PORT,
                       config.API_TOKEN, config.API_TOKEN_FILE)
        config.TELEGRAM_BOT_TOKEN = config.TELEGRAM_CHAT_ID = config.API_TOKEN = ""
        config.API_TOKEN_FILE = pathlib.Path(tempfile.mkdtemp(prefix="tok-")) / "api_token.txt"      # never the real data/ folder
        self.db = tmp_db()

    def tearDown(self):
        (config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.API_ENABLED, config.API_PORT,
         config.API_TOKEN, config.API_TOKEN_FILE) = self._saved

    def service(self, **kw):
        svc = Service(self.db, ai=make_ai(self.db, [{"content": "ok"}]), scanner=lambda db: {"opened": 0, "new": 0}, **kw)
        svc._memory_check = lambda: (True, 8000.0)
        return svc

    def test_without_a_bot_token_the_service_runs_for_the_desktop_app_alone(self):
        svc = self.service()
        self.assertFalse(svc.telegram_enabled)
        svc.start()
        try:
            names = {t.name for t in svc._threads}
            self.assertEqual(names, {"worker"}, "no Telegram polling thread")
        finally:
            svc.stop()

    def test_notices_still_reach_the_app_as_events_and_nothing_is_sent_to_telegram(self):
        svc = self.service()
        sent = []
        svc.tg = mock.Mock(send_message=lambda *a, **k: sent.append(a))
        svc.notify("captcha", "⚠️ TikTok is showing a <b>security check</b> &amp; waiting", "warn")
        self.assertEqual(sent, [])
        e = svc.events.since(0)[-1]
        self.assertEqual((e["type"], e["level"], e["data"]), ("notice", "warn", {"key": "captcha",
                                                                                "text": "⚠️ TikTok is showing a security check & waiting"}))
        svc.stop()

    def test_with_telegram_on_the_same_notice_goes_to_both(self):
        config.TELEGRAM_CHAT_ID = "4242"
        tg = FakeTG()
        svc = self.service(tg=tg)
        self.assertTrue(svc.telegram_enabled)
        svc.notify("scan_done", "✅ Scan finished")
        self.assertEqual(tg.last_text(), "✅ Scan finished")
        self.assertEqual(svc.events.since(0)[-1]["data"]["key"], "scan_done")
        svc.stop()

    def test_a_scan_without_telegram_still_alerts_the_app(self):
        svc = self.service()
        svc._scan_fn = lambda db: (_ for _ in ()).throw(RuntimeError("TikTok said 403"))
        svc.do_scan()
        kinds = [(e["type"], e["level"]) for e in svc.events.since(0)]
        self.assertIn(("scan.started", "info"), kinds)
        self.assertIn(("scan.finished", "error"), kinds)
        notice = [e for e in svc.events.since(0) if e["type"] == "notice" and e["data"]["key"] == "scan_error"]
        self.assertIn("TikTok said 403", notice[0]["data"]["text"])
        svc.stop()

    def test_the_api_starts_with_the_service_when_asked_and_stops_with_it(self):
        config.API_PORT = 0
        svc = self.service(api=True)
        svc.start()
        try:
            self.assertIsNotNone(svc.api_server)
            conn = http.client.HTTPConnection("127.0.0.1", svc.api_server.port, timeout=5)
            conn.request("GET", "/api/ping")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            port = svc.api_server.port
        finally:
            svc.stop()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_a_taken_port_is_logged_not_fatal(self):
        blocker = api.ApiServer(mock.Mock(), mock.Mock(), mock.Mock(), host="127.0.0.1", port=0, token=TOKEN).start()
        try:
            config.API_PORT = blocker.port
            svc = self.service(api=True)
            svc.start()
            self.assertIsNone(svc.api_server)
            self.assertTrue([e for e in self.db.recent_events(10) if e["kind"] == "api_failed"])
            svc.stop()
        finally:
            blocker.stop()

    def test_the_api_is_off_unless_asked_for(self):
        config.API_ENABLED = False
        svc = self.service()
        svc.start()
        self.assertIsNone(svc.api_server)
        svc.stop()


if __name__ == "__main__":
    unittest.main()
