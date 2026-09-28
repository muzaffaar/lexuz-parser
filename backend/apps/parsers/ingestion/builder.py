"""Crawler record -> ParsedDocument. No database access; everything here is deterministic and unit-testable."""
import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.conf import settings

from apps.common.hashing import sha256_hex
from apps.common.ids import uuid7
from apps.legal_documents.constants import (
    CardKind,
    DocumentStatus,
    RelationType,
    Representation,
    TextSource,
    TextStatus,
    ValidFromSource,
)
from apps.legal_documents.constants import SectionType as T
from apps.legal_documents.parsing import cards as cards_parser
from apps.legal_documents.parsing.annotations import parse_annotations
from apps.legal_documents.parsing.relations import dedupe, make_relation, section_citations
from apps.legal_documents.parsing.structure import STRUCTURE_VERSION, SectionDraft, build_sections, unknown_classes
from apps.legal_documents.parsing.titles import external_id_and_edition, number_key, parse_title
from apps.legal_documents.text import detect as lang
from apps.legal_documents.text.pdf import extract_pdf_text
from apps.legal_documents.text.normalize import clean_text, html_to_text, normalize_search

from .archive import ArchivedDocument, CrawlArchive
from .zips import extract_pdfs
from .drafts import CardDraft, ParsedDocument, RawSnapshotDraft

_BODY_TYPES = {T.CLAUSE, T.SUBCLAUSE, T.ITEM, T.CHAPTER, T.ARTICLE, T.PART, T.SECTION, T.APPENDIX, T.TABLE, T.FOOTNOTE, T.PARAGRAPH}
_CARD_URLS = {
    CardKind.PASSPORT: "https://lex.uz/doc-passport/{id}",
    CardKind.CARD1: "https://lex.uz/actinfo/card1/{id}",
    CardKind.CARD2: "https://lex.uz/actinfo/card2/{id}",
    CardKind.BASREV: "https://lex.uz/actinfo/basrev/{id}",
    CardKind.REVHIS: "https://lex.uz/actinfo/revhis/{id}",
    CardKind.CORRESPONDENTS: "https://lex.uz/actinfo/correspondents/{id}",
    CardKind.RESPONDENTS: "https://lex.uz/actinfo/respondents/{id}",
}


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _norm_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _read_cards(archive: CrawlArchive, external_id: str):
    """-> (card drafts, card1 info | None, passport info | None)."""
    drafts, card1, passport = [], None, None
    for kind, pattern in _CARD_URLS.items():
        folder = archive.resource_dir(pattern.format(id=external_id))
        if folder is None or not (folder / "page.html").exists():
            continue
        html = (folder / "page.html").read_bytes()
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".lx_lact_passport") if kind == CardKind.PASSPORT else soup.select_one("#tab-container")
        raw_text = html_to_text(str(node or soup.body or soup))
        fields = {}
        if kind == CardKind.CARD1:
            card1 = cards_parser.parse_card1(html)
            fields = {"pairs": card1.raw_pairs}
        elif kind == CardKind.PASSPORT:
            passport = cards_parser.parse_passport(html)
            fields = {"requisites": passport.requisites}
        fetched = None
        source_json = folder / "source.json"
        if source_json.exists():
            fetched = _utc(json.loads(source_json.read_text(encoding="utf-8")).get("retrieved_at", 0))
        drafts.append(CardDraft(kind, raw_text, fields, fetched))
    return drafts, card1, passport


def _pdf_sections(pages: list[dict], *, start: int = 0, prefix: str = "") -> list[SectionDraft]:
    """One PDF_PAGE section per page that has text. `start` continues the order index after other sections;
    `prefix` keeps paths unique when several PDFs belong to one document."""
    sections = []
    for page in pages:
        text = "\n".join(line.strip() for line in clean_text(page.get("text") or "").split("\n") if line.strip())
        if not text:
            continue
        n = page.get("page", len(sections) + 1)
        sections.append(
            SectionDraft(
                id=uuid7(), section_type=T.PDF_PAGE, number=str(n), title="", text=text,
                order_index=start + len(sections), level=99, path=f"{prefix}pg{n}",
                metadata={"page": n, **({"file": prefix.rstrip("_")} if prefix else {})},
            )
        )
    return sections


@dataclasses.dataclass
class _AssetPdf:
    zip_draft: RawSnapshotDraft | None  # provenance: the zip it came from (None for a direct .pdf)
    pdf_draft: RawSnapshotDraft
    member: str


def _file_asset_pdfs(rec, archive, fetched_at) -> list[_AssetPdf]:
    """PDFs that the page itself points at: `/files/<n>.pdf` or `/files/<n>.zip` (some acts are published as a
    zip holding the PDF, with only requisites in the HTML). The crawler already downloads these as content
    assets; here they are opened (safely) and turned into snapshots."""
    found = []
    for url in rec.get("assets") or []:
        m = re.fullmatch(r"/files/[^/]+\.(zip|pdf)", urlsplit(url).path, re.I)
        if not m:
            continue
        folder = archive.resource_dir(url)
        ext = m.group(1).lower()
        path = folder / f"file.{ext}" if folder else None
        if path is None or not path.exists():
            continue
        info = json.loads((folder / "source.json").read_text(encoding="utf-8")) if (folder / "source.json").exists() else {}
        retrieved = _utc(info.get("retrieved_at") or 0) if info else fetched_at
        if ext == "pdf":
            raw = path.read_bytes()
            if raw.startswith(b"%PDF-"):
                pdf = RawSnapshotDraft(role="primary_pdf", kind="pdf", url=url, sha256=info.get("sha256") or hashlib.sha256(raw).hexdigest(),
                                       path=path, fetched_at=retrieved, content_type="application/pdf")
                found.append(_AssetPdf(None, pdf, path.name))
            continue
        zip_draft = RawSnapshotDraft(role="source_zip", kind="zip", url=url, path=path, fetched_at=retrieved, content_type="application/zip",
                                     sha256=info.get("sha256") or hashlib.sha256(path.read_bytes()).hexdigest())
        for member in extract_pdfs(path):
            pdf = RawSnapshotDraft(role="primary_pdf", kind="pdf", url=f"{url}#{member.name}", sha256=hashlib.sha256(member.data).hexdigest(),
                                   path=path, fetched_at=retrieved, content_type="application/pdf", data=member.data)
            found.append(_AssetPdf(zip_draft, pdf, member.name))
    return found


def _text_from_asset_pdfs(doc, asset_pdfs, meta, warnings, page_snapshot):
    """The page has requisites but no body: the act's text is in the PDF(s). Append their pages as sections."""
    total_pages = chars = 0
    details = []
    for i, ap in enumerate(asset_pdfs, start=1):
        result = extract_pdf_text(ap.pdf_draft.read(), timeout=90)
        pages = result.get("pages", [])
        new = _pdf_sections(pages, start=len(doc.sections), prefix=f"f{i}_")
        doc.sections.extend(new)
        n_chars = sum(len(s.text) for s in new)
        total_pages += result.get("total_pages") or len(pages)
        chars += n_chars
        details.append({"file": ap.member, "status": result.get("status"), "pages": result.get("total_pages"), "chars": n_chars, "url": ap.pdf_draft.url})
        if result.get("status") in ("encrypted", "extraction_failed", "extraction_timeout"):
            warnings.append(f"PDF text unavailable for {ap.member}: {result['status']}")
    per_page = chars / total_pages if total_pages else 0
    meta["pdf_in_files"] = {"files": details, "chars_per_page": round(per_page, 1)}
    doc.text_source = TextSource.PDF_EMBEDDED
    doc.normalized_text = "\n\n".join(s.text for s in doc.sections if s.text)
    if per_page >= settings.YURIST["PDF_MIN_CHARS_PER_PAGE"]:
        doc.text_status = TextStatus.OK
    else:
        doc.text_status = TextStatus.NEEDS_OCR
        warnings.append(f"PDF looks scanned ({per_page:.0f} chars/page): needs OCR")
    # the PDF is what the text came from; the HTML page is only the wrapper
    doc.snapshots.insert(0, dataclasses.replace(asset_pdfs[0].pdf_draft, role="primary"))
    if page_snapshot:
        page_snapshot.role = "page"


def build_parsed_document(archived: ArchivedDocument, archive: CrawlArchive) -> ParsedDocument:
    rec = archived.record
    external_id, edition_on = external_id_and_edition(rec["url"])
    fetched_at = _utc(rec.get("retrieved_at") or 0)
    title = parse_title(rec.get("title") or "")
    warnings: list[str] = []
    meta: dict = {"structure_version": STRUCTURE_VERSION}

    doc = ParsedDocument(
        external_id=external_id, edition_on=edition_on, source_url=rec["url"], fetched_at=fetched_at,
        source_sha256=rec.get("source_sha256", ""), title=title.title or rec.get("title", ""),
        representation=rec.get("representation", Representation.HTML),
    )

    # -- raw snapshot of the page itself
    page_snapshot = None
    if rec.get("source_blob"):
        page_snapshot = RawSnapshotDraft(
            role="page", kind="html", url=rec["url"], sha256=rec["source_sha256"],
            path=archive.blob_path(rec["source_blob"]), fetched_at=fetched_at,
            content_encoding="gzip", content_type="text/html",
        )
        doc.snapshots.append(page_snapshot)

    annotations_blocks: list[dict] = []
    if doc.representation == Representation.PDF_ONLY:
        _build_pdf_only(doc, rec, archive, meta, warnings)
    else:
        sections, annotations_blocks = build_sections(rec.get("blocks", []))
        doc.sections = sections
        doc.text_source = TextSource.HTML
        doc.normalized_text = "\n\n".join(s.text for s in sections if s.text)
        has_body = any(s.section_type in _BODY_TYPES and s.text.strip() for s in sections)
        doc.text_status = TextStatus.OK if has_body else (TextStatus.STUB if doc.normalized_text else TextStatus.EMPTY)
        if page_snapshot:
            page_snapshot.role = "primary"
        asset_pdfs = _file_asset_pdfs(rec, archive, fetched_at)
        if asset_pdfs and not has_body:
            _text_from_asset_pdfs(doc, asset_pdfs, meta, warnings, page_snapshot)
        seen_zips = set()
        for ap in asset_pdfs:  # always attach, whether or not the text came from them
            doc.attachments.append(ap.pdf_draft)
            if ap.zip_draft is not None and ap.zip_draft.sha256 not in seen_zips:
                seen_zips.add(ap.zip_draft.sha256)
                doc.attachments.append(ap.zip_draft)
        unknown = sorted(unknown_classes(rec.get("blocks", [])))
        if unknown:
            meta["unknown_css_classes"] = unknown
            warnings.append(f"unmapped CSS classes (layout drift?): {', '.join(unknown)}")

    _collect_pdf_attachments(doc, archive, external_id, fetched_at)

    ann = parse_annotations(annotations_blocks)
    if ann.comments:
        meta["editor_comments"] = ann.comments
    if ann.warnings:
        meta["site_warnings"] = ann.warnings
    if ann.registry_number:
        meta["source_registry_number"] = ann.registry_number
    if ann.other:
        meta["other_annotations"] = ann.other
    doc.classifications = ann.classifications
    if doc.text_status == TextStatus.STUB:
        meta["stub_reason"] = ann.warnings[0] if ann.warnings else "page has no body text, only requisites"

    # -- content hash (TZ 55): hash of the text; fall back to the raw source when there is no text,
    #    so two different scanned PDFs are never "unchanged" just because both have empty text.
    if doc.normalized_text:
        doc.content_hash = sha256_hex(doc.normalized_text)
    else:
        primary = doc.primary_snapshot
        doc.content_hash = sha256_hex("raw:" + (primary.sha256 if primary else doc.source_sha256))
        meta["hash_basis"] = "raw_source"

    # -- cards (optional enrichment)
    doc.cards, card1, passport = _read_cards(archive, external_id)

    doc.document_number = title.number or (card1.document_number if card1 else "") or (passport.document_number if passport else "")
    doc.number_key = number_key(doc.document_number) if doc.document_number else ""
    doc.adopted_at = (card1 and card1.adopted_at) or (passport and passport.adopted_at) or title.date
    if card1:
        doc.document_type, doc.status, doc.status_raw = card1.document_type, card1.status, card1.status_raw
        doc.effective_from, doc.effective_to = card1.effective_from, card1.effective_to
        doc.rank_key = (_norm_key(card1.document_type), _norm_key(card1.document_form))
    else:
        doc.status = DocumentStatus.UNKNOWN
    doc.published_at = (card1 and card1.published_at) or ann.published_at

    def first_text(section_type):
        return next((s.text for s in doc.sections if s.section_type == section_type and s.parent is None), "")

    doc.document_form = (card1 and card1.document_form) or (passport and passport.document_form) or first_text(T.FORM)
    doc.authority = (card1 and card1.authority) or (passport and passport.authority) or first_text(T.BODY)
    if doc.authority or doc.document_form:
        doc.requisite_key = (normalize_search(doc.authority, language="uz"), normalize_search(doc.document_form, language="uz"))

    # -- language: the site's own statement beats our guess
    guess = lang.from_card_value(card1.language_value) if card1 else None
    if guess is None:
        guess = lang.detect(f"{doc.title} {doc.normalized_text[:4000]}")
        if not guess.confident:
            warnings.append("language detection not confident")
    doc.language, doc.script, doc.language_source = guess.language, guess.script, guess.source

    # -- validity start (recorded with its provenance; see ValidFromSource)
    if edition_on:
        doc.valid_from, doc.valid_from_source = edition_on, ValidFromSource.EDITION
    elif doc.effective_from:
        doc.valid_from, doc.valid_from_source = doc.effective_from, ValidFromSource.CARD_EFFECTIVE
    elif doc.published_at:
        # Better than the adoption date: an act cannot be in force before it is officially published, and
        # this is available from the page itself even when no metadata card has been fetched yet.
        doc.valid_from, doc.valid_from_source = doc.published_at, ValidFromSource.PUBLISHED
    elif doc.adopted_at:
        doc.valid_from, doc.valid_from_source = doc.adopted_at, ValidFromSource.ADOPTED
    else:
        doc.valid_from, doc.valid_from_source = fetched_at.date(), ValidFromSource.OBSERVED

    # -- relations
    relations = []
    for target, relation in archive.edges(rec["url"]):
        rtype = RelationType.LANGUAGE_VARIANT if relation == "language" else RelationType.EDITION
        rel = make_relation(target, rtype)
        if rel and (rel.target_external_id != external_id or rel.target_edition_on):
            relations.append(rel)
    for block in annotations_blocks:
        for link in block.get("links") or []:
            rel = make_relation(link["url"], RelationType.CITES, text=link.get("text", ""))
            if rel and rel.target_external_id != external_id:
                relations.append(rel)
    relations.extend(section_citations(doc.sections, external_id))
    doc.relations = dedupe(relations)

    # Identity of the *source* (TZ 55): lets the loader recognise "same bytes, smarter parser" and NOT mint a
    # new legal version just because our own text extraction improved.
    meta["source_sha256"] = doc.source_sha256
    meta["primary_sha256"] = doc.primary_snapshot.sha256 if doc.primary_snapshot else ""
    doc.version_metadata = meta
    doc.warnings = warnings
    return doc


def _collect_pdf_attachments(doc, archive, external_id, fetched_at):
    """The PDF to show next to the text: the primary file of a PDF-only act, else the site's PDF export."""
    primary = doc.primary_snapshot
    if doc.representation == Representation.PDF_ONLY and primary is not None and primary.kind == "pdf":
        doc.attachments.append(dataclasses.replace(primary, role="primary_pdf"))
        return
    url = f"https://lex.uz/pdffile/{external_id}"
    folder = archive.resource_dir(url, include_failed=True)
    if folder is None or not (folder / "file.pdf").exists():
        return
    path = folder / "file.pdf"
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-"):
        return  # never trust a "PDF" that is not one
    source_json = folder / "source.json"
    if source_json.exists():
        info = json.loads(source_json.read_text(encoding="utf-8"))
        sha, retrieved = info["sha256"], _utc(info.get("retrieved_at") or 0)
    else:  # download succeeded but the crawler's later step failed before writing source.json
        sha, retrieved = hashlib.sha256(raw).hexdigest(), fetched_at
    doc.attachments.append(
        RawSnapshotDraft(role="pdf_export", kind="pdf", url=url, sha256=sha, path=path, fetched_at=retrieved, content_type="application/pdf")
    )


def _build_pdf_only(doc, rec, archive, meta, warnings):
    pdf_urls = rec.get("primary_pdf_urls") or []
    folder = archive.resource_dir(pdf_urls[0]) if pdf_urls else None
    doc.text_source = TextSource.PDF_EMBEDDED
    doc.text_status = TextStatus.NEEDS_OCR
    if folder is None or not (folder / "file.pdf").exists():
        warnings.append("primary PDF not in the archive yet")
        meta["pdf"] = {"status": "missing"}
        return
    source = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    pdf = RawSnapshotDraft(
        role="primary", kind="pdf", url=pdf_urls[0], sha256=source["sha256"], path=folder / "file.pdf",
        fetched_at=_utc(source.get("retrieved_at") or 0), content_type=source.get("content_type", "application/pdf"),
    )
    doc.snapshots.insert(0, pdf)
    text_json = folder / "text.json"
    if not text_json.exists():
        warnings.append("PDF text extraction has not run")
        meta["pdf"] = {"status": "no_text_json"}
        return
    data = json.loads(text_json.read_text(encoding="utf-8"))
    pages = data.get("pages", [])
    total_pages = data.get("total_pages") or len(pages) or 1
    chars = sum(p.get("characters", 0) for p in pages)
    per_page = chars / total_pages
    meta["pdf"] = {
        "status": data.get("status"), "total_pages": total_pages, "chars": chars,
        "chars_per_page": round(per_page, 1), "low_text_pages": data.get("low_text_pages", []),
    }
    if data.get("status") in ("encrypted", "extraction_failed", "extraction_timeout"):
        warnings.append(f"PDF text unavailable: {data.get('status')}")
        return
    if per_page >= settings.YURIST["PDF_MIN_CHARS_PER_PAGE"]:
        doc.sections = _pdf_sections(pages)
        doc.normalized_text = "\n\n".join(s.text for s in doc.sections)
        doc.text_status = TextStatus.OK
    else:
        warnings.append(f"PDF looks scanned ({per_page:.0f} chars/page): needs OCR")
