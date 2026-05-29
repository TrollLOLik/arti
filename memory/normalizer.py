import re


_ENTITY_CLEAN_RE = re.compile(r"[^0-9a-zа-яё _.-]+", re.IGNORECASE)
_SPACES_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_+-]{3,}")
_STOP_WORDS = {
    "это", "что", "как", "когда", "где", "куда", "почему", "зачем", "если", "или", "для",
    "про", "при", "без", "над", "под", "она", "они", "оно", "его", "её", "мне", "тебе",
    "меня", "тебя", "себя", "тут", "там", "уже", "еще", "ещё", "был", "была", "были",
    "будет", "буду", "есть", "нет", "можно", "нужно", "надо", "арти", "пользователь",
}


def normalize_entity_name(value: str) -> str:
    text = (value or "").strip().lower().replace("ё", "е")
    text = _ENTITY_CLEAN_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text).strip()
    return text[:160]


def keyword_query(text: str, limit: int = 10) -> str:
    words = []
    seen = set()
    for match in _WORD_RE.findall(text or ""):
        word = normalize_entity_name(match)
        if not word or word in _STOP_WORDS or word in seen:
            continue
        seen.add(word)
        words.append(word)
        if len(words) >= limit:
            break
    return " ".join(words)


def compact_text(text: str, limit: int = 1200) -> str:
    clean = _SPACES_RE.sub(" ", (text or "").strip())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "..."
