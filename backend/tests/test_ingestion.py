import gzip
import json
import tempfile
from datetime import date, datetime, timezone
from unittest import mock, skipUnless

from django.db import transaction
from django.test import TestCase

from apps.common.hashing import sha256_hex
from apps.common.storage import FilesystemObjectStore, snapshot_key
from apps.legal_documents.constants import DocumentStatus, RelationType, TextStatus
from apps.legal_documents.models import DocumentTypeRank, LegalDocument, LegalDocumentRelation, LegalDocumentSection, LegalDocumentVersion
from apps.legal_documents.parsing.relations import RelationDraft
from apps.legal_documents.services.groups import link_language_groups, resolve_relation_targets
from apps.legal_documents.services.versions import VersionConflict, rebuild_validity
from apps.legal_monitoring.models import LegalUpdate
from apps.legal_sources.models import SourceSnapshot
from apps.parsers.ingestion.loader import IngestError, IngestService
from apps.parsers.ingestion.runner import run_ingest
from apps.parsers.models import ItemStatus, JobStatus, ParserError, ParserItem
from apps.search.models import DocumentChunk
from apps.search.selectors import fts_candidates

from .factories import DATA_DIR, ORIGINAL_IDS, make_source, parsed, snapshot_draft
from .test_db_guarantees import as_role


def later(days):
    return datetime(2026, 9, 28 + days, tzinfo=timezone.utc) if days < 3 else datetime(2026, 10, days, tzinfo=timezone.utc)


class IngestBase(TestCase):
    def setUp(self):
        self.source = make_source()
        self.store = FilesystemObjectStore(tempfile.mkdtemp())
        self.svc = IngestService(self.source, self.store)

    def ingest(self, **kw):
        return self.svc.ingest(parsed(**kw))


class NewAndUnchangedTests(IngestBase):
    def test_new_document_creates_everything(self):
        r = self.ingest()
        self.assertEqual(r.status, ItemStatus.NEW)
        doc = r.document
        v = doc.versions.get()
        self.assertTrue(v.is_current)
        self.assertEqual(doc.current_version_id, v.id)
        self.assertEqual(v.sections.count(), 2)
        self.assertGreaterEqual(DocumentChunk.objects.filter(version=v).count(), 1)
        self.assertEqual(LegalUpdate.objects.filter(document=doc, update_type="new").count(), 1)

    def test_snapshot_bytes_live_in_object_storage_not_postgres(self):
        r = self.ingest()
        snap = r.snapshot
        self.assertTrue(self.store.exists(snap.object_key))
        self.assertEqual(snap.object_key, snapshot_key(snap.sha256))
        self.assertEqual(self.store.get(snap.object_key), gzip.compress(b"raw" + r.version.normalized_text.encode(), mtime=0))
        self.assertEqual(r.version.raw_snapshot_id, snap.id)

    def test_reingesting_the_same_content_is_a_noop(self):
        first = self.ingest()
        again = self.ingest(fetched=later(1))
        self.assertEqual(again.status, ItemStatus.UNCHANGED)
        self.assertEqual(first.document.versions.count(), 1)
        self.assertEqual(LegalUpdate.objects.count(), 1)
        self.assertEqual(SourceSnapshot.objects.count(), 1)
        first.document.refresh_from_db()
        self.assertEqual(first.document.last_checked_at, later(1))

    def test_snapshot_is_deduplicated_across_documents(self):
        self.ingest(external_id="1")
        p2 = parsed(external_id="2")
        p2.snapshots = [snapshot_draft(("raw" + p2.normalized_text).encode(), url="https://lex.uz/docs/2")]
        self.svc.ingest(p2)
        self.assertEqual(SourceSnapshot.objects.count(), 1)

    def test_checksum_mismatch_aborts_and_leaves_nothing_behind(self):
        p = parsed()
        p.snapshots[0].sha256 = "0" * 64
        with self.assertRaises(IngestError), transaction.atomic():
            self.svc.ingest(p)
        self.assertEqual(LegalDocument.objects.count(), 0)
        self.assertEqual(DocumentChunk.objects.count(), 0)


class AmendmentTests(IngestBase):
    def test_changed_text_creates_a_new_version_and_closes_the_old_window(self):
        self.ingest(paragraphs=("1. eski matn.",))
        r2 = self.ingest(paragraphs=("1. yangi matn.",), fetched=later(3))
        self.assertEqual(r2.status, ItemStatus.UPDATED)
        v1, v2 = r2.document.versions.order_by("version_number")
        self.assertEqual((v1.is_current, v2.is_current), (False, True))
        self.assertEqual(v1.valid_to, date.fromordinal(v2.valid_from.toordinal() - 1))
        self.assertIsNone(v2.valid_to)
        self.assertEqual(v1.normalized_text, "1. eski matn.")  # the old edition is preserved verbatim
        r2.document.refresh_from_db()
        self.assertEqual(r2.document.current_version_id, v2.id)
        self.assertEqual(LegalUpdate.objects.filter(update_type="amended").count(), 1)
        self.assertTrue(DocumentChunk.objects.filter(version=v1).exists())  # history stays searchable (TZ 77)

    def test_as_of_search_returns_the_edition_in_force(self):
        self.ingest(paragraphs=("1. eskisi mehnat shartnomasi.",), adopted=date(2026, 1, 10))
        self.ingest(paragraphs=("1. yangisi mehnat shartnomasi.",), fetched=later(3), adopted=date(2026, 1, 10))
        self.assertEqual(len(fts_candidates("eskisi", as_of=date(2026, 6, 1))), 1)
        self.assertEqual(len(fts_candidates("yangisi", as_of=date(2026, 12, 1))), 1)
        self.assertEqual(fts_candidates("eskisi", as_of=date(2026, 12, 1)), [])
        self.assertEqual(fts_candidates("yangisi", as_of=date(2026, 6, 1)), [])
        self.assertEqual(fts_candidates("mehnat", as_of=date(2025, 1, 1)), [])  # before the act existed

    def test_parser_improvement_on_identical_bytes_is_not_an_amendment(self):
        # Improving our own text extraction must never mint a new *legal* version or an "amended" alert.
        self.ingest(paragraphs=("1. eski   spacing bilan matn.",), source_sha256="S" * 64)
        r = self.ingest(paragraphs=("1. tuzatilgan spacing bilan matn.",), source_sha256="S" * 64, fetched=later(3))
        self.assertEqual(r.status, ItemStatus.UNCHANGED)
        self.assertEqual(LegalDocumentVersion.objects.count(), 1)
        self.assertEqual(LegalUpdate.objects.filter(update_type="amended").count(), 0)
        # ...but genuinely different source bytes with different text still is one
        r3 = self.ingest(paragraphs=("1. haqiqatan o'zgargan.",), source_sha256="T" * 64, fetched=later(4))
        self.assertEqual(r3.status, ItemStatus.UPDATED)
        self.assertEqual(LegalDocumentVersion.objects.count(), 2)

    def test_same_day_double_change_never_overlaps(self):
        self.ingest(paragraphs=("1. a.",))
        self.ingest(paragraphs=("1. b.",))
        self.ingest(paragraphs=("1. c.",))
        starts = list(LegalDocumentVersion.objects.order_by("version_number").values_list("valid_from", flat=True))
        self.assertEqual(starts, sorted(set(starts)))  # strictly increasing


class GuessedStartUpgradeTests(IngestBase):
    def test_a_start_that_was_only_the_crawl_date_is_upgraded_once_a_real_date_is_known(self):
        # first ingest: no date could be parsed (older parser) -> the version starts on the day we saw it
        self.ingest(paragraphs=("1. matn.",), adopted=None, valid_from=date(2026, 9, 20), valid_from_source="observed", source_sha256="A" * 64)
        v = LegalDocumentVersion.objects.get()
        self.assertEqual((v.valid_from, v.valid_from_source), (date(2026, 9, 20), "observed"))
        self.assertEqual(fts_candidates("matn", as_of=date(2020, 1, 1)), [])  # wrongly invisible before the crawl date
        # same bytes, but the parser now finds the adoption date
        r = self.ingest(paragraphs=("1. matn.",), adopted=date(1997, 11, 4), source_sha256="A" * 64, fetched=later(3))
        v.refresh_from_db()
        self.assertEqual(r.status, ItemStatus.UPDATED)
        self.assertEqual((v.valid_from, v.valid_from_source), (date(1997, 11, 4), "adopted"))
        self.assertEqual(LegalDocumentVersion.objects.count(), 1)  # content untouched, no fake amendment
        self.assertEqual(LegalUpdate.objects.filter(update_type="amended").count(), 0)
        self.assertEqual(len(fts_candidates("matn", as_of=date(2020, 1, 1))), 1)
        again = self.ingest(paragraphs=("1. matn.",), adopted=date(1997, 11, 4), source_sha256="A" * 64, fetched=later(4))
        self.assertEqual(again.status, ItemStatus.UNCHANGED)  # nothing left to upgrade

    def test_a_real_start_date_is_never_overwritten_by_a_later_observation(self):
        self.ingest(paragraphs=("1. matn.",), adopted=date(2010, 1, 1), source_sha256="B" * 64)
        r = self.ingest(paragraphs=("1. matn.",), adopted=date(2015, 5, 5), source_sha256="B" * 64, fetched=later(3))
        self.assertEqual(r.status, ItemStatus.UNCHANGED)
        self.assertEqual(LegalDocumentVersion.objects.get().valid_from, date(2010, 1, 1))

    def test_an_amended_versions_observed_start_is_not_touched(self):
        self.ingest(paragraphs=("1. eski.",), adopted=date(2010, 1, 1), source_sha256="C" * 64)
        self.ingest(paragraphs=("1. yangi.",), adopted=date(2010, 1, 1), source_sha256="D" * 64, fetched=later(3))
        v2 = LegalDocumentVersion.objects.get(version_number=2)
        self.assertEqual(v2.valid_from_source, "observed")
        r = self.ingest(paragraphs=("1. yangi.",), adopted=date(2010, 1, 1), source_sha256="D" * 64, fetched=later(4))
        self.assertEqual(r.status, ItemStatus.UNCHANGED)  # only a FIRST version's guessed start is upgradeable


class ExpiryTests(IngestBase):
    def test_expired_act_drops_out_of_search_after_it_lost_force(self):
        self.ingest(paragraphs=("1. mehnat tartibi.",), status=DocumentStatus.EXPIRED, effective_to=date(2026, 6, 30))
        v = LegalDocumentVersion.objects.get()
        self.assertEqual(v.valid_to, date(2026, 6, 30))
        self.assertEqual(len(fts_candidates("mehnat", as_of=date(2026, 6, 30))), 1)
        self.assertEqual(fts_candidates("mehnat", as_of=date(2026, 7, 1)), [])

    def test_status_turning_expired_on_unchanged_text_closes_the_window(self):
        self.ingest(paragraphs=("1. mehnat tartibi.",), status=DocumentStatus.ACTIVE)
        r = self.ingest(paragraphs=("1. mehnat tartibi.",), status=DocumentStatus.EXPIRED, effective_to=date(2026, 8, 1), fetched=later(3))
        v = LegalDocumentVersion.objects.get()
        self.assertEqual(v.valid_to, date(2026, 8, 1))
        self.assertEqual(r.document.status, DocumentStatus.EXPIRED)
        self.assertEqual(LegalUpdate.objects.filter(update_type="expired").count(), 1)
        self.assertEqual(r.status, ItemStatus.UPDATED)

    def test_unknown_status_act_is_still_returned_and_flagged(self):
        self.ingest(paragraphs=("1. mehnat tartibi.",), status=DocumentStatus.UNKNOWN)
        hit = fts_candidates("mehnat", as_of=date(2026, 12, 1))[0]
        self.assertEqual(hit["status"], "unknown")  # surfaced, never silently "active"

    def test_not_yet_effective_act_is_excluded_until_it_starts(self):
        self.ingest(paragraphs=("1. mehnat tartibi.",), status=DocumentStatus.NOT_YET_EFFECTIVE, valid_from=date(2027, 1, 1), valid_from_source="card_effective")
        self.assertEqual(fts_candidates("mehnat", as_of=date(2026, 12, 1)), [])
        self.assertEqual(len(fts_candidates("mehnat", as_of=date(2027, 1, 1))), 1)


class EditionTests(IngestBase):
    def test_history_backfill_orders_the_chain_and_keeps_current_last(self):
        self.ingest(paragraphs=("1. hozirgi matn.",), adopted=date(2020, 1, 10))
        self.ingest(paragraphs=("1. 2023 tahriri.",), edition_on=date(2023, 5, 1))
        self.ingest(paragraphs=("1. 2018 tahriri.",), edition_on=date(2018, 1, 1))
        doc = LegalDocument.objects.get()
        chain = list(doc.versions.order_by("valid_from"))
        self.assertEqual([v.normalized_text for v in chain], ["1. 2018 tahriri.", "1. 2023 tahriri.", "1. hozirgi matn."])
        self.assertEqual([v.is_current for v in chain], [False, False, True])
        self.assertEqual(chain[0].valid_to, date(2023, 4, 30))
        self.assertEqual(chain[1].valid_to, date.fromordinal(chain[2].valid_from.toordinal() - 1))
        self.assertIsNone(chain[2].valid_to)
        # our guessed start of the current text was pushed past the last real edition, and says so
        self.assertEqual(chain[2].valid_from_source, "observed_adjusted")
        self.assertEqual(chain[0].valid_from_source, "edition")
        self.assertEqual(LegalUpdate.objects.filter(update_type="amended").count(), 0)  # backfill is not a change

    def test_edition_observation_does_not_overwrite_current_attributes(self):
        self.ingest(title="Joriy sarlavha", paragraphs=("1. hozirgi.",))
        self.ingest(title="Eski sarlavha", paragraphs=("1. eski.",), edition_on=date(2019, 1, 1))
        self.assertEqual(LegalDocument.objects.get().title, "Joriy sarlavha")

    def test_restated_history_is_a_conflict_not_an_overwrite(self):
        self.ingest(paragraphs=("1. a.",), edition_on=date(2019, 1, 1))
        with self.assertRaises(VersionConflict):
            self.ingest(paragraphs=("1. b.",), edition_on=date(2019, 1, 1))
        self.assertEqual(LegalDocumentVersion.objects.get().normalized_text, "1. a.")

    def test_edition_identical_to_existing_text_adds_no_version_but_applies_its_real_date(self):
        # The current page was stored with a GUESSED start (adoption date). An ?ONDATE= fetch of identical text
        # tells us the real edition date: no new version, but the guess is replaced by the authoritative date.
        self.ingest(paragraphs=("1. same.",), adopted=date(2020, 1, 10))
        r = self.ingest(paragraphs=("1. same.",), edition_on=date(2021, 1, 1))
        self.assertEqual(r.status, ItemStatus.UPDATED)
        v = LegalDocumentVersion.objects.get()
        self.assertEqual((v.valid_from, v.valid_from_source), (date(2021, 1, 1), "edition"))
        again = self.ingest(paragraphs=("1. same.",), edition_on=date(2021, 1, 1))
        self.assertEqual(again.status, ItemStatus.UNCHANGED)  # nothing left to apply
        self.assertEqual(LegalDocumentVersion.objects.count(), 1)

    def test_current_text_identical_to_stored_edition_is_adopted_as_current(self):
        self.ingest(paragraphs=("1. same.",), edition_on=date(2021, 1, 1))
        r = self.ingest(paragraphs=("1. same.",))
        self.assertEqual(LegalDocumentVersion.objects.count(), 1)
        v = LegalDocumentVersion.objects.get()
        self.assertTrue(v.is_current)
        self.assertEqual(r.document.current_version_id, v.id)

    def test_two_authoritative_editions_cannot_share_a_start_date(self):
        self.ingest(paragraphs=("1. a.",), edition_on=date(2019, 1, 1))
        doc = LegalDocument.objects.get()
        with self.assertRaises(VersionConflict), transaction.atomic():  # savepoint: the bad row must not survive
            LegalDocumentVersion.objects.create(
                document=doc, version_number=2, edition_on=date(2019, 6, 1), valid_from=date(2019, 1, 1),
                valid_from_source="edition", content_hash="b" * 64, normalized_text="x", text_source="html", parser_version=1,
            )
            rebuild_validity(doc, None)
        self.assertEqual(doc.versions.count(), 1)


class StatusAndAttributeTests(IngestBase):
    def test_stub_and_scanned_documents_are_not_chunked(self):
        stub = self.ingest(external_id="s", text_status=TextStatus.STUB)
        ocr = self.ingest(external_id="o", text_status=TextStatus.NEEDS_OCR)
        self.assertEqual(DocumentChunk.objects.filter(document__in=[stub.document, ocr.document]).count(), 0)
        self.assertEqual(stub.document.text_status, TextStatus.STUB)

    def test_textless_documents_hash_on_the_raw_source(self):
        # two different scanned PDFs both have empty text; they must not be seen as "unchanged"
        def scanned(raw):
            p = parsed(external_id="7", paragraphs=(), text_status=TextStatus.NEEDS_OCR, snapshot=False)
            p.normalized_text, p.sections = "", []
            p.snapshots = [snapshot_draft(raw, kind="pdf", gz=False, url="https://lex.uz/pdffile/7")]
            p.content_hash = sha256_hex("raw:" + p.snapshots[0].sha256)
            p.source_sha256 = p.snapshots[0].sha256
            p.version_metadata = {"source_sha256": p.source_sha256, "primary_sha256": p.snapshots[0].sha256}
            return p

        self.assertEqual(self.svc.ingest(scanned(b"%PDF-1 first")).status, ItemStatus.NEW)
        self.assertEqual(self.svc.ingest(scanned(b"%PDF-1 first")).status, ItemStatus.UNCHANGED)
        self.assertEqual(self.svc.ingest(scanned(b"%PDF-1 second (revised scan)")).status, ItemStatus.UPDATED)
        self.assertEqual(LegalDocumentVersion.objects.count(), 2)

    def test_later_run_without_a_card_does_not_erase_what_an_earlier_run_learned(self):
        self.ingest(status=DocumentStatus.ACTIVE, effective_to=None)
        p = parsed(status=DocumentStatus.UNKNOWN, fetched=later(3))
        p.document_type, p.authority = "", ""
        r = self.svc.ingest(p)
        self.assertEqual(r.document.status, DocumentStatus.ACTIVE)

    def test_unmapped_document_types_are_reported_not_guessed(self):
        p = parsed()
        p.rank_key = ("акты президента", "указ")
        r = self.svc.ingest(p)
        self.assertIsNone(r.document.legal_rank)
        self.assertIn(("card", "акты президента", "указ"), self.svc.unmapped_types)

    def test_requisite_key_is_the_fallback_and_the_card_wins_when_both_exist(self):
        DocumentTypeRank.objects.create(key_kind="requisite", type_key="ozbekiston respublikasi prezidentining", form_key="qarori", rank=75)
        p = parsed(external_id="a")
        p.requisite_key = ("ozbekiston respublikasi prezidentining", "qarori")
        self.assertEqual(self.svc.ingest(p).document.legal_rank, 75)  # no card at all
        DocumentTypeRank.objects.create(key_kind="card", type_key="акты президента", form_key="указ", rank=80)
        p2 = parsed(external_id="b")
        p2.rank_key, p2.requisite_key = ("акты президента", "указ"), ("ozbekiston respublikasi prezidentining", "qarori")
        self.assertEqual(self.svc.ingest(p2).document.legal_rank, 80)

    def test_rank_lookup_is_data_driven(self):
        DocumentTypeRank.objects.create(type_key="акты президента", form_key="указ", rank=80)
        p = parsed()
        p.rank_key = ("акты президента", "указ")
        self.assertEqual(self.svc.ingest(p).document.legal_rank, 80)


class RelationAndGroupTests(IngestBase):
    def rel(self, target, kind=RelationType.CITES):
        return RelationDraft(kind, f"https://lex.uz/docs/{target}", target, None, "")

    def test_links_to_not_yet_ingested_documents_are_kept_and_resolved_later(self):
        a = self.ingest(external_id="1", relations=[self.rel("2")]).document
        r = LegalDocumentRelation.objects.get(source_document=a)
        self.assertIsNone(r.target_document_id)
        self.ingest(external_id="2")
        self.assertEqual(resolve_relation_targets(self.source), 1)
        r.refresh_from_db()
        self.assertEqual(r.target_document.external_id, "2")

    def test_language_variants_share_one_stable_group(self):
        self.ingest(external_id="10", relations=[self.rel("11", RelationType.LANGUAGE_VARIANT), self.rel("12", RelationType.LANGUAGE_VARIANT)])
        self.ingest(external_id="11", relations=[self.rel("10", RelationType.LANGUAGE_VARIANT)])
        self.ingest(external_id="12")
        self.ingest(external_id="99")
        resolve_relation_targets(self.source)
        link_language_groups(self.source)
        groups = dict(LegalDocument.objects.values_list("external_id", "group_id"))
        self.assertEqual(groups["10"], groups["11"])
        self.assertEqual(groups["10"], groups["12"])
        self.assertIsNone(groups["99"])
        before = groups["10"]
        link_language_groups(self.source)  # idempotent, ids are stable
        self.assertEqual(LegalDocument.objects.get(external_id="10").group_id, before)

    def test_section_level_citations_hang_off_their_section(self):
        p = parsed(external_id="1")
        rel = RelationDraft(RelationType.CITES, "https://lex.uz/docs/5#77", "5", None, "77", "see", p.sections[0])
        p.relations = [rel]
        self.svc.ingest(p)
        stored = LegalDocumentRelation.objects.get()
        self.assertEqual(stored.source_section_id, p.sections[0].id)
        self.assertEqual(stored.target_anchor, "77")


class ChunkIdentityTests(IngestBase):
    def test_every_chunk_names_its_legal_document(self):
        r = self.ingest(external_id="900", title="Mehnat kodeksi", number="ORQ-599", adopted=date(2023, 10, 30),
                        paragraphs=tuple(f"{i}. " + "matn " * 40 for i in range(1, 12)))
        chunks = list(DocumentChunk.objects.filter(version=r.version))
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.metadata["document_title"], "Mehnat kodeksi")
            self.assertEqual(c.metadata["document_number"], "ORQ-599")
            self.assertEqual(c.metadata["document_external_id"], "900")
            self.assertEqual(c.metadata["source_url"], "https://lex.uz/docs/900")
            self.assertEqual(c.metadata["adopted_at"], "2023-10-30")
            self.assertEqual(c.metadata["version_number"], 1)
            self.assertNotIn("valid_from", c.metadata)  # dates that can change later live on the version, not copied
            self.assertNotIn("valid_to", c.metadata)
            self.assertNotIn("status", c.metadata)
            self.assertEqual(c.text.count("Mehnat kodeksi"), 0)  # the verbatim text is untouched: the title is metadata only

    def test_a_query_naming_the_law_finds_chunks_whose_own_text_does_not_contain_the_words(self):
        self.ingest(external_id="901", title="Yer kodeksi", paragraphs=("1. Birinchi band matni.",))
        self.assertEqual(len(fts_candidates("yer kodeksi", as_of=date(2026, 9, 28))), 1)

    def test_chunks_without_any_identity_are_found_stale(self):
        from apps.search.services import versions_needing_rechunk

        r = self.ingest(external_id="903")
        for c in DocumentChunk.objects.filter(version=r.version):
            metadata = {k: v for k, v in c.metadata.items() if not k.startswith("document_")}
            DocumentChunk.objects.filter(pk=c.pk).update(metadata=metadata)
        self.assertEqual(list(versions_needing_rechunk()), [r.version])

    def test_a_corrected_title_marks_the_chunks_for_rebuild_and_the_rebuild_fixes_them(self):
        from apps.search.services import rechunk_version, versions_needing_rechunk

        r = self.ingest(external_id="902", title="Eski nom")
        self.assertEqual(list(versions_needing_rechunk()), [])
        LegalDocument.objects.filter(external_id="902").update(title="Yangi nom")
        self.assertEqual(list(versions_needing_rechunk()), [r.version])
        rechunk_version(LegalDocumentVersion.objects.get(pk=r.version.pk))  # as the command does: fresh rows
        self.assertEqual({c.metadata["document_title"] for c in DocumentChunk.objects.filter(version=r.version)}, {"Yangi nom"})
        self.assertEqual(list(versions_needing_rechunk()), [])


class RechunkTests(IngestBase):
    def test_rechunk_replaces_chunks_but_never_touches_sections(self):
        from apps.embeddings.models import ChunkEmbedding, EmbeddingProfile
        from apps.search.chunking import CHUNKER_VERSION
        from apps.search.services import rechunk_version, versions_needing_rechunk

        r = self.ingest(paragraphs=tuple(f"{i}. " + "matn " * 40 for i in range(1, 12)))
        v = r.version
        section_ids = set(v.sections.values_list("id", flat=True))
        old_ids = set(DocumentChunk.objects.filter(version=v).values_list("id", flat=True))
        profile = EmbeddingProfile.objects.create(name="p3", model_name="m", model_version="1", dimension=3)
        ChunkEmbedding.objects.create(chunk_id=next(iter(old_ids)), embedding_profile=profile, embedding=[1, 2, 3])

        self.assertEqual(list(versions_needing_rechunk()), [])  # up to date
        DocumentChunk.objects.filter(version=v).update(chunker_version=CHUNKER_VERSION - 1)
        self.assertEqual(list(versions_needing_rechunk()), [v])

        written = rechunk_version(v)
        self.assertEqual(written, len(old_ids))
        new_ids = set(DocumentChunk.objects.filter(version=v).values_list("id", flat=True))
        self.assertFalse(new_ids & old_ids)  # fresh chunks
        self.assertEqual(set(v.sections.values_list("id", flat=True)), section_ids)  # sections untouched
        self.assertEqual(ChunkEmbedding.objects.count(), 0)  # stale embeddings went with the old chunks
        self.assertEqual(list(versions_needing_rechunk()), [])

    def test_rechunk_recovers_a_document_whose_chunks_are_missing(self):
        from apps.search.services import rechunk_version, versions_needing_rechunk

        r = self.ingest()
        DocumentChunk.objects.filter(version=r.version).delete()
        self.assertEqual(list(versions_needing_rechunk()), [r.version])
        self.assertGreater(rechunk_version(r.version), 0)


class GrantTests(TestCase):
    def test_grants_are_idempotent_to_reapply(self):
        from apps.common.db import apply_role_grants

        apply_role_grants()
        apply_role_grants()
        make_source()
        with as_role("yurist_api"):
            self.assertEqual(LegalDocument.objects.count(), 0)  # still readable after re-applying


# --------------------------------------------------------------------------- job runner

ARCHIVE = DATA_DIR / "state.sqlite3"


@skipUnless(ARCHIVE.exists(), "crawler archive not present")
class RunnerTests(TestCase):
    def run_job(self, **kw):
        store = FilesystemObjectStore(tempfile.mkdtemp())
        return run_ingest(DATA_DIR, store=store, retry_delays=(0, 0, 0), **kw)

    def test_one_bad_document_does_not_stop_the_job(self):
        from apps.parsers.ingestion import runner

        real = runner.build_parsed_document

        def flaky(archived, archive):
            if archived.record["document_id"] == "8501626":
                raise ValueError("boom")
            return real(archived, archive)

        with mock.patch.object(runner, "build_parsed_document", flaky):
            job = self.run_job(only_ids={"8501626", "-8501626", "8477063"})
        self.assertEqual(job.status, JobStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual((job.new_count, job.failed_count), (2, 1))
        err = ParserError.objects.get(job=job)
        self.assertIn("boom", err.exception)
        self.assertTrue(err.url.endswith("/docs/8501626"))
        self.assertEqual(LegalDocument.objects.count(), 2)  # the bad one left nothing behind

    def test_transient_db_errors_are_retried_with_backoff(self):
        from django.db import OperationalError

        calls = {"n": 0}
        real = IngestService.ingest

        def flaky(self_, parsed_doc):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OperationalError("deadlock detected")
            return real(self_, parsed_doc)

        with mock.patch.object(IngestService, "ingest", flaky):
            job = self.run_job(only_ids={"8477063"})
        self.assertEqual((job.new_count, job.failed_count), (1, 0))
        self.assertEqual(calls["n"], 3)

    def test_ingestion_works_with_only_the_parser_role_privileges(self):
        make_source()
        with as_role("yurist_parser"):
            job = self.run_job(only_ids={"8501626", "-8501626", "8508670", "8477063"})
        self.assertEqual(job.status, JobStatus.COMPLETED, job.error_summary)
        self.assertEqual(job.failed_count, 0)
        self.assertGreater(DocumentChunk.objects.count(), 0)


@skipUnless(ARCHIVE.exists(), "crawler archive not present")
class FullArchiveTests(TestCase):
    """The whole real archive end to end."""

    @classmethod
    def setUpTestData(cls):
        cls.store = FilesystemObjectStore(tempfile.mkdtemp())
        cls.job = run_ingest(DATA_DIR, store=cls.store, only_ids=ORIGINAL_IDS, retry_delays=(0,))

    def test_everything_ingests(self):
        self.assertEqual(self.job.failed_count, 0, list(ParserError.objects.values_list("url", "exception")))
        self.assertEqual(LegalDocument.objects.count(), self.job.total_count)
        self.assertEqual(self.job.new_count, self.job.total_count)

    def test_text_status_drives_chunking(self):
        for doc in LegalDocument.objects.all():
            n = DocumentChunk.objects.filter(document=doc).count()
            if doc.text_status == TextStatus.OK:
                self.assertGreater(n, 0, doc.external_id)
            else:
                self.assertEqual(n, 0, f"{doc.external_id} ({doc.text_status}) must not be indexed")
        statuses = set(LegalDocument.objects.values_list("text_status", flat=True))
        self.assertEqual(statuses, {"ok", "stub", "needs_ocr"})

    def test_sections_match_source_blocks_one_to_one(self):
        for folder in sorted((DATA_DIR / "documents").iterdir()):
            if folder.name.rsplit("_", 1)[0] not in ORIGINAL_IDS:
                continue
            rec = json.loads((folder / "document.json").read_text(encoding="utf-8"))
            if rec["representation"] != "html":
                continue
            expected = sum(1 for b in rec["blocks"] if b["is_content"])
            got = LegalDocumentSection.objects.filter(version__document__external_id=rec["document_id"]).count()
            self.assertEqual(got, expected, rec["url"])

    def test_no_document_left_without_exactly_one_current_version(self):
        for doc in LegalDocument.objects.all():
            self.assertEqual(doc.versions.filter(is_current=True).count(), 1, doc.external_id)
            self.assertTrue(doc.current_version.is_current)

    def test_publication_date_is_used_when_no_card_says_when_the_act_took_effect(self):
        by_source = {}
        for v in LegalDocumentVersion.objects.select_related("document"):
            by_source.setdefault(v.valid_from_source, []).append(v)
        self.assertGreater(len(by_source.get("published", [])), 30)
        for v in by_source["published"]:
            self.assertEqual(v.valid_from, v.document.published_at)
        for v in by_source.get("card_effective", []):
            self.assertEqual(v.valid_from, v.document.effective_from)

    def test_cyrillic_and_latin_variants_are_grouped_and_share_number_keys(self):
        grouped = 0
        by_group = {}
        for d in LegalDocument.objects.exclude(group_id=None):
            by_group.setdefault(d.group_id, []).append(d)
        for docs in by_group.values():
            uz = {d.script: d for d in docs if d.language == "uz"}
            if {"cyrl", "latn"} <= set(uz):
                grouped += 1
                self.assertEqual(uz["cyrl"].number_key, uz["latn"].number_key, (uz["cyrl"].external_id, uz["latn"].external_id))
                self.assertEqual(uz["cyrl"].adopted_at, uz["latn"].adopted_at)
        self.assertGreater(grouped, 5)

    def test_latin_query_finds_cyrillic_documents_and_vice_versa(self):
        hits = fts_candidates("mehnat faoliyati baxtsiz hodisalar", as_of=date(2026, 12, 1), limit=100)
        scripts = {LegalDocument.objects.get(pk=h["document_id"]).script for h in hits}
        self.assertEqual(scripts, {"cyrl", "latn"})
        hits_cyr = fts_candidates("меҳнат фаолияти бахтсиз ҳодисалар", as_of=date(2026, 12, 1), limit=100)
        self.assertEqual({h["document_id"] for h in hits}, {h["document_id"] for h in hits_cyr})

    def test_exact_number_lookup_across_scripts(self):
        from apps.legal_documents.parsing.titles import number_key

        found = LegalDocument.objects.filter(number_key=number_key("ПҚ-345"))
        self.assertEqual({d.script for d in found if d.language == "uz"}, {"cyrl", "latn"})

    def test_only_events_get_item_rows_unchanged_are_just_counted(self):
        self.assertEqual(ParserItem.objects.filter(job=self.job).count(), self.job.new_count + self.job.updated_count + self.job.failed_count)
        rerun = run_ingest(DATA_DIR, store=self.store, only_ids=ORIGINAL_IDS, retry_delays=(0,))
        self.assertEqual(ParserItem.objects.filter(job=rerun).count(), 0)
        self.assertEqual(rerun.unchanged_count, rerun.total_count)

    def test_second_run_is_a_pure_noop(self):
        before = (LegalDocumentVersion.objects.count(), DocumentChunk.objects.count(), LegalUpdate.objects.count(), SourceSnapshot.objects.count())
        job = run_ingest(DATA_DIR, store=self.store, only_ids=ORIGINAL_IDS, retry_delays=(0,))
        self.assertEqual((job.unchanged_count, job.new_count, job.updated_count, job.failed_count), (job.total_count, 0, 0, 0))
        after = (LegalDocumentVersion.objects.count(), DocumentChunk.objects.count(), LegalUpdate.objects.count(), SourceSnapshot.objects.count())
        self.assertEqual(before, after)

    def test_unmapped_types_are_surfaced_on_the_job(self):
        self.assertIn("DocumentTypeRank", self.job.error_summary)

    def test_every_snapshot_row_has_its_bytes_in_the_store(self):
        for snap in SourceSnapshot.objects.all():
            self.assertTrue(self.store.exists(snap.object_key), snap.object_key)

    def test_every_chunk_points_at_a_section_of_its_own_version(self):
        from django.db.models import F

        self.assertEqual(DocumentChunk.objects.exclude(section__version_id=F("version_id")).count(), 0)
