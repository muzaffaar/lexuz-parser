"""Non-content ("annotation") blocks of a lex.uz page.

  INDEXES_ON_REF        subject classification (OKOZ = general legal classifier, TSZ = thematic guide)
  PUBLICATION_ORIGIN    "(Legislation database, 23.09.2026, 09/26/505/0960-son)"
  COMMENT               LexUZ editorial cross-reference - NOT law text
  COMMENT_FOR_WARNING   site notice, e.g. "text of the act is given in Uzbek and Russian"

Labels differ per UI language (ОКОЗ / OKOZ / LQBL, ТСЗ / TSZ / TDL), so systems are matched by alias.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from ..text.normalize import html_to_text
from .titles import parse_date

_SYSTEM_ALIASES = {
    "okoz": "okoz", "окоз": "okoz", "lqbl": "okoz",
    "tsz": "tsz", "тсз": "tsz", "tdl": "tsz",
}
_GROUP = re.compile(r"\[\s*(?P<sys>[^\]:\[]+?)\s*:\s*(?P<body>.*?)\]", re.S)
_ENTRY_SPLIT = re.compile(r";\s*(?=\d+\.\s)")
_CODE = re.compile(r"^(\d{2}(?:\.\d{2}){3})\s+")
_REGISTRY = re.compile(r"\b(\d{2}/\d{2}/\d+/\d+)\b")
_PLACEHOLDER = re.compile(r"no description|тавсиф йўқ|нет описания|tavsif yo", re.I)


@dataclass
class ClassificationDraft:
    system: str
    code: str
    label: str

    @property
    def fingerprint(self) -> str:
        return hashlib.md5(f"{self.system}\x00{self.label}".encode()).hexdigest()


@dataclass
class AnnotationInfo:
    classifications: list[ClassificationDraft] = field(default_factory=list)
    published_at: date | None = None
    registry_number: str = ""
    warnings: list[str] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    other: list[dict] = field(default_factory=list)


def parse_classification(text: str) -> list[ClassificationDraft]:
    out = []
    for group in _GROUP.finditer(text):
        system = _SYSTEM_ALIASES.get(group["sys"].strip().lower(), group["sys"].strip().lower())
        for entry in _ENTRY_SPLIT.split(group["body"].strip()):
            entry = re.sub(r"^\s*\d+\.\s*", "", entry).strip(" ;")
            if not entry or _PLACEHOLDER.search(entry):
                continue
            items = [i.strip() for i in entry.split(" / ") if i.strip()]
            code = ""
            for item in reversed(items):
                m = _CODE.match(item)
                if m:
                    code = m.group(1)
                    break
            out.append(ClassificationDraft(system, code, " / ".join(items)))
    return out


def parse_annotations(blocks: list[dict]) -> AnnotationInfo:
    info = AnnotationInfo()
    seen = set()
    for block in blocks:
        classes = block.get("classes") or []
        text = html_to_text(block.get("html") or "")
        if not text:
            continue
        css = " ".join(classes)
        if "INDEXES_ON_REF" in css:
            for c in parse_classification(text):
                if (c.system, c.fingerprint) not in seen:
                    seen.add((c.system, c.fingerprint))
                    info.classifications.append(c)
        elif "PUBLICATION_ORIGIN" in css:
            info.published_at = info.published_at or parse_date(text)
            m = _REGISTRY.search(text)
            if m and not info.registry_number:
                info.registry_number = m.group(1)
        elif "COMMENT_FOR_WARNING" in css:
            info.warnings.append(text)
        elif "COMMENT" in css:
            info.comments.append({"text": text, "block_order": block.get("order")})
        else:
            info.other.append({"css": css, "text": text[:500], "block_order": block.get("order")})
    return info
