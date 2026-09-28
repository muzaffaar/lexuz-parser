"""Tests against the LIVE crawler archive, which keeps growing while the crawler runs. They therefore assert
invariants, never exact counts. They exist because the first 84 documents hid two real problems that only
appeared with more data: historical editions, and numberless (treaty) titles."""
import tempfile
from datetime import date
from unittest import skipUnless

from django.test import TestCase

from apps.common.storage import FilesystemObjectStore
from apps.legal_documents.models import LegalDocument, LegalDocumentVersion
from apps.parsers.ingestion.runner import run_ingest
from apps.parsers.models import ParserError
from apps.search.models import DocumentChunk

from .factories import DATA_DIR

DOCS = DATA_DIR / "documents"
EDITION_IDS = {"6384439", "-6384439"}  # a law the crawler fetched as 2 editions (?ONDATE=) + the current text


def folders_for(ids):
    return [f for f in DOCS.iterdir() if f.name.rsplit("_", 1)[0] in ids]


@skipUnless(DOCS.exists() and len(folders_for(EDITION_IDS)) >= 4, "live archive with the edition example not present")
class RealHistoricalEditionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.job = run_ingest(DATA_DIR, store=FilesystemObjectStore(tempfile.mkdtemp()), only_ids=EDITION_IDS, retry_delays=(0,))

    def test_editions_ingest_as_versions_of_one_document(self):
        self.assertEqual(self.job.failed_count, 0, list(ParserError.objects.values_list("url", "exception")))
        self.assertEqual(LegalDocument.objects.count(), 2)  # 4+ folders, 2 documents (Cyrillic + Latin)
        for doc in LegalDocument.objects.all():
            self.assertGreaterEqual(doc.versions.count(), 2, doc.external_id)

    def test_the_version_chain_is_contiguous_with_exactly_one_current(self):
        for doc in LegalDocument.objects.all():
            chain = list(doc.versions.order_by("valid_from"))
            self.assertEqual(sum(v.is_current for v in chain), 1, doc.external_id)
            self.assertTrue(chain[-1].is_current)
            self.assertIsNone(chain[-1].valid_to)
            for older, newer in zip(chain, chain[1:]):
                self.assertEqual(older.valid_to, date.fromordinal(newer.valid_from.toordinal() - 1), doc.external_id)
            self.assertEqual(chain[0].valid_from_source, "edition")  # the oldest start date is the site's own edition date
            self.assertIsNotNone(chain[0].edition_on)

    def test_as_of_picks_the_edition_in_force(self):
        doc = LegalDocument.objects.get(external_id="6384439")
        chain = list(doc.versions.order_by("valid_from"))
        old, current = chain[0], chain[-1]
        self.assertEqual(doc.versions.get(valid_period__contains=old.valid_from), old)
        self.assertEqual(doc.versions.get(valid_period__contains=current.valid_from), current)
        self.assertNotEqual(old.content_hash, current.content_hash)  # the amendment really changed the text
        self.assertGreater(DocumentChunk.objects.filter(version=old).count(), 0)  # history stays searchable


def newest_external_ids(n):
    def key(folder):
        try:
            return int(folder.name.rsplit("_", 1)[1])
        except ValueError:
            return 0

    ids = []
    for folder in sorted(DOCS.iterdir(), key=key, reverse=True):
        external_id = folder.name.rsplit("_", 1)[0]
        if external_id not in ids:
            ids.append(external_id)
        if len(ids) >= n:
            break
    return set(ids)


@skipUnless(DOCS.exists(), "live archive not present")
class NewestDocumentsSmokeTests(TestCase):
    """The most recently crawled documents are the ones no test has ever seen: they must ingest, and their key
    attributes must be found (this is how the numberless-title gap showed up)."""

    @classmethod
    def setUpTestData(cls):
        cls.ids = newest_external_ids(60)
        cls.job = run_ingest(DATA_DIR, store=FilesystemObjectStore(tempfile.mkdtemp()), only_ids=cls.ids, retry_delays=(0,))

    def test_everything_ingests(self):
        self.assertEqual(self.job.failed_count, 0, list(ParserError.objects.values_list("url", "exception")[:5]))

    def test_documents_with_text_are_chunked_and_have_a_language(self):
        for doc in LegalDocument.objects.filter(text_status="ok"):
            self.assertTrue(DocumentChunk.objects.filter(document=doc).exists(), doc.external_id)
            self.assertNotEqual(doc.language, "", doc.external_id)

    def test_an_adoption_date_is_found_for_nearly_every_document(self):
        total = LegalDocument.objects.count()
        missing = list(LegalDocument.objects.filter(adopted_at__isnull=True).values_list("external_id", "title")[:5])
        self.assertLessEqual(len(missing), max(1, total // 20), f"no adoption date parsed for: {missing}")

    def test_validity_never_falls_back_to_the_crawl_date_for_dated_titles(self):
        observed = LegalDocumentVersion.objects.filter(valid_from_source="observed").count()
        self.assertLessEqual(observed, max(1, LegalDocumentVersion.objects.count() // 20))
