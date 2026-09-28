"""Builders for synthetic documents, so DB/ingestion tests don't depend on the crawler archive."""
import gzip
import hashlib
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from apps.common.hashing import sha256_hex
from apps.legal_documents.constants import TextSource, TextStatus, ValidFromSource
from apps.legal_documents.parsing.structure import build_sections
from apps.legal_sources.models import LegalSource
from apps.parsers.ingestion.drafts import ParsedDocument, RawSnapshotDraft

_TMP = Path(tempfile.mkdtemp(prefix="yurist-test-"))
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
# The crawler keeps growing DATA_DIR while tests run, so whole-archive tests use the first 84 documents (frozen ids):
import json as _json
ORIGINAL_IDS = set(_json.loads((Path(__file__).parent / "fixtures" / "original_archive_ids.json").read_text(encoding="utf-8")))


def make_source() -> LegalSource:
    return LegalSource.objects.get_or_create(
        code="lexuz", defaults={"name": "Lex.uz", "base_url": "https://lex.uz/", "is_official": True}
    )[0]


def block(order, css, text, anchor=None, html=None):
    return {
        "order": order, "id": anchor or str(1000 + order), "classes": [css, "lx_elem"], "is_content": True,
        "html": html or f'<div class="{css} lx_elem">{text}</div>', "links": [],
    }


def blocks_for(paragraphs, first_css="ACT_TEXT"):
    return [block(i, first_css, p) for i, p in enumerate(paragraphs)]


def snapshot_draft(content: bytes = b"<html>raw</html>", kind="html", role="primary", gz=True, url="https://lex.uz/docs/1"):
    sha = hashlib.sha256(content).hexdigest()
    path = _TMP / f"{sha}{'.gz' if gz else ''}"
    path.write_bytes(gzip.compress(content, mtime=0) if gz else content)
    return RawSnapshotDraft(
        role=role, kind=kind, url=url, sha256=sha, path=path,
        fetched_at=datetime(2026, 9, 28, tzinfo=timezone.utc), content_encoding="gzip" if gz else "",
        content_type="text/html" if kind == "html" else "application/pdf",
    )


def parsed(
    external_id="100",
    paragraphs=("1. Birinchi band.", "2. Ikkinchi band."),
    *,
    edition_on=None,
    fetched=datetime(2026, 9, 28, tzinfo=timezone.utc),
    status="active",
    effective_to=None,
    adopted=date(2026, 1, 10),
    title="Test sarlavha",
    text_status=TextStatus.OK,
    number="PQ-1",
    language="uz",
    script="latn",
    relations=(),
    snapshot=True,
    valid_from=None,
    valid_from_source=None,
    classifications=(),
    source_sha256=None,
    css="ACT_TEXT",
) -> ParsedDocument:
    sections, _ = build_sections(blocks_for(list(paragraphs), first_css=css))
    text = "\n\n".join(s.text for s in sections if s.text)
    doc = ParsedDocument(
        external_id=external_id, edition_on=edition_on, source_url=f"https://lex.uz/docs/{external_id}",
        fetched_at=fetched, source_sha256=sha256_hex("raw" + text), title=title, document_number=number,
        number_key=number.lower(), adopted_at=adopted, effective_to=effective_to, status=status,
        language=language, script=script, language_source="detected", sections=sections, normalized_text=text,
        content_hash=sha256_hex(text), text_status=text_status, text_source=TextSource.HTML,
        valid_from=valid_from or edition_on or adopted,
        valid_from_source=valid_from_source or (ValidFromSource.EDITION if edition_on else ValidFromSource.ADOPTED),
        relations=list(relations), classifications=list(classifications),
    )
    if source_sha256:
        doc.source_sha256 = source_sha256
    if snapshot:
        # raw bytes follow the *source identity*, not the extracted text (a smarter parser sees the same bytes)
        doc.snapshots.append(snapshot_draft(("raw" + (source_sha256 or text)).encode(), url=doc.source_url))
    doc.version_metadata = {
        "source_sha256": doc.source_sha256,
        "primary_sha256": doc.primary_snapshot.sha256 if doc.primary_snapshot else "",
    }
    return doc
