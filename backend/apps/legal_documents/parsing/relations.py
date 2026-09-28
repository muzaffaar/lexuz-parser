"""Document links -> relation drafts (TZ 44: LegalDocumentRelation)."""
from dataclasses import dataclass
from datetime import date

from ..constants import RelationType
from .titles import DocLink, parse_document_link


@dataclass
class RelationDraft:
    relation_type: str
    target_url: str
    target_external_id: str
    target_edition_on: date | None
    target_anchor: str
    anchor_text: str = ""
    source_section: object | None = None  # SectionDraft or None (document-level)


def canonical_url(link: DocLink) -> str:
    url = f"https://lex.uz/docs/{link.external_id}"
    if link.edition_on:
        url += f"?ONDATE={link.edition_on:%d.%m.%Y}"
    if link.anchor:
        url += f"#{link.anchor}"
    return url


def make_relation(url: str, relation_type: str, *, text: str = "", section=None, base: str = "https://lex.uz/") -> RelationDraft | None:
    link = parse_document_link(url, base)
    if link is None:
        return None
    return RelationDraft(relation_type, canonical_url(link), link.external_id, link.edition_on, link.anchor, text[:500], section)


def section_citations(sections, self_external_id: str) -> list[RelationDraft]:
    """Links inside content blocks -> CITES relations attached to their section.
    Links back into the same document are kept only when they carry an anchor (internal cross-references)."""
    out = []
    for section in sections:
        for link in section.links:
            if link.get("kind", "href") != "href":
                continue
            rel = make_relation(link["url"], RelationType.CITES, text=link.get("text", ""), section=section)
            if rel is None:
                continue
            if rel.target_external_id == self_external_id and not rel.target_anchor:
                continue
            out.append(rel)
    return out


def dedupe(relations: list[RelationDraft]) -> list[RelationDraft]:
    seen, out = set(), []
    for r in relations:
        key = (r.target_url, r.relation_type, r.source_section.id if r.source_section else None)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
