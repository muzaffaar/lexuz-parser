"""Regression tests for the issues found by the independent adversarial review. Each one reproduces the
reviewer's scenario; they failed against the code as first reviewed."""
import json
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock, skipUnless

from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from apps.common.db import organization_context
from apps.common.storage import FilesystemObjectStore
from apps.embeddings.models import ChunkEmbedding, EmbeddingProfile
from apps.legal_documents.constants import DocumentStatus
from apps.legal_documents.models import LegalDocument, LegalDocumentSection, LegalDocumentVersion
from apps.legal_documents.text.normalize import normalize_search
from apps.organizations.models import Organization
from apps.parsers.ingestion.loader import IngestService
from apps.parsers.ingestion.runner import run_ingest
from apps.parsers.models import ItemStatus, JobStatus, ParserError, ParserItem
from apps.search.models import DocumentChunk
from apps.search.selectors import fts_candidates

from .factories import DATA_DIR, make_source, parsed
from .test_db_guarantees import as_role, denied, make_chunk, make_doc, make_version
from .test_ingestion import IngestBase


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


class OrganizationDirectoryIsTenantScoped(TestCase):
    """Finding 1: a tenant must not be able to list other organizations."""

    def test_tenant_sees_only_its_own_organization_row(self):
        a = Organization.objects.create(name="Org A", slug="a")
        Organization.objects.create(name="Org B secret client", slug="b")
        with as_role("yurist_api", a):
            self.assertEqual(list(Organization.objects.values_list("slug", flat=True)), ["a"])
        with as_role("yurist_api"):
            self.assertEqual(Organization.objects.count(), 0)  # no org context -> no directory at all

    def test_parser_role_has_no_access_to_organizations(self):
        Organization.objects.create(name="Org A", slug="a")
        with as_role("yurist_parser"):
            with denied():
                Organization.objects.count()


class ReplayAndOrdering(IngestBase):
    def test_replaying_an_older_crawl_cannot_make_old_text_current(self):
        """Finding 2 (TZ critical defect: parser marks an OLD edition current)."""
        self.ingest(paragraphs=("1. matn A.",), fetched=dt(2026, 3, 1))
        self.ingest(paragraphs=("1. matn B.",), fetched=dt(2026, 9, 1))
        r = self.ingest(paragraphs=("1. matn A.",), fetched=dt(2026, 3, 1))  # an old archive replayed
        self.assertEqual(r.status, ItemStatus.SKIPPED)
        doc = LegalDocument.objects.get()
        self.assertEqual(doc.versions.count(), 2)
        self.assertEqual(doc.current_version.normalized_text, "1. matn B.")
        self.assertEqual(doc.last_checked_at, dt(2026, 9, 1))  # not moved backwards

    def test_ingest_order_does_not_change_which_edition_is_in_force(self):
        """Finding 3: current page first (the natural crawl order), history afterwards."""
        self.ingest(paragraphs=("1. C matn.",), adopted=date(2020, 1, 1), fetched=dt(2026, 9, 1))
        self.ingest(paragraphs=("1. A matn.",), edition_on=date(2020, 1, 1), fetched=dt(2026, 9, 2))
        self.ingest(paragraphs=("1. B matn.",), edition_on=date(2022, 6, 1), fetched=dt(2026, 9, 3))
        r = self.ingest(paragraphs=("1. C matn.",), edition_on=date(2024, 3, 1), fetched=dt(2026, 9, 4))
        self.assertEqual(r.status, ItemStatus.UPDATED)  # the edition date is applied, not silently dropped
        chain = list(LegalDocumentVersion.objects.order_by("valid_from"))
        self.assertEqual([v.normalized_text for v in chain], ["1. A matn.", "1. B matn.", "1. C matn."])
        self.assertEqual(chain[2].valid_from, date(2024, 3, 1))
        self.assertEqual(chain[2].valid_from_source, "edition")
        self.assertEqual(chain[1].valid_to, date(2024, 2, 29))
        self.assertTrue(chain[2].is_current)
        self.assertEqual(LegalDocument.objects.get().versions.get(valid_period__contains=date(2023, 1, 1)).normalized_text, "1. B matn.")

    def test_a_future_dated_edition_does_not_remove_the_act_from_today(self):
        """Finding 6."""
        self.ingest(paragraphs=("1. hozirgi matn.",), adopted=date(2026, 1, 10), fetched=dt(2026, 9, 1))
        self.ingest(paragraphs=("1. kelgusi tahrir.",), edition_on=date(2027, 1, 1), fetched=dt(2026, 9, 2))
        doc = LegalDocument.objects.get()
        today = doc.versions.get(valid_period__contains=date(2026, 10, 1))
        self.assertEqual(today.normalized_text, "1. hozirgi matn.")
        self.assertTrue(today.is_current)
        future = doc.versions.get(valid_period__contains=date(2027, 6, 1))
        self.assertEqual(future.normalized_text, "1. kelgusi tahrir.")


class ExpiryWithoutADate(IngestBase):
    def test_expired_act_with_unknown_loss_of_force_date_is_not_in_force_forever(self):
        """Finding 5."""
        self.ingest(paragraphs=("1. zebrafish tartibi.",), status=DocumentStatus.EXPIRED, effective_to=None, fetched=dt(2026, 9, 10))
        v = LegalDocumentVersion.objects.get()
        self.assertEqual(v.valid_to, date(2026, 9, 10))  # honest fallback: first seen expired
        self.assertEqual(fts_candidates("zebrafish", as_of=date(2030, 1, 1)), [])
        # re-running later must not slide the date forward
        self.ingest(paragraphs=("1. zebrafish tartibi.",), status=DocumentStatus.EXPIRED, effective_to=None, fetched=dt(2026, 9, 20))
        v.refresh_from_db()
        self.assertEqual(v.valid_to, date(2026, 9, 10))
        # a later card supplies the real date
        self.ingest(paragraphs=("1. zebrafish tartibi.",), status=DocumentStatus.EXPIRED, effective_to=date(2026, 8, 1), fetched=dt(2026, 9, 25))
        v.refresh_from_db()
        self.assertEqual(v.valid_to, date(2026, 8, 1))

    def test_loss_of_force_before_the_versions_start_yields_a_one_day_window_not_open_ended(self):
        self.ingest(paragraphs=("1. zebrafish tartibi.",), status=DocumentStatus.EXPIRED, effective_to=date(2020, 1, 1), adopted=date(2026, 1, 10))
        v = LegalDocumentVersion.objects.get()
        self.assertIsNotNone(v.valid_to)
        self.assertEqual(fts_candidates("zebrafish", as_of=date(2027, 1, 1)), [])


class SearchUnderRowLevelSecurity(TestCase):
    """Finding 4: `@@` is not leakproof, so a plain RLS query cannot use the GIN index. Official search runs
    through a SECURITY DEFINER function that can only ever return official (organization-less) chunks."""

    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc, valid_from=date(2020, 1, 1))
        self.org = Organization.objects.create(name="A", slug="a")
        self.official = make_chunk(self.v, self.doc, index=0, text="qizil olma bozori")
        self.private = make_chunk(self.v, self.doc, org=self.org, index=1, text="qizil olma maxfiy hisobot")
        DocumentChunk.objects.filter(pk__in=[self.official.pk, self.private.pk]).update()

    def hits(self):
        return {h["text"] for h in fts_candidates("qizil olma", as_of=date(2026, 1, 1))}

    def test_api_role_finds_official_text_and_never_a_tenants_private_text(self):
        with as_role("yurist_api", self.org):
            self.assertEqual(self.hits(), {"qizil olma bozori"})
        with as_role("yurist_api"):
            self.assertEqual(self.hits(), {"qizil olma bozori"})

    def test_search_function_can_use_the_gin_index(self):
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(
                "EXPLAIN SELECT chunk_id FROM search_official_fts(ARRAY['qizil olma'], %s::date, NULL, 10, 100)", [date(2026, 1, 1)]
            )
            plan = "\n".join(r[0] for r in cur.fetchall())
        self.assertIn("search_official_fts", plan)

    def test_function_is_not_callable_by_the_parser_role_and_returns_no_private_rows_even_for_superuser(self):
        with connection.cursor() as cur:
            cur.execute("SELECT text FROM search_official_fts(ARRAY['qizil olma'], %s::date, NULL, 10, 100)", [date(2026, 1, 1)])
            self.assertEqual({r[0] for r in cur.fetchall()}, {"qizil olma bozori"})


class BadInputDoesNotStopTheJob(TestCase):
    """Finding 7: one corrupt document.json must not abort the run."""

    @staticmethod
    def mini_archive(corrupt=True):
        root = Path(tempfile.mkdtemp())
        shutil.copy(DATA_DIR / "state.sqlite3", root / "state.sqlite3")
        (root / "documents").mkdir()
        wanted = {"8501626", "8477063"}
        for folder in (DATA_DIR / "documents").iterdir():
            rec = json.loads((folder / "document.json").read_text(encoding="utf-8"))
            if rec["document_id"] in wanted:
                shutil.copytree(folder, root / "documents" / folder.name)
                blob = DATA_DIR / rec["source_blob"]
                (root / rec["source_blob"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(blob, root / rec["source_blob"])
        if corrupt:
            bad = root / "documents" / "-999_1"
            bad.mkdir()
            (bad / "document.json").write_text('{"url": "https://lex.uz/docs/-999", "blocks": [', encoding="utf-8")  # truncated
        return root

    @skipUnless((DATA_DIR / "state.sqlite3").exists(), "crawler archive not present")
    def test_truncated_document_json_is_a_failed_item_not_a_dead_job(self):
        root = self.mini_archive()
        job = run_ingest(root, store=FilesystemObjectStore(tempfile.mkdtemp()), retry_delays=(0,))
        self.assertEqual(job.status, JobStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual((job.new_count, job.failed_count), (2, 1))
        err = ParserError.objects.get(job=job)
        self.assertIn("-999", err.url)
        self.assertIsNotNone(err.item_id)  # the error is linked to its item

    @skipUnless((DATA_DIR / "state.sqlite3").exists(), "crawler archive not present")
    def test_unchanged_documents_are_not_reparsed(self):
        """Finding 11: a daily run must not re-parse everything."""
        root = self.mini_archive(corrupt=False)
        store = FilesystemObjectStore(tempfile.mkdtemp())
        run_ingest(root, store=store, retry_delays=(0,))
        from apps.parsers.ingestion import runner

        with mock.patch.object(runner, "build_parsed_document", side_effect=AssertionError("re-parsed an unchanged document")):
            job = run_ingest(root, store=store, retry_delays=(0,))
        self.assertEqual((job.unchanged_count, job.failed_count), (2, 0))

    @skipUnless((DATA_DIR / "state.sqlite3").exists(), "crawler archive not present")
    def test_a_changed_card_is_still_noticed(self):
        """The cheap pre-check must include the metadata cards, or a status change would be missed."""
        from apps.parsers.ingestion.archive import CrawlArchive

        root = self.mini_archive(corrupt=False)
        arch = CrawlArchive(root)
        rec = next(a.record for a in arch.iter_documents({"8501626"}))
        before = arch.fingerprint(rec)
        arch._tasks["https://lex.uz/actinfo/card1/8501626"] = {"id": 1, "url": "x", "kind": "metadata", "status": "done", "source_hash": "changed"}
        self.assertNotEqual(before, arch.fingerprint(rec))


class TransientCollisions(IngestBase):
    def test_concurrent_duplicate_create_is_retried_not_failed(self):
        """Finding 12: the loser of a create race must retry, and then see the winner's row."""
        from apps.parsers.ingestion import runner

        calls = {"n": 0}
        real = IngestService.ingest

        def racy(self_, p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError('duplicate key value violates unique constraint "uniq_document_source_external"')
            return real(self_, p)

        class Archived:
            record = {"url": "https://lex.uz/docs/1", "document_id": "1"}

        with mock.patch.object(IngestService, "ingest", racy), mock.patch.object(runner, "build_parsed_document", lambda a, b: parsed()):
            status, kw = runner._process_one(self.svc, None, Archived, mock.Mock(), self.source, (0, 0))
        self.assertEqual(status, ItemStatus.NEW)
        self.assertEqual(calls["n"], 2)


class OrganizationContextNesting(TestCase):
    def test_leaving_an_inner_context_restores_the_outer_organization(self):
        """Finding 8."""
        a = Organization.objects.create(name="A", slug="a")
        b = Organization.objects.create(name="B", slug="b")

        def current():
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.organization_id', true)")
                return cur.fetchone()[0]

        with organization_context(a.id):
            with organization_context(b.id):
                self.assertEqual(current(), str(b.id))
            self.assertEqual(current(), str(a.id))


class LanguageCorrectionRefreshesChunks(IngestBase):
    def test_correcting_language_rebuilds_search_text(self):
        """Finding 9: a wrong language guess (no card yet) must not freeze Cyrillic search text forever."""
        p = parsed(paragraphs=("1. Меҳнат шартномаси тузилади.",), language="ru", script="cyrl")
        self.svc.ingest(p)
        chunk = DocumentChunk.objects.get(chunk_index=1) if DocumentChunk.objects.filter(chunk_index=1).exists() else DocumentChunk.objects.first()
        self.assertNotIn("mehnat", chunk.search_text)
        fixed = parsed(paragraphs=("1. Меҳнат шартномаси тузилади.",), language="uz", script="cyrl", fetched=dt(2026, 9, 29))
        self.svc.ingest(fixed)
        texts = " ".join(DocumentChunk.objects.values_list("search_text", flat=True))
        self.assertIn("mehnat", texts)
        self.assertEqual(set(DocumentChunk.objects.values_list("language", flat=True)), {"uz"})


class EmbeddingAndChunkIntegrity(TestCase):
    def setUp(self):
        self.doc = make_doc()
        self.v = make_version(self.doc)
        self.profile = EmbeddingProfile.objects.create(name="p3", model_name="m", model_version="1", dimension=3)

    def test_profile_dimension_and_metric_are_frozen_once_embeddings_exist(self):
        """Finding 13."""
        chunk = make_chunk(self.v, self.doc)
        self.profile.dimension = 4  # allowed while nothing is embedded
        self.profile.save()
        self.profile.dimension = 3
        self.profile.save()
        ChunkEmbedding.objects.create(chunk=chunk, embedding_profile=self.profile, embedding=[1, 2, 3])
        with denied():
            EmbeddingProfile.objects.filter(pk=self.profile.pk).update(dimension=4)
        with denied():
            EmbeddingProfile.objects.filter(pk=self.profile.pk).update(distance_metric="l2")

    def test_chunk_content_is_immutable_so_an_embedding_can_never_go_stale(self):
        chunk = make_chunk(self.v, self.doc)
        for field, value in (("text", "changed"), ("search_text", "changed"), ("content_hash", "f" * 64)):
            with denied():
                DocumentChunk.objects.filter(pk=chunk.pk).update(**{field: value})

    def test_chunk_document_must_match_its_versions_document(self):
        other = make_doc("other")
        with denied():
            make_chunk(self.v, other)  # version belongs to self.doc

    def test_section_path_is_unique_within_a_version(self):
        LegalDocumentSection.objects.create(version=self.v, section_type="clause", text="a", order_index=0, path="cl_1", text_hash="0" * 64)
        with denied():
            LegalDocumentSection.objects.create(version=self.v, section_type="clause", text="b", order_index=1, path="cl_1", text_hash="0" * 64)


class Normalization(TestCase):
    def test_hyphenated_numbers_match_their_spaced_query(self):
        self.assertEqual(normalize_search("PQ-330-son"), normalize_search("pq 330 son"))
        self.assertEqual(normalize_search("ПҚ-330", language="uz", script="cyrl"), normalize_search("pq 330"))

    def test_homoglyph_letters_inside_latin_words_are_folded(self):
        self.assertEqual(normalize_search("Нigher standards"), normalize_search("Higher standards"))  # Cyrillic Н
        self.assertEqual(normalize_search("Республика", language="ru", script="cyrl"), "республика")  # pure Cyrillic untouched


class OperationalGuards(TestCase):
    def test_rechunk_all_needs_explicit_confirmation(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("rechunk", "--all")

    def _load_production(self, env):
        import importlib
        import os
        import sys

        import config.settings.base as base

        sys.modules.pop("config.settings.production", None)
        try:
            with mock.patch.dict(os.environ, env):
                importlib.reload(base)  # base reads the environment at import time
                return importlib.import_module("config.settings.production")
        finally:
            sys.modules.pop("config.settings.production", None)
            importlib.reload(base)

    def test_production_settings_refuse_unsafe_defaults(self):
        good = {
            "DJANGO_SECRET_KEY": "k" * 50, "DB_USER": "svc_parser", "DB_PASSWORD": "a-real-secret",
            "OBJECT_STORE_BACKEND": "s3", "OBJECT_STORE_BUCKET": "raw",
        }
        self._load_production(good)  # a properly configured environment starts
        for override in ({"DB_USER": "postgres"}, {"DB_PASSWORD": "postgres"}, {"OBJECT_STORE_BACKEND": "filesystem"}, {"DJANGO_SECRET_KEY": "short"}):
            with self.assertRaises(RuntimeError, msg=str(override)):
                self._load_production({**good, **override})

    def test_abandoned_running_jobs_are_reaped(self):
        from apps.parsers.models import ParserJob

        source = make_source()
        old = ParserJob.objects.create(source=source)
        ParserJob.objects.filter(pk=old.pk).update(started_at=dt(2026, 1, 1))
        if (DATA_DIR / "state.sqlite3").exists():
            run_ingest(DATA_DIR, store=FilesystemObjectStore(tempfile.mkdtemp()), only_ids={"8477063"}, retry_delays=(0,))
            old.refresh_from_db()
            self.assertEqual(old.status, JobStatus.FAILED)
            self.assertIn("abandoned", old.error_summary)
