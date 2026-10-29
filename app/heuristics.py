"""Cheap, offline comment screening - the first line of defence for the AI budget.

Most comments on a brand page are emoji, tags, laughter or one-word praise. They are
settled here for free. Anything unclear goes on to the AI. Strongly negative wording is
only ever *flagged* here (never auto-decided) - except as a last-resort fallback when
the AI has no budget left, so a real complaint still reaches a human.
"""
import re
import unicodedata


_FOLD_EXTRA = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d"})   # NFKD does not decompose these


def _fold(text: str) -> str:
    """lowercase + strip accents so 'péssimo' == 'pessimo' == 'PESSIMO' (and Polish 'ł' -> 'l')."""
    text = unicodedata.normalize("NFKD", (text or "").translate(_FOLD_EXTRA))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


_PRAISE = (
    # pt
    "top", "lindo", "linda", "amei", "adorei", "amo ", "maravilh", "parabens", "obrigad", "perfeit", "show",
    "gostei", "excelente", "otimo", "demais", "incrivel", "sensacional", "fantastic", "uau", "melhor",
    "recomendo", "sucesso", "abencoad", "arrasou", "chic", "maravilha", "bom dia", "boa tarde", "boa noite",
    # es
    "hermoso", "me encanta", "genial", "gracias", "felicidades", "bravo", "buenisimo", "perfecto", "increible",
    "exito", "saludos", "bendiciones", "precioso",
    # en
    "great", "nice", "love", "awesome", "amazing", "thanks", "thank you", "perfect", "beautiful", "cool",
    "wow", "good",
    # pl
    "super", "swietn", "polecam", "dziekuj", "piekn", "wspanial", "doskonal", "fajn", "rewelac", "genialn",
    "brawo", "gratulac", "uwielbiam", "kocham", "cudown", "najlepsz",
)

_NEGATIVE = (
    # pt
    "golpe", "golpista", "fraude", "enganad", "enganacao", "mentira", "mentiroso", "roubo", "roubar", "ladr",
    "lixo", "pessimo", "horrivel", "nunca mais", "nao recomendo", "decepcion", "procon", "reclame aqui",
    "processo", "absurdo", "vergonha", "desrespeit", "abusivo", "nao chegou", "nao recebi", "demora",
    "atras", "falsificad", "pirata", "replica", "falso", "enganou", "sem resposta", "ninguem responde",
    "cancelar", "reembolso", "devolucao", "estorno", "ruim", "caro demais", "muito caro", "muito ruim",
    "descaso", "mau atendimento", "atendimento ruim", "propaganda enganosa",
    # es
    "estafa", "estafador", "engano", "timo", "malisimo", "nunca mas", "no recomiendo", "robo", "terrible",
    "pesimo", "mentiroso", "falsificado", "reclamo", "devolucion",
    # en
    "scam", "fraud", "worst", "never again", "rip off", "ripoff", "cheat", "terrible", "awful", "horrible",
    "fake", "refund",
    # pl
    "oszust", "oszustw", "oszukan", "kradziez", "zlodziej", "tragedi", "okropn", "beznadziejn", "nigdy wiecej",
    "nie polecam", "fatalny", "fatalna", "skandal", "wstyd", "niezadowol", "zwrot pieniedzy",
    "nie dotarl", "nie dostal", "opoznien", "kiepsk", "nikt nie odpowiada", "reklamacj", "naciag", "falszyw",
    "podrobk", "zero kontaktu", "bubel",
)

_HARD_SCAM = ("golpe", "golpista", "fraude", "ladr", "roubo", "roubar", "estafa", "estafador", "scam", "fraud",
              "enganacao", "enganou", "propaganda enganosa", "timo", "robo", "oszust", "oszustw", "oszukan",
              "kradziez", "zlodziej", "naciag")
_DELIVERY = ("chegou", "recebi", "entrega", "demora", "atras", "envio", "rastre", "pedido", "llego", "pedido",
             "delivery", "shipping", "dostaw", "przesylk", "paczk", "zamowien", "wysylk")
_PRICE = ("caro", "preco", "valor", "precio", "price", "expensive", "barato", "cena", "koszt", "drogo", "tanio")

_LAUGH = re.compile(r"^(k{2,}|(ha){2,}h?|(he){2,}|(rs)+|(kk)+|lol|lmao|ahah+)+$")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def letters(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isalpha())


def strip_mentions(text: str) -> str:
    return re.sub(r"@[\w.]+", " ", text or "").strip()


def negative_score(text: str) -> int:
    t = _fold(text)
    return sum(1 for w in _NEGATIVE if w in t)


def is_hard_scam(text: str) -> bool:
    t = _fold(text)
    return any(w in t for w in _HARD_SCAM)


def guess_category(text: str) -> str:
    t = _fold(text)
    if any(w in t for w in _HARD_SCAM):
        return "scam"
    if any(w in t for w in _DELIVERY):
        return "delivery"
    if any(w in t for w in _PRICE):
        return "price"
    return "other"


# Words a question typically STARTS with (pt / es / en / pl). Matched only at the start of the comment or of a
# sentence, so "Amei, como sempre!" (praise) is not mistaken for a question. A "?" / "¿" anywhere also counts.
_ASKING = (
    "qual", "quais", "quanto", "quanta", "quando", "onde", "como", "quem", "porque", "por que", "tem ", "tem o", "tem a",
    "faz ", "fazem", "aceita", "voces", "posso", "podem", "pode ", "existe", "ha ", "vende", "entreg", "abre", "fecha",
    "cual", "cuales", "cuanto", "cuanta", "cuando", "donde", "quien", "tienen", "tiene", "hacen", "aceptan", "puedo",
    "pueden", "hay ", "venden", "que horario", "a que hora", "que hora",
    "what", "where", "when", "how", "who", "which", "do you", "does ", "can i", "can you", "is there", "are there",
    "will you", "is it", "are you",
    "czy ", "jak ", "gdzie", "kiedy", "ile ", "jaki", "jaka", "jakie", "kto ", "dlaczego", "czemu", "mozna",
)
_GREETING = re.compile(r"^(?:oi|ola|hola|hi|hello|ei|bom dia|boa tarde|boa noite|buenas)\b[\s,!.]*")


def looks_like_question(text: str) -> bool:
    """A free, offline guess whether a comment asks something: a question mark, or a typical opening word. Used to keep
    questions away from the 'short praise' shortcut and as the fallback when the AI cannot be asked. The opening-word
    test applies at the start of the comment or of a sentence only, so 'Amei, como sempre!' or 'Que lindo!' (praise)
    are not mistaken for questions."""
    raw = strip_mentions(text or "")
    if not raw:
        return False
    if "?" in raw or "\u00bf" in raw:
        return True
    folded = _fold(raw)
    for sentence in re.split(r"(?<=[.!;])\s+", folded):
        s = _GREETING.sub("", sentence.strip())
        if any(s.startswith(w) for w in _ASKING):
            return True
    return False


def screen(text: str) -> dict | None:
    """Settle obviously harmless comments without the AI.

    Returns {"sentiment": "OK", "source": "heuristic", "reason": ...} or None (= ask the AI).
    """
    raw = (text or "").strip()
    if not raw:
        return {"sentiment": "OK", "source": "heuristic", "reason": "empty"}
    core = strip_mentions(raw)
    if letters(core) == 0:
        return {"sentiment": "OK", "source": "heuristic", "reason": "emoji/mention only"}
    words = [w for w in _WORD.findall(_fold(core)) if w]
    if words and all(_LAUGH.match(w) or len(w) <= 2 for w in words) and len(words) <= 4:
        return {"sentiment": "OK", "source": "heuristic", "reason": "laughter / filler"}
    if "?" in raw or negative_score(raw) or looks_like_question(raw):
        return None
    folded = _fold(core)
    if len(core) <= 60 and any(p in folded for p in _PRAISE):
        return {"sentiment": "OK", "source": "heuristic", "reason": "short praise"}
    return None


def fallback_classify(text: str) -> dict | None:
    """Used ONLY when the AI cannot be asked (no budget / all models failing): a strongly negative
    wording is flagged for a human rather than silently dropped. Everything else returns None."""
    if negative_score(text) < 1:
        if looks_like_question(text):                        # a customer asking something still reaches a human
            return {"sentiment": "Q", "source": "fallback", "lang": None, "severity": 0,
                    "category": guess_category(text), "reason": "looks like a question (AI unavailable)"}
        return None
    return {"sentiment": "NEG", "source": "fallback", "lang": None,
            "severity": 3 if is_hard_scam(text) else 2, "category": guess_category(text),
            "reason": "flagged by keywords (AI unavailable)"}
