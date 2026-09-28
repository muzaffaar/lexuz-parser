"""Read-only browser for the legal corpus. The document page renders the law as one readable document
(headings, numbered clauses, tables, appendices) from the stored sections, with the original PDF(s) embedded."""
import gzip

from django.contrib import admin
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.urls import path, reverse
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

from apps.common.admin import ReadOnlyAdmin, ReadOnlyTabularInline
from apps.common.storage import get_object_store

from .constants import SectionType as T
from .models import (
    DocumentTypeRank,
    LegalDocument,
    LegalDocumentAttachment,
    LegalDocumentCard,
    LegalDocumentClassification,
    LegalDocumentRelation,
    LegalDocumentSection,
    LegalDocumentVersion,
)

_HEADINGS = {T.APPENDIX: "h2", T.PART: "h2", T.SECTION: "h3", T.CHAPTER: "h3", T.ARTICLE: "h4"}
_MUTED = {T.SIGNATURE, T.PLACE_DATE, T.NOTE, T.FOOTNOTE}
_VIEWABLE_ROLES = ["primary_pdf", "pdf_export"]  # the source zip is provenance, not something to render


def _html(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def render_law(version, limit: int | None = None) -> str:
    """The stored sections as one readable page. Indentation follows the section tree (path depth)."""
    rows = version.sections.order_by("order_index").values_list("section_type", "number", "text", "path", "source_anchor")
    if limit:
        rows = rows[:limit]
    out = ['<div style="max-width:980px;line-height:1.55;font-size:14px;background:#fff;color:#222;border:1px solid #ccc;padding:18px 26px;border-radius:4px">']
    for section_type, number, text, section_path, anchor in rows:
        if not text:
            continue
        depth = section_path.count(".")
        body = _html(text)
        if section_type in _HEADINGS:
            out.append(f'<{_HEADINGS[section_type]} style="margin:18px 0 6px">{body}</{_HEADINGS[section_type]}>')
        elif section_type == T.TITLE:
            out.append(f'<p style="font-weight:bold;font-size:16px;margin:10px 0">{body}</p>')
        elif section_type in (T.FORM, T.BODY, T.NUMBER):
            out.append(f'<p style="margin:2px 0;color:#444">{body}</p>')
        elif section_type == T.TABLE:
            out.append(f'<pre style="white-space:pre-wrap;background:#f6f6f6;padding:8px;margin:8px 0;border:1px solid #ddd">{escape(text)}</pre>')
        elif section_type == T.PDF_PAGE:  # text taken from a PDF: keep its line breaks, mark page boundaries
            out.append(f'<div style="color:#999;font-size:12px;margin:14px 0 2px">- PDF page {escape(number)} -</div><p style="margin:0">{body}</p>')
        elif section_type in _MUTED:
            out.append(f'<p style="color:#777;font-style:italic;margin:4px 0">{body}</p>')
        else:  # clause / subclause / item / paragraph
            out.append(f'<p style="margin:4px 0 4px {min(depth, 6) * 18}px">{body}</p>')
    out.append("</div>")
    return mark_safe("".join(out))


class VersionInline(ReadOnlyTabularInline):
    model = LegalDocumentVersion
    fields = ("version_number", "edition_on", "valid_from", "valid_to", "valid_from_source", "is_current", "text_source")
    readonly_fields = fields
    ordering = ("valid_from", "version_number")


class RelationInline(ReadOnlyTabularInline):
    model = LegalDocumentRelation
    fk_name = "source_document"
    fields = ("relation_type", "target_document", "target_url", "anchor_text")
    readonly_fields = fields
    verbose_name_plural = "Language variants and editions (citations are in their own list)"
    ordering = ("relation_type",)

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(relation_type="cites").select_related("target_document")


@admin.register(LegalDocument)
class LegalDocumentAdmin(ReadOnlyAdmin):
    list_display = ("external_id", "short_title", "language", "script", "document_number", "status", "text_status", "adopted_at", "n_versions")
    list_display_links = ("external_id", "short_title")
    list_filter = ("text_status", "language", "script", "status", "representation")
    search_fields = ("external_id", "document_number", "number_key", "title")
    date_hierarchy = "adopted_at"
    ordering = ("-adopted_at", "external_id")
    list_per_page = 50
    inlines = [VersionInline, RelationInline]
    fieldsets = (
        (None, {"fields": ("title", "document_number", "external_id", "open_on_lex", "variants")}),
        ("The law (current version, all sections combined)", {"fields": ("law_text",)}),
        ("Original PDF", {"fields": ("pdf_viewer",)}),
        ("Classification", {"fields": ("document_type", "document_form", "authority", "legal_rank", "language", "script", "language_source")}),
        ("Dates and status", {"fields": ("adopted_at", "published_at", "effective_from", "effective_to", "status", "status_raw", "expiry_observed_on")}),
        ("Text", {"fields": ("representation", "text_status", "current_version")}),
        ("Bookkeeping", {"classes": ("collapse",), "fields": ("source", "group_id", "number_key", "last_checked_at", "last_source_sha256", "observation_fingerprint", "deleted_at")}),
    )
    readonly_fields = ("open_on_lex", "variants", "law_text", "pdf_viewer")

    def get_urls(self):
        custom = [path("<path:object_id>/pdf/", self.admin_site.admin_view(self.pdf_view), name="legal_documents_legaldocument_pdf")]
        return custom + super().get_urls()

    # NOTE: format_html() escapes its arguments to str first, so a format spec like {:,} raises ValueError, which the
    # admin then silently renders as '-'. Pre-format numbers in Python (regression: tests/test_admin.py).
    def _pdf_attachments(self, obj):
        # primary files (the act's own PDF) before the site's export
        return list(obj.attachments.filter(role__in=_VIEWABLE_ROLES).select_related("snapshot").order_by("role", "created_at"))

    def pdf_view(self, request, object_id):
        """Streams a stored PDF (from object storage) for embedding. Same permission as viewing the document.
        `?attachment=<id>` selects one when a document has several; the default is the first."""
        obj = self.get_object(request, object_id)
        if obj is None or not self.has_view_permission(request, obj):
            raise Http404("No such document")
        attachments = self._pdf_attachments(obj)
        wanted = request.GET.get("attachment")
        attachment = next((a for a in attachments if str(a.pk) == wanted), None) if wanted else (attachments[0] if attachments else None)
        if attachment is None:
            raise Http404("No PDF stored for this document")
        try:
            data = get_object_store().get(attachment.snapshot.object_key)
        except (FileNotFoundError, KeyError, OSError) as exc:
            raise Http404("PDF file is missing from object storage") from exc
        if attachment.snapshot.content_encoding == "gzip":
            data = gzip.decompress(data)
        if not data.startswith(b"%PDF-"):
            raise Http404("Stored file is not a PDF")
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="lexuz-{obj.external_id}.pdf"'
        response["X-Frame-Options"] = "SAMEORIGIN"  # embeddable in this admin page only
        return response

    @admin.display(description="Law text")
    def law_text(self, obj):
        version = obj.current_version
        if version is None or not version.normalized_text:
            return format_html("<em>No text stored for this document ({}). See the PDF below if there is one.</em>", obj.get_text_status_display())
        header = format_html(
            '<p style="margin:0 0 8px;color:#666">Current version {} - valid from {}{} - {} characters. <a href="{}">Open version page</a></p>',
            version.version_number, version.valid_from, f" to {version.valid_to}" if version.valid_to else "", f"{len(version.normalized_text):,}",
            reverse("admin:legal_documents_legaldocumentversion_change", args=[version.pk]),
        )
        return header + render_law(version)

    @admin.display(description="PDF")
    def pdf_viewer(self, obj):
        attachments = self._pdf_attachments(obj)
        if not attachments:
            return format_html("<em>No PDF has been downloaded for this document yet.</em>")
        base = reverse("admin:legal_documents_legaldocument_pdf", args=[obj.pk])
        blocks = []
        for att in attachments:
            url = f"{base}?attachment={att.pk}"
            name = att.source_url.rsplit("#", 1)[-1] if "#" in att.source_url else att.source_url.rsplit("/", 1)[-1]
            blocks.append(
                format_html(
                    '<p style="margin:12px 0 6px"><strong>{1}</strong> <span style="color:#666">({2}, {3} bytes)</span> - '
                    '<a href="{0}" target="_blank" rel="noopener">open in a new tab</a></p>'
                    '<iframe src="{0}" title="{1}" style="width:100%;max-width:980px;height:80vh;border:1px solid #ccc;background:#eee"></iframe>',
                    url, name, att.get_role_display(), f"{att.snapshot.size_bytes:,}",
                )
            )
        return mark_safe("".join(blocks))

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_n_versions=Count("versions"))

    @admin.display(description="Title")
    def short_title(self, obj):
        return obj.title if len(obj.title) <= 90 else obj.title[:87] + "..."

    @admin.display(description="Versions", ordering="_n_versions")
    def n_versions(self, obj):
        return obj._n_versions

    @admin.display(description="Open on lex.uz")
    def open_on_lex(self, obj):
        return format_html('<a href="{0}" target="_blank" rel="noopener">{0}</a>', obj.source_url)

    @admin.display(description="Same act in other languages / scripts")
    def variants(self, obj):
        if not obj.group_id:
            return "-"
        others = LegalDocument.objects.filter(group_id=obj.group_id).exclude(pk=obj.pk).order_by("language", "script")
        return format_html_join(
            " | ", '<a href="{}">{} {} ({})</a>',
            ((reverse("admin:legal_documents_legaldocument_change", args=[d.pk]), d.language or "?", d.script, d.external_id) for d in others),
        ) or "-"


@admin.register(LegalDocumentVersion)
class LegalDocumentVersionAdmin(ReadOnlyAdmin):
    list_display = ("document_link", "version_number", "edition_on", "valid_from", "valid_to", "valid_from_source", "is_current", "text_status", "chars")
    list_filter = ("is_current", "valid_from_source", "text_source")
    search_fields = ("document__external_id", "document__document_number", "document__title")
    ordering = ("-created_at",)
    list_per_page = 50
    list_select_related = ("document",)
    fieldsets = (
        (None, {"fields": ("document_link", "version_number", "is_current", ("valid_from", "valid_to", "valid_from_source"), "edition_on")}),
        ("The law, as stored", {"fields": ("reader",)}),
        ("Plain text (exactly what search/citations see)", {"classes": ("collapse",), "fields": ("normalized_text",)}),
        ("Provenance", {"classes": ("collapse",), "fields": ("content_hash", "text_source", "raw_snapshot", "parser_version", "metadata", "created_at")}),
    )
    readonly_fields = ("document_link", "reader")

    @admin.display(description="Document", ordering="document__external_id")
    def document_link(self, obj):
        return format_html(
            '<a href="{}">{} - {}</a>', reverse("admin:legal_documents_legaldocument_change", args=[obj.document_id]),
            obj.document.external_id, obj.document.title[:70],
        )

    @admin.display(description="Text status")
    def text_status(self, obj):
        return obj.metadata.get("text_status", "")

    @admin.display(description="Characters")
    def chars(self, obj):
        return len(obj.normalized_text)

    @admin.display(description="Reader")
    def reader(self, obj):
        if not obj.normalized_text:
            reason = obj.metadata.get("stub_reason") or (obj.metadata.get("warnings") or ["no text captured"])[0]
            return format_html("<em>No text stored: {}</em>", reason)
        return render_law(obj)


@admin.register(LegalDocumentSection)
class LegalDocumentSectionAdmin(ReadOnlyAdmin):
    list_display = ("document_external_id", "order_index", "section_type", "number", "path", "short_text")
    list_filter = ("section_type",)
    search_fields = ("version__document__external_id", "path")
    ordering = ("version__document__external_id", "order_index")
    list_select_related = ("version__document",)
    list_per_page = 100
    raw_id_fields = ("version", "parent")

    @admin.display(description="Document")
    def document_external_id(self, obj):
        return obj.version.document.external_id

    @admin.display(description="Text")
    def short_text(self, obj):
        return obj.text[:110]


@admin.register(LegalDocumentCard)
class LegalDocumentCardAdmin(ReadOnlyAdmin):
    list_display = ("document", "kind", "fetched_at")
    list_filter = ("kind",)
    search_fields = ("document__external_id",)
    raw_id_fields = ("document",)


@admin.register(LegalDocumentClassification)
class LegalDocumentClassificationAdmin(ReadOnlyAdmin):
    list_display = ("document", "system", "code", "short_label")
    list_filter = ("system",)
    search_fields = ("code", "label", "document__external_id")
    raw_id_fields = ("document",)

    @admin.display(description="Label")
    def short_label(self, obj):
        return obj.label[:100]


@admin.register(LegalDocumentRelation)
class LegalDocumentRelationAdmin(ReadOnlyAdmin):
    list_display = ("source_document", "relation_type", "target_external_id", "target_document", "target_anchor")
    list_filter = ("relation_type",)
    search_fields = ("source_document__external_id", "target_external_id")
    raw_id_fields = ("source_document", "target_document", "source_section")


@admin.register(LegalDocumentAttachment)
class LegalDocumentAttachmentAdmin(ReadOnlyAdmin):
    list_display = ("document", "role", "source_url", "created_at")
    list_filter = ("role",)
    search_fields = ("document__external_id",)
    raw_id_fields = ("document", "snapshot")


@admin.register(DocumentTypeRank)
class DocumentTypeRankAdmin(admin.ModelAdmin):
    """The one EDITABLE table here: lawyers maintain (type, form) -> legal rank without a deploy."""

    list_display = ("key_kind", "type_key", "form_key", "rank", "label")
    list_filter = ("key_kind",)
    search_fields = ("type_key", "form_key", "label")
    ordering = ("-rank",)
