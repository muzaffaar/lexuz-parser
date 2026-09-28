"""Turn lex.uz content blocks into a section tree (TZ 48-49).

Invariants (tested against the real archive):
  1. every content block -> exactly one section, in source order (nothing dropped);
  2. section.text is the block's verbatim rendered text;
  3. path labels are unique among siblings, so `path` is a stable ltree address.

Levels (lower number = higher in the tree):
  appendix 1 > part 2 > section 3 > chapter 4 > article 5 > clause 6 > subclause 7+ (per dot depth)
Leaves (item, paragraph, table, footnote, requisites) attach to the current container.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from apps.common.hashing import sha256_hex
from apps.common.ids import uuid7
from apps.common.fields import ltree_label

from ..constants import SectionType as T
from ..text.normalize import HTML_PARSER, clean_text, html_to_text, table_structure
from ..text.translit import cyrillic_to_latin
from bs4 import BeautifulSoup

STRUCTURE_VERSION = 1

_APOS = "ʻʼ’‘`'"
LEVEL = {T.APPENDIX: 1, T.PART: 2, T.SECTION: 3, T.CHAPTER: 4, T.ARTICLE: 5, T.CLAUSE: 6}
_LEAF_TYPES = {T.ITEM, T.PARAGRAPH, T.TABLE, T.FOOTNOTE, T.TITLE, T.FORM, T.BODY, T.NUMBER, T.PLACE_DATE,
               T.SIGNATURE, T.NOTE, T.PDF_PAGE}

# css class -> section type (first match wins)
_CLASS_TYPE = [
    ("APPL_BANNER", T.APPENDIX),         # prefix match: APPL_BANNER_LANDSCAPE_TITLE, ...
    ("ACT_TITLE_APPL", T.TITLE),
    ("ACT_TITLE", T.TITLE),
    ("ACT_FORM", T.FORM),
    ("ACCEPTING_BODY", T.BODY),
    ("ACT_ESSENTIAL_ELEMENTS_NUM", T.NUMBER),
    ("ACT_ESSENTIAL_ELEMENTS", T.PLACE_DATE),
    ("SIGNATURE", T.SIGNATURE),
    ("DEPARTMENTAL", T.NOTE),
    ("TEXT_CENTER", T.NOTE),
    ("FOOTNOTE", T.FOOTNOTE),
    ("TEXT_HEADER", T.CHAPTER),          # prefix: TEXT_HEADER_DEFAULT
]
# requisites that end the running text: they reset the container stack
_RESET_TYPES = {T.FORM, T.BODY, T.NUMBER, T.PLACE_DATE, T.SIGNATURE}
_KNOWN_CLASS_PREFIXES = tuple(k for k, _ in _CLASS_TYPE) + ("ACT_TEXT", "BY_DEFAULT", "TABLE_STD", "COMMENT")

_KW_UZ = re.compile(
    rf"^(?:(?P<num1>\d+(?:[-–]\d+)?|[IVXLC]+)\s*[-–]?\s*(?P<kw1>modda|модда|bob|боб|bo[{_APOS}]?lim|бўлим|қисм|qism)\b"
    rf"|(?P<kw2>modda|модда)\s+(?P<num2>\d+(?:[-–]\d+)?))\.?\s*(?P<rest>.*)$",
    re.I | re.S,
)
_KW_RU = re.compile(
    r"^(?P<kw>Статья|Глава|Раздел|Часть)\s+(?P<num>\d+(?:[-–.]\d+)?|[IVXLC]+)\.?\s*(?P<rest>.*)$", re.I | re.S
)
_ROMAN_HEAD = re.compile(r"^(?P<num>[IVXLC]+)[.)]\s+(?P<rest>.+)$", re.S)
_NUM_HEAD = re.compile(r"^(?P<num>\d+)[.)]\s+(?P<rest>.+)$", re.S)
_CLAUSE = re.compile(r"^(?P<num>\d+(?:\.\d+)*)[.)](?:\s+\S|\s*$)", re.S)  # also a bare label: "40."
_ITEM = re.compile(rf"^\(?(?P<lbl>[a-zа-яўқғҳ]|\d+)\)\s+\S", re.I | re.S)
_APPENDIX_NO = [
    re.compile(r"(\d+)\s*[-–]?\s*(?:илова|ilova|приложение)", re.I),
    re.compile(r"(?:илова|ilova|приложение|appendix|annex)\s*(?:№|n[o.]?|raqam)?\s*(\d+)", re.I),
]

_KW_TYPE = {
    "modda": T.ARTICLE, "модда": T.ARTICLE, "статья": T.ARTICLE,
    "bob": T.CHAPTER, "боб": T.CHAPTER, "глава": T.CHAPTER,
    "бўлим": T.SECTION, "раздел": T.SECTION,
    "қисм": T.PART, "qism": T.PART, "часть": T.PART,
}
_ABBR = {
    T.APPENDIX: "app", T.PART: "pt", T.SECTION: "sec", T.CHAPTER: "ch", T.ARTICLE: "art", T.CLAUSE: "cl",
    T.SUBCLAUSE: "sc", T.ITEM: "it", T.PARAGRAPH: "p", T.TABLE: "tb", T.FOOTNOTE: "fn", T.TITLE: "ti",
    T.FORM: "fo", T.BODY: "bo", T.NUMBER: "no", T.PLACE_DATE: "pd", T.SIGNATURE: "sg", T.NOTE: "nt", T.PDF_PAGE: "pg",
}


@dataclass
class SectionDraft:
    id: UUID
    section_type: str
    number: str
    title: str
    text: str
    order_index: int
    level: int
    parent: "SectionDraft | None" = None
    path: str = ""
    source_anchor: str = ""
    metadata: dict = field(default_factory=dict)
    links: list = field(default_factory=list)  # block links, used to build section-level relations

    @property
    def parent_id(self):
        return self.parent.id if self.parent else None

    @property
    def text_hash(self) -> str:
        return sha256_hex(self.text)


def _section_kw_type(kw: str):
    return _KW_TYPE.get(kw.lower().replace("ʻ", "").replace("'", "")) or (
        T.SECTION if kw.lower().startswith(("bo", "бў")) else None
    )


def _block_type(classes: list[str], has_table: bool, table_ratio: float):
    joined = classes
    for prefix, section_type in _CLASS_TYPE:
        if any(c.startswith(prefix) for c in joined):
            return section_type
    if has_table and table_ratio >= 0.5:
        return T.TABLE
    return None


def classify_block(classes, text, has_table=False, table_ratio=0.0):
    """-> (section_type, number, title, level). Pure function of one block."""
    css_type = _block_type(classes, has_table, table_ratio)
    is_header_class = any(c.startswith("TEXT_HEADER") for c in classes)

    if css_type == T.APPENDIX:
        number = ""
        for pat in _APPENDIX_NO:
            m = pat.search(text)
            if m:
                number = m.group(1)
                break
        return T.APPENDIX, number, text, LEVEL[T.APPENDIX]
    if css_type in _LEAF_TYPES and css_type != T.CHAPTER:
        return css_type, "", "", 99

    # keyword headings ("25-modda.", "I bob", "Статья 5.") apply to any class when short enough
    if is_header_class or len(text) <= 300:
        m = _KW_UZ.match(text)
        if m:
            kw = m.group("kw1") or m.group("kw2")
            number = (m.group("num1") or m.group("num2") or "").strip()
            stype = _section_kw_type(kw)
            if stype:
                return stype, number, m.group("rest").strip(), LEVEL[stype]
        m = _KW_RU.match(text)
        if m:
            stype = _KW_TYPE[m.group("kw").lower()]
            return stype, m.group("num"), m.group("rest").strip(), LEVEL[stype]

    if css_type == T.CHAPTER or is_header_class:
        m = _ROMAN_HEAD.match(text) or _NUM_HEAD.match(text)
        if m:
            return T.CHAPTER, m.group("num"), m.group("rest").strip(), LEVEL[T.CHAPTER]
        return T.CHAPTER, "", text, LEVEL[T.CHAPTER]

    m = _CLAUSE.match(text)
    if m:
        number = m.group("num")
        depth = number.count(".") + 1
        if depth == 1:
            return T.CLAUSE, number, "", LEVEL[T.CLAUSE]
        return T.SUBCLAUSE, number, "", LEVEL[T.CLAUSE] + depth - 1
    m = _ITEM.match(text)
    if m:
        return T.ITEM, m.group("lbl"), "", 99
    return T.PARAGRAPH, "", "", 99


def _label(stype: str, number: str, seq: int) -> str:
    abbr = _ABBR.get(stype, "x")
    if number:
        ascii_number = cyrillic_to_latin(number).replace(".", "_").replace("-", "_")
        return f"{abbr}_{ltree_label(ascii_number)}"
    return f"{abbr}{seq}"


def build_sections(blocks: list[dict]) -> tuple[list[SectionDraft], list[dict]]:
    """blocks: crawler `document.json` blocks. Returns (sections, annotation_blocks)."""
    sections: list[SectionDraft] = []
    annotations: list[dict] = []
    stack: list[SectionDraft] = []
    used_labels: dict = {}
    seq_by_parent: dict = {}
    appendix_seq = 0

    for block in blocks:
        if not block.get("is_content"):
            annotations.append(block)
            continue
        classes = block.get("classes") or []
        html = block.get("html") or ""
        soup = BeautifulSoup(html, HTML_PARSER)
        top_tables = [t for t in soup.find_all("table") if t.find_parent("table") is None]
        text = html_to_text(html)
        table_len = sum(len(html_to_text(str(t))) for t in top_tables)
        ratio = table_len / len(text) if text else 0.0
        stype, number, title, level = classify_block(classes, text, bool(top_tables), ratio)
        if stype == T.APPENDIX and not number:
            appendix_seq += 1
            number = str(appendix_seq)
        elif stype == T.APPENDIX:
            appendix_seq = max(appendix_seq, int(number)) if number.isdigit() else appendix_seq

        if stype in _RESET_TYPES or (stype == T.NOTE and "DEPARTMENTAL" in " ".join(classes)):
            del stack[:]
        elif stype == T.TITLE and not any(c.startswith("ACT_TITLE_APPL") for c in classes):
            del stack[:]  # main act title precedes everything; appendix titles stay inside their appendix
        if stype in LEVEL or stype == T.SUBCLAUSE:
            while stack and stack[-1].level >= level:
                stack.pop()
        parent = stack[-1] if stack else None

        seq_by_parent[parent.id if parent else None] = seq_by_parent.get(parent.id if parent else None, 0) + 1
        label = _label(stype, number, seq_by_parent[parent.id if parent else None])
        taken = used_labels.setdefault(parent.id if parent else None, set())
        base, n = label, 1
        while label in taken:
            n += 1
            label = f"{base}_{n}"
        taken.add(label)

        anchor = block.get("id") or ""
        draft = SectionDraft(
            id=uuid7(),
            section_type=stype,
            number=number,
            title=title,
            text=text,
            order_index=len(sections),
            level=level,
            parent=parent,
            path=f"{parent.path}.{label}" if parent else label,
            source_anchor=anchor if re.fullmatch(r"-?\d+", str(anchor)) else "",
            metadata={"css": " ".join(classes), "block_order": block.get("order")},
            links=block.get("links") or [],
        )
        if top_tables:
            draft.metadata["tables"] = [table_structure(t) for t in top_tables]
        sections.append(draft)
        if stype in LEVEL or stype == T.SUBCLAUSE:
            stack.append(draft)
    return sections, annotations


def unknown_classes(blocks: list[dict]) -> set[str]:
    """CSS classes we have no mapping for; surfaced in ingestion reports as layout-drift warnings."""
    seen = set()
    for block in blocks:
        for c in block.get("classes") or []:
            if c not in ("lx_elem", "lx_no_select") and not c.startswith(_KNOWN_CLASS_PREFIXES) and not c.startswith("INDEXES_ON_REF") and c != "PUBLICATION_ORIGIN":
                seen.add(c)
    return seen
