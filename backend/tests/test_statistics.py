"""lex.uz's own statistics vs. what the database holds."""
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.common.storage import FilesystemObjectStore
from apps.legal_documents.models import LegalDocument
from apps.parsers.ingestion.archive import CrawlArchive
from apps.parsers.ingestion.loader import IngestService
from apps.parsers.ingestion.runner import run_ingest
from apps.statistics.comparison import Row, compare, latest_snapshot
from apps.statistics.keys import category_key
from apps.statistics.loader import load_source_statistics
from apps.statistics.models import DocumentCategoryMap, SourceStatisticsSnapshot

from .factories import make_source, parsed

GRAND = {"total": 1000, "ru": 300, "uz_cyrl": 300, "uz_latn": 300, "en": 100, "in_force": 700, "new_edition": 10, "lost_force": 290}
CATEGORIES = [{"category": "Prezident hujjatlari", "total": 600}, {"category": "Qonun hujjatlari", "total": 400}]


def stats_json(captured_at="2026-09-28T11:00:00+00:00", total=1000, **extra):
    grand = {**GRAND, "total": total}
    return {
        "url": "https://lex.uz/uz/statistic", "captured_at": captured_at, "source_sha256": "ab" * 32,
        "total_documents": total, "header_total": total, "grand_total": grand, "categories": CATEGORIES,
        "rows": [], "consistent": True, "problems": [], **extra,
    }


def make_archive(tmp, files):
    root = Path(tmp)
    (root / "documents").mkdir()
    (root / "source_stats").mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, url TEXT, kind TEXT, status TEXT, source_hash TEXT);"
        "CREATE TABLE edges(source TEXT, target TEXT, relation TEXT);"
    )
    db.commit()
    db.close()
    for name, content in files.items():
        (root / "source_stats" / name).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return root


class CategorySeedTests(TestCase):
    def test_seeded_keys_still_equal_what_the_folding_code_produces(self):
        # the migration's keys are literal; if normalize_search ever changes, seeds would silently stop matching
        rows = DocumentCategoryMap.objects.all()
        self.assertEqual(rows.count(), 5)
        for row in rows:
            self.assertEqual(row.type_key, category_key(row.label), row.label)

    def test_every_seeded_category_is_a_name_used_on_the_statistics_page(self):
        page = {"Prezident hujjatlari", "Hukumat qarorlari", "Sud hujjatlari", "Texnik hujjatlar", "Xalqaro hujjatlar"}
        self.assertEqual({r.category for r in DocumentCategoryMap.objects.all()}, page)


class LoaderTests(TestCase):
    def setUp(self):
        self.source = make_source()

    def load(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            archive = CrawlArchive(make_archive(tmp, files))
            try:
                return load_source_statistics(archive, self.source)
            finally:
                archive.close()

    def test_import_is_idempotent_and_skips_latest_json(self):
        files = {"20260928T110000Z.json": stats_json(), "latest.json": stats_json()}
        self.assertEqual(len(self.load(files)), 1)
        self.assertEqual(self.load(files), [])
        self.assertEqual(SourceStatisticsSnapshot.objects.count(), 1)
        snap = SourceStatisticsSnapshot.objects.get()
        self.assertEqual((snap.total_documents, snap.consistent), (1000, True))
        self.assertEqual(snap.payload["grand_total"]["ru"], 300)

    def test_a_new_capture_is_a_new_snapshot_and_history_is_kept(self):
        self.load({"20260928T110000Z.json": stats_json()})
        self.load({"20260928T110000Z.json": stats_json(), "20260929T110000Z.json": stats_json("2026-09-29T11:00:00+00:00", 1010)})
        self.assertEqual(SourceStatisticsSnapshot.objects.count(), 2)
        self.assertEqual(latest_snapshot(self.source).total_documents, 1010)

    def test_inconsistent_capture_is_stored_but_flagged(self):
        self.load({"a.json": stats_json(consistent=False, problems=["header says 5 documents"])})
        snap = SourceStatisticsSnapshot.objects.get()
        self.assertFalse(snap.consistent)
        self.assertEqual(snap.problems, ["header says 5 documents"])

    def test_malformed_files_are_skipped_not_fatal(self):
        stored = self.load({"a.json": {"captured_at": "2026-09-28T11:00:00+00:00"}, "b.json": stats_json("not-a-date"), "c.json": stats_json()})
        self.assertEqual(len(stored), 1)

    def test_an_unreadable_file_is_skipped_and_newer_captures_still_load(self):
        with self.assertLogs("apps.parsers.ingestion.archive", "WARNING"):
            stored = self.load({"a.json": "{not json", "b.json": stats_json()})
        self.assertEqual(len(stored), 1)

    def test_a_document_run_imports_statistics_and_survives_bad_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_archive(tmp, {"20260928T110000Z.json": stats_json()})
            job = run_ingest(root, store=FilesystemObjectStore(tempfile.mkdtemp()))
            self.assertEqual(job.status, "completed")
            self.assertEqual(SourceStatisticsSnapshot.objects.count(), 1)
            (root / "source_stats" / "zzz.json").write_text("{broken", encoding="utf-8")
            job = run_ingest(root, store=FilesystemObjectStore(tempfile.mkdtemp()))
            self.assertEqual(job.status, "completed")  # a bad statistics file never fails the document run


class ComparisonTests(TestCase):
    def setUp(self):
        self.source = make_source()
        self.service = IngestService(self.source, FilesystemObjectStore(tempfile.mkdtemp()))
        self.snapshot = SourceStatisticsSnapshot.objects.create(
            source=self.source, captured_at=datetime(2026, 9, 28, 11, tzinfo=timezone.utc), source_sha256="a" * 64,
            total_documents=1000, header_total=1000, consistent=True,
            payload={"grand_total": GRAND, "categories": CATEGORIES, "rows": []},
        )

    def add(self, external_id, *, document_type="", **kwargs):
        p = parsed(external_id=external_id, **kwargs)
        p.document_type = document_type
        self.service.ingest(p)

    def section(self, result, key):
        return next(s for s in result.sections if s.key == key)

    def test_overall_and_language_counts_use_lexuz_language_variants(self):
        self.add("1", language="ru", script="cyrl")
        self.add("2", language="uz", script="cyrl")
        self.add("3", language="uz", script="latn")
        self.add("4", language="uz", script="latn")
        self.add("5", language="en", script="latn")
        result = compare(self.snapshot)
        self.assertEqual((result.overall.site, result.overall.ours, result.overall.missing, result.overall.coverage), (1000, 5, 995, 0.5))
        rows = {r.label.split(" (")[0]: (r.site, r.ours) for r in self.section(result, "language").rows}
        self.assertEqual(rows, {"Russian": (300, 1), "Uzbek Cyrillic": (300, 1), "Uzbek Latin": (300, 2), "English": (100, 1)})

    def job(self, *, total, unchanged=0, new=0, skipped=0, params=None, status="completed"):
        from apps.parsers.models import ParserJob

        return ParserJob.objects.create(
            source=self.source, status=status, total_count=total, unchanged_count=unchanged, new_count=new,
            skipped_count=skipped, params=params or {"archive": "x", "limit": None, "only_ids": []},
        )

    def test_progress_shows_crawled_and_ingested_against_lexuz(self):
        self.add("1")
        self.add("2")
        self.assertIsNone(compare(self.snapshot).crawled)  # no ingest run recorded yet
        self.job(total=3, new=2, skipped=1)
        result = compare(self.snapshot)
        self.assertEqual((result.crawled.site, result.crawled.ours, result.crawled.coverage), (1000, 3, 0.3))
        self.assertEqual((result.overall.ours, result.overall.coverage), (2, 0.2))
        rows = {r.label.split(" (")[0]: r for r in self.section(result, "progress").rows}
        self.assertEqual(rows["Document versions stored"].ours, 2)
        ingested_of_crawled = next(r for r in rows.values() if r.label.startswith("Of the crawled"))
        self.assertEqual((ingested_of_crawled.site, ingested_of_crawled.ours), (3, 2))
        self.assertIn("skipped 1", ingested_of_crawled.note)

    def test_only_a_full_finished_run_measures_what_was_crawled(self):
        self.job(total=500, unchanged=500)
        self.job(total=5, params={"archive": "x", "limit": 5, "only_ids": []})  # a --limit run sees a fraction
        self.job(total=2, params={"archive": "x", "limit": None, "only_ids": ["1", "2"]})
        self.job(total=9999, status="failed")
        self.assertEqual(compare(self.snapshot).crawled.ours, 500)

    def test_text_status_breakdown(self):
        self.add("1")
        rows = self.section(compare(self.snapshot), "text").rows
        self.assertEqual([(r.label, r.ours) for r in rows], [("ok", 1)])

    def test_undetected_language_is_reported_as_ours_only(self):
        self.add("1", language="", script="")
        rows = self.section(compare(self.snapshot), "language").rows
        extra = rows[-1]
        self.assertEqual((extra.label, extra.site, extra.ours), ("Language not detected", None, 1))

    def test_status_unknown_is_never_counted_as_in_force(self):
        self.add("1", status="active")
        self.add("2", status="expired", effective_to=datetime(2026, 1, 1).date())
        self.add("3", status="unknown")
        rows = {r.label.split(" (")[0]: r for r in self.section(compare(self.snapshot), "status").rows}
        self.assertEqual(rows["In force"].ours, 1)
        self.assertEqual(rows["Lost force"].ours, 1)
        self.assertEqual(rows["Status not known"].ours, 1)
        self.assertIsNone(rows["New edition pending"].ours)  # cannot be compared, shown as such

    def test_category_comes_only_from_the_card_and_the_rest_is_not_guessed(self):
        self.add("1", document_type="Акты Президента")
        self.add("2", document_type="Акты Президента")
        self.add("3", document_type="Что-то новое")
        self.add("4")  # no card
        rows = {r.label: r for r in self.section(compare(self.snapshot), "category").rows}
        self.assertEqual((rows["Prezident hujjatlari"].site, rows["Prezident hujjatlari"].ours), (600, 2))
        self.assertEqual(rows["Qonun hujjatlari"].ours, 0)
        self.assertEqual(rows["Not classified (no legal-analysis card yet)"].ours, 1)
        self.assertEqual(rows["Card type not mapped yet: Что-то новое"].ours, 1)

    def test_lawyer_edit_of_the_map_takes_effect(self):
        self.add("1", document_type="Что-то новое")
        DocumentCategoryMap.objects.create(type_key=category_key("Что-то новое"), category="Qonun hujjatlari")
        rows = {r.label: r for r in self.section(compare(self.snapshot), "category").rows}
        self.assertEqual(rows["Qonun hujjatlari"].ours, 1)

    def test_soft_deleted_documents_are_not_counted(self):
        self.add("1")
        self.add("2")
        LegalDocument.objects.filter(external_id="2").update(deleted_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(compare(self.snapshot).overall.ours, 1)

    def test_another_source_is_not_counted(self):
        from apps.legal_sources.models import LegalSource

        other = LegalSource.objects.create(code="other", name="Other", base_url="https://other.example/")
        IngestService(other, FilesystemObjectStore(tempfile.mkdtemp())).ingest(parsed(external_id="9"))
        self.assertEqual(compare(self.snapshot).overall.ours, 0)

    def test_change_since_previous_snapshot(self):
        self.assertIsNone(compare(self.snapshot).site_change)
        newer = SourceStatisticsSnapshot.objects.create(
            source=self.source, captured_at=datetime(2026, 9, 29, 11, tzinfo=timezone.utc), source_sha256="b" * 64,
            total_documents=1042, consistent=True, payload={"grand_total": {**GRAND, "total": 1042}, "categories": CATEGORIES, "rows": []},
        )
        self.assertEqual(compare(newer).site_change, 42)
        self.assertEqual(compare(self.snapshot).site_change, None)

    def test_row_arithmetic(self):
        self.assertEqual(Row("x", 200, 50).coverage, 25.0)
        self.assertEqual(Row("x", 200, 50).missing, 150)
        self.assertIsNone(Row("x", None, 5).coverage)
        self.assertIsNone(Row("x", 0, 5).coverage)  # no division by zero
        self.assertIsNone(Row("x", 10, None).missing)
        over = Row("x", 10, 12)
        self.assertTrue(over.more_than_site)
        self.assertEqual(over.coverage, 100.0)  # never above 100%


class StatisticsAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("admin", "a@example.com", "pw-not-used-in-prod")

    def setUp(self):
        self.client.force_login(self.user)
        self.source = make_source()

    def snapshot(self, **kw):
        return SourceStatisticsSnapshot.objects.create(
            source=self.source, captured_at=datetime(2026, 9, 28, 11, tzinfo=timezone.utc), source_sha256="a" * 64,
            total_documents=1000, header_total=1000, consistent=True,
            payload={"grand_total": GRAND, "categories": CATEGORIES, "rows": []}, **kw,
        )

    def test_page_without_statistics_explains_how_to_get_them(self):
        response = self.client.get(reverse("admin:statistics_compare_latest"))
        self.assertContains(response, "lex_crawler stats")

    def test_comparison_page_shows_both_sides(self):
        self.snapshot()
        IngestService(self.source, FilesystemObjectStore(tempfile.mkdtemp())).ingest(parsed(external_id="1"))
        response = self.client.get(reverse("admin:statistics_compare_latest"))
        self.assertContains(response, "1,000")  # lex.uz total, thousands separated
        self.assertContains(response, "Prezident hujjatlari")
        self.assertContains(response, "By language")
        self.assertContains(response, "Ingested into the database")
        self.assertContains(response, "Progress: lex.uz")
        self.assertContains(response, "https://lex.uz/uz/statistic")
        self.assertNotContains(response, "did not add up")
        self.assertNotContains(response, "None")  # a row lex.uz or we cannot fill shows a dash, never the word None
        self.assertContains(response, "tracked as document versions")

    def test_inconsistent_snapshot_shows_a_warning(self):
        SourceStatisticsSnapshot.objects.create(
            source=self.source, captured_at=datetime(2026, 9, 28, 11, tzinfo=timezone.utc), source_sha256="a" * 64,
            total_documents=1000, consistent=False, problems=["header says 5 documents but the grand-total row says 1000"],
            payload={"grand_total": GRAND, "categories": CATEGORIES, "rows": []},
        )
        self.assertContains(self.client.get(reverse("admin:statistics_compare_latest")), "did not add up")

    def test_specific_snapshot_and_changelist_and_404(self):
        snap = self.snapshot()
        self.assertEqual(self.client.get(reverse("admin:statistics_snapshot_compare", args=[snap.pk])).status_code, 200)
        changelist = self.client.get(reverse("admin:statistics_sourcestatisticssnapshot_changelist"))
        self.assertContains(changelist, "Compare latest with our database")
        self.assertContains(changelist, "Compare</a>")
        import uuid

        self.assertEqual(self.client.get(reverse("admin:statistics_snapshot_compare", args=[uuid.uuid4()])).status_code, 404)

    def test_snapshots_are_read_only_but_the_category_map_is_editable(self):
        snap = self.snapshot()
        self.assertEqual(self.client.get(reverse("admin:statistics_sourcestatisticssnapshot_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:statistics_sourcestatisticssnapshot_delete", args=[snap.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:statistics_documentcategorymap_add")).status_code, 200)
