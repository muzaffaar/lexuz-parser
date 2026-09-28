"""Language / script detection for lex.uz documents (uz-Cyrl, uz-Latn, ru, en).

The site's legal-analysis card states the language authoritatively but only ~7% of the archive has
it so far; the text heuristic is the fallback. Both report `source` so downstream code can tell.
"""
import re
from dataclasses import dataclass

_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")
_UZ_CYR_SPECIFIC = re.compile(r"[ЎўҚқҒғҲҳ]")
_RU_MARKERS = re.compile(r"[ЩщЫыЁё]")
_WORD = re.compile(r"[a-z]+")

_EN_STOP = {"the", "of", "and", "to", "on", "in", "for", "a", "is", "be", "shall", "by", "with", "as", "measures", "approval"}
_UZ_LAT_MARKERS = re.compile(r"(?:[oOgG][ʻʼ’‘'`]|\b(?:va|uchun|bilan|toʻgʻrisida|to'g'risida|qaror|farmon|buyruq|son|yil)\b)", re.I)


@dataclass(frozen=True)
class LanguageGuess:
    language: str  # "uz" | "ru" | "en" | ""
    script: str    # "cyrl" | "latn" | ""
    source: str    # "card" | "detected"
    confident: bool


def from_card_value(value: str) -> LanguageGuess | None:
    """Parse the card's 'Document language' value, e.g. 'Узбекский (к)', 'Русский', 'Английский'."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if "узбек" in v or "ўзбек" in v or "uzbek" in v or "o'zbek" in v or "oʻzbek" in v:
        if re.search(r"\((?:к|k)\)|кирил|kiril|cyril", v):
            return LanguageGuess("uz", "cyrl", "card", True)
        if re.search(r"\((?:л|l)\)|лотин|latin|lotin", v):
            return LanguageGuess("uz", "latn", "card", True)
        return LanguageGuess("uz", "", "card", True)
    if "рус" in v or "russian" in v or "rus" in v:
        return LanguageGuess("ru", "cyrl", "card", True)
    if "англ" in v or "english" in v or "ingliz" in v:
        return LanguageGuess("en", "latn", "card", True)
    return None


def detect(text: str) -> LanguageGuess:
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    total = cyr + lat
    if total == 0:
        return LanguageGuess("", "", "detected", False)

    if cyr >= lat:
        uz_specific = len(_UZ_CYR_SPECIFIC.findall(text))
        if uz_specific >= 3 or uz_specific / max(cyr, 1) >= 0.005:
            return LanguageGuess("uz", "cyrl", "detected", True)
        if total < 60:
            return LanguageGuess("", "cyrl", "detected", False)  # too short to separate uz from ru
        if _RU_MARKERS.search(text) or uz_specific == 0:
            return LanguageGuess("ru", "cyrl", "detected", True)
        return LanguageGuess("uz", "cyrl", "detected", False)

    words = _WORD.findall(text.lower())
    en_share = sum(w in _EN_STOP for w in words) / max(len(words), 1)
    if en_share >= 0.12 and not _UZ_LAT_MARKERS.search(text):
        return LanguageGuess("en", "latn", "detected", len(words) >= 6)
    if _UZ_LAT_MARKERS.search(text):
        return LanguageGuess("uz", "latn", "detected", True)
    return LanguageGuess("en" if en_share >= 0.12 else "uz", "latn", "detected", False)
