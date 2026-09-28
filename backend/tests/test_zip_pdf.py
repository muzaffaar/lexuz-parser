"""Acts published as a wrapper page + `/files/<n>.zip` holding the real PDF (e.g. lex.uz/docs/5875370).
Uses the genuine page/zip fetched from lex.uz (tests/fixtures/zip_pdf), parsed by the crawler's own parser."""
import gzip
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.common.storage import FilesystemObjectStore, get_object_store
from apps.legal_documents.constants import TextStatus
from apps.legal_documents.models import LegalDocument
from apps.legal_documents.text.pdf import extract_pdf_text
from apps.parsers.ingestion.archive import CrawlArchive
from apps.parsers.ingestion.builder import build_parsed_document
from apps.parsers.ingestion.runner import run_ingest
from apps.parsers.ingestion.zips import extract_pdfs
from apps.search.models import DocumentChunk
from apps.search.selectors import fts_candidates

FIXTURE = Path(__file__).parent / "fixtures" / "zip_pdf"
DOC_URL = "https://lex.uz/docs/5875370"
ZIP_URL = "https://lex.uz/files/5875605.zip"


def make_zip_archive(*, zip_bytes=None, as_direct_pdf=False, extra_body_block=False) -> Path:
    """A minimal crawler archive laid out exactly like the real one, around the fixture page."""
    root = Path(tempfile.mkdtemp(prefix="yurist-ziparch-"))
    rec = json.loads((FIXTURE / "document.json").read_text(encoding="utf-8"))
    blob = root / rec["source_blob"]
    blob.parent.mkdir(parents=True)
    shutil.copy(FIXTURE / "page.html.gz", blob)
    payload = zip_bytes if zip_bytes is not None else (FIXTURE / "file.zip").read_bytes()
    asset_url = ZIP_URL
    if as_direct_pdf:
        payload = zipfile.ZipFile(io.BytesIO(payload)).read(zipfile.ZipFile(io.BytesIO(payload)).namelist()[0])
        asset_url = "https://lex.uz/files/5875605.pdf"
        rec["assets"] = [asset_url]
    if extra_body_block:
        rec["blocks"].append({"order": 99, "id": "123456", "classes": ["ACT_TEXT", "lx_elem"], "is_content": True,
                              "html": '<div class="ACT_TEXT lx_elem">1. Sahifaning oʻzida ham matn bor.</div>', "links": []})
    folder = root / "documents" / "5875370_1"
    folder.mkdir(parents=True)
    (folder / "document.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    res = root / "resources" / "2"
    res.mkdir(parents=True)
    (res / ("file.pdf" if as_direct_pdf else "file.zip")).write_bytes(payload)
    import hashlib

    sha = hashlib.sha256(payload).hexdigest()
    (res / "source.json").write_text(json.dumps({"url": asset_url, "sha256": sha, "retrieved_at": 1790500001.0}), encoding="utf-8")
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, url TEXT, kind TEXT, status TEXT, source_hash TEXT);"
        "CREATE TABLE edges(source TEXT, target TEXT, relation TEXT);"
    )
    db.execute("INSERT INTO tasks VALUES (1, ?, 'document', 'done', ?)", (DOC_URL, rec["source_sha256"]))
    db.execute("INSERT INTO tasks VALUES (2, ?, 'asset', 'done', ?)", (asset_url, sha))
    db.commit()
    db.close()
    return root


def build(root):
    archive = CrawlArchive(root)
    try:
        return build_parsed_document(next(archive.iter_documents()), archive)
    finally:
        archive.close()


def zip_of(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


REAL_PDF = zipfile.ZipFile(FIXTURE / "file.zip").read(zipfile.ZipFile(FIXTURE / "file.zip").namelist()[0])


class ZipSafetyTests(TestCase):
    def extract(self, members, **kw):
        path = Path(tempfile.mkdtemp()) / "x.zip"
        path.write_bytes(zip_of(members))
        return extract_pdfs(path, **kw)

    def test_real_pdf_is_extracted(self):
        got = self.extract({"act.pdf": REAL_PDF})
        self.assertEqual([g.name for g in got], ["act.pdf"])
        self.assertTrue(got[0].data.startswith(b"%PDF-"))

    def test_only_real_pdfs_are_accepted_whatever_their_name_says(self):
        got = self.extract({"readme.txt": b"hello", "fake.pdf": b"<html>not a pdf</html>", "real.pdf": REAL_PDF, "image.png": b"\x89PNG"})
        self.assertEqual([g.name for g in got], ["real.pdf"])

    def test_zip_slip_names_never_reach_the_filesystem(self):
        got = self.extract({"../../evil.pdf": REAL_PDF, "..\\..\\evil2.pdf": REAL_PDF, "/abs/evil3.pdf": REAL_PDF})
        self.assertEqual({g.name for g in got}, {"evil.pdf", "evil2.pdf", "evil3.pdf"})  # display names only (basename)
        self.assertFalse(any("/" in g.name or "\\" in g.name or ".." in g.name for g in got))

    def test_oversized_members_are_skipped(self):
        self.assertEqual(self.extract({"big.pdf": REAL_PDF}, max_pdf_bytes=1000), [])
        two = self.extract({"a.pdf": REAL_PDF, "b.pdf": REAL_PDF}, max_total_bytes=len(REAL_PDF) + 10)
        self.assertEqual(len(two), 1)

    def test_too_many_members_and_broken_zips_yield_nothing(self):
        many = {f"f{i}.pdf": REAL_PDF for i in range(201)}
        self.assertEqual(self.extract(many), [])
        bad = Path(tempfile.mkdtemp()) / "bad.zip"
        bad.write_bytes(b"PK\x03\x04 this is not really a zip")
        self.assertEqual(extract_pdfs(bad), [])


class BoundedPdfExtractionTests(TestCase):
    def test_real_pdf_text_is_extracted(self):
        r = extract_pdf_text(REAL_PDF)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["total_pages"], 8)
        self.assertIn("базавий солиқ ставкаларини", " ".join(" ".join(p["text"].split()) for p in r["pages"]))

    def test_garbage_is_reported_never_raised(self):
        r = extract_pdf_text(b"%PDF-1.4 this is garbage, not a document")
        self.assertIn(r["status"], ("extraction_failed", "ok"))
        self.assertEqual(r["pages"], [] if r["status"] != "ok" else r["pages"])

    def test_a_hanging_parser_is_cut_off(self):
        r = extract_pdf_text(REAL_PDF, timeout=0.001)
        self.assertEqual(r["status"], "extraction_timeout")


class WrapperPageWithZippedPdfTests(TestCase):
    def test_builder_takes_the_text_from_the_pdf_instead_of_calling_the_page_a_stub(self):
        p = build(make_zip_archive())
        self.assertEqual(p.text_status, TextStatus.OK)  # NOT "stub": the act's text exists, inside the zip
        self.assertEqual(p.text_source, "pdf_embedded")
        types = [s.section_type for s in p.sections]
        self.assertIn("title", types)  # requisites from the HTML wrapper are kept
        self.assertEqual(types.count("pdf_page"), 8)
        self.assertIn("базавий солиқ ставкаларини", p.normalized_text)
        self.assertEqual(p.version_metadata["pdf_in_files"]["files"][0]["pages"], 8)
        self.assertTrue(p.version_metadata["site_warnings"])  # "Ҳужжат матни PDF шаклида берилган" is preserved
        self.assertNotIn("stub_reason", p.version_metadata)
        self.assertEqual({r for r in (a.role for a in p.attachments)}, {"primary_pdf", "source_zip"})
        self.assertEqual(p.primary_snapshot.kind, "pdf")  # provenance points at the PDF, the wrapper page is secondary
        self.assertEqual([s.role for s in p.snapshots], ["primary", "page"])

    def test_direct_pdf_link_works_too(self):
        p = build(make_zip_archive(as_direct_pdf=True))
        self.assertEqual((p.text_status, p.text_source), (TextStatus.OK, "pdf_embedded"))
        self.assertEqual([a.role for a in p.attachments], ["primary_pdf"])

    def test_a_page_that_already_has_body_text_keeps_its_html_text_and_still_attaches_the_pdf(self):
        p = build(make_zip_archive(extra_body_block=True))
        self.assertEqual(p.text_source, "html")
        self.assertEqual(p.text_status, TextStatus.OK)
        self.assertNotIn("pdf_page", [s.section_type for s in p.sections])
        self.assertEqual({a.role for a in p.attachments}, {"primary_pdf", "source_zip"})

    def test_a_zip_without_a_pdf_leaves_the_page_a_stub(self):
        p = build(make_zip_archive(zip_bytes=zip_of({"notes.txt": b"no pdf here"})))
        self.assertEqual(p.text_status, TextStatus.STUB)
        self.assertEqual([a.role for a in p.attachments], [])

    def test_a_missing_download_leaves_the_page_a_stub_and_says_nothing_false(self):
        root = make_zip_archive()
        db = sqlite3.connect(root / "state.sqlite3")
        db.execute("UPDATE tasks SET status='pending' WHERE id=2")
        db.commit()
        db.close()
        p = build(root)
        self.assertEqual(p.text_status, TextStatus.STUB)


class EndToEndTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = make_zip_archive()
        cls.store = get_object_store()
        cls.job = run_ingest(cls.root, store=cls.store, retry_delays=(0,))
        cls.doc = LegalDocument.objects.get(external_id="5875370")

    def test_document_is_ingested_with_text_chunks_and_files(self):
        self.assertEqual((self.job.new_count, self.job.failed_count), (1, 0))
        self.assertEqual((self.doc.text_status, self.doc.current_version.text_source), ("ok", "pdf_embedded"))
        self.assertGreater(DocumentChunk.objects.filter(document=self.doc).count(), 3)
        self.assertEqual(sorted(a.role for a in self.doc.attachments.all()), ["primary_pdf", "source_zip"])
        for att in self.doc.attachments.select_related("snapshot"):
            self.assertTrue(self.store.exists(att.snapshot.object_key))

    def test_the_pdfs_text_is_searchable_in_latin_and_cyrillic(self):
        latin = fts_candidates("bazaviy soliq stavkalarini", as_of=date(2026, 12, 1))
        cyril = fts_candidates("базавий солиқ ставкаларини", as_of=date(2026, 12, 1))
        self.assertTrue(latin)
        self.assertEqual({h["external_id"] for h in latin}, {"5875370"})
        self.assertEqual({h["chunk_id"] for h in latin}, {h["chunk_id"] for h in cyril})

    def test_admin_shows_the_whole_act_and_embeds_the_pdf(self):
        user = get_user_model().objects.create_superuser("zipadmin", "z@example.com", "pw")
        self.client.force_login(user)
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[self.doc.pk])).content.decode()
        self.assertIn("базавий солиқ ставкаларини", page)  # the act's text, combined and readable
        self.assertIn("- PDF page 1 -", page)
        self.assertIn("<iframe", page)
        self.assertNotIn("source_zip", page.split("Original PDF")[1].split("Classification")[0])  # zip is provenance, not rendered
        pdf = self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[self.doc.pk]))
        self.assertEqual((pdf.status_code, pdf.content[:5]), (200, b"%PDF-"))

    def test_rerunning_is_a_noop(self):
        job = run_ingest(self.root, store=self.store, retry_delays=(0,))
        self.assertEqual((job.unchanged_count, job.new_count, job.updated_count), (1, 0, 0))

    def test_a_changed_zip_download_is_noticed_by_the_fingerprint(self):
        archive = CrawlArchive(self.root)
        rec = next(archive.iter_documents()).record
        before = archive.fingerprint(rec)
        archive._tasks[ZIP_URL] = {**archive._tasks[ZIP_URL], "source_hash": "different"}
        self.assertNotEqual(before, archive.fingerprint(rec))
        archive.close()

    def test_several_pdfs_in_one_zip_are_all_attached_and_shown(self):
        other = REAL_PDF + b"\n% second file\n"
        root = make_zip_archive(zip_bytes=zip_of({"one.pdf": REAL_PDF, "two.pdf": other}))
        # a fresh DB row set: ingest into the same test DB under a different external id is not possible for a
        # fixed fixture, so check at the builder level and via the admin helper
        p = build(root)
        self.assertEqual(len([a for a in p.attachments if a.role == "primary_pdf"]), 2)
        self.assertEqual(p.version_metadata["pdf_in_files"]["files"][0]["file"], "one.pdf")
        self.assertEqual(len(p.version_metadata["pdf_in_files"]["files"]), 2)
