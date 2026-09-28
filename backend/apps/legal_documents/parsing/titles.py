"""Title / number / date / URL parsing for lex.uz."""
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlsplit

from ..text.normalize import clean_text
from ..text.translit import cyrillic_to_latin

_TITLE_RE = re.compile(
    r"^\s*(?P<number>.+?)\s*-\s*(?:сон|son|№)\s+(?P<date>\d{2}\.\d{2}\.\d{4})\s*\.?\s*(?P<title>.*)$",
    re.S | re.I,
)
# International treaties/agreements and some laws have NO number, only a leading date:
#   "04.11.1997. Xalqaro avtomobil aloqasi toʻgʻrisida"  (61% of the archive at the time this was found)
_DATE_ONLY_TITLE_RE = re.compile(r"^\s*(?P<date>\d{2}\.\d{2}\.\d{4})\s*\.\s*(?P<title>.+)$", re.S)
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@dataclass(frozen=True)
class ParsedTitle:
    number: str
    date: date | None
    title: str


def parse_date(value: str | None) -> date | None:
    """dd.mm.yyyy (optionally followed by a time / '00') -> date. Invalid dates -> None."""
    if not value:
        return None
    m = _DATE_RE.search(value)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def parse_title(raw: str) -> ParsedTitle:
    """'PQ-330-сон 17.09.2026. “Title”' -> number 'PQ-330', date 2026-09-17, title '“Title”'."""
    raw = clean_text(raw or "").strip()
    m = _TITLE_RE.match(raw)
    if m:
        return ParsedTitle(m["number"].strip(), parse_date(m["date"]), m["title"].strip())
    m = _DATE_ONLY_TITLE_RE.match(raw)
    if m and parse_date(m["date"]):
        return ParsedTitle("", parse_date(m["date"]), m["title"].strip())
    return ParsedTitle("", None, raw)


def number_key(number: str) -> str:
    """Script-folded key so 'ПҚ-330', 'PQ-330' and 'pq-330' match (TZ 81: exact number search)."""
    key = cyrillic_to_latin(number)
    key = re.sub(r"[^a-z0-9]+", "-", key.lower())
    return key.strip("-")


_QUERY_SKIP = {"type", "action", "ondate2", "otherlang"}  # word export, comparison views


@dataclass(frozen=True)
class DocLink:
    external_id: str
    edition_on: date | None
    anchor: str


def parse_document_link(url: str, base: str = "https://lex.uz/") -> DocLink | None:
    """A link to a lex.uz *text* document (not PDF/passport/export/comparison views), else None."""
    from urllib.parse import urljoin

    parts = urlsplit(urljoin(base, url.strip()))
    if parts.hostname not in ("lex.uz", "www.lex.uz"):
        return None
    m = re.fullmatch(r"/(?:(?:uz|ru|en)/)?docs/(-?\d+)/?", parts.path)
    if not m:
        return None
    query = {k.lower(): v for k, v in parse_qsl(parts.query)}
    if _QUERY_SKIP & query.keys():
        return None
    return DocLink(m.group(1), parse_date(query.get("ondate")), parts.fragment.strip())


def external_id_and_edition(url: str) -> tuple[str, date | None]:
    link = parse_document_link(url)
    if link is None:
        raise ValueError(f"Not a lex.uz document URL: {url!r}")
    return link.external_id, link.edition_on
