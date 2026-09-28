from django.conf import settings

from apps.common.hashing import sha256_hex
from apps.legal_documents.text.normalize import normalize_search

from .chunking import CHUNKER_VERSION, HeuristicTokenCounter, chunk_sections
from .models import DocumentChunk, SourceType


def document_identity(document, version) -> dict:
    """Who a chunk belongs to, stored on every chunk so a retrieved chunk names its law without a join.

    Only what does not change for a stored version is copied. Validity dates and status are deliberately NOT
    copied: a newer edition or a later start-date correction would leave every chunk with a stale value, so
    "as of" and "in force" filters must read the version/document rows."""
    return {
        "document_title": document.title,
        "document_number": document.document_number,
        "document_external_id": document.external_id,
        "source_url": document.source_url,
        "adopted_at": document.adopted_at.isoformat() if document.adopted_at else None,
        "version_number": version.version_number,
    }


def build_chunk_rows(version, document, sections, *, counter=None) -> list[DocumentChunk]:
    """Chunk a version's sections into unsaved official-law DocumentChunk rows.

    `sections` are SectionDrafts or ORM rows exposing id/parent/section_type/text/order_index/
    source_anchor. Search text folds apostrophes and Uzbek Cyrillic -> Latin so one FTS index serves
    both scripts (see text.normalize).
    """
    counter = counter or HeuristicTokenCounter()
    drafts = chunk_sections(
        sections,
        max_tokens=settings.YURIST["CHUNK_MAX_TOKENS"],
        min_tokens=settings.YURIST["CHUNK_MIN_TOKENS"],
        counter=counter,
    )
    identity = document_identity(document, version)
    rows = []
    for d in drafts:
        first = d.first_section
        # the title heads the search text and the embedding input: a query that names the law finds all its chunks
        embedding_input = "\n".join([document.title, *d.heading_path, d.text])
        rows.append(
            DocumentChunk(
                organization=None,
                source_type=SourceType.OFFICIAL_LAW,
                document=document,
                version=version,
                section_id=getattr(first, "id", None),
                chunk_index=d.chunk_index,
                text=d.text,
                search_text=normalize_search(embedding_input, language=document.language, script=document.script),
                token_count=d.token_count,
                content_hash=sha256_hex(embedding_input),
                language=document.language,
                chunker_version=CHUNKER_VERSION,
                metadata={**d.metadata(counter.name), "script": document.script, **identity},
            )
        )
    return rows


def sections_from_db(version):
    """Light section objects (with .parent links) rebuilt from stored, immutable sections."""
    from types import SimpleNamespace

    rows = list(
        version.sections.order_by("order_index").values(
            "id", "parent_id", "section_type", "number", "title", "text", "order_index", "path", "source_anchor"
        )
    )
    nodes = {r["id"]: SimpleNamespace(**r, parent=None, metadata={}) for r in rows}
    for node in nodes.values():
        node.parent = nodes.get(node.parent_id)
    return [nodes[r["id"]] for r in rows]


def rechunk_version(version) -> int:
    """Replace a version's chunks using the *current* chunker. Sections (and therefore citations that
    point at them) are never touched; embeddings of the old chunks cascade-delete and must be regenerated.
    Returns the number of chunks written."""
    from django.db import transaction

    document = version.document
    with transaction.atomic():
        DocumentChunk.objects.filter(version=version).delete()
        if version.metadata.get("text_status") != "ok":
            return 0
        rows = build_chunk_rows(version, document, sections_from_db(version))
        DocumentChunk.objects.bulk_create(rows, batch_size=1000)
        return len(rows)


def versions_needing_rechunk():
    """OK-text versions with no chunks, or with chunks from an older chunker."""
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models import Exists, OuterRef, Q

    from apps.legal_documents.models import LegalDocumentVersion

    # chunks built under a different language than the document now has carry a stale search key space
    language_mismatch = DocumentChunk.objects.filter(version=OuterRef("pk")).exclude(language=OuterRef("document__language"))
    # a corrected title/number on the document must reach the copies kept on its chunks
    stale_identity = (
        DocumentChunk.objects.filter(version=OuterRef("pk"))
        .annotate(t=KeyTextTransform("document_title", "metadata"), n=KeyTextTransform("document_number", "metadata"))
        # a chunk with no identity at all (key missing -> NULL) is as stale as one with an outdated title
        .filter(Q(t__isnull=True) | Q(n__isnull=True) | ~Q(t=OuterRef("document__title")) | ~Q(n=OuterRef("document__document_number")))
    )
    return (
        LegalDocumentVersion.objects.filter(metadata__text_status="ok")
        .filter(
            Q(chunks__isnull=True)
            | Q(chunks__chunker_version__lt=CHUNKER_VERSION)
            | Q(Exists(language_mismatch))
            | Q(Exists(stale_identity))
        )
        .distinct()
    )
