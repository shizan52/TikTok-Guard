"""Minimal Telegram Bot API client (stdlib only -- no python-telegram-bot dependency).

Everything the bot needs: send / edit text and photo messages with inline keyboards, answer
button presses, long-poll for updates, send and download documents, publish the command menu.
Errors from Telegram (HTTP 400 "message is not modified", ...) are returned as {"ok": False, ...}
instead of raised, so callers can treat them as ordinary outcomes.
"""
import json
import mimetypes
import urllib.error
import urllib.request
import uuid

import config

API_BASE = "https://api.telegram.org/bot{token}/{method}"
FILE_BASE = "https://api.telegram.org/file/bot{token}/{path}"


def _url(method: str) -> str:
    return API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method=method)


def _read_json(resp_or_err) -> dict:
    try:
        return json.loads(resp_or_err.read())
    except Exception:  # noqa: BLE001
        return {"ok": False, "description": "unreadable response"}


def _post_json(method: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(_url(method), data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        data = _read_json(exc)
        data.setdefault("ok", False)
        data.setdefault("error_code", exc.code)
        return data


def _post_multipart(method: str, fields: dict, file_field: str, filename: str, file_bytes: bytes,
                    mime: str | None = None) -> dict:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8"))
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(_url(method), data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        data = _read_json(exc)
        data.setdefault("ok", False)
        return data


def get_me() -> dict:
    with urllib.request.urlopen(_url("getMe"), timeout=15) as resp:
        return json.loads(resp.read())


def get_updates(offset: int = 0, timeout: int = 0) -> dict:
    """Long-poll: with timeout>0 Telegram holds the request open until an update arrives."""
    payload = {"offset": offset, "timeout": timeout,
               "allowed_updates": ["message", "callback_query"]}
    return _post_json("getUpdates", payload, timeout=timeout + 15)


def send_message(text: str, reply_markup: dict | None = None, chat_id=None, reply_to: int | None = None) -> dict:
    payload = {"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "text": text[:4096], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    return _post_json("sendMessage", payload)


def edit_message_text(chat_id, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    payload["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
    return _post_json("editMessageText", payload)


def edit_message_reply_markup(chat_id, message_id: int, reply_markup: dict | None) -> dict:
    return _post_json("editMessageReplyMarkup", {
        "chat_id": chat_id, "message_id": message_id,
        "reply_markup": reply_markup or {"inline_keyboard": []}})


def answer_callback_query(callback_query_id: str, text: str = "", alert: bool = False) -> dict:
    return _post_json("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:200],
                                              "show_alert": alert})


def send_photo(photo_path: str, caption: str, reply_markup: dict | None = None, chat_id=None,
               reply_to: int | None = None) -> dict:
    with open(photo_path, "rb") as f:
        data = f.read()
    fields = {"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup)
    if reply_to:
        fields["reply_to_message_id"] = reply_to
        fields["allow_sending_without_reply"] = "true"
    return _post_multipart("sendPhoto", fields, "photo", photo_path.replace("\\", "/").split("/")[-1], data)


def send_document(filename: str, content: bytes, caption: str | None = None, chat_id=None) -> dict:
    fields = {"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "caption": caption}
    return _post_multipart("sendDocument", fields, "document", filename, content, "text/plain")


def get_file_bytes(file_id: str, *, max_bytes: int = 300_000) -> bytes | None:
    info = _post_json("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return None
    url = FILE_BASE.format(token=config.TELEGRAM_BOT_TOKEN, path=path)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read(max_bytes + 1)


def set_my_commands(commands: list) -> dict:
    return _post_json("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})


def send_chat_action(chat_id=None, action: str = "typing") -> dict:
    return _post_json("sendChatAction", {"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "action": action})
