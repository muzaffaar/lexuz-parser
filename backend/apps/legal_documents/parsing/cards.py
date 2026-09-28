"""Metadata cards (passport, legal-analysis card1). Optional enrichment: only a minority of the
archive has them, and every value they give has a text-derived fallback.

card1 is a label/value table whose *labels* follow the site UI language while many *values*
(status, type) are always Russian. We match labels across languages and fall back to the fixed row
order of the template when a label is unknown.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

from ..constants import DocumentStatus
from ..text.normalize import _cell_text, clean_text
from .titles import parse_date


def _key(label: str) -> str:
    return re.sub(r"[^\w]+", "", label.lower())


# normalised label -> field. Cyrillic-Uzbek + Russian verified against real card1 pages;
# Latin-Uzbek / English are best-effort (unknown labels simply fall back to position).
_LABELS = {
    "ҳужжаттури": "doc_type", "hujjatturi": "doc_type", "типдокумента": "doc_type", "видакта": "doc_type", "documenttype": "doc_type",
    "ҳужжатшакли": "doc_form", "hujjatshakli": "doc_form", "формадокумента": "doc_form", "documentform": "doc_form",
    "ҳужжаттили": "language", "hujjattili": "language", "языкдокумента": "language", "documentlanguage": "language",
    "ҳужжатҳолати": "status", "hujjatholati": "status", "статусдокумента": "status", "состояниедокумента": "status", "documentstatus": "status",
    "кучгакиришсанаси": "effective_from", "kuchgakirishsanasi": "effective_from", "датавступлениявсилу": "effective_from",
    "кучинийўқотгансанаси": "effective_to", "kuchiniyoqotgansanasi": "effective_to", "датаутратысилы": "effective_to",
    "кучинийўқотишсабаби": "lost_reason", "причинаутратысилы": "lost_reason",
    "амалқилишмуддати": "validity", "срокдействия": "validity",
    "меъёрийликхарактери": "normativity", "характернормативности": "normativity",
    "давлатрўйхатрақами": "state_reg_no",
    "давлатрўйхатиданўтгансанаси": "state_reg_date",
}

# fixed order of label cells in the card1 template (21 cells)
_POSITIONAL = [
    "name", "doc_type", "doc_form", "state_reg_no", "state_reg_date", "language", "moj_reg_no", "moj_reg_date",
    "status", "effective_from", "validity", "effective_to", "lost_reason", "normativity", "source",
    "source_edition_no", "source_published_at", "source_article", "source_permit_date", "used_for_effective", "developer",
]


@dataclass
class CardInfo:
    """Document attributes a card can supply. Every field is optional."""

    document_type: str = ""
    document_form: str = ""
    language_value: str = ""
    status: str = DocumentStatus.UNKNOWN
    status_raw: str = ""
    effective_from: date | None = None
    effective_to: date | None = None
    published_at: date | None = None
    adopted_at: date | None = None
    authority: str = ""
    document_number: str = ""
    requisites: str = ""
    raw_pairs: list = field(default_factory=list)


def map_status(raw: str) -> str:
    v = (raw or "").strip().lower()
    if not v:
        return DocumentStatus.UNKNOWN
    if re.search(r"не\s+вступил|kuchga\s+kirmagan|кучга\s+кирмаган|not\s+yet", v):
        return DocumentStatus.NOT_YET_EFFECTIVE
    if re.search(r"утрат|бекор|bekor|йўқот|yo.?qot|отмен|expired|repealed|lost|прекрат", v):
        return DocumentStatus.EXPIRED
    if v.startswith(("действ", "амалда", "amalda", "in force", "active", "valid", "кучда", "kuchda")):
        return DocumentStatus.ACTIVE
    return DocumentStatus.UNKNOWN


def _pairs(soup) -> list[tuple[str, str]]:
    pairs = []
    for lbl in soup.select("td.lbl"):
        value_cell = lbl.find_next_sibling("td")
        value = ""
        if value_cell is not None and "lbl" not in (value_cell.get("class") or []):
            value = clean_text(_cell_text(value_cell)).strip()
        pairs.append((clean_text(_cell_text(lbl)).strip(), value))
    return pairs


def parse_card1(html: bytes | str) -> CardInfo:
    soup = BeautifulSoup(html, "lxml")
    pairs = _pairs(soup)
    positional_ok = len(pairs) == len(_POSITIONAL)
    values: dict[str, str] = {}
    for i, (label, value) in enumerate(pairs):
        name = _LABELS.get(_key(label)) or (_POSITIONAL[i] if positional_ok else None)
        if name and name not in values:
            values[name] = value

    info = CardInfo(raw_pairs=pairs)
    info.document_type = values.get("doc_type", "")
    info.document_form = values.get("doc_form", "")
    info.language_value = values.get("language", "")
    info.status_raw = values.get("status", "")
    info.status = map_status(info.status_raw)
    info.effective_from = parse_date(values.get("effective_from"))
    info.effective_to = parse_date(values.get("effective_to"))
    info.published_at = parse_date(values.get("source_published_at"))

    # adopting bodies table: [n, body, signer post, signer name, adopted date, number, place]
    body_table = soup.select_one("table#bodyTab tbody")
    if body_table is not None:
        rows = [[clean_text(_cell_text(td)).strip() for td in tr.find_all("td", recursive=False)] for tr in body_table.find_all("tr", recursive=False)]
        rows = [r for r in rows if len(r) >= 6]
        if rows:
            info.authority = "; ".join(dict.fromkeys(r[1] for r in rows if r[1]))
            info.adopted_at = parse_date(rows[0][4])
            info.document_number = rows[0][5]
    return info


_FORM_WORDS = {
    "қарори", "қарор", "қонуни", "қонун", "фармони", "фармон", "буйруғи", "буйруқ", "низоми", "низом",
    "qarori", "qaror", "qonuni", "qonun", "farmoni", "farmon", "buyrugʻi", "buyruq", "nizomi", "nizom",
    "постановление", "закон", "указ", "приказ", "распоряжение", "решение",
}
_REQ_RE = re.compile(r"^(?P<who>.+?),\s*(?P<date>\d{2}\.\d{2}\.\d{4})\s*(?:йилдаги|yildagi|г\.?|от)?\s*(?P<number>\S+?)(?:-(?:сон|son))?\s*$", re.I)


def parse_passport(html: bytes | str) -> CardInfo:
    """Passport page: title + one 'requisites' line such as
    'Ўзбекистон Республикаси Вазирлар Маҳкамасининг қарори, 22.09.2026 йилдаги 508-сон'."""
    soup = BeautifulSoup(html, "lxml")
    info = CardInfo()
    sub = soup.select_one(".lx_lp_subtitle")
    if sub is None:
        return info
    info.requisites = clean_text(sub.get_text(" ", strip=True))
    m = _REQ_RE.match(info.requisites)
    if m:
        who = m["who"].strip()
        words = who.split()
        if len(words) > 1 and words[-1].lower() in _FORM_WORDS:
            info.authority = " ".join(words[:-1])
            info.document_form = words[-1]
        else:
            info.authority = who
        info.adopted_at = parse_date(m["date"])
        info.document_number = m["number"]
    return info
