"""SQLite storage for the whole bot (stdlib only).

One file (data/bot.db, WAL mode) holds everything the bot knows: the editable AI
settings (+ version history), the watched posts, every comment seen and what was
decided about it, a cache of analysis results (so identical comment texts never
cost a second AI call), per-day AI usage, Telegram approvals and conversation
state. Retention / size management lives in app/maintenance.py.

Thread model: one connection per thread (sqlite3 connections are not shared);
WAL + busy_timeout let the Telegram thread and the browser worker write at once.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS setting_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_setting_versions_key ON setting_versions(key, id);

CREATE TABLE IF NOT EXISTS posts (
    video_id TEXT PRIMARY KEY, url TEXT NOT NULL, description TEXT, created_ts INTEGER,
    is_pinned INTEGER NOT NULL DEFAULT 0, watch_rank INTEGER,
    comment_count INTEGER NOT NULL DEFAULT 0, like_count INTEGER, play_count INTEGER,
    checked_comment_count INTEGER,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_checked_at TEXT, last_error TEXT);

CREATE TABLE IF NOT EXISTS comments (
    cid TEXT PRIMARY KEY, video_id TEXT NOT NULL, username TEXT, text TEXT, text_hash TEXT,
    create_ts INTEGER, digg_count INTEGER NOT NULL DEFAULT 0, reply_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    sentiment TEXT, lang TEXT, severity INTEGER, category TEXT, reason TEXT, source TEXT,
    analyzed_at TEXT, screenshot TEXT, attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id, status);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(text_hash);

CREATE TABLE IF NOT EXISTS analysis_cache (
    text_hash TEXT PRIMARY KEY, sentiment TEXT NOT NULL, lang TEXT, severity INTEGER,
    category TEXT, reason TEXT, source TEXT, created_at TEXT NOT NULL,
    last_hit_at TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, cid TEXT NOT NULL, video_id TEXT NOT NULL, video_url TEXT,
    username TEXT, comment_text TEXT, lang TEXT, category TEXT, severity INTEGER, reason TEXT,
    reply TEXT, status TEXT NOT NULL,
    chat_id TEXT, message_id INTEGER, simulated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, posted_at TEXT,
    post_attempts INTEGER NOT NULL DEFAULT 0, regen_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, posted_by TEXT, posted_reply_cid TEXT, posted_visibility TEXT,
    kind TEXT NOT NULL DEFAULT 'alert', account TEXT);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_open_cid ON approvals(cid)
    WHERE status IN ('pending', 'approved', 'posting');

CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0, ok_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, kind));

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id TEXT PRIMARY KEY, mode TEXT, payload TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:20]


class _Mine:
    """A thread's own connection. When the thread ends (HTTP request threads come and go) its thread-local storage is dropped, this
    object goes with it and the connection is closed and forgotten - without it every request left one open connection behind."""

    def __init__(self, db: "DB", conn: sqlite3.Connection) -> None:
        self.db, self.conn = db, conn

    def __del__(self) -> None:
        try:
            self.db._forget(self.conn)
        except Exception:  # noqa: BLE001 - at interpreter shutdown anything may be gone already
            pass


class DB:
    def __init__(self, path=None) -> None:
        self.path = Path(path or config.DB_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list = []
        self._lock = threading.RLock()                 # re-entrant: a connection may be released (by garbage collection) while it is held
        self._listeners: list = []
        self._ensure_schema()

    # -- change notifications (the desktop app's live updates) ---------------------------
    def add_listener(self, fn) -> None:
        """fn(kind, data) is called after an approval is created / updated, a setting changes or an event is logged.
        A listener that raises never disturbs the database operation."""
        self._listeners.append(fn)

    def _notify(self, kind: str, data: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, data)
            except Exception:  # noqa: BLE001
                pass

    # -- connections -----------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            # check_same_thread=False: each connection is still only USED by the thread that made it, but it may be CLOSED by another
            # (close(), or the end of its thread) - otherwise that close silently failed and the file stayed locked.
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
            self._local.mine = _Mine(self, c)
            with self._lock:
                self._conns.append(c)
        return c

    def _forget(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if conn in self._conns:
                self._conns.remove(conn)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conns.clear()
        self._local = threading.local()

    def _ensure_schema(self) -> None:
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        c = self.conn()
        if fresh:
            # only effective before the first table exists: lets incremental_vacuum
            # hand freed pages back to the OS without a full VACUUM
            c.execute("PRAGMA auto_vacuum=INCREMENTAL")
        c.executescript(_SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(approvals)")}
        for column, ddl in (("posted_visibility", "TEXT"),                         # databases made before these columns
                            ("kind", "TEXT NOT NULL DEFAULT 'alert'"),             # 'alert' | 'link_post' | 'link_reply'
                            ("account", "TEXT"),                                   # account chosen for a link comment
                            ("replies_before", "INTEGER")):                        # the comment's reply count when it was sent
            if column not in have:
                c.execute(f"ALTER TABLE approvals ADD COLUMN {column} {ddl}")
        if self.meta_get("schema_version") is None:
            self.meta_set("schema_version", str(SCHEMA_VERSION))

    # -- meta (runtime state) ---------------------------------------------------
    def meta_get(self, key: str, default=None):
        row = self.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value) -> None:
        self.conn().execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # -- settings (user-editable, versioned) ------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str, *, keep_versions: int = 10) -> bool:
        """Store `value`; keeps the previous values (newest `keep_versions`) for /undo.
        Returns False when nothing changed."""
        value = str(value)
        now = utcnow()
        with self.tx() as c:
            cur = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if cur is not None and cur["value"] == value:
                return False
            if cur is not None:
                c.execute("INSERT INTO setting_versions(key, value, created_at) VALUES(?,?,?)",
                          (key, cur["value"], now))
                c.execute("DELETE FROM setting_versions WHERE key=? AND id NOT IN "
                          "(SELECT id FROM setting_versions WHERE key=? ORDER BY id DESC LIMIT ?)",
                          (key, key, keep_versions))
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (key, value, now))
        self._notify("setting.changed", {"key": key})
        return True

    def delete_setting(self, key: str) -> None:
        with self.tx() as c:
            cur = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if cur is not None:
                c.execute("INSERT INTO setting_versions(key, value, created_at) VALUES(?,?,?)",
                          (key, cur["value"], utcnow()))
                c.execute("DELETE FROM settings WHERE key=?", (key,))
        self._notify("setting.changed", {"key": key})

    def undo_setting(self, key: str) -> bool:
        """Swap back to the most recent previous value. False if there is none."""
        with self.tx() as c:
            prev = c.execute("SELECT id, value FROM setting_versions WHERE key=? ORDER BY id DESC LIMIT 1",
                             (key,)).fetchone()
            if prev is None:
                return False
            c.execute("DELETE FROM setting_versions WHERE id=?", (prev["id"],))
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (key, prev["value"], utcnow()))
        self._notify("setting.changed", {"key": key})
        return True

    def setting_version_count(self, key: str) -> int:
        return self.conn().execute("SELECT COUNT(*) n FROM setting_versions WHERE key=?", (key,)).fetchone()["n"]

    # -- posts ----------------------------------------------------------------
    def upsert_post(self, p: dict) -> None:
        now = utcnow()
        self.conn().execute(
            "INSERT INTO posts(video_id, url, description, created_ts, is_pinned, comment_count, like_count,"
            " play_count, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(video_id) DO UPDATE SET url=excluded.url, description=excluded.description,"
            " is_pinned=excluded.is_pinned, comment_count=excluded.comment_count,"
            " like_count=excluded.like_count, play_count=excluded.play_count, last_seen_at=excluded.last_seen_at",
            (p["video_id"], p["url"], p.get("description"), p.get("created_ts"), int(bool(p.get("is_pinned"))),
             int(p.get("comment_count") or 0), p.get("like_count"), p.get("play_count"), now, now))

    def set_watch_set(self, video_ids: list) -> None:
        with self.tx() as c:
            c.execute("UPDATE posts SET watch_rank=NULL WHERE watch_rank IS NOT NULL")
            for rank, vid in enumerate(video_ids, start=1):
                c.execute("UPDATE posts SET watch_rank=? WHERE video_id=?", (rank, vid))

    def watched_posts(self) -> list:
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM posts WHERE watch_rank IS NOT NULL ORDER BY watch_rank")]

    def get_post(self, video_id: str):
        r = self.conn().execute("SELECT * FROM posts WHERE video_id=?", (video_id,)).fetchone()
        return dict(r) if r else None

    def mark_post_checked(self, video_id: str, comment_count, error: str | None = None) -> None:
        if error:
            self.conn().execute("UPDATE posts SET last_error=?, last_checked_at=? WHERE video_id=?",
                                (error[:300], utcnow(), video_id))
        else:
            self.conn().execute("UPDATE posts SET checked_comment_count=?, last_checked_at=?, last_error=NULL "
                                "WHERE video_id=?", (comment_count, utcnow(), video_id))

    # -- comments ---------------------------------------------------------------
    def insert_new_comments(self, video_id: str, comments: list, *, legacy_before_ts: int | None = None,
                            skip_users: frozenset = frozenset()) -> list:
        """Insert comments never seen before; returns exactly those (dicts with 'status').
        Comments older than `legacy_before_ts` are stored as 'legacy' (kept so they are
        never analysed or announced later, but cost nothing)."""
        now = utcnow()
        fresh = []
        with self.tx() as c:
            for cm in comments:
                cid = str(cm.get("cid") or "")
                if not cid:
                    continue
                text = (cm.get("text") or "").strip()
                ts = cm.get("create_time")
                status = "new"
                if legacy_before_ts and ts and int(ts) < legacy_before_ts:
                    status = "legacy"
                elif not text or (cm.get("username") or "").lower() in skip_users:
                    status = "skipped"                  # empty, or written by one of OUR OWN accounts
                cur = c.execute(
                    "INSERT OR IGNORE INTO comments(cid, video_id, username, text, text_hash, create_ts,"
                    " digg_count, reply_count, first_seen_at, status, screenshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, video_id, cm.get("username"), text, text_hash(text) if text else None, ts,
                     int(cm.get("digg_count") or 0), int(cm.get("reply_count") or 0), now, status,
                     cm.get("screenshot")))
                if cur.rowcount:
                    fresh.append({**cm, "cid": cid, "text": text, "status": status, "video_id": video_id})
                else:                                   # seen before: keep its counters current (a check compares reply counts)
                    c.execute("UPDATE comments SET digg_count=?, reply_count=? WHERE cid=?",
                              (int(cm.get("digg_count") or 0), int(cm.get("reply_count") or 0), cid))
        return fresh

    def update_comment(self, cid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn().execute(f"UPDATE comments SET {cols} WHERE cid=?", (*fields.values(), cid))

    def get_comment(self, cid: str):
        r = self.conn().execute("SELECT * FROM comments WHERE cid=?", (cid,)).fetchone()
        return dict(r) if r else None

    def backfill_legacy(self, days: int, limit: int = 300) -> int:
        """Queue stored-but-never-analysed ('legacy') comments from the last `days` days for analysis.
        Only comments whose text was kept can be analysed (the very first scan keeps it)."""
        cutoff = int(time.time()) - days * 86400
        return self.conn().execute(
            "UPDATE comments SET status='new', source=NULL, attempts=0 WHERE cid IN ("
            " SELECT cid FROM comments WHERE status='legacy' AND text IS NOT NULL AND text <> ''"
            " AND COALESCE(create_ts, 0) >= ? ORDER BY COALESCE(create_ts, 0) DESC LIMIT ?)",
            (cutoff, limit)).rowcount

    def comments_to_analyze(self, limit: int = 200, video_id: str | None = None) -> list:
        """Unanalysed comments, newest first (fresh complaints matter more than old ones). `video_id`: only that post's."""
        where, args = "status='new' AND attempts < 3", []
        if video_id:
            where, args = where + " AND video_id=?", [video_id]
        return [dict(r) for r in self.conn().execute(
            f"SELECT * FROM comments WHERE {where} ORDER BY COALESCE(create_ts, 0) DESC, first_seen_at DESC LIMIT ?",
            (*args, limit))]

    def requeue_post_comments(self, video_id: str) -> int:
        """An on-demand analysis wants EVERY comment of the post judged: comments the watcher stored as 'legacy' (too old
        on its first scan) and ones it gave up on are put back in the queue. Returns how many."""
        return self.conn().execute(
            "UPDATE comments SET status='new', source=NULL, attempts=0 WHERE video_id=? AND text IS NOT NULL AND text<>''"
            " AND (status='legacy' OR (status='skipped' AND source='unclassified'))", (video_id,)).rowcount

    def post_comments(self, video_id: str) -> list:
        """Every stored comment of one post, the most serious first (then the most liked, then the newest)."""
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM comments WHERE video_id=? ORDER BY COALESCE(severity,0) DESC, digg_count DESC,"
            " COALESCE(create_ts,0) DESC", (video_id,))]

    def latest_approvals_for(self, cids: list) -> dict:
        """cid -> the newest approval (reply card) made for that comment."""
        out: dict = {}
        for i in range(0, len(cids), 400):
            chunk = cids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.conn().execute(
                    f"SELECT * FROM approvals WHERE cid IN ({marks}) ORDER BY created_at, rowid", chunk):
                out[r["cid"]] = dict(r)
        return out

    def open_approval_for(self, cid: str) -> dict | None:
        r = self.conn().execute("SELECT * FROM approvals WHERE cid=? AND status IN ('pending','approved','posting')",
                                (cid,)).fetchone()
        return dict(r) if r else None

    def recent_analyzed_posts(self, limit: int = 8) -> list:
        """Posts that were read (by the watcher or on demand), newest first, with their comment tallies."""
        return [dict(r) for r in self.conn().execute(
            "SELECT p.video_id, p.url, p.description, p.comment_count, p.last_checked_at,"
            " (SELECT COUNT(*) FROM comments c WHERE c.video_id=p.video_id AND c.status='negative') AS negatives,"
            " (SELECT COUNT(*) FROM comments c WHERE c.video_id=p.video_id AND c.status='question') AS questions,"
            " (SELECT COUNT(*) FROM comments c WHERE c.video_id=p.video_id) AS stored"
            " FROM posts p WHERE p.last_checked_at IS NOT NULL ORDER BY p.last_checked_at DESC LIMIT ?", (limit,))]

    def replies_posted_today(self) -> int:
        return self.conn().execute(
            "SELECT COUNT(*) n FROM approvals WHERE status='posted' AND substr(COALESCE(posted_at, updated_at), 1, 10)=?",
            (utc_day(),)).fetchone()["n"]

    def negatives_without_approval(self, include_questions: bool, limit: int = 50) -> list:
        statuses = ("negative", "question") if include_questions else ("negative",)
        marks = ",".join("?" * len(statuses))
        return [dict(r) for r in self.conn().execute(
            f"SELECT c.* FROM comments c WHERE c.status IN ({marks}) AND NOT EXISTS "
            "(SELECT 1 FROM approvals a WHERE a.cid=c.cid) "
            "ORDER BY COALESCE(c.severity,0) DESC, COALESCE(c.create_ts,0) DESC LIMIT ?", (*statuses, limit))]

    # -- analysis cache -----------------------------------------------------------
    def cache_get(self, h: str):
        r = self.conn().execute("SELECT * FROM analysis_cache WHERE text_hash=?", (h,)).fetchone()
        if r is None:
            return None
        self.conn().execute("UPDATE analysis_cache SET hits=hits+1, last_hit_at=? WHERE text_hash=?",
                            (utcnow(), h))
        return dict(r)

    def cache_put(self, h: str, *, sentiment: str, lang=None, severity=None, category=None,
                  reason=None, source=None) -> None:
        now = utcnow()
        self.conn().execute(
            "INSERT INTO analysis_cache(text_hash, sentiment, lang, severity, category, reason, source,"
            " created_at, last_hit_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(text_hash) DO UPDATE SET sentiment=excluded.sentiment, lang=excluded.lang,"
            " severity=excluded.severity, category=excluded.category, reason=excluded.reason,"
            " source=excluded.source, last_hit_at=excluded.last_hit_at",
            (h, sentiment, lang, severity, category, (reason or "")[:200], source, now, now))

    # -- AI usage -----------------------------------------------------------------
    def add_usage(self, model: str, kind: str, *, ok: bool, prompt_tokens: int = 0,
                  completion_tokens: int = 0, day: str | None = None) -> None:
        self.conn().execute(
            "INSERT INTO ai_usage(day, model, kind, calls, ok_calls, prompt_tokens, completion_tokens) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day, model, kind) DO UPDATE SET calls=calls+1,"
            " ok_calls=ok_calls+excluded.ok_calls, prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
            " completion_tokens=completion_tokens+excluded.completion_tokens",
            (day or utc_day(), model, kind, 1, 1 if ok else 0, prompt_tokens, completion_tokens))

    def usage_for_day(self, day: str | None = None) -> dict:
        rows = self.conn().execute("SELECT * FROM ai_usage WHERE day=?", (day or utc_day(),)).fetchall()
        by_kind: dict = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["calls"]
        return {"calls": sum(r["calls"] for r in rows), "ok_calls": sum(r["ok_calls"] for r in rows),
                "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
                "completion_tokens": sum(r["completion_tokens"] for r in rows), "by_kind": by_kind}

    # -- approvals ------------------------------------------------------------------
    def create_approval(self, rec: dict) -> str | None:
        """Insert an approval; None when the comment already has an open one."""
        now = utcnow()
        try:
            self.conn().execute(
                "INSERT INTO approvals(id, cid, video_id, video_url, username, comment_text, lang, category,"
                " severity, reason, reply, status, chat_id, message_id, simulated, kind, account, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["cid"], rec["video_id"], rec.get("video_url"), rec.get("username"),
                 rec.get("comment_text"), rec.get("lang"), rec.get("category"), rec.get("severity"),
                 rec.get("reason"), rec.get("reply"), rec.get("status", "pending"), rec.get("chat_id"),
                 rec.get("message_id"), int(bool(rec.get("simulated"))), rec.get("kind") or "alert",
                 rec.get("account") or None, now, now))
        except sqlite3.IntegrityError:
            return None
        self._notify("approval.created", {"id": rec["id"], "kind": rec.get("kind") or "alert",
                                          "status": rec.get("status", "pending")})
        return rec["id"]

    def get_approval(self, approval_id: str):
        r = self.conn().execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(r) if r else None

    def update_approval(self, approval_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn().execute(f"UPDATE approvals SET {cols} WHERE id=?", (*fields.values(), approval_id))
        self._notify("approval.updated", {"id": approval_id, "status": fields.get("status"),
                                          "fields": sorted(k for k in fields if k != "updated_at")})

    def interrupt_posting(self, reason: str) -> list:
        """Cards left in 'posting' by a process that died or was closed mid-send. Nothing is sending them any more (the job queue
        lives in memory) and nobody can tell whether TikTok got the reply, so they become 'post_failed' with `reason` - a person
        decides, never an automatic second send. -> the ids."""
        ids = [r["id"] for r in self.conn().execute("SELECT id FROM approvals WHERE status='posting'")]
        for approval_id in ids:
            self.update_approval(approval_id, status="post_failed", last_error=reason[:300])
        return ids

    def list_approvals(self, statuses: tuple, limit: int = 20) -> list:
        marks = ",".join("?" * len(statuses))
        return [dict(r) for r in self.conn().execute(
            f"SELECT * FROM approvals WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT ?",
            (*statuses, limit))]

    def approvals_missing_reply(self, limit: int = 10) -> list:
        # alert cards only: a link comment without text waits for the operator's own words / brief, never for the AI budget
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM approvals WHERE status='pending' AND (reply IS NULL OR reply='') AND simulated=0"
            " AND kind='alert' ORDER BY severity DESC, created_at LIMIT ?", (limit,))]

    # -- comments the operator asked for with /comment <link> ---------------------------------
    def link_posts_today(self) -> int:
        """Link comments sent (or being sent) today (UTC) - what LINK_COMMENTS_PER_DAY is counted against."""
        return self.conn().execute(
            "SELECT COUNT(*) n FROM approvals WHERE kind!='alert' AND status IN ('posted','posting')"
            " AND substr(COALESCE(posted_at, updated_at), 1, 10)=?", (utc_day(),)).fetchone()["n"]

    def recent_link_post(self, cid: str, hours: int = 24) -> dict | None:
        """The latest link comment already posted on this target (`cid` is the comment id, or link-post:<video>)."""
        r = self.conn().execute(
            "SELECT * FROM approvals WHERE kind!='alert' AND status='posted' AND cid=? AND posted_at>=?"
            " ORDER BY posted_at DESC LIMIT 1", (cid, utc_ago(hours=hours))).fetchone()
        return dict(r) if r else None

    # -- chat conversation state --------------------------------------------------------
    def get_chat_state(self, chat_id) -> dict | None:
        r = self.conn().execute("SELECT * FROM chat_state WHERE chat_id=?", (str(chat_id),)).fetchone()
        if r is None:
            return None
        return {"mode": r["mode"], "payload": json.loads(r["payload"] or "null"), "updated_at": r["updated_at"]}

    def set_chat_state(self, chat_id, mode: str, payload=None) -> None:
        self.conn().execute(
            "INSERT INTO chat_state(chat_id, mode, payload, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode, payload=excluded.payload,"
            " updated_at=excluded.updated_at", (str(chat_id), mode, json.dumps(payload), utcnow()))

    def clear_chat_state(self, chat_id) -> None:
        self.conn().execute("DELETE FROM chat_state WHERE chat_id=?", (str(chat_id),))

    # -- events (small audit trail) ----------------------------------------------------
    def log_event(self, kind: str, detail: str = "", level: str = "info") -> None:
        self.conn().execute("INSERT INTO events(ts, level, kind, detail) VALUES(?,?,?,?)",
                            (utcnow(), level, kind, (detail or "")[:500]))
        self._notify("log", {"kind": kind, "detail": (detail or "")[:300], "level": level})

    def recent_events(self, limit: int = 10) -> list:
        return [dict(r) for r in self.conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # -- introspection --------------------------------------------------------------------
    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def table_counts(self) -> dict:
        out = {}
        for t in ("posts", "comments", "analysis_cache", "approvals", "ai_usage", "events", "setting_versions"):
            out[t] = self.conn().execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        return out

    def comment_status_counts(self) -> dict:
        return {r["status"]: r["n"] for r in self.conn().execute(
            "SELECT status, COUNT(*) n FROM comments GROUP BY status")}
