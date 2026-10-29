import os
import sys
import threading
from pathlib import Path

from app.env_loader import load_env

# Packaged as an exe (PyInstaller): the program's own files live in a read-only bundle (RESOURCE_DIR) and everything the person
# creates - settings, database, accounts, screenshots, logs - lives in a folder of their own: %LOCALAPPDATA%\TikTok Guard
# (TIKTOK_GUARD_HOME overrides it). Run from source, both are the project folder, as always.
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    ROOT = Path(os.environ.get("TIKTOK_GUARD_HOME")
                or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "TikTok Guard"))
    ROOT.mkdir(parents=True, exist_ok=True)
else:
    ROOT = RESOURCE_DIR = Path(__file__).resolve().parent
load_env(ROOT / ".env")                                          # the person's own settings come first and win ...
if FROZEN:
    load_env(RESOURCE_DIR / "client_defaults.env")               # ... then what this build ships with (no secrets in it)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BRAND_NAME = os.environ.get("BRAND_NAME", "Marca (demo)")
TARGET_PROFILE_URL = os.environ.get("TARGET_PROFILE_URL", "https://www.tiktok.com/@examplebrand")
TARGET_VIDEO_URL = os.environ.get("TARGET_VIDEO_URL", "")

# The page is read every POLL_INTERVAL_SECONDS (start to start: a fixed 120 s rhythm, not 120 s + however long the
# last scan took). Reading is cheap - the post list comes from one profile load and a post is only opened when its
# comment count changed - so a quiet round is ~12 s of work.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
# Optional: let quiet periods stretch the interval step by step (x1.5 per idle scan) up to this ceiling. The default
# equals the interval itself = no stretching = the page is read every 120 s, always. Any change/alert/manual /scan
# resets a stretched interval.
POLL_INTERVAL_MAX_SECONDS = int(os.environ.get("POLL_INTERVAL_MAX_SECONDS", str(POLL_INTERVAL_SECONDS)))
# A scan that overran its slot is followed by this short breather before the next one starts (never back to back).
SCAN_MIN_GAP_SECONDS = int(os.environ.get("SCAN_MIN_GAP_SECONDS", "10"))
# After TikTok shows its security check (captcha) the bot stops OPENING posts for 5 min, then 10, 20, 40, up to 60 min
# while it keeps happening (the cheap profile read goes on). The first clean post open ends the episode.
CAPTCHA_COOLDOWN_MIN_MINUTES = int(os.environ.get("CAPTCHA_COOLDOWN_MIN_MINUTES", "5"))
CAPTCHA_COOLDOWN_MAX_MINUTES = int(os.environ.get("CAPTCHA_COOLDOWN_MAX_MINUTES", "60"))
# Shared-server guard: no browser is launched while less than this much memory is available (a scan then waits for
# the next 120 s slot; a reply the operator approved waits up to 90 s). 0 switches the guard off.
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "900"))
MAX_COMMENTS_PER_POLL = int(os.environ.get("MAX_COMMENTS_PER_POLL", "20"))
BFP_HEADLESS = os.environ.get("BFP_HEADLESS", "false").strip().lower() in ("1", "true", "yes")

# Real posting (approve -> bot actually replies on TikTok, using a logged-in
# session saved by `python run.py login`). The demo profile has no login, so
# it fails closed on its own, but this is the explicit production gate.
POSTING_ENABLED = os.environ.get("POSTING_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# Minimum gap between two reply-post attempts, in ANY process (jittered 0.8-1.4x
# at use). Every post loads the same video URL, and TikTok answers a burst of
# loads of one URL with an empty HTTP 403 for ~minutes (confirmed live,
# 2026-10-07: 3 loads in 16s were fine, the 4th and every one after it failed
# until the URL had rested ~3 min) - and several replies landing within seconds
# of each other is not how a person replies anyway.
POST_MIN_GAP_SECONDS = int(os.environ.get("POST_MIN_GAP_SECONDS", "60"))

# -- paths ------------------------------------------------------------------
NETWORK_DIR = ROOT / "network"
DATA_DIR = ROOT / "data"
DEMO_OUTPUT_DIR = ROOT / ("output" if FROZEN else "demo_output")
SCREENSHOTS_DIR = DEMO_OUTPUT_DIR / "screenshots"
BFP_VAULT_DIR = NETWORK_DIR / "bfp_profiles"
BFP_RUNS_DIR = NETWORK_DIR / "bfp_runs"
SEEN_COMMENTS_FILE = DATA_DIR / "seen_comments.json"
PENDING_APPROVALS_FILE = DATA_DIR / "pending_approvals.json"
TELEGRAM_OFFSET_FILE = DATA_DIR / "telegram_update_offset.json"

for d in (DATA_DIR, DEMO_OUTPUT_DIR, SCREENSHOTS_DIR, BFP_VAULT_DIR, BFP_RUNS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# -- database + retention (see app/db.py, app/maintenance.py) ---------------------
DB_FILE = DATA_DIR / "bot.db"
DB_MAX_MB = _int("DB_MAX_MB", 50)                         # hard size target; retention tightens to stay under it
RETENTION_COMMENT_DAYS = _int("RETENTION_COMMENT_DAYS", 30)       # analysed, harmless comments
RETENTION_NEGATIVE_DAYS = _int("RETENTION_NEGATIVE_DAYS", 180)    # negative/question comments (audit trail)
RETENTION_POST_DAYS = _int("RETENTION_POST_DAYS", 14)             # posts that left the watch set
RETENTION_APPROVAL_DAYS = _int("RETENTION_APPROVAL_DAYS", 180)    # finished approvals
APPROVAL_EXPIRE_DAYS = _int("APPROVAL_EXPIRE_DAYS", 14)           # still-pending approvals go stale
CACHE_MAX_ROWS = _int("CACHE_MAX_ROWS", 20000)
SCREENSHOT_RETENTION_DAYS = _int("SCREENSHOT_RETENTION_DAYS", 7)
SCREENSHOT_MAX_MB = _int("SCREENSHOT_MAX_MB", 100)
RUNS_RETENTION_DAYS = _int("RUNS_RETENTION_DAYS", 3)              # network/bfp_runs/* session folders
BROWSER_CACHE_MAX_MB = _int("BROWSER_CACHE_MAX_MB", 200)          # per saved browser profile (cache dirs only)

# -- what to watch ----------------------------------------------------------------
WATCH_LATEST_POSTS = _int("WATCH_LATEST_POSTS", 5)        # newest non-pinned posts
WATCH_PINNED_POSTS = _int("WATCH_PINNED_POSTS", 2)        # pinned posts at the top of the profile
RECHECK_MAX_HOURS = _int("RECHECK_MAX_HOURS", 12)         # re-read a post even if its comment count is unchanged
MAX_VIDEO_OPENS_PER_SCAN = _int("MAX_VIDEO_OPENS_PER_SCAN", 4)
MAX_COMMENT_PAGES = _int("MAX_COMMENT_PAGES", 12)       # TikTok sends 20 comments per page: 12 pages = 240 comments per post
INITIAL_LOOKBACK_DAYS = _int("INITIAL_LOOKBACK_DAYS", 7)  # first scan of a post: only comments this recent are analysed
# Customer QUESTIONS are announced (with an answer drafted from the company information) just like complaints.
# /questions off|on changes it at runtime; this is only the starting value.
NOTIFY_QUESTIONS_DEFAULT = os.environ.get("NOTIFY_QUESTIONS", "on").strip().lower() not in ("0", "off", "false", "no")
NOTIFY_BURST = _int("NOTIFY_BURST", 5)                    # max new Telegram alerts per scan; the rest wait their turn

# -- AI budget (free OpenRouter tier: a small number of requests per UTC day) ------
AI_DAILY_REQUESTS = _int("AI_DAILY_REQUESTS", 40)         # adjustable at runtime with /budget
AI_BATCH_SIZE = _int("AI_BATCH_SIZE", 12)                 # comments classified per single request
AI_BATCH_SIZE_ON_DEMAND = _int("AI_BATCH_SIZE_ON_DEMAND", 24)   # ... when a person analyses a whole post (fewer, bigger requests)
AI_CONTEXT_CHARS_PER_CALL = _int("AI_CONTEXT_CHARS_PER_CALL", 1200)
AI_CONTEXT_CHARS_FOR_QUESTIONS = _int("AI_CONTEXT_CHARS_FOR_QUESTIONS", 6000)   # answers need the facts: ~1500 tokens
AI_CONTEXT_MAX_CHARS = _int("AI_CONTEXT_MAX_CHARS", 20000)  # size of the stored company context
AI_INSTRUCTIONS_MAX_CHARS = _int("AI_INSTRUCTIONS_MAX_CHARS", 1500)

# -- local API for the desktop app (app/api.py) ---------------------------------------------------------------------
# Bound to this machine only (127.0.0.1) and guarded by a token: the desktop app starts the backend and talks to it here.
API_ENABLED = os.environ.get("API_ENABLED", "false").strip().lower() in ("1", "true", "yes")
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("API_PORT", "8765"))
API_TOKEN = os.environ.get("API_TOKEN", "").strip()                  # default: a random one kept in data/api_token.txt
API_TOKEN_FILE = DATA_DIR / "api_token.txt"
API_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("API_ALLOWED_ORIGINS", "").split(",") if o.strip()]
# the fresh window of "Add account": which browser (auto | chrome | brave | edge), and how long a login may take
BROWSER_PREFERENCE = os.environ.get("BROWSER_PREFERENCE", "auto").strip().lower()
ACCOUNT_LOGIN_TIMEOUT_S = int(os.environ.get("ACCOUNT_LOGIN_TIMEOUT_S", "900"))
ACCOUNT_SETTLE_S = int(os.environ.get("ACCOUNT_SETTLE_S", "8"))      # after the login is seen: let cookies/storage settle

# /comment <link>: how many comments the operator may have the accounts post on OTHER posts per UTC day (0 = no limit).
# Every one is a deliberate, confirmed action, but TikTok's anti-spam reacts to bursts from an account, so the cap
# protects the accounts (and the brand) from a slip such as pasting the same link twenty times.
LINK_COMMENTS_PER_DAY = _int("LINK_COMMENTS_PER_DAY", 10)
BOT_LANG = "en"                                          # the bot speaks English only (fixed on purpose)

# Free OpenRouter models, tried in order (the free pool is shared and models come and go: app/ai.py checks
# the live model list once a day, drops vanished ones and may append a few discovered ones).
# Re-verified 2026-10-07 with raw requests: every free model is a *reasoning* model. With reasoning switched
# off (app/ai.py does that by default) nemotron-3-super answered the classification prompt correctly in
# 2.6 s / 60 tokens and dots-3 in 6.8 s / 53 tokens; with reasoning on, dots-3 spent 629 of 700 tokens
# thinking and returned nothing. liquid/lfm-2.5 cannot disable reasoning, so it is the last resort.
# Override with AI_MODELS=a,b,c in .env.
AI_MODEL_CHAIN = [m.strip() for m in os.environ.get("AI_MODELS", "").split(",") if m.strip()] or [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "liquid/lfm-2.5-2.6b:free",
]

BFP_PROFILE_NAME = os.environ.get("BFP_PROFILE_NAME", "tiktok-monitor")

# The "master" account is the one configured in .env (BFP_USER_DATA_DIR): the server always has it. The desktop app does not need
# one - accounts are added from the app - so there it only exists when .env names its folder (a fresh install has none, and must
# not show a phantom account that nobody can log in to).
MASTER_OPTIONAL = FROZEN


def master_enabled() -> bool:
    return not MASTER_OPTIONAL or bool(BFP_MASTER_USER_DATA_DIR)

# Account fallback chain: try BFP_PROFILE_NAME first, then each of these in
# order, only moving to the next when the current one is actually blocked
# (captcha) rather than on any failure - a transient error on account 1
# should not burn account 2's trust for no reason. Comma-separated, e.g.
# "examplebrand-prod-v4,examplebrand-account2".
# Confirmed live (2026-10-07): a single account can get risk-walled from
# heavy same-day use regardless of which browser/backend posts through it;
# a second, independently-trusted account is a real mitigation, not a
# workaround for a code bug.
# The account that should answer (and read) FIRST. Set it to the page's own account (python run.py add-account, then
# REPLY_ACCOUNT_FIRST=<its name>): a page's own replies are never hidden by its own comment filters, whereas replies from
# other, unproven accounts can be published by TikTok as "visible to the author only".
REPLY_ACCOUNT_FIRST = os.environ.get("REPLY_ACCOUNT_FIRST", "").strip()

BFP_PROFILE_NAMES = [BFP_PROFILE_NAME] + [
    p.strip() for p in os.environ.get("BFP_PROFILE_NAMES_EXTRA", "").split(",") if p.strip()
]

# -- extra real accounts (python run.py add-account; see app/accounts.py) ----
# Each extra account is its OWN saved real-Brave profile folder (cookies,
# localStorage, IndexedDB native, exactly like the master one in .env's
# BFP_USER_DATA_DIR) and is listed in data/accounts.json. post_reply() walks
# account_chain() in order - master first, then the registered accounts - and
# only moves on when the current one is captcha-walled / logged out.
BFP_MASTER_USER_DATA_DIR = os.environ.get("BFP_USER_DATA_DIR", "")
ACCOUNTS_DIR = NETWORK_DIR / "bfp_profiles" / "real"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


REGISTRY_LOCK = threading.RLock()     # readers and writers of data/accounts.json in THIS process never touch it together


def _read_registry() -> dict:
    """data/accounts.json as a dict ({} when there is none). Inside this process the registry lock keeps a reader away from
    a writer's file swap (on Windows a file that is open cannot be replaced, and one being replaced cannot be opened). Another
    PROCESS (the add-account command line) is not covered by it, so a read that fails is still retried before concluding
    anything - otherwise the account list would flicker EMPTY and a scan or post at that instant would see no accounts."""
    import json
    import time
    for attempt in range(6):
        with REGISTRY_LOCK:
            if not ACCOUNTS_FILE.exists():
                return {}
            try:
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                pass
        time.sleep(0.03 * (attempt + 1))                 # outside the lock: do not hold a writer up while waiting
    return {}


def load_accounts() -> list:
    """Registered extra accounts, oldest first: [{name, user_data_dir, username, region, added, enabled}]."""
    return [a for a in _read_registry().get("accounts", []) if a.get("name") and a.get("user_data_dir")]


def registry_value(key: str, default=None):
    """A top-level value of data/accounts.json ('first', 'master_username', 'master_session' ...)."""
    return _read_registry().get(key, default)


def account_chain() -> list:
    """Names to try for a post, in order. Re-read on every call so an account
    added while the monitor is running is picked up without a restart."""
    names = list(BFP_PROFILE_NAMES) if master_enabled() else [n for n in BFP_PROFILE_NAMES if n != BFP_PROFILE_NAME]
    for a in load_accounts():
        if a.get("enabled", True) and a["name"] not in names:
            names.append(a["name"])
    first = REPLY_ACCOUNT_FIRST or registry_value("first")      # .env wins; the desktop app stores its choice in the registry
    if first and first in names:                       # e.g. the page's own account: its replies are never hidden
        names.remove(first)
        names.insert(0, first)
    return names


def account_browser(name: str) -> str:
    """The browser executable an account was created with (the desktop app records it), or '' = the .env default.
    A profile is always driven by the browser that made it."""
    for a in load_accounts():
        if a["name"] == name and a.get("browser") and os.path.exists(a["browser"]):
            return a["browser"]
    return ""


def account_user_data_dir(name: str) -> str:
    """The real-browser profile folder behind an account name; the .env master
    folder for the master / any unregistered name."""
    for a in load_accounts():
        if a["name"] == name:
            return a["user_data_dir"]
    return BFP_MASTER_USER_DATA_DIR

# bfp's own BackendRegistry.plan() independently recommends "patchright" FIRST
# for tiktok.com (confirmed live, 2026-10-06) over the plain "playwright"
# default: it attaches over CDP instead of a Playwright-launched browser,
# which avoids Playwright's own Runtime.enable bookkeeping - a real,
# documented automation tell, separate from (and in addition to) the
# click-trust/ticket-guard-key fixes already made. bfp.Session now carries
# storage_state (cookies + localStorage) across to this backend too (fixed
# 2026-10-07 - it used to only work for the default "playwright" backend).
BFP_BACKEND = os.environ.get("BFP_BACKEND", "patchright")

# Optional, off by default: route browser traffic through a proxy. Confirmed
# live (2026-10-06) this is NOT needed, and explicitly not wanted for
# production - see BFP_PROFILE_COUNTRY below for the actual fix.
BFP_PROXY = os.environ.get("BFP_PROXY", "")


def _detect_egress_country() -> str | None:
    """Best-effort: the real, current egress IP's own country - not a
    guess, not a brand-matched literal. Confirmed live (2026-10-06) that
    forcing the browser profile to claim a DIFFERENT country than wherever
    this machine's traffic actually originates from (e.g. hardcoding "BR"
    for a Brazilian brand while actually running from a Bangladesh IP,
    with no proxy) is what silently broke every reply-post: TikTok returned
    HTTP 200 with a completely empty body and never posted anything,
    confirmed via a fresh re-fetch never showing the reply, while every
    READ (page loads, comment fetches, login checks) worked perfectly the
    whole time - reads are low-risk to allow from anywhere; a write from an
    account whose claimed origin doesn't match its real one is exactly what
    anti-fraud systems are built to quietly catch. Switching to an actual
    Brazilian proxy mid-session for the SAME already-established login made
    it worse, not better (the sudden location jump on an existing session
    looked more suspicious, not less - TikTok started rendering the
    logged-out nav even though the session cookie was technically still
    there). The durable fix is simpler and free: never claim a country this
    automation doesn't actually run from. A real human users's account can
    legitimately be managed by a social-media team in a different country
    than the brand's own market - that's normal and not itself suspicious;
    an account whose OWN login history is consistent is what matters, not
    which specific country that happens to be.
    """
    import urllib.request
    try:
        with urllib.request.urlopen("http://ip-api.com/json/", timeout=5) as resp:
            import json
            data = json.loads(resp.read())
        cc = data.get("countryCode")
        return cc if cc and len(cc) == 2 else None
    except Exception:  # noqa: BLE001
        return None


# BFP_PROFILE_COUNTRY: explicit override if set (e.g. once BFP_PROXY above
# is actually in use and genuinely matches it); otherwise auto-detected from
# this machine's real egress IP - so the claimed identity can never silently drift away from reality the way a hardcoded
# literal did. Looked up the FIRST time it is needed (a network call with a 5 s timeout must not hold up every start-up),
# then remembered.
def __getattr__(name: str):                                   # PEP 562: config.BFP_PROFILE_COUNTRY
    if name == "BFP_PROFILE_COUNTRY":
        value = os.environ.get("BFP_PROFILE_COUNTRY") or _detect_egress_country() or "US"
        globals()["BFP_PROFILE_COUNTRY"] = value
        return value
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# -- server friendliness ------------------------------------------------------------------------------------
# What the bot's browser refuses to download: resource types ("media", "image", ...) plus "url:<substring>" entries.
# DEFAULT: NOTHING. Measured live on the real page: with the video blocked ("media,url:/video/tos/") TikTok's post
# overlay never delivers its comment data (3 of 3 scans failed: "no comment data arrived"), while the same scan with
# nothing blocked succeeded 5 of 5 (~55 s, ~17-21 MB). Sound is muted by the browser flag instead (BFP_MUTE_AUDIO),
# so nothing audible reaches the server either way. Only set this if you have re-tested a full scan with it.
BROWSER_BLOCK_RESOURCES = {r.strip() for r in os.environ.get("BROWSER_BLOCK_RESOURCES", "").split(",") if r.strip()}
LOG_DIR = DATA_DIR / "logs"
