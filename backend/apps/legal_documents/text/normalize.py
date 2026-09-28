"""Text extraction and normalisation.

Two different outputs exist on purpose:
  * `html_to_text`     - the VERBATIM text stored in sections/chunks (what a citation must match).
  * `normalize_search` - a lossy key space for full-text search (folded apostrophes/scripts).
"""
import re
import unicodedata

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .translit import cyrillic_to_latin, has_cyrillic

# Parser for the hot path (called for every content block: millions of times on the full corpus). Pure-Python
# `html.parser` extracts IDENTICAL text to lxml on all 43,209 real blocks tested (tests/test_text.py pins the
# equivalence) at the same speed, and keeps native lxml code out of the hottest loop after an intermittent native
# crash (access violation inside a lxml/bs4 parse) was seen under load.
HTML_PARSER = "html.parser"

_HTML_WS = re.compile(r"[ \t\r\n\f]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Uzbek Latin uses o' g' with many different apostrophe-like characters.
APOSTROPHES = "ʻʼ’‘`´′ʹ'ʼʻ‘’‛"
_APOSTROPHE_RE = re.compile("[" + re.escape(APOSTROPHES) + "]")

_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "blockquote", "pre", "tr", "thead", "tbody", "tfoot", "caption", "center",
}
_SKIP_TAGS = {"script", "style", "noscript"}


# Cyrillic letters that look identical to Latin ones. Real documents contain words like "Нigher" (Cyrillic Н),
# which would otherwise never match a Latin query. Folded per token, towards the token's majority script.
_TO_LATIN = str.maketrans("АВЕКМНОРСТХаеоіјсрух", "ABEKMHOPCTXaeoijcpyx")
_TO_CYRILLIC = {v: k for k, v in zip("ABEKMHOPCTXaeopcyx", "АВЕКМНОРСТХаеорсух")}
_LETTER_RUN = re.compile(r"[^\W\d_]+")
_CYR_CH = re.compile(r"[\u0400-\u04FF]")
_LAT_CH = re.compile(r"[A-Za-z]")


def fold_mixed_script_tokens(text: str) -> str:
    def fix(match):
        token = match.group(0)
        cyr, lat = len(_CYR_CH.findall(token)), len(_LAT_CH.findall(token))
        if not cyr or not lat:
            return token
        if lat >= cyr:
            return token.translate(_TO_LATIN)
        return "".join(_TO_CYRILLIC.get(ch, ch) for ch in token)

    return _LETTER_RUN.sub(fix, text)


def clean_text(text: str) -> str:
    """NUL/control-character strip (Postgres text rejects NUL), NFC, NBSP -> space."""
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    return text.replace(" ", " ").replace("​", "")


def _render_inline(node, out):
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            out.append(_HTML_WS.sub(" ", str(child)))
        elif isinstance(child, Tag):
            if child.name in _SKIP_TAGS:
                continue
            if child.name == "br":
                out.append("\n")
            elif child.name == "table":
                out.append("\n" + _render_table(child) + "\n")
            elif child.name in _BLOCK_TAGS:
                out.append("\n")
                _render_inline(child, out)
                out.append("\n")
            else:
                _render_inline(child, out)  # inline element: NO separator, like a browser


def _cell_text(cell) -> str:
    out = []
    _render_inline(cell, out)
    return re.sub(r"\s+", " ", "".join(out).replace(" ", " ")).strip()


def _render_table(table) -> str:
    lines = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_cell_text(c) for c in tr.find_all(["th", "td"], recursive=False)]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def table_structure(table) -> list[list[dict]]:
    """Row/cell structure (text + spans) of a top-level table, for section metadata."""
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        rows.append(
            [
                {
                    "t": clean_text(_cell_text(c)),
                    "rs": int(c.get("rowspan", 1) or 1) if str(c.get("rowspan", "1")).isdigit() else 1,
                    "cs": int(c.get("colspan", 1) or 1) if str(c.get("colspan", "1")).isdigit() else 1,
                    "h": c.name == "th",
                }
                for c in tr.find_all(["th", "td"], recursive=False)
            ]
        )
    return rows


def html_to_text(html: str) -> str:
    """Render an HTML fragment like a browser would: inline tags join without spaces, <br> and block
    tags break lines, table rows become ' | '-joined lines."""
    # strip control characters BEFORE parsing: libxml2 would turn a NUL into a space inside a word
    soup = BeautifulSoup(_CONTROL.sub("", html), HTML_PARSER)
    root = soup.body or soup
    out = []
    _render_inline(root, out)
    text = clean_text("".join(out))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def normalize_search(text: str, *, language: str = "", script: str = "") -> str:
    """Lossy search key: lowercase, apostrophes removed, Uzbek Cyrillic folded to Latin."""
    text = fold_mixed_script_tokens(clean_text(text)).lower()
    text = _APOSTROPHE_RE.sub("", text)
    if language == "uz" and (script == "cyrl" or (script == "" and has_cyrillic(text))):
        text = cyrillic_to_latin(text)
        text = _APOSTROPHE_RE.sub("", text)
    # "PQ-330" must match a query "pq 330": the default tokenizer would keep "-330" as one lexeme
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def query_variants(query: str) -> list[str]:
    """Search-key variants for a user query. A Cyrillic query may be Uzbek (needs folding to match
    the Latin key space) or Russian (must stay Cyrillic), and we cannot tell them apart reliably."""
    base = normalize_search(query)
    variants = [base]
    if has_cyrillic(base):
        folded = normalize_search(query, language="uz", script="cyrl")
        if folded and folded != base:
            variants.append(folded)
    return variants
