"""Shared fakes for the offline test-suite (no Telegram, no OpenRouter, no browser)."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from app.db import DB  # noqa: E402


def tmp_db() -> DB:
    d = Path(tempfile.mkdtemp(prefix="botdb-"))
    return DB(d / "bot.db")


class FakeTG:
    """Records everything the bot sends; mimics the telegram_notify surface."""
    def __init__(self) -> None:
        self.sent: list = []
        self.edits: list = []
        self.answers: list = []
        self.docs: list = []
        self.file_bytes: bytes | None = None
        self._mid = 100

    def send_message(self, text, reply_markup=None, chat_id=None, reply_to=None):
        self._mid += 1
        self.sent.append({"text": text, "markup": reply_markup, "chat_id": chat_id, "id": self._mid})
        return {"ok": True, "result": {"message_id": self._mid}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "markup": reply_markup})
        return {"ok": True}

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        return {"ok": True}

    def answer_callback_query(self, cqid, text="", alert=False):
        self.answers.append({"id": cqid, "text": text, "alert": alert})
        return {"ok": True}

    def send_document(self, filename, content, caption=None, chat_id=None):
        self.docs.append({"filename": filename, "content": content, "chat_id": chat_id})
        return {"ok": True}

    def get_file_bytes(self, file_id, max_bytes=300000):
        return self.file_bytes

    photos: list = None

    def send_photo(self, photo_path, caption, reply_markup=None, chat_id=None, reply_to=None):
        if self.photos is None:
            self.photos = []
        self.photos.append({"path": photo_path, "caption": caption, "chat_id": chat_id, "reply_to": reply_to})
        return {"ok": True, "result": {"message_id": 1}}

    def last_text(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""

    # long-poll surface used by Service._telegram_loop
    queued_updates: list = None

    def get_updates(self, offset=0, timeout=0):
        import time as _t
        if self.queued_updates:
            batch, self.queued_updates = self.queued_updates, []
            return {"ok": True, "result": batch}
        _t.sleep(0.05)
        return {"ok": True, "result": []}

    def set_my_commands(self, commands):
        self.commands = commands
        return {"ok": True}


class FakeSvc:
    def __init__(self) -> None:
        self.posts: list = []

    def scan_running(self):
        return False

    def request_post(self, approval_id):
        self.posts.append(approval_id)

    def run_background(self, fn, *args):
        fn(*args)

    def last_scan_text(self):
        return "never"

    def run_cleanup(self):
        return {"freed_mb_total": 1.5, "db": {"db_before_mb": 0.2, "db_after_mb": 0.1}}


def make_transport(replies):
    """Fake OpenRouter transport. `replies`: list of dicts returned in order (last one repeats),
    or a callable(model, system, user) -> dict. Records every call in .calls."""
    calls: list = []

    def transport(model, system, user, max_tokens, reasoning=None, temperature=None):
        calls.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens,
                      "reasoning": reasoning, "temperature": temperature})
        if callable(replies):
            res = replies(model, system, user)
        else:
            res = replies[min(len(calls) - 1, len(replies) - 1)]
        base = {"content": None, "http": 200, "error": None, "body": "", "pt": 50, "ct": 20}
        return {**base, **res}

    transport.calls = calls
    return transport


def make_ai(db, replies, **kw):
    from app.ai import AI
    t = make_transport(replies)
    kw.setdefault("key_info", lambda: None)          # tests never touch the network
    kw.setdefault("models_info", lambda: None)
    ai = AI(db, transport=t, sleep=lambda s: None, **kw)
    ai.calls = t.calls
    return ai


def comment(cid="c1", text="hello", video_id="v1", username="user", create_time=None, digg=0):
    import time
    return {"cid": cid, "text": text, "username": username, "create_time": create_time or int(time.time()),
            "digg_count": digg, "reply_count": 0, "video_id": video_id}
