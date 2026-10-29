"""Company-context handling: store a (possibly long) text, send only the relevant parts to the AI.

The owner can paste a whole FAQ / policy sheet through /ai_context. Sending all of it with
every reply would burn the token budget, so it is split into chunks and only the chunks that
share words with the comment (plus the opening 'about us' chunk) are included, up to a
character cap per call. No embeddings, no extra AI calls - plain word overlap.
"""
import re
import unicodedata

_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
_STOP = frozenset("""para com que uma uns umas por nos nas dos das mais como mas nao sim esta este esse essa isso
the and for you your with that this have are was were not but from they them del las los una unos por con que
sus pero como mas muy jest sie nie dla czy jak ale oraz tak ten tej tego jestem bardzo""".split())


_FOLD_EXTRA = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d"})


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_FOLD_EXTRA))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _stem(word: str) -> str:
    return word[:6]          # crude, language-agnostic: 'entregas'/'entrega'/'entregue' share a stem


# The owner may write the company text in one language while customers comment in another (Portuguese /
# Spanish / English). Plain word overlap cannot bridge that, so the commonest customer topics are mapped to
# language-independent concepts: 'horario', 'hours' and 'abre' all become '@hours'. Word-start matching.
_CONCEPTS = {
    "hours": ("horari", "hours", "abert", "abiert", "funcion", "abre", "fecha", "open", "opening", "cierr",
              "sabado", "domingo", "saturday", "sunday", "segunda", "monday",
              "godzin", "otwart", "czynn", "zamkn", "niedziel", "sobot", "poniedzial"),
    "address": ("endere", "direcc", "address", "local", "onde", "donde", "where", "mapa", "location", "ubica",
                "chegar", "como ir", "cidade", "ciudad", "city", "estaciona", "parking",
                "adres", "gdzie", "lokaliz", "dojazd", "miasto"),
    "shipping": ("entreg", "envio", "frete", "shipp", "deliver", "rastre", "correio", "transport", "flete",
                 "encomenda", "motoboy", "despacho", "wysylk", "przesylk", "paczk", "kurier"),
    "price": ("preco", "preci", "valor", "price", "custo", "cost", "caro", "barato", "descont", "descuent",
              "discount", "promo", "oferta", "offer", "cena", "koszt", "zniz", "promoc", "rabat"),
    "returns": ("troca", "devol", "return", "refund", "reembols", "estorno", "garant", "warrant", "exchang",
                "cambio", "defeito", "defect", "zwrot", "reklamacj", "wymian", "gwarancj"),
    "payment": ("pagam", "pagar", "pago", "payment", "pix", "cartao", "tarjeta", "card", "parcel", "cuota",
                "boleto", "credit", "debito", "dinheiro", "dolar", "dollar", "platnosc", "zaplac", "karta", "karty",
                "gotowk"),
    "stock": ("estoque", "stock", "dispon", "availab", "tienen", "have you", "tem o", "tem a", "dostepn",
              "magazyn"),
    "contact": ("contato", "whatsapp", "telefone", "telefono", "phone", "contact", "email", "instagram", "falar",
                "kontakt", "telefon", "mail"),
    "documents": ("document", "passaporte", "passport", "cpf", "visto", "fronteira", "frontera", "border",
                  "aduana", "alfandega", "customs", "cota", "quota", "limite"),
    "stores": ("loja", "tienda", "store", "shop", "sklep", "filial", "sucursal", "unidade", "mall", "locations",
               "central city", "northtown", "riverside"),
    "history": ("histor", "fund", "found", "since", "desde", "antig", "ancien", "year", "anos", "tradic", "generation",
                "geracao", "rok", "lat ", "zalozon", "tempo", "quando"),
}


def tokens(text: str) -> set:
    folded = _fold(text)
    words = _WORD.findall(folded)
    out = {_stem(w) for w in words if w not in _STOP}
    for concept, keys in _CONCEPTS.items():
        # word-START match ('open' must not fire inside 'unopened'); keys with a space match as a phrase
        if any((k in folded) if " " in k else any(w.startswith(k) for w in words) for k in keys):
            out.add(f"@{concept}")
    return out


def split_chunks(context: str, *, max_chunk: int = 600) -> list:
    """Paragraph-based chunks; over-long paragraphs are cut at sentence/space boundaries."""
    chunks = []
    for para in re.split(r"\n\s*\n", (context or "").strip()):
        para = para.strip()
        while len(para) > max_chunk:
            cut = max(para.rfind(". ", 0, max_chunk), para.rfind("\n", 0, max_chunk), para.rfind(" ", 0, max_chunk))
            cut = cut if cut > max_chunk // 3 else max_chunk
            rest = para[cut + 1:].strip()
            if len(rest) < max_chunk // 4:                 # a stray tail ("to Brazil.") stays with its section
                break
            chunks.append(para[:cut + 1].strip())
            para = rest
        if para:
            chunks.append(para)
    return chunks


def select_context(context: str, query: str, *, max_chars: int) -> str:
    """The first chunk (who the company is) + the best-matching chunks for `query`, <= max_chars."""
    chunks = split_chunks(context)
    if not chunks:
        return ""
    if len(context) <= max_chars:
        return context.strip()
    q = tokens(query)
    scored = sorted(((len(q & tokens(c)), -i, i) for i, c in enumerate(chunks[1:], start=1)), reverse=True)
    nothing_matched = bool(scored) and scored[0][0] == 0
    if nothing_matched:
        # different language / unusual topic: better the opening sections in document order than nothing
        scored = [(0, -i, i) for i in range(1, len(chunks))]
    picked = [0]
    used = len(chunks[0])
    for score, _neg, i in scored:
        if score == 0 and not nothing_matched:
            break
        if used + len(chunks[i]) + 2 > max_chars:
            continue
        picked.append(i)
        used += len(chunks[i]) + 2
    if used > max_chars:                       # even the first chunk alone is too long
        return chunks[0][:max_chars]
    return "\n\n".join(chunks[i] for i in sorted(picked))
