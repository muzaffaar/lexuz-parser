"""Apply a ParsedDocument to PostgreSQL (the "Ingestion Layer" of TZ 53).

One `ingest()` call = one transaction = one document. It either fully applies or leaves nothing
behind, so a failure in one document can never leave partial state (TZ 58, 131).

Version rules (TZ 54, 55, 47):
  * observation of the *current* text (no ?ONDATE=): unchanged iff content_hash equals the current
    version's; otherwise a NEW version is created and the old one is kept (window closed, not edited);
  * observation of a *historical edition* (?ONDATE=): fills in history; if that edition was already
    stored with different text, the site restated history -> VersionConflict, for a human to review;
  * document-level attributes only ever come from current-text observations, and are never blanked
    by an observation that simply lacks a card.
"""
import gzip
import logging
from dataclasses import dataclass, field
from datetime import date

from django.db import connection, transaction

from apps.common.hashing import sha256_hex
from apps.common.storage import ObjectStore, snapshot_key
from apps.legal_documents.constants import DocumentStatus, RelationType, TextStatus, ValidFromSource
from apps.legal_documents.models import (
    DocumentTypeRank,
    LegalDocument,
    LegalDocumentAttachment,
    LegalDocumentCard,
    LegalDocumentClassification,
    LegalDocumentRelation,
    LegalDocumentSection,
    LegalDocumentVersion,
)
from apps.legal_documents.parsing.structure import STRUCTURE_VERSION
from apps.legal_documents.services.versions import VersionConflict, next_version_number, rebuild_validity
from apps.legal_monitoring.models import LegalUpdate, UpdateType
from apps.legal_sources.models import LegalSource, SourceSnapshot
from apps.parsers.models import ItemStatus
from apps.search.models import DocumentChunk
from apps.search.services import build_chunk_rows, rechunk_version

from .drafts import ParsedDocument, RawSnapshotDraft

log = logging.getLogger(__name__)
BATCH = 1000


class IngestError(Exception):
    pass


@dataclass
class IngestResult:
    status: str
    document: LegalDocument | None = None
    version: LegalDocumentVersion | None = None
    snapshot: SourceSnapshot | None = None
    detail: str = ""
    updates: list = field(default_factory=list)


def _blank(value) -> bool:
    return value is None or value == "" or value == ()


class IngestService:
    def __init__(self, source: LegalSource, store: ObjectStore, job=None):
        self.source, self.store, self.job = source, store, job
        self.unmapped_types: set[tuple[str, str]] = set()
        self._rank_cache: dict = {}

    # ------------------------------------------------------------------ public

    def ingest(self, parsed: ParsedDocument) -> IngestResult:
        with transaction.atomic():
            result = self._ingest(parsed)
            with connection.cursor() as cur:
                cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # FK/exclusion problems surface here, not at COMMIT
                # Restore deferral: the setting outlives this call inside a caller's transaction, and version
                # windows are shuffled with the exclusion constraint deliberately deferred.
                cur.execute("SET CONSTRAINTS ALL DEFERRED")
            return result

    # ------------------------------------------------------------------ internals

    def _ingest(self, parsed: ParsedDocument) -> IngestResult:
        is_current_obs = parsed.edition_on is None
        doc = LegalDocument.objects.select_for_update().filter(source=self.source, external_id=parsed.external_id).first()
        created = doc is None

        # An observation older than the last one we applied (a replayed/restored archive, a second collector)
        # must never re-install old text as current (TZ critical defect: parser marks an OLD edition current).
        if not created and is_current_obs and doc.last_checked_at and parsed.fetched_at < doc.last_checked_at:
            return IngestResult(
                ItemStatus.SKIPPED, doc,
                detail=f"stale observation {parsed.fetched_at:%Y-%m-%d} is older than the last check {doc.last_checked_at:%Y-%m-%d}; ignored",
            )

        before = None if created else {
            "status": doc.status, "expiry": (doc.status, doc.effective_to, doc.expiry_observed_on), "lang": (doc.language, doc.script),
        }
        if created:
            doc = LegalDocument(
                source=self.source, external_id=parsed.external_id,
                source_url=f"https://lex.uz/docs/{parsed.external_id}",
                title=parsed.title, representation=parsed.representation, text_status=parsed.text_status,
            )
            self._apply_attributes(doc, parsed, initial=True)
            self._track_expiry(doc, parsed)
            doc.save()
        elif is_current_obs:
            self._apply_attributes(doc, parsed)
            self._track_expiry(doc, parsed)

        version, outcome, detail = self._decide_version(doc, parsed)
        snapshot = None
        updates = []
        if outcome == "create":
            snapshot = self._store_snapshot(parsed.primary_snapshot) if parsed.primary_snapshot else None
            for extra in (s for s in parsed.snapshots if s is not parsed.primary_snapshot):
                self._store_snapshot(extra)
            version = self._create_version(doc, parsed, snapshot, first=created or not doc.versions.exists())
            self._save_sections(version, parsed)
            self._save_chunks(version, doc, parsed)
        elif outcome == "retime":
            # Identical text already stored under a GUESSED start date; the site has now told us the real
            # edition date. Window columns are mutable (content is not), so apply it and rebuild.
            if parsed.edition_on is not None:
                new_from, new_source = parsed.edition_on, ValidFromSource.EDITION
            else:  # start-date upgrade of the current text
                new_from, new_source = parsed.valid_from, parsed.valid_from_source
            LegalDocumentVersion.objects.filter(pk=version.pk).update(valid_from=new_from, valid_from_source=new_source)

        current = version if is_current_obs else doc.current_version
        expiry_changed = is_current_obs and before is not None and before["expiry"] != (doc.status, doc.effective_to, doc.expiry_observed_on)
        if outcome in ("create", "adopt", "retime") or expiry_changed:
            # windows depend on the chain AND on whether the act has lost force
            rebuild_validity(doc, current, self._expire_on(doc), reference=parsed.fetched_at.date())
        if is_current_obs and version is not None:
            doc.current_version = version

        if is_current_obs:
            doc.last_checked_at = parsed.fetched_at
            doc.last_source_sha256 = parsed.source_sha256
            doc.observation_fingerprint = parsed.fingerprint
        doc.save()

        if before is not None and before["lang"] != (doc.language, doc.script):
            # a corrected language changes the search key space (Cyrillic folding): stale chunks would never match
            skip = version.pk if (outcome == "create" and version is not None) else None
            for other in doc.versions.filter(metadata__text_status="ok").exclude(pk=skip):
                rechunk_version(other)

        if is_current_obs:
            self._save_cards(doc, parsed)
            self._save_classifications(doc, parsed)
            self._save_attachments(doc, parsed)
        self._save_relations(doc, parsed, new_sections=outcome == "create")

        # -- change monitoring (TZ 118)
        if outcome == "create" and is_current_obs:
            kind = UpdateType.NEW if created or doc.versions.count() == 1 else UpdateType.AMENDED
            updates.append(self._update(doc, version, kind, {"content_hash": parsed.content_hash}))
        previous_status = before["status"] if before else None
        if is_current_obs and not created and previous_status != doc.status:
            if doc.status == DocumentStatus.EXPIRED:
                updates.append(self._update(doc, version, UpdateType.EXPIRED, {"from": previous_status, "effective_to": str(doc.effective_to or "")}))
            elif previous_status == DocumentStatus.NOT_YET_EFFECTIVE and doc.status == DocumentStatus.ACTIVE:
                updates.append(self._update(doc, version, UpdateType.EFFECTIVE, {"from": previous_status}))

        if outcome == "create":
            status = ItemStatus.NEW if created else ItemStatus.UPDATED
        elif outcome == "retime" or (is_current_obs and updates):
            status = ItemStatus.UPDATED  # edition date applied / status change only
        else:
            status = ItemStatus.UNCHANGED
        return IngestResult(status, doc, version, snapshot, detail, updates)

    @staticmethod
    def _track_expiry(doc, parsed):
        """Remember when we first saw the act expired WITHOUT a known loss-of-force date, so its last window can
        be closed honestly and stably (the real date, once a card provides it, replaces this)."""
        if parsed.edition_on is not None:
            return
        if doc.status == DocumentStatus.EXPIRED:
            if doc.effective_to is None and doc.expiry_observed_on is None:
                doc.expiry_observed_on = parsed.fetched_at.date()
        else:
            doc.expiry_observed_on = None

    @staticmethod
    def _expire_on(doc):
        return (doc.effective_to or doc.expiry_observed_on) if doc.status == DocumentStatus.EXPIRED else None

    def _update(self, doc, version, kind, details):
        return LegalUpdate.objects.create(document=doc, version=version, parser_job=self.job, update_type=kind, details=details)

    # -- versions

    @staticmethod
    def _same_source(version, parsed) -> bool:
        """True when the page (and primary file) bytes are identical to what produced `version`.

        This is checked BEFORE comparing extracted text: an improved parser yields different text from the
        same bytes, and that must never look like an amendment of the law."""
        meta = version.metadata or {}
        primary = parsed.primary_snapshot.sha256 if parsed.primary_snapshot else ""
        return bool(parsed.source_sha256) and meta.get("source_sha256") == parsed.source_sha256 and meta.get("primary_sha256", "") == primary

    @staticmethod
    def _start_can_be_upgraded(doc, version, parsed) -> bool:
        """The FIRST version's start was only the day we first saw the act (nothing better was known then), and a
        better source now exists (e.g. the adoption date became parseable). Window columns are mutable; content is
        not, so the guess is replaced by the real date."""
        return (
            version.valid_from_source == ValidFromSource.OBSERVED
            and version.version_number == 1
            and parsed.valid_from_source != ValidFromSource.OBSERVED
            and parsed.valid_from is not None
            and not doc.versions.exclude(pk=version.pk).exists()
        )

    def _decide_version(self, doc, parsed):
        """-> (existing version or None, 'create' | 'unchanged' | 'adopt', detail)."""
        versions = doc.versions
        if parsed.edition_on is None:
            current = doc.current_version
            if current is not None:
                if self._same_source(current, parsed) or current.content_hash == parsed.content_hash:
                    if self._start_can_be_upgraded(doc, current, parsed):
                        return current, "retime", "validity start upgraded from the crawl date to a known date"
                    detail = "" if current.parser_version == STRUCTURE_VERSION else "same source; parser upgraded (reprocess pending)"
                    return current, "unchanged", detail
                return None, "create", ""
            # no current pointer yet: an identical edition fetched earlier IS the current text
            twin = versions.filter(content_hash=parsed.content_hash).order_by("-valid_from").first()
            if twin is not None:
                return twin, "adopt", "current text identical to an already-stored edition"
            return None, "create", ""
        same_edition = versions.filter(edition_on=parsed.edition_on).first()
        if same_edition is not None:
            if self._same_source(same_edition, parsed) or same_edition.content_hash == parsed.content_hash:
                return same_edition, "unchanged", ""
            raise VersionConflict(
                f"Historical edition {parsed.edition_on} of {doc.external_id} changed on the site "
                f"(stored {same_edition.content_hash[:12]} != new {parsed.content_hash[:12]}); needs human review"
            )
        twin = versions.filter(content_hash=parsed.content_hash).first()
        if twin is not None:
            if twin.valid_from_source != ValidFromSource.EDITION:
                return twin, "retime", "edition date applied to an already-stored identical text"
            return twin, "unchanged", "edition text identical to an existing version"
        return None, "create", ""

    def _create_version(self, doc, parsed, snapshot, *, first: bool):
        if parsed.edition_on is not None:
            valid_from, source = parsed.edition_on, ValidFromSource.EDITION
        elif first:
            valid_from, source = parsed.valid_from, parsed.valid_from_source
        else:
            # the text changed and the site does not say when: the honest start is when we saw it
            valid_from, source = parsed.fetched_at.date(), ValidFromSource.OBSERVED
        return LegalDocumentVersion.objects.create(
            document=doc, version_number=next_version_number(doc), edition_on=parsed.edition_on,
            valid_from=valid_from, valid_from_source=source, is_current=False,
            content_hash=parsed.content_hash, raw_snapshot=snapshot, normalized_text=parsed.normalized_text,
            text_source=parsed.text_source, parser_version=STRUCTURE_VERSION,
            metadata={**parsed.version_metadata, "text_status": parsed.text_status,
                      "observed_at": parsed.fetched_at.isoformat(), "warnings": parsed.warnings},
        )

    def _save_sections(self, version, parsed):
        rows = [
            LegalDocumentSection(
                id=d.id, version=version, parent_id=d.parent_id, section_type=d.section_type, number=d.number[:32],
                title=d.title, text=d.text, order_index=d.order_index, path=d.path, source_anchor=d.source_anchor,
                text_hash=d.text_hash, metadata=d.metadata,
            )
            for d in parsed.sections
        ]
        LegalDocumentSection.objects.bulk_create(rows, batch_size=BATCH)

    def _save_chunks(self, version, doc, parsed):
        # Stubs and scanned PDFs have no retrieval value; indexing them would pollute search (TZ 86).
        if parsed.text_status != TextStatus.OK or not parsed.sections:
            return
        rows = build_chunk_rows(version, doc, parsed.sections)
        DocumentChunk.objects.bulk_create(rows, batch_size=BATCH)

    # -- attributes

    def _rank_for(self, parsed):
        """Legal rank from the editable lookup: the card's (type, form) first, then the act's own
        (authority, form). Unmatched pairs are collected and reported, never guessed."""
        for kind, key in (("card", parsed.rank_key), ("requisite", parsed.requisite_key)):
            if not key or not key[0]:
                continue
            cache_key = (kind, *key)
            if cache_key not in self._rank_cache:
                qs = DocumentTypeRank.objects.filter(key_kind=kind, type_key=key[0])
                row = qs.filter(form_key=key[1]).first() or qs.filter(form_key="").first()
                self._rank_cache[cache_key] = row.rank if row else None
                if row is None:
                    self.unmapped_types.add(cache_key)
            if self._rank_cache[cache_key] is not None:
                return self._rank_cache[cache_key]
        return None

    def _apply_attributes(self, doc, parsed, initial=False):
        values = {
            "title": parsed.title, "document_number": parsed.document_number, "number_key": parsed.number_key,
            "language": parsed.language, "script": parsed.script, "language_source": parsed.language_source,
            "document_type": parsed.document_type, "document_form": parsed.document_form, "authority": parsed.authority,
            "adopted_at": parsed.adopted_at, "published_at": parsed.published_at,
            "effective_from": parsed.effective_from, "effective_to": parsed.effective_to,
            "representation": parsed.representation, "text_status": parsed.text_status,
        }
        for name, value in values.items():
            # never erase what an earlier run learned (e.g. from a card this run did not have)
            if not _blank(value) or initial:
                setattr(doc, name, value if not _blank(value) else getattr(doc, name))
        if parsed.status != DocumentStatus.UNKNOWN:
            doc.status, doc.status_raw = parsed.status, parsed.status_raw
        rank = self._rank_for(parsed)
        if rank is not None:
            doc.legal_rank = rank

    # -- snapshots

    def _save_attachments(self, doc, parsed):
        for draft in parsed.attachments:
            snapshot = self._store_snapshot(draft)
            LegalDocumentAttachment.objects.get_or_create(
                document=doc, snapshot=snapshot, defaults={"role": draft.role, "source_url": draft.url}
            )

    def _store_snapshot(self, draft: RawSnapshotDraft) -> SourceSnapshot:
        existing = SourceSnapshot.objects.filter(source=self.source, sha256=draft.sha256).first()
        if existing is not None:
            if not self.store.exists(existing.object_key):  # self-heal: store moved/rebuilt, DB row survived
                self.store.put(existing.object_key, draft.read(), content_type=draft.content_type, content_encoding=draft.content_encoding)
            return existing
        raw = draft.read()
        content = gzip.decompress(raw) if draft.content_encoding == "gzip" else raw
        if sha256_hex(content) != draft.sha256:
            raise IngestError(f"Checksum mismatch for {draft.path} (archive corrupted?)")
        key = snapshot_key(draft.sha256)
        self.store.put(key, raw, content_type=draft.content_type, content_encoding=draft.content_encoding)
        return SourceSnapshot.objects.create(
            source=self.source, kind=draft.kind, url=draft.url, sha256=draft.sha256, object_key=key,
            content_encoding=draft.content_encoding, content_type=draft.content_type,
            size_bytes=len(content), fetched_at=draft.fetched_at,
        )

    # -- side tables

    def _save_cards(self, doc, parsed):
        for card in parsed.cards:
            LegalDocumentCard.objects.update_or_create(
                document=doc, kind=card.kind,
                defaults={"raw_text": card.raw_text, "fields": card.fields, "fetched_at": card.fetched_at},
            )

    def _save_classifications(self, doc, parsed):
        rows = [
            LegalDocumentClassification(document=doc, system=c.system, code=c.code, label=c.label, fingerprint=c.fingerprint)
            for c in parsed.classifications
        ]
        LegalDocumentClassification.objects.bulk_create(rows, ignore_conflicts=True)

    def _save_relations(self, doc, parsed, *, new_sections: bool):
        ids = {r.target_external_id for r in parsed.relations}
        known = dict(LegalDocument.objects.filter(source=self.source, external_id__in=ids).values_list("external_id", "id"))
        rows = []
        for r in parsed.relations:
            if r.source_section is not None and not new_sections:
                continue  # section-level links belong to the sections of a newly created version
            rows.append(
                LegalDocumentRelation(
                    source_document=doc, source_section_id=r.source_section.id if r.source_section else None,
                    target_document_id=known.get(r.target_external_id), target_url=r.target_url,
                    target_external_id=r.target_external_id, target_edition_on=r.target_edition_on,
                    target_anchor=r.target_anchor, relation_type=r.relation_type, anchor_text=r.anchor_text,
                )
            )
        LegalDocumentRelation.objects.bulk_create(rows, ignore_conflicts=True, batch_size=BATCH)
