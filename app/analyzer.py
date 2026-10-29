"""Analyse ONE post on demand: read all its comments through an account, find the negative ones, keep the results.

This is the desktop app's core, and nothing here runs by itself: a person pastes a link and asks. Steps of a job
  queued     waiting for the browser (the service has ONE browser thread; scans and posts never overlap)
  reading    the post is opened by its link and its comments are paged through ("Read 60 of 152")
  analysing  free rules first (emoji, praise, obvious scam words), then the AI in batches for the rest - at most once per
             comment, and only this post's comments are sent
  done       the results are in the database; `result()` shows them, sorted, with the reply card of each comment
Comments stored earlier (by the watcher or by an earlier analysis) keep their verdict: re-analysing a post only pays for new ones.
"""
import threading
import time
import uuid

import config
from app import analysis, links
from app.controller import ControlError

ACTIVE = ("queued", "reading", "analysing")
STATUS_TEXT = {"negative": "negative", "question": "question", "ok": "ok"}


def _hidden(row: dict) -> bool:
    """Not shown: empty comments, and ones written by our own accounts (stored as 'skipped'). A comment the AI gave up on is
    also 'skipped' but is shown, as unchecked."""
    return row["status"] == "skipped" and (not row.get("text") or row.get("source") != "unclassified")


class AnalysisJob:
    def __init__(self, target, source: str, account: str | None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.source, self.url, self.video_id, self.account = source, target.url, target.video_id, account or None
        self.state, self.message = "queued", "Waiting for the browser..."
        self.read, self.total = 0, None
        self.outcome: str | None = None
        self.error: dict | None = None
        self.note: str | None = None
        self.used_account: str | None = None
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.cancel = threading.Event()

    @property
    def finished(self) -> bool:
        return self.state not in ACTIVE

    def set(self, state: str, message: str) -> None:
        self.state, self.message, self.updated_at = state, message, time.time()

    def view(self) -> dict:
        return {"id": self.id, "url": self.url, "video_id": self.video_id, "state": self.state, "message": self.message,
                "finished": self.finished, "outcome": self.outcome, "error": self.error, "note": self.note,
                "progress": {"read": self.read, "total": self.total}, "account": self.account,
                "used_account": self.used_account, "started_at": self.started_at, "updated_at": self.updated_at}


class PostAnalyzer:
    def __init__(self, db, ai, *, run, reader=None, read_limit=None) -> None:
        """`run(fn, timeout)` executes fn on the service's browser thread and returns its result. `reader` reads a post's
        comments (default: the real browser reader; tests inject a fake). `read_limit()` -> how many comments to read."""
        self.db, self.ai, self._run = db, ai, run
        self.reader = reader
        self._read_limit = read_limit or (lambda: int(self.db.get_setting("read_limit", config.MAX_COMMENT_PAGES * 20)))
        self._lock = threading.RLock()
        self._active: AnalysisJob | None = None
        self._last: AnalysisJob | None = None

    # ------------------------------------------------------------------ jobs
    def start(self, url: str, account: str | None = None) -> dict:
        try:
            target = links.resolve(url)
        except links.LinkError as err:
            from app.i18n import tr
            raise ControlError(f"link_{err.key}", tr("en", f"lk_err_{err.key}")) from err
        if account and account not in config.account_chain():
            raise ControlError("bad_request", f"'{account}' is not an active account.")
        with self._lock:
            if self._active is not None:
                raise ControlError("busy", "Another post is being analysed right now - wait for it to finish (or stop it).",
                                   job=self._active.view())
            job = AnalysisJob(target, url, account)
            self._active = job
        threading.Thread(target=self._work, args=(job,), name=f"analyse-{job.id}", daemon=True).start()
        return job.view()

    def current(self) -> dict | None:
        with self._lock:
            job = self._active or self._last
            return job.view() if job else None

    def job(self, job_id: str) -> dict:
        with self._lock:
            for job in (self._active, self._last):
                if job is not None and job.id == job_id:
                    return job.view()
        raise ControlError("not_found", "There is no such analysis.")

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._active
            if job is None or job.id != job_id:
                return self.job(job_id)
        job.cancel.set()
        return job.view()

    def _finish(self, job: AnalysisJob, outcome: str, message: str, *, error: dict | None = None) -> None:
        job.outcome, job.error = outcome, error
        with self._lock:                                 # free the slot BEFORE the state flips: a client may start the next one
            if self._active is job:
                self._active, self._last = None, job
        job.set({"done": "done", "cancelled": "cancelled"}.get(outcome, "failed"), message)

    def _work(self, job: AnalysisJob) -> None:
        try:
            data = self._run(lambda: self._read(job), 1500)
            if job.cancel.is_set() or data.get("cancelled"):
                return self._finish(job, "cancelled", "Stopped.")
            if not data.get("ok"):
                code = "captcha" if data.get("captcha") else "blocked" if data.get("blocked") else "read_failed"
                return self._finish(job, "failed", self._friendly(data),
                                    error={"code": code, "message": data.get("error") or "The comments could not be read."})
            job.used_account = data.get("profile")
            job.set("analysing", "Checking which comments are negative...")
            self._store(job, data)
            self._classify(job)
            if data.get("logged_in") is False:
                job.note = (job.note + " " if job.note else "") + "The account used is not logged in - log it in again in Accounts."
            self._finish(job, "done", self._summary(job))
        except TimeoutError:
            self._finish(job, "failed", "The browser is busy with something else - try again in a moment.",
                         error={"code": "busy", "message": "browser busy"})
        except Exception as exc:  # noqa: BLE001 - whatever happened, the job ends and the person is told
            self._finish(job, "failed", f"Something went wrong: {str(exc)[:160]}",
                         error={"code": "internal", "message": f"{type(exc).__name__}: {exc}"[:200]})

    @staticmethod
    def _friendly(data: dict) -> str:
        tried = len({a.get("account") for a in data.get("attempts") or []})
        accounts = f" ({tried} accounts tried)" if tried > 1 else ""
        if data.get("captcha"):
            return (f"TikTok is showing a security check (captcha) right now{accounts}. Wait a few minutes and try again, "
                    "or read with a different account.")
        if data.get("blocked"):
            return (f"TikTok would not open the page for the account(s) used{accounts}. This usually passes within a few "
                    "minutes - wait a little and press Find again, or choose another account under \"Read the comments with\".")
        err = data.get("error") or "The comments could not be read."
        if "could not open" in err:
            return f"The post could not be opened - it may be private, deleted, or TikTok blocked the page for a moment. ({err})"
        return err

    def _read(self, job: AnalysisJob) -> dict:
        """Runs on the browser thread."""
        reader = self.reader
        if reader is None:
            from app import tiktok_monitor
            reader = tiktok_monitor.read_comments_of_post
        job.set("reading", "Opening the post...")

        def progress(read, total):
            job.read, job.total, job.updated_at = read, total, time.time()
            job.message = f"Reading comments... {read}" + (f" of {total}" if total else "")

        def stage(text):                                 # what the browser is doing: "Opening the post with @name..."
            job.message, job.updated_at = text, time.time()
        return reader(job.url, account=job.account, max_comments=max(20, min(int(self._read_limit()), 1000)),
                      progress=progress, stage=stage, should_stop=job.cancel.is_set)

    # ------------------------------------------------------------------ keeping and judging the comments
    def _store(self, job: AnalysisJob, data: dict) -> None:
        comments = data.get("comments") or []
        job.read = len(comments)
        if data.get("author") and "/@_/" in job.url:        # the link did not name the owner; the page did
            job.url = job.url.replace("/@_/", f"/@{data['author']}/")
        self.db.upsert_post({"video_id": job.video_id, "url": job.url, "description": data.get("description") or "",
                             "is_pinned": 0, "comment_count": data.get("total") or len(comments)})
        analysis.ingest_comments(self.db, job.video_id, comments, first_scan=False)
        self.db.requeue_post_comments(job.video_id)           # the watcher may have left this post's old comments unjudged
        self.db.mark_post_checked(job.video_id, data.get("total") or len(comments))
        if data.get("truncated"):
            limit = max(20, min(int(self._read_limit()), 1000))
            job.note = (f"Read the first {len(comments)} comments - the reading limit is {limit} (change it in Controls)."
                        if len(comments) >= limit else
                        f"TikTok stopped loading more comments after {len(comments)}. Press Read again to try for the rest.")

    def _classify(self, job: AnalysisJob) -> None:
        def progress(done, total):                       # the AI takes seconds per request: say how far it is
            job.read, job.total, job.updated_at = done, total, time.time()
            job.message = f"Checking which comments are negative... {done} of {total}"
        for _ in range(3):                               # one round judges every comment; later rounds only retry unparsed lines
            stats = analysis.classify_pending(self.db, self.ai, limit=1000, video_id=job.video_id, progress=progress,
                                              batch_size=config.AI_BATCH_SIZE_ON_DEMAND)
            if stats["error"]:
                job.note = ((job.note + " ") if job.note else "") + (
                    "The AI could not be used right now, so only obvious cases were judged by keywords "
                    f"({str(stats['error'])[:90]}).")
            if not stats["candidates"] or stats["error"] or stats["classified"] + stats["fallback"] == 0:
                break

    def _summary(self, job: AnalysisJob) -> str:
        counts = self._counts(self.db.post_comments(job.video_id))
        return (f"Read {counts['total']} comments - {counts['negative']} negative, {counts['question']} "
                f"question{'s' if counts['question'] != 1 else ''}.")

    @staticmethod
    def _counts(rows: list) -> dict:
        c = {"total": 0, "negative": 0, "question": 0, "ok": 0, "unchecked": 0}
        for r in rows:
            if _hidden(r):
                continue
            c["total"] += 1
            if r["status"] in ("negative", "question", "ok"):
                c[r["status"]] += 1
            else:
                c["unchecked"] += 1
        return c

    # ------------------------------------------------------------------ what the screen shows
    def result(self, video_id: str) -> dict:
        post = self.db.get_post(video_id)
        rows = self.db.post_comments(video_id)
        if post is None and not rows:
            raise ControlError("not_found", "This post has not been analysed yet.")
        cards = self.db.latest_approvals_for([r["cid"] for r in rows])
        comments = []
        for r in rows:
            if _hidden(r):
                continue
            card = cards.get(r["cid"])
            comments.append({
                "cid": r["cid"], "text": r.get("text") or "", "username": r.get("username"), "likes": r.get("digg_count") or 0,
                "created_ts": r.get("create_ts"), "replies": r.get("reply_count") or 0,
                "status": r["status"] if r["status"] in ("negative", "question", "ok") else "unchecked",
                "severity": r.get("severity") or 0, "category": r.get("category"), "reason": r.get("reason"),
                "lang": r.get("lang"), "judged_by": r.get("source"),
                "card": ({"id": card["id"], "status": card["status"], "reply": card.get("reply"), "account": card.get("account"),
                          "posted_by": card.get("posted_by")} if card else None)})
        return {"post": ({"video_id": post["video_id"], "url": post["url"], "description": post.get("description") or "",
                          "comment_count": post.get("comment_count"), "last_checked_at": post.get("last_checked_at"),
                          "author": (post["url"].split("/@")[1].split("/")[0]
                                     if "/@" in post["url"] and "/@_/" not in post["url"] else None)}
                         if post else {"video_id": video_id}),
                "counts": self._counts(rows), "comments": comments}
