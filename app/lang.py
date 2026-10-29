"""Which language a reply must be written in: always the language of the customer's comment
(Polish -> Polish, Spanish -> Spanish, Portuguese -> Portuguese, ...).

The AI labels each comment's language during classification; this module double-checks that label
offline, because a wrong label would make the reply come out in the wrong language. Strong evidence
(Polish letters, Spanish inverted punctuation, a handful of typical words) beats the AI's label;
weak evidence never does. Short comments that are ambiguous between Portuguese and Spanish keep the AI's label.
"""
import re
import unicodedata

LANG_NAMES = {
    "pt": "Portuguese", "es": "Spanish", "pl": "Polish", "en": "English", "fr": "French", "de": "German",
    "it": "Italian", "nl": "Dutch", "ro": "Romanian", "cs": "Czech", "sk": "Slovak", "hu": "Hungarian",
    "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "bn": "Bengali",
    "id": "Indonesian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ca": "Catalan",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_FOLD_EXTRA = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d"})   # NFKD does not decompose these


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_FOLD_EXTRA))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


# whole-word cues (accent-folded); unique-to-the-language characters are checked separately below
_WORDS = {
    "pl": {"nie", "jest", "sie", "ze", "jak", "czy", "ale", "dla", "bardzo", "dziekuje", "prosze", "tak", "ile",
           "kiedy", "gdzie", "cena", "polecam", "super", "dzien", "dobry", "paczka", "zamowienie", "wszystko",
           "mam", "chce", "mozna", "jestem", "bylo", "dobrze", "nigdy", "wiecej", "pieniadze", "sklep",
           "ktorej", "godzinie", "otwieracie", "macie", "kosztuje", "witam", "pozdrawiam", "dostawa", "wysylka",
           "mozecie", "czynne", "otwarte", "ktora", "ktory", "mnie", "teraz", "tutaj", "jeszcze"},
    "es": {"el", "los", "las", "que", "por", "para", "con", "una", "muy", "pero", "gracias", "cuanto", "donde",
           "precio", "hola", "buenas", "tienen", "tambien", "esta", "estoy", "quiero", "nunca", "compre",
           "producto", "envio", "cuando", "como", "tiene", "mas", "son", "del", "hay", "puedo", "buenos",
           # words that differ from Portuguese
           "cuesta", "cuestan", "ustedes", "hacen", "aceptan", "cual", "cuales", "envian", "dinero", "tambien"},
    "pt": {"nao", "voce", "voces", "obrigado", "obrigada", "uma", "com", "para", "muito", "mas", "os", "tem",
           "quero", "comprei", "produto", "entrega", "bom", "dia", "cade", "onde", "qual", "horario", "pra",
           "ate", "mais", "sao", "tambem", "aqui", "aceitam", "faz", "fazem", "estacionamento",
           # words that differ from Spanish
           "quanto", "custa", "custam", "quais", "preco", "brasil", "dinheiro", "obrigadissimo"},
    "en": {"the", "and", "you", "are", "this", "that", "with", "for", "not", "very", "thanks", "please", "what",
           "where", "how", "price", "have", "your", "can", "does", "do", "is", "it", "my", "order", "never"},
}
# characters that (among these languages) essentially identify one of them
_PL_DIGRAPHS = ("cz", "sz", "rz", "dz")
_CHARS = {"pl": "ąćęłńśźż", "es": "ñ¿¡", "pt": "ãõçâêô"}


def guess_language(text: str) -> tuple:
    """-> (code or None, score). Score >= 3 is strong evidence; 0 means 'no idea'."""
    raw = text or ""
    scores = {k: 0 for k in _WORDS}
    for lang, chars in _CHARS.items():
        scores[lang] += 3 * sum(1 for ch in raw.lower() if ch in chars)
    words = _WORD.findall(_fold(raw))
    for w in set(words):
        matched = False
        for lang, vocab in _WORDS.items():
            if w in vocab:
                scores[lang] += 1
                matched = True
        if not matched and len(w) >= 4 and any(d in w for d in _PL_DIGRAPHS):
            scores["pl"] += 1                      # cz / sz / rz / dz inside a word: typically Polish here
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, top), (_second, runner) = ranked[0], ranked[1]
    if top <= 0 or top == runner:
        return None, 0
    return best, top - runner


def resolve_language(text: str, ai_code) -> str | None:
    """The language code a reply to `text` must use. The AI's label wins unless the offline cues clearly
    contradict it."""
    ai = (str(ai_code or "").strip().lower()[:2]) or None
    if ai not in LANG_NAMES:
        ai = None
    guess, margin = guess_language(text)
    if guess and margin >= 3 and guess != ai:
        return guess
    if ai:
        return ai
    return guess if guess and margin >= 2 else None


def lang_name(code) -> str | None:
    return LANG_NAMES.get(str(code or "").lower()[:2])
