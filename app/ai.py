"""OpenRouter client built around ONE constraint: the free tier allows only a small number of
requests per UTC day. So:

  * every HTTP request is counted in the database (app.db ai_usage) and checked against a
    daily budget (/budget) BEFORE it is made; 20% of the budget is kept back for reply drafts;
  * comments are classified in batches (AI_BATCH_SIZE per request) with a compact line format;
  * a daily-limit 429 from OpenRouter parks the AI until the next UTC midnight (no retry storm),
    a per-minute 429 / 5xx only cools down that one model for a short while;
  * only the context chunks relevant to a comment are sent with a reply request (app.context).

`AI(db, transport=...)` takes an injectable transport so the logic is testable offline.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import config
from app import context as ctx
from app import heuristics
from app.lang import lang_name, resolve_language
from app.db import DB, utc_day

DEFAULT_INSTRUCTIONS = (
    "Tone: warm, friendly, respectful and professional. Reply in the same language as the comment. "
    "Keep it to 1-2 short sentences. Acknowledge the person's concern and apologise briefly when it fits; "
    "never be defensive and never argue. Do not promise refunds, dates or prices. If the problem needs "
    "follow-up, invite the person to send a direct message with their order details."
)

CLASSIFY_SYSTEM = (
    "You screen comments left on social-media videos of the business '{brand}'. For EACH numbered comment "
    "output exactly one line in this format:\n"
    "N|S|LANG|SEV|CAT|WHY\n"
    "N = the comment number. S = NEG (criticism, complaint, accusation, hostility, claims of scam or fraud, "
    "insults aimed at the business), Q (a genuine question or request for information - price, address, "
    "shipping, availability - that the business should answer) or OK (praise, neutral remarks, jokes, chatter "
    "between users, harmless spam). LANG = ISO 639-1 code of the comment's language. SEV = 1..3 (1 mild, "
    "2 serious complaint, 3 hostile or accusing the business of fraud/scam; 0 for OK and Q). "
    "CAT = one word: price, delivery, quality, service, scam, other. WHY = at most 10 words, in English, "
    "and EMPTY for OK. "
    "SEV 3 is ONLY for insults, threats or accusations of fraud, scam or lies; a plain demand, wish or complaint "
    "without an insult is SEV 1 (SEV 2 when the person reports real harm, a loss or a repeated failure). "
    "A demand or wish ('bring X back', 'I want X') is NEG with SEV 1, never Q: Q needs a question the "
    "business can answer. "
    "Sarcasm counts as NEG. When unsure between NEG and OK choose OK. Output ONLY these lines."
)

DRAFT_SYSTEM = (
    "You write replies on behalf of the community manager of '{brand}' on TikTok.\n"
    "Rules: output ONLY the reply text, nothing else. Write the reply in {language}. The reply is ALWAYS in the "
    "language of the customer's comment - never in the language of these instructions, of the owner's style "
    "notes or of the company information (translate the facts if needed). "
    "Speak AS the business, in the first person plural ('we' / 'nós' / 'nosotros' / 'my'), never about the business "
    "in the third person ('they have'). "
    "Maximum 140 characters, 1-2 short sentences. Warm, polite, never defensive, "
    "no hashtags. Never invent facts, prices, dates, policies or promises: use only the company information "
    "below.\n"
    "Task: {task}\n"
    "Style instructions from the owner (they set the TONE; the task above decides what the reply must do):\n"
    "{instructions}\n"
    "Company information:\n{context}"
)

# What the reply is FOR. A complaint gets an acknowledgement; a question gets the answer itself.
TASK_NEGATIVE = ("The customer is unhappy or critical. Acknowledge the concern and apologise briefly when it fits; "
                 "never be defensive and never argue. If what the person needs is not covered by the company "
                 "information, invite them to send a direct message / contact the team.")
TASK_QUESTION = ("The customer asks a QUESTION. Use ONLY facts that are written in the company information. If those "
                 "facts do NOT contain the answer, output exactly the single word UNKNOWN and nothing else - never "
                 "guess or fill a gap (opening hours, years, prices, counts, policies, addresses are the usual gaps). "
                 "If they do contain it, answer directly in the FIRST sentence with that fact. Do not apologise - "
                 "nothing went wrong. Quote numbers EXACTLY as they are written there - never round, convert or "
                 "calculate (write 1950, not 'more than 70 years') and never add a number, year, time or price that "
                 "is not written there.")

# Sent instead of an answer the company information cannot support (the AI said UNKNOWN, or its reply contained a number
# found nowhere in the company information). The team confirms and answers by direct message. Per comment language.
CONFIRM_REPLY = {
    "pt": "Obrigado pela pergunta! Vamos confirmar essa informação. Envie-nos uma mensagem direta, por favor. \U0001F642",
    "es": "¡Gracias por tu pregunta! Vamos a confirmar esa información. Escríbenos por mensaje directo, por favor. \U0001F642",
    "en": "Thanks for your question! We'll confirm that for you. Please send us a direct message. \U0001F642",
    "pl": "Dziękujemy za pytanie! Potwierdzimy tę informację. Napisz do nas prywatną wiadomością. \U0001F642",
}

# A comment the OPERATOR asked for (/comment <link>): the AI only turns the operator's short brief into a polished comment.
LINK_DRAFT_SYSTEM = (
    "You write a TikTok comment on behalf of the community manager of '{brand}'.\n"
    "Rules: output ONLY the comment text, nothing else. Language: {language} "
    "Speak AS the business in the first person plural, never about the business in the third person. "
    "Maximum 140 characters, 1-2 short sentences, natural and warm like a real person, at most one emoji, "
    "no hashtags, no links, no @mentions unless the brief asks for one. Never invent facts, prices, dates, policies "
    "or promises: use only what the brief and the company information below say.\n"
    "Task: {task}\n"
    "Style instructions from the owner (they set the TONE):\n"
    "{instructions}\n"
    "Company information:\n{context}"
)
LINK_TASK_POST = "You are leaving a NEW comment under someone's TikTok post."
LINK_TASK_COMMENT = "You are replying to another user's comment under a TikTok post."
LINK_LANGUAGE = ("the language the brief asks for; if it names none, the language the brief itself is written in "
                 "(never the language of these instructions).")

_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


_CLOCK12 = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", re.IGNORECASE)


def _numbers(text: str) -> set:
    return {re.sub(r"[.,:]", "", n) for n in _NUMBER.findall(text or "")}


def _clock_forms(text: str) -> set:
    """'6pm' / '8:30 am' also stand for the 24-hour forms people write in Portuguese or Spanish: 18, 1800, 830, 8h30 ..."""
    out = set()
    for h, m, ap in _CLOCK12.findall(text or ""):
        hour = int(h) % 12 + (12 if ap.lower().startswith("p") else 0)
        minute = int(m) if m else 0
        out |= {str(hour), str(hour).zfill(2), f"{hour}{minute:02d}", f"{hour:02d}{minute:02d}"}
        if minute:
            out.add(str(minute))
    return out


def unsupported_numbers(reply: str, *sources: str) -> list:
    """Numbers (years, hours, prices, counts) in `reply` that appear in none of `sources` - a free, language-independent
    catch for the commonest invention. Conservative on purpose: '33 mil' vs '33,000' counts as unsupported, and the worst
    that does is send the safe 'we will confirm' reply instead of a possibly wrong fact."""
    allowed: set = set()
    for src in sources:
        allowed |= _numbers(src) | _clock_forms(src)
    return sorted(n for n in _numbers(reply) if n not in allowed)


def is_question(comment: dict) -> bool:
    """Is this comment a customer question rather than a complaint? The stored verdict wins; for a comment that has
    none (the /ai test box) a free offline guess decides, and anything with negative wording counts as a complaint."""
    status, sentiment = comment.get("status"), comment.get("sentiment")
    if status == "question" or sentiment == "Q":
        return True
    if status == "negative" or sentiment == "NEG":
        return False
    text = comment.get("text", "")
    return heuristics.looks_like_question(text) and heuristics.negative_score(text) == 0

_LINE = re.compile(
    r"^\W*(\d+)\s*[|:]\s*(NEG(?:ATIVE)?|Q(?:UESTION)?|OK)\s*[|:]\s*([A-Za-z]{2,3})\s*[|:]\s*([0-3])\s*[|:]\s*"
    r"([A-Za-z]+)\s*(?:[|:]\s*(.*))?$", re.IGNORECASE)

TIKTOK_REPLY_LIMIT = 150
_MIN_GAP_S = 3.5           # the free pool allows ~20 requests/minute


def _http_call(model: str, system: str, user: str, max_tokens: int, reasoning: dict | None = None,
               temperature: float = 0.3) -> dict:
    payload = {
        "model": model, "max_tokens": max_tokens, "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(config.OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        return {"content": None, "http": exc.code, "error": f"http {exc.code}", "body": raw[:400], "pt": 0, "ct": 0}
    except Exception as exc:  # noqa: BLE001
        return {"content": None, "http": 0, "error": str(exc)[:200], "body": "", "pt": 0, "ct": 0}
    if data.get("error"):
        err = data["error"]
        return {"content": None, "http": int(err.get("code") or 0) if str(err.get("code", "")).isdigit() else 0,
                "error": str(err.get("message"))[:200], "body": json.dumps(err)[:400], "pt": 0, "ct": 0}
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    usage = data.get("usage") or {}
    return {"content": content.strip() if content else None, "http": 200,
            "error": None if content else f"empty content (finish_reason={choice.get('finish_reason')})",
            "body": "", "pt": int(usage.get("prompt_tokens") or 0), "ct": int(usage.get("completion_tokens") or 0)}


def _fetch_key_info() -> dict | None:
    """The account's own free-tier counter (GET /key). Costs no quota. None when unavailable."""
    try:
        req = urllib.request.Request(config.OPENROUTER_URL.replace("/chat/completions", "/key"),
                                     headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (json.loads(resp.read()).get("data") or {}).get("free_model_daily_requests")
    except Exception:  # noqa: BLE001
        return None


_UNWANTED_MODEL = re.compile(r"safety|guard|code|coder|embed|rerank|vision|image|audio|tts|whisper", re.IGNORECASE)


def _fetch_free_models() -> list | None:
    """Ids of the free text models OpenRouter lists right now (public endpoint, no quota)."""
    try:
        req = urllib.request.Request(config.OPENROUTER_URL.replace("/chat/completions", "/models"),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read()).get("data") or []
    except Exception:  # noqa: BLE001
        return None
    free = [m for m in data if str(m.get("id", "")).endswith(":free")
            and "text" in ((m.get("architecture") or {}).get("output_modalities") or ["text"])]
    free.sort(key=lambda m: -(m.get("context_length") or 0))
    return [m["id"] for m in free]


def sanitize_reply(text: str) -> str:
    """One clean line that fits TikTok's 150-character comment limit."""
    t = (text or "").strip()
    t = re.sub(r"^(reply|resposta|respuesta|answer)\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
    t = t.strip().strip('"“”\'`').strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) <= TIKTOK_REPLY_LIMIT:
        return t
    cut = max(t.rfind(". ", 0, TIKTOK_REPLY_LIMIT), t.rfind("! ", 0, TIKTOK_REPLY_LIMIT),
              t.rfind("? ", 0, TIKTOK_REPLY_LIMIT))
    if cut > 40:
        return t[:cut + 1].strip()
    cut = t.rfind(" ", 0, TIKTOK_REPLY_LIMIT - 1)
    return (t[:cut] if cut > 40 else t[:TIKTOK_REPLY_LIMIT - 1]).rstrip(" ,;:-") + "…"


def parse_classification(content: str, expected: set) -> dict:
    """-> {number: {"sentiment": NEG|Q|OK, "lang", "severity", "category", "reason"}} for the lines that parse."""
    out: dict = {}
    for line in (content or "").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        n = int(m.group(1))
        if n not in expected or n in out:
            continue
        s = m.group(2).upper()
        out[n] = {"sentiment": "NEG" if s.startswith("NEG") else ("Q" if s.startswith("Q") else "OK"),
                  "lang": m.group(3).lower()[:2], "severity": int(m.group(4)),
                  "category": m.group(5).lower(), "reason": (m.group(6) or "").strip()[:160]}
    return out


class AI:
    def __init__(self, db: DB, *, transport=None, sleep=time.sleep, clock=time.monotonic,
                 key_info=None, models_info=None) -> None:
        self.db = db
        self._transport = transport or _http_call
        self._sleep, self._clock = sleep, clock
        self._key_info = _fetch_key_info if key_info is None else key_info
        self._models_info = _fetch_free_models if models_info is None else models_info
        self._cool: dict = {}
        self._last_call = -1e9
        self._remote: tuple = (-1e9, None)          # (checked_at, remaining on OpenRouter's side)
        self._lock = threading.Lock()

    # ---- budget --------------------------------------------------------------------
    def limit(self) -> int:
        try:
            return max(0, int(self.db.get_setting("ai_daily_requests", config.AI_DAILY_REQUESTS)))
        except (TypeError, ValueError):
            return config.AI_DAILY_REQUESTS

    def used(self) -> int:
        return self.db.usage_for_day()["calls"]

    def exhausted(self) -> bool:
        return self.db.meta_get("ai_exhausted_day") == utc_day()

    def remote_remaining(self) -> int | None:
        """What OpenRouter itself says is left today (cached 10 min): protects against other users of the
        same key. None when it cannot be read."""
        checked, value = self._remote
        if self._clock() - checked > 600:
            info = self._key_info()
            value = None
            if isinstance(info, dict) and info.get("remaining") is not None:
                value = max(0, int(info["remaining"]))
            self._remote = (self._clock(), value)
        return value

    def remaining(self) -> int:
        if self.exhausted():
            return 0
        local = max(0, self.limit() - self.used())
        remote = self.remote_remaining()
        return local if remote is None else min(local, remote)

    def reserve(self) -> int:
        return max(2, self.limit() // 5)

    def can(self, kind: str) -> bool:
        r = self.remaining()
        if r <= 0:
            return False
        return r > self.reserve() if kind == "classify" else True

    def budget_text(self) -> str:
        u = self.db.usage_for_day()
        remote = self.remote_remaining()
        tail = "" if remote is None else f" (OpenRouter free quota left: {remote})"
        return (f"{u['calls']}/{self.limit()} requests today"
                f"{' (daily limit reached)' if self.exhausted() else ''}, "
                f"{u['prompt_tokens'] + u['completion_tokens']} tokens{tail}")

    # ---- model list --------------------------------------------------------------------
    def chain(self) -> list:
        """The configured models, checked once per UTC day against OpenRouter's live list: vanished models are
        dropped, and a few discovered free text models are appended as extra fallbacks."""
        today = utc_day()
        if self.db.meta_get("models_checked_day") != today:
            self.db.meta_set("models_checked_day", today)
            live = self._models_info()
            if live:
                keep = [m for m in config.AI_MODEL_CHAIN if m in live]
                extras = [m for m in live if m not in keep and not _UNWANTED_MODEL.search(m)][:3]
                dropped = [m for m in config.AI_MODEL_CHAIN if m not in live]
                if dropped:
                    self.db.log_event("ai_models_dropped", ", ".join(dropped), "warn")
                if keep or extras:
                    self.db.meta_set("model_chain", json.dumps(keep + extras))
        try:
            chain = json.loads(self.db.meta_get("model_chain") or "null")
        except ValueError:
            chain = None
        return chain or list(config.AI_MODEL_CHAIN)

    def _needs_reasoning(self, model: str) -> bool:
        return self.db.meta_get(f"needs_reasoning:{model}") == "1"

    # ---- one request, with model fallback ----------------------------------------------------
    def _call(self, kind: str, system: str, user: str, max_tokens: int, temperature: float | None = None) -> tuple:
        """-> (content|None, model|None, error|None). Serialised and rate-limited.

        Reasoning is switched OFF by default: every current free model is a reasoning model and, left on,
        spends most of max_tokens thinking (seen live: 629 of 700 tokens, empty answer). A model that
        refuses ('Reasoning is mandatory') is remembered and thereafter called with reasoning on and a
        larger token budget."""
        last_error = "no model available"
        with self._lock:
            for model in self.chain():
                if self._cool.get(model, 0) > self._clock():
                    continue
                http, body = 0, ""
                for attempt in range(2):
                    if not self.can(kind):
                        return None, None, ("AI budget exhausted for today" if self.remaining() <= 0
                                            else "AI budget kept in reserve for replies")
                    wait = self._last_call + _MIN_GAP_S - self._clock()
                    if wait > 0:
                        self._sleep(wait)
                    needs = self._needs_reasoning(model)
                    extra = {} if temperature is None else {"temperature": temperature}
                    res = self._transport(model, system, user, max(max_tokens, 1800) if needs else max_tokens,
                                          {"effort": "low"} if needs else {"enabled": False}, **extra)
                    self._last_call = self._clock()
                    ok = bool(res.get("content"))
                    self.db.add_usage(model, kind, ok=ok, prompt_tokens=res.get("pt", 0),
                                      completion_tokens=res.get("ct", 0))
                    if ok:
                        return res["content"], model, None
                    http, body = res.get("http", 0), (res.get("body") or "").lower()
                    last_error = f"{model}: {res.get('error')}"
                    if http == 400 and not needs and ("mandatory" in body or "cannot be disabled" in body):
                        self.db.meta_set(f"needs_reasoning:{model}", "1")
                        continue                                   # same model again, reasoning on
                    break
                if http == 429 and ("per-day" in body or "per day" in body or "daily" in body):
                    self.db.meta_set("ai_exhausted_day", utc_day())
                    self.db.log_event("ai_daily_limit", last_error, "warn")
                    return None, None, "daily AI limit reached (OpenRouter)"
                if http in (401, 403):
                    self.db.log_event("ai_auth_error", last_error, "error")
                    return None, None, f"OpenRouter auth error ({http}) - check OPENROUTER_API_KEY"
                if http == 429:
                    self._cool[model] = self._clock() + 60
                elif http >= 500 or http == 0:
                    self._cool[model] = self._clock() + 120
                else:                                   # empty content / odd reply / 400: skip it for a while
                    self._cool[model] = self._clock() + 300
        return None, None, last_error

    # ---- classification ------------------------------------------------------------------------
    def classify_batch(self, items: list, brand: str, *, batch_size: int | None = None, progress=None) -> tuple:
        """items: [{"cid","text"}]. -> ({cid: result}, error|None). Unparsed items are simply absent.
        `batch_size`: comments per request (default AI_BATCH_SIZE). `progress(done, total)` is called after every request.
        (Requests are made one after the other on purpose - see _call - so the number of requests is what costs time.)"""
        results: dict = {}
        error = None
        size = max(1, int(batch_size or config.AI_BATCH_SIZE))
        for i in range(0, len(items), size):
            chunk = items[i:i + size]
            if not self.can("classify"):
                error = "AI budget reserved for replies / exhausted"
                break
            numbered = {n: it for n, it in enumerate(chunk, start=1)}
            user = "\n".join(f"{n}. {it['text'][:300].replace(chr(10), ' ')}" for n, it in numbered.items())
            content, _model, err = self._call("classify", CLASSIFY_SYSTEM.format(brand=brand), user, max(900, 75 * len(chunk)))
            if not content:
                error = err
                break
            parsed = parse_classification(content, set(numbered))
            for n, res in parsed.items():
                results[numbered[n]["cid"]] = res
            if progress:
                try:
                    progress(min(i + size, len(items)), len(items))
                except Exception:  # noqa: BLE001 - a screen that raises must not stop the judging
                    pass
        return results, error

    # ---- reply drafting ---------------------------------------------------------------------------
    def draft_reply(self, comment: dict, *, post_desc: str = "", previous: str | None = None,
                    kind: str = "draft") -> tuple:
        """-> (reply|None, model|None, error|None)"""
        instructions = self.db.get_setting("ai_instructions", DEFAULT_INSTRUCTIONS)
        company = self.db.get_setting("company_context", "")
        query = f"{comment.get('text', '')} {post_desc} {comment.get('category') or ''}"
        question = is_question(comment)
        # An answer needs the FACTS, so a question gets a much bigger share of the company information (a typical FAQ sheet
        # goes in whole); a complaint only needs the gist. Measured: with the small cap the 'History' and 'Locations'
        # sections were left out and the AI could only say it did not know.
        cap = config.AI_CONTEXT_CHARS_FOR_QUESTIONS if question else config.AI_CONTEXT_CHARS_PER_CALL
        selected = ctx.select_context(company, query, max_chars=cap)
        code = resolve_language(comment.get("text", ""), comment.get("lang"))
        if question and not selected:
            # No company information at all: whatever the AI said would be a guess (seen live: "yes, we ship to all of Brazil"
            # from a model that had been told nothing). The honest reply needs no AI request.
            self.db.log_event("question_unanswerable", f"{(comment.get('text') or '')[:60]!r}: no company information saved")
            return (CONFIRM_REPLY.get(code) or CONFIRM_REPLY["en"]), "rules", None
        selected = selected or "(no company information has been provided yet)"
        language = (f"{lang_name(code) or code} (language code {code})" if code
                    else "the same language as the comment")
        system = DRAFT_SYSTEM.format(brand=config.BRAND_NAME, language=language,
                                     task=TASK_QUESTION if question else TASK_NEGATIVE,
                                     instructions=instructions, context=selected)
        user = (f"Post: {(post_desc or '')[:200]}\nCategory: {comment.get('category') or 'other'}\n"
                f"Type: {'customer question' if question else 'complaint / criticism'}\n"
                f"Comment by @{comment.get('username') or 'user'}: {comment.get('text', '')[:400]}")
        if previous:
            user += f"\n\nWrite a DIFFERENT reply than this one: {previous}"
        # a factual answer must not depend on a dice roll: low randomness for questions (measured: the same question was
        # answered once and "unknown" the next time at the default 0.3)
        content, model, err = self._call(kind, system, user, 700, temperature=0.1 if question else None)
        if not content:
            return None, None, err
        reply = sanitize_reply(content)
        if question and reply:
            reason = None
            if re.sub(r"\W+", "", reply).upper().startswith("UNKNOWN"):
                reason = "the company information does not contain the answer"
            else:
                bad = unsupported_numbers(reply, company, comment.get("text", ""), post_desc)
                if bad:
                    reason = f"the answer contained numbers not in the company information ({', '.join(bad)})"
            if reason:                                    # never send a guess: the standard 'we will confirm' reply instead
                self.db.log_event("question_unanswerable", f"{(comment.get('text') or '')[:60]!r}: {reason}")
                reply = CONFIRM_REPLY.get(code, CONFIRM_REPLY["en"]) if code else CONFIRM_REPLY["en"]
        return (reply or None), model, (None if reply else "empty reply after cleaning")

    def draft_link_comment(self, brief: str, *, target: str = "post", previous: str | None = None) -> tuple:
        """Turn the operator's brief ("thank them and invite them to the store") into a comment for a link the operator
        sent. `target` is "post" (new comment) or "comment" (a reply). -> (text|None, model|None, error|None)"""
        instructions = self.db.get_setting("ai_instructions", DEFAULT_INSTRUCTIONS)
        company = self.db.get_setting("company_context", "")
        selected = ctx.select_context(company, brief, max_chars=config.AI_CONTEXT_CHARS_PER_CALL) \
            or "(no company information has been provided yet)"
        system = LINK_DRAFT_SYSTEM.format(brand=config.BRAND_NAME, language=LINK_LANGUAGE,
                                          task=LINK_TASK_COMMENT if target == "comment" else LINK_TASK_POST,
                                          instructions=instructions, context=selected)
        user = f"Brief from the operator: {(brief or '')[:500]}"
        if previous:
            user += f"\n\nWrite a DIFFERENT comment than this one: {previous}"
        content, model, err = self._call("draft", system, user, 700)
        if not content:
            return None, None, err
        text = sanitize_reply(content)
        return (text or None), model, (None if text else "empty comment after cleaning")
