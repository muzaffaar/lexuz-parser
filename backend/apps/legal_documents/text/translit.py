"""Uzbek Cyrillic -> Latin folding, used to build ONE search key space across both scripts.

Every law exists in Uzbek-Cyrillic and Uzbek-Latin. Folding Cyrillic to Latin (and dropping
apostrophes) lets a single full-text index and a single document-number key serve both scripts.
It is a *matching* transform, not a publishable transliteration: it favours agreeing with how
the Latin edition of the same word is written, and is intentionally lossy.
"""
import re

_SIMPLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "э": "e",
    "ю": "yu", "я": "ya", "ё": "yo", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h", "ы": "i",
    "ъ": "", "ь": "", "ҷ": "j", "ӯ": "o",
}
_VOWELS = set("аеёиоуўэюяы")
_E_RE = re.compile(r"е")


def cyrillic_to_latin(text: str) -> str:
    """Lowercases and transliterates. Non-Cyrillic characters pass through unchanged."""
    text = text.lower()
    out = []
    prev = " "
    for ch in text:
        if ch == "е":
            # word-initial or after a vowel/sign, Latin uses "ye" (e.g. "етказиш" -> "yetkazish")
            out.append("ye" if (not prev.isalpha() or prev in _VOWELS or prev in "ъь") else "e")
        else:
            out.append(_SIMPLE.get(ch, ch))
        prev = ch
    return "".join(out)


_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))
