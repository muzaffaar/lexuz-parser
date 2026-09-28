"""The read-only admin browser: every page must render, show the law, and refuse edits."""
import tempfile
from unittest import skipUnless

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.common.storage import FilesystemObjectStore, get_object_store
from django.utils.html import escape
from apps.legal_documents.models import LegalDocument, LegalDocumentVersion
from apps.parsers.ingestion.loader import IngestService
from apps.parsers.ingestion.runner import run_ingest
from apps.search.models import DocumentChunk

from .factories import DATA_DIR, make_source, parsed


class AdminBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("admin", "a@example.com", "pw-not-used-in-prod")

    def setUp(self):
        self.client.force_login(self.user)


class AdminBrowseTests(AdminBase):
    def setUp(self):
        super().setUp()
        svc = IngestService(make_source(), FilesystemObjectStore(tempfile.mkdtemp()))
        svc.ingest(parsed(external_id="700", paragraphs=("1. Birinchi band &lt;b&gt;matni&lt;/b&gt;.", "(a) kichik band"), title="Mehnat <script>x</script> kodeksi"))
        svc.ingest(parsed(external_id="702", paragraphs=("I. Umumiy qoidalar",), css="TEXT_HEADER_DEFAULT"))
        self.doc = LegalDocument.objects.get(external_id="700")
        self.version = self.doc.current_version

    def test_every_registered_changelist_renders(self):
        checked = 0
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label in ("auth", "contenttypes", "sessions", "admin"):
                continue
            url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            checked += 1
        self.assertGreaterEqual(checked, 12)

    def test_document_list_search_and_filter(self):
        url = reverse("admin:legal_documents_legaldocument_changelist")
        self.assertContains(self.client.get(url, {"q": "700"}), "700")
        self.assertContains(self.client.get(url, {"text_status": "ok"}), "Mehnat")
        self.assertNotContains(self.client.get(url, {"text_status": "stub"}), "Mehnat")

    def test_document_page_shows_versions_and_escapes_html(self):
        response = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[self.doc.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://lex.uz/docs/700")
        self.assertNotContains(response, "<script>x</script>")  # titles are untrusted input: must be escaped

    def test_version_page_renders_the_law_as_a_readable_document(self):
        response = self.client.get(reverse("admin:legal_documents_legaldocumentversion_change", args=[self.version.pk]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("1. Birinchi band &lt;b&gt;matni&lt;/b&gt;.", body)  # law text is escaped, not interpreted as HTML
        self.assertNotIn("<b>matni</b>", body)
        self.assertIn('margin:4px 0 4px 18px">(a) kichik band', body)  # item indented under its clause
        chapter = LegalDocument.objects.get(external_id="702").current_version
        page = self.client.get(reverse("admin:legal_documents_legaldocumentversion_change", args=[chapter.pk])).content.decode()
        self.assertIn(">I. Umumiy qoidalar</h3>", page)  # chapter rendered as a heading

    def test_chunk_page(self):
        chunk = DocumentChunk.objects.first()
        self.assertEqual(self.client.get(reverse("admin:search_documentchunk_change", args=[chunk.pk])).status_code, 200)

    def test_legal_data_cannot_be_added_changed_or_deleted_through_the_admin(self):
        for name in ("legaldocument", "legaldocumentversion", "legaldocumentsection"):
            app = "legal_documents"
            self.assertEqual(self.client.get(reverse(f"admin:{app}_{name}_add")).status_code, 403)
        change = reverse("admin:legal_documents_legaldocument_change", args=[self.doc.pk])
        self.assertEqual(self.client.post(change, {"title": "hacked"}).status_code, 403)
        delete = reverse("admin:legal_documents_legaldocument_delete", args=[self.doc.pk])
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.title, "Mehnat <script>x</script> kodeksi")

    def test_anonymous_users_are_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:legal_documents_legaldocument_changelist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_stub_versions_explain_why_there_is_no_text(self):
        from apps.legal_documents.constants import TextStatus

        svc = IngestService(make_source(), FilesystemObjectStore(tempfile.mkdtemp()))
        p = parsed(external_id="701", paragraphs=(), text_status=TextStatus.NEEDS_OCR)
        p.normalized_text, p.sections = "", []
        p.version_metadata["warnings"] = ["PDF looks scanned (39 chars/page): needs OCR"]
        r = svc.ingest(p)
        response = self.client.get(reverse("admin:legal_documents_legaldocumentversion_change", args=[r.version.pk]))
        self.assertContains(response, "No text stored")


@skipUnless((DATA_DIR / "state.sqlite3").exists(), "crawler archive not present")
class AdminOnRealDataTests(AdminBase):
    def test_real_law_page_and_language_variants(self):
        run_ingest(DATA_DIR, store=FilesystemObjectStore(tempfile.mkdtemp()), only_ids={"8501626", "-8501626"}, retry_delays=(0,))
        doc = LegalDocument.objects.get(external_id="8501626")
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[doc.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "uz")  # the Latin edition is linked from the Cyrillic one
        version = self.client.get(reverse("admin:legal_documents_legaldocumentversion_change", args=[doc.current_version_id]))
        self.assertContains(version, "Меҳнат")
        self.assertGreater(LegalDocumentVersion.objects.count(), 1)


class DocumentPageShowsTheWholeLawTests(AdminBase):
    """The document page must show every section combined, in order, human readable - while the sections
    themselves stay separate rows in the database."""

    def test_document_page_contains_all_sections_in_order(self):
        paragraphs = tuple(f"{i}. Band raqami {i} matni." for i in range(1, 30))
        IngestService(make_source(), FilesystemObjectStore(tempfile.mkdtemp())).ingest(parsed(external_id="800", paragraphs=paragraphs))
        doc = LegalDocument.objects.get(external_id="800")
        self.assertEqual(doc.current_version.sections.count(), 29)  # still separate rows
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[doc.pk])).content.decode()
        positions = [page.index(escape(f"{i}. Band raqami {i} matni.")) for i in range(1, 30)]
        self.assertEqual(positions, sorted(positions))  # all present, in document order
        self.assertIn("The law (current version, all sections combined)", page)
        self.assertNotIn("No text stored", page)

    def test_document_without_text_says_so_instead_of_showing_an_empty_box(self):
        from apps.legal_documents.constants import TextStatus

        p = parsed(external_id="801", paragraphs=(), text_status=TextStatus.STUB)
        p.normalized_text, p.sections = "", []
        IngestService(make_source(), FilesystemObjectStore(tempfile.mkdtemp())).ingest(p)
        doc = LegalDocument.objects.get(external_id="801")
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[doc.pk])).content.decode()
        self.assertIn("No text stored for this document", page)
        self.assertIn("No PDF has been downloaded", page)


class PdfViewerTests(AdminBase):
    PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

    def make_doc_with_pdf(self, external_id="810", role="pdf_export", data=None):
        from apps.legal_documents.models import LegalDocument as D
        from .factories import snapshot_draft

        p = parsed(external_id=external_id)
        pdf = snapshot_draft(data or self.PDF, kind="pdf", gz=False, url=f"https://lex.uz/pdffile/{external_id}", role=role)
        p.attachments = [pdf]
        IngestService(make_source(), get_object_store()).ingest(p)
        return D.objects.get(external_id=external_id)

    def test_pdf_is_embedded_on_the_document_page_and_served(self):
        doc = self.make_doc_with_pdf()
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[doc.pk])).content.decode()
        url = reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk])
        self.assertIn(f'<iframe src="{url}?attachment=', page)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_pdf_endpoint_requires_login(self):
        doc = self.make_doc_with_pdf()
        self.client.logout()
        response = self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_missing_pdf_file_is_a_clean_404_not_a_crash(self):
        import os

        doc = self.make_doc_with_pdf()
        snap = doc.attachments.get().snapshot
        os.remove(get_object_store()._path(snap.object_key))
        self.assertEqual(self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk])).status_code, 404)

    def test_a_file_that_is_not_a_pdf_is_never_served_as_one(self):
        doc = self.make_doc_with_pdf(external_id="811", data=b"<html>not a pdf</html>")
        self.assertEqual(self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk])).status_code, 404)

    def test_document_without_a_pdf_gets_404_on_the_endpoint(self):
        IngestService(make_source(), get_object_store()).ingest(parsed(external_id="812"))
        doc = LegalDocument.objects.get(external_id="812")
        self.assertEqual(self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk])).status_code, 404)

    def test_reingesting_does_not_duplicate_the_attachment(self):
        doc = self.make_doc_with_pdf()
        self.make_doc_with_pdf()
        self.assertEqual(doc.attachments.count(), 1)


@skipUnless((DATA_DIR / "state.sqlite3").exists(), "crawler archive not present")
class PdfOnRealDataTests(AdminBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        run_ingest(DATA_DIR, store=get_object_store(), only_ids={"8501626", "8508670", "8477063", "8493606"}, retry_delays=(0,))

    def test_regular_law_has_the_sites_pdf_export_attached(self):
        doc = LegalDocument.objects.get(external_id="8501626")
        self.assertEqual(doc.attachments.get().role, "pdf_export")
        response = self.client.get(reverse("admin:legal_documents_legaldocument_pdf", args=[doc.pk]))
        self.assertEqual((response.status_code, response.content[:5]), (200, b"%PDF-"))

    def test_pdf_only_act_shows_its_primary_pdf(self):
        doc = LegalDocument.objects.get(external_id="8508670")
        self.assertEqual(doc.attachments.get().role, "primary_pdf")
        page = self.client.get(reverse("admin:legal_documents_legaldocument_change", args=[doc.pk])).content.decode()
        self.assertIn("<iframe", page)
        self.assertIn("No text stored for this document", page)  # scanned: the PDF is the only readable form

    def test_pdf_whose_crawler_task_was_marked_failed_is_still_recovered(self):
        # 8493606: the old crawler encoding bug marked its (valid) PDF task "failed"
        doc = LegalDocument.objects.get(external_id="8493606")
        self.assertTrue(doc.attachments.filter(role="pdf_export").exists())

    def test_stub_without_a_pdf_says_so(self):
        doc = LegalDocument.objects.get(external_id="8477063")
        self.assertFalse(doc.attachments.exists())
