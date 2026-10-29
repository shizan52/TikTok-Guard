"""The desktop app's local API: JSON over HTTP on THIS machine, plus a live event stream.

    python run.py serve --api          (or API_ENABLED=true in .env)     ->  http://127.0.0.1:8765/
    python run.py api-token            prints the token every request must carry

Security, because this port controls TikTok accounts:
  * it listens on 127.0.0.1 only, and on Windows no other program can take the port over (SO_EXCLUSIVEADDRUSE);
  * every call except /api/ping and the console page needs the token: `Authorization: Bearer <token>`
    (GET requests may use `?token=` instead - the browser's EventSource cannot set headers);
  * the Host header must be this machine (blocks DNS-rebinding from a web page) and a browser Origin must be local, the
    desktop shell's own scheme, or listed in API_ALLOWED_ORIGINS - a random website can never call it.

Responses are JSON. Errors always look like {"error": {"code": "...", "message": "...", ...extra}}.
The full list of endpoints is in docs/API.md.
"""
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from app import account_manager as am
from app.controller import ControlError

API_VERSION = 1
MAX_BODY = 1_000_000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOCAL_ORIGIN = re.compile(r"^(https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?|null|tauri://localhost|"
                           r"https?://tauri\.localhost|app://[\w.-]*|file://)$", re.IGNORECASE)
_SCREENSHOT = re.compile(r"^[A-Za-z0-9._-]+\.png$")
ACCOUNT_HTTP = {"not_found": 404, "busy": 409, "master": 409, "no_browser": 422, "low_memory": 503,
                "bad_request": 400, "no_profile": 409}
WEB = Path(__file__).resolve().parent / "web"
PAGES = {"/": "desktop.html", "/app": "desktop.html", "/console": "console.html", "/console.html": "console.html"}


def load_token() -> str:
    """The API token: API_TOKEN from .env, else the random one kept in data/api_token.txt (created on first use)."""
    if config.API_TOKEN:
        return config.API_TOKEN
    path = Path(config.API_TOKEN_FILE)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 20:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **data) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.data = status, code, message, data


class Req:
    def __init__(self, method: str, path: str, query: dict, body: dict, params: tuple, headers) -> None:
        self.method, self.path, self.query, self.body, self.params, self.headers = method, path, query, body, params, headers

    def q(self, key: str, default=None):
        vals = self.query.get(key)
        return vals[0] if vals else default

    def need(self, key: str):
        if key not in self.body:
            raise ApiError(400, "bad_request", f"'{key}' is required.")
        return self.body[key]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False                       # SO_REUSEADDR on Windows would let another program share the port

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        """A browser that closes (or is killed) drops its keep-alive connections mid-read: normal, and no reason to print a
        traceback in the person's console. Anything else is still reported."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Routes:
    """Every endpoint. Each method takes a Req and returns (http status, JSON-able) - or raises ControlError /
    AccountError / ApiError."""

    def __init__(self, api: "ApiServer") -> None:
        self.api, self.ctl, self.events, self.accounts = api, api.controller, api.events, api.accounts

    # -- discovery / status / live events
    def ping(self, r):
        return 200, {"ok": True, "app": "tiktok-guard", "api": API_VERSION}

    def status(self, r):
        out = self.ctl.status()
        out["api"] = {"version": API_VERSION, "host": self.api.host, "port": self.api.port}
        return 200, out

    def events_poll(self, r):
        """Long-poll. Without `since` it only tells where the stream is now (start from here); with `since=N` it returns
        what happened after N, waiting up to `timeout` seconds (max 30) when nothing has yet."""
        if r.q("since") is None:
            return 200, {"events": [], "last_id": self.events.last_id()}
        since = int(r.q("since") or 0)
        timeout = max(0.0, min(float(r.q("timeout", 0) or 0), 30.0))
        found = self.events.wait(since, timeout) if timeout else self.events.since(since)
        return 200, {"events": found, "last_id": found[-1]["id"] if found else since}

    # -- approvals (alert cards and link comments)
    def approvals(self, r):
        statuses = tuple(s for s in (r.q("status", "pending,post_failed") or "").split(",") if s)
        return 200, {"approvals": self.ctl.list_approvals(statuses or ("pending", "post_failed"), kind=r.q("kind"),
                                                          limit=int(r.q("limit", 50)))}

    def approval(self, r):
        return 200, self.ctl.approval(r.params[0])

    def approval_reply(self, r):
        return 200, self.ctl.edit_reply(r.params[0], str(r.need("text")))

    def approval_regenerate(self, r):
        return 200, self.ctl.regenerate(r.params[0], r.body.get("brief"))

    def approval_verify(self, r):
        return 202, self.ctl.verify(r.params[0])

    def approval_post(self, r):
        return 200, self.ctl.request_post(r.params[0])

    def approval_ignore(self, r):
        return 200, self.ctl.ignore(r.params[0])

    def approval_account(self, r):
        return 200, self.ctl.set_account(r.params[0], r.body.get("account") or None)

    # -- comment on a link
    def link_preview(self, r):
        return 200, self.ctl.preview_link(str(r.need("link")))

    def link_comment(self, r):
        return 201, self.ctl.offer_link(str(r.need("link")), str(r.body.get("text") or ""), account=r.body.get("account"))

    # -- the desktop app: analyse a post, answer its comments
    def dashboard(self, r):
        return 200, self.ctl.dashboard()

    def activity(self, r):
        return 200, self.ctl.activity()

    def health(self, r):
        return 200, self.ctl.health(internet=bool(r.q("net")))

    def diagnostics(self, r):
        return 200, self.ctl.diagnostics()

    def analyze_start(self, r):
        return 202, self.ctl.analyze(str(r.need("url")), r.body.get("account"))

    def analyze_current(self, r):
        return 200, {"job": self.ctl.analysis_current()}

    def analyze_job(self, r):
        return 200, self.ctl.analysis_job(r.params[0])

    def analyze_cancel(self, r):
        return 200, self.ctl.analysis_cancel(r.params[0])

    def analysis_result(self, r):
        return 200, self.ctl.analysis_result(r.params[0])

    def recent_posts(self, r):
        return 200, {"posts": self.ctl.recent_posts(int(r.q("limit", 8)))}

    def comment_reply(self, r):
        view = self.ctl.reply_to_comment(r.params[0], r.params[1], r.body.get("account"))
        return (200 if view.get("existing") else 201), view

    # -- posts / settings / maintenance
    def posts(self, r):
        return 200, {"posts": self.ctl.posts()}

    def settings(self, r):
        return 200, self.ctl.settings()

    def settings_update(self, r):
        return 200, self.ctl.update_settings(r.body)

    def settings_reset(self, r):
        return 200, self.ctl.reset_setting(r.params[0].replace("-", "_"))

    def settings_undo(self, r):
        return 200, self.ctl.undo_setting(r.params[0].replace("-", "_"))

    def ai_test(self, r):
        return 200, self.ctl.test_ai(str(r.need("text")))

    def backfill(self, r):
        return 200, self.ctl.backfill(r.need("days"))

    def cleanup(self, r):
        if r.q("wait"):                                   # ?wait=1: do it now and return the numbers
            return 200, self.ctl.cleanup_now()
        return 202, self.ctl.start_cleanup()

    # -- accounts
    def accounts_list(self, r):
        return 200, self.accounts.list()

    def account_get(self, r):
        rows = {a["name"]: a for a in self.accounts.list()["accounts"]}
        if r.params[0] not in rows:
            raise am.AccountError("not_found", f"There is no account called '{r.params[0]}'.")
        return 200, rows[r.params[0]]

    def browsers(self, r):
        from app import browsers
        return 200, {"browsers": browsers.find_browsers(), "preference": config.BROWSER_PREFERENCE}

    def add_start(self, r):
        return 202, self.accounts.start_add(browser=r.body.get("browser"), replace=r.body.get("replace"),
                                            label=r.body.get("label"))

    def add_status(self, r):
        return 200, {"flow": self.accounts.current()}

    def add_cancel(self, r):
        return 200, {"flow": self.accounts.cancel()}

    def relogin(self, r):
        return 202, self.accounts.start_add(browser=r.body.get("browser"), replace=r.params[0])

    def account_patch(self, r):
        name, out = r.params[0], None
        if "label" in r.body:
            out = self.accounts.set_label(name, str(r.body["label"]))
        if "enabled" in r.body:
            out = self.accounts.set_enabled(name, bool(r.body["enabled"]))
        if out is None:
            raise ApiError(400, "bad_request", "Send 'label' and/or 'enabled'.")
        return 200, out

    def account_delete(self, r):
        return 200, self.accounts.remove(r.params[0])

    def account_check(self, r):
        return 200, self.accounts.check(r.params[0])

    def accounts_check_all(self, r):
        return 202, self.accounts.check_all()

    def accounts_order(self, r):
        names = r.need("names")
        if not isinstance(names, list):
            raise ApiError(400, "bad_request", "'names' must be a list.")
        return 200, self.accounts.reorder([str(n) for n in names])

    def accounts_first(self, r):
        return 200, self.accounts.set_first(r.body.get("name") or None)


# (method, path pattern, Routes method) - literal paths come before the generic /accounts/{name} ones
ROUTES = [
    ("GET", r"/api/ping", "ping"), ("GET", r"/api/status", "status"), ("GET", r"/api/events", "events_poll"),
    ("GET", r"/api/approvals", "approvals"), ("GET", r"/api/approvals/([\w-]+)", "approval"),
    ("POST", r"/api/approvals/([\w-]+)/reply", "approval_reply"),
    ("POST", r"/api/approvals/([\w-]+)/regenerate", "approval_regenerate"),
    ("POST", r"/api/approvals/([\w-]+)/post", "approval_post"),
    ("POST", r"/api/approvals/([\w-]+)/verify", "approval_verify"),
    ("POST", r"/api/approvals/([\w-]+)/ignore", "approval_ignore"),
    ("POST", r"/api/approvals/([\w-]+)/account", "approval_account"),
    ("POST", r"/api/links/preview", "link_preview"), ("POST", r"/api/comments/link", "link_comment"),
    ("GET", r"/api/posts", "posts"), ("GET", r"/api/posts/recent", "recent_posts"),
    ("GET", r"/api/dashboard", "dashboard"), ("GET", r"/api/activity", "activity"),
    ("GET", r"/api/health", "health"), ("GET", r"/api/diagnostics", "diagnostics"),
    ("POST", r"/api/analyze", "analyze_start"), ("GET", r"/api/analyze", "analyze_current"),
    ("GET", r"/api/analyze/([\w-]+)", "analyze_job"), ("POST", r"/api/analyze/([\w-]+)/cancel", "analyze_cancel"),
    ("GET", r"/api/analysis/(\d+)", "analysis_result"),
    ("POST", r"/api/comments/(\d+)/(\d+)/reply", "comment_reply"),
    ("GET", r"/api/settings", "settings"), ("PUT", r"/api/settings", "settings_update"),
    ("POST", r"/api/settings/(ai-instructions|company-context)/reset", "settings_reset"),
    ("POST", r"/api/settings/(ai-instructions|company-context)/undo", "settings_undo"),
    ("POST", r"/api/ai/test", "ai_test"), ("POST", r"/api/backfill", "backfill"),
    ("POST", r"/api/maintenance/cleanup", "cleanup"),
    ("GET", r"/api/accounts", "accounts_list"), ("GET", r"/api/browsers", "browsers"),
    ("POST", r"/api/accounts/add", "add_start"), ("GET", r"/api/accounts/add", "add_status"),
    ("POST", r"/api/accounts/add/cancel", "add_cancel"),
    ("POST", r"/api/accounts/check-all", "accounts_check_all"),
    ("PUT", r"/api/accounts/order", "accounts_order"), ("PUT", r"/api/accounts/first", "accounts_first"),
    ("GET", r"/api/accounts/([\w.@-]+)", "account_get"), ("PATCH", r"/api/accounts/([\w.@-]+)", "account_patch"),
    ("DELETE", r"/api/accounts/([\w.@-]+)", "account_delete"),
    ("POST", r"/api/accounts/([\w.@-]+)/check", "account_check"),
    ("POST", r"/api/accounts/([\w.@-]+)/relogin", "relogin"),
]
_COMPILED = [(m, re.compile(f"^{p}$"), name) for m, p, name in ROUTES]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TikTokGuardAPI/1"

    def log_message(self, *args) -> None:             # no per-request console noise
        pass

    @property
    def api(self) -> "ApiServer":
        return self.server.api

    # -- the checks every request passes
    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _origin_ok(self) -> bool:
        origin = self._origin()
        return origin is None or bool(_LOCAL_ORIGIN.match(origin)) or origin in config.API_ALLOWED_ORIGINS

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if ":" in host else host
        return name in LOCAL_HOSTS or name == self.api.host.lower()

    def _token_ok(self, query: dict, method: str) -> bool:
        auth = self.headers.get("Authorization") or ""
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not given and method == "GET":              # EventSource / <img> cannot send headers
            given = (query.get("token") or [""])[0]
        return bool(given) and hmac.compare_digest(given.encode(), self.api.token.encode())

    # -- output
    def _cors(self) -> None:
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Last-Event-ID")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", **(extra or {})}
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, http_status: int, code: str, message: str, data: dict | None = None) -> None:
        """`data` (e.g. existing_id, limit) is kept apart from the fixed fields, so a key of its own can never collide."""
        extra = {k: v for k, v in (data or {}).items() if k not in ("code", "message")}
        self._json(http_status, {"error": {"code": code, "message": message, **extra}})

    # -- dispatch
    def do_OPTIONS(self) -> None:                      # CORS preflight: no token (browsers never send one), no data
        if not (self._host_ok() and self._origin_ok()):
            return self._error(403, "forbidden", "This origin is not allowed.")
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _slurp(self, method: str) -> None:
        """Read the request body FIRST, whatever the verdict will be. Answering (401, 404 ...) while the client's body is
        still unread and then closing makes the OS reset the connection - the client would never see the answer."""
        self._raw, self._too_large = b"", False
        try:
            length = int(self.headers.get("Content-Length") or 0) if method in ("POST", "PUT", "PATCH") else 0
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self._raw = self.rfile.read(length)
        elif length > MAX_BODY:                        # too big to keep: read and drop (a sane amount), then say 413
            self._too_large = True
            left = min(length, 8_000_000)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            if length > 8_000_000:
                self.close_connection = True

    def _body(self) -> dict:
        if self._too_large:
            raise ApiError(413, "too_large", "The request is too large.")
        if not self._raw:
            return {}
        try:
            data = json.loads(self._raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_request", "The body must be valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_request", "The body must be a JSON object.")
        return data

    def _dispatch(self, method: str) -> None:
        self._slurp(method)
        try:
            self._route(method)
        except Exception as exc:  # noqa: BLE001 - even a failure while reporting a failure must still answer the caller
            self.api.log_error(f"{method} {self.path}: {type(exc).__name__}: {exc}")
            try:
                self._json(500, {"error": {"code": "internal_error", "message": "Something went wrong on our side."}})
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _route(self, method: str) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path, query = urllib.parse.unquote(parts.path), urllib.parse.parse_qs(parts.query)
        try:
            if not self._host_ok():
                return self._error(403, "forbidden", "This API only answers on the local machine.")
            if not self._origin_ok():
                return self._error(403, "forbidden", "This origin is not allowed.")
            if method == "GET" and path in PAGES:
                return self._page(PAGES[path])
            if not (method == "GET" and path == "/api/ping") and not self._token_ok(query, method):
                return self._error(401, "unauthorized", "A valid token is required (Authorization: Bearer <token>).")
            if method == "GET" and path == "/api/events/stream":
                return self._stream(query)
            if method == "GET" and path.startswith("/api/screenshots/"):
                return self._screenshot(path[len("/api/screenshots/"):])
            known = False
            for m, pattern, name in _COMPILED:
                hit = pattern.match(path)
                if not hit:
                    continue
                known = True
                if m != method:
                    continue
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                status, payload = getattr(self.api.routes, name)(Req(method, path, query, body, hit.groups(), self.headers))
                return self._json(status, payload)
            if known:
                return self._error(405, "method_not_allowed", f"{method} is not supported here.")
            return self._error(404, "not_found", "There is no such endpoint.")
        except ControlError as err:
            self._error(err.http_status, err.code, err.message, err.data)
        except am.AccountError as err:
            self._error(ACCOUNT_HTTP.get(err.code, 400), err.code, err.message)
        except ApiError as err:
            self._error(err.status, err.code, err.message, err.data)
        except TimeoutError as err:
            self._error(503, "busy", str(err))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - a bug in one request must not take the API down
            self.api.log_error(f"{self.command} {path}: {type(exc).__name__}: {exc}")
            self._error(500, "internal_error", "Something went wrong on our side.")

    # -- the console page, screenshots, live stream
    def _page(self, name: str) -> None:
        try:
            html = (WEB / name).read_bytes()
        except OSError:
            return self._error(404, "not_found", "This page is not installed.")
        self._send(200, html, "text/html; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:",
                    "Referrer-Policy": "no-referrer"})

    def _screenshot(self, name: str) -> None:
        root = Path(config.SCREENSHOTS_DIR).resolve()
        target = (root / name).resolve() if _SCREENSHOT.match(name) else None
        if target is None or target.parent != root or not target.is_file():
            return self._error(404, "not_found", "There is no such screenshot.")
        self._send(200, target.read_bytes(), "image/png", {"Cache-Control": "private, max-age=60"})

    def _stream(self, query: dict) -> None:
        """Server-Sent Events. Without Last-Event-ID / ?since= it starts from NOW; with one it replays what was missed."""
        raw = self.headers.get("Last-Event-ID") or (query.get("since") or [None])[0]
        last = int(raw) if raw and str(raw).isdigit() else self.api.events.last_id()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        self.close_connection = True
        ping_at = time.monotonic() + 15
        try:
            self.wfile.write(b"retry: 3000\n: connected\n\n")
            self.wfile.flush()
            while not self.api.stopping.is_set():
                batch = self.api.events.wait(last, timeout=1.0)
                for e in batch:
                    last = e["id"]
                    self.wfile.write(f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
                                     .encode("utf-8"))
                if batch:
                    self.wfile.flush()
                    ping_at = time.monotonic() + 15
                elif time.monotonic() >= ping_at:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    ping_at = time.monotonic() + 15
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                                       # the app closed its window: nothing to clean up


class ApiServer:
    def __init__(self, controller, events, accounts, service=None, *, host: str | None = None, port: int | None = None,
                 token: str | None = None) -> None:
        self.controller, self.events, self.accounts, self.service = controller, events, accounts, service
        self.host = host or config.API_HOST
        self.port = config.API_PORT if port is None else port
        self.token = token or load_token()
        self.stopping = threading.Event()
        self.routes = Routes(self)
        self.httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self.errors: list = []

    def log_error(self, text: str) -> None:
        self.errors = (self.errors + [text])[-20:]
        try:
            if self.service is not None:
                self.service.db.log_event("api_error", text, "error")
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> "ApiServer":
        self.httpd = _Server((self.host, self.port), Handler)
        self.httpd.api = self
        self.port = self.httpd.server_address[1]       # port 0 = "any free port" (tests)
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="api", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        self.events.wake_all()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"
