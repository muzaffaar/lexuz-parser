from django.contrib import admin
from django.utils.html import format_html

from apps.common.admin import ReadOnlyAdmin

from .models import DocumentChunk


@admin.register(DocumentChunk)
class DocumentChunkAdmin(ReadOnlyAdmin):
    """What retrieval / RAG will actually see: each chunk with the headings it was cut under."""

    list_display = ("document_external_id", "chunk_index", "kind", "token_count", "headings", "preview")
    list_filter = ("source_type", "language")
    search_fields = ("document__external_id", "document__document_number")
    ordering = ("document__external_id", "version_id", "chunk_index")
    list_select_related = ("document",)
    list_per_page = 50
    raw_id_fields = ("organization", "document", "version", "section")
    exclude = ("search_tsv",)

    @admin.display(description="Document")
    def document_external_id(self, obj):
        return obj.document.external_id if obj.document_id else "(org)"

    @admin.display(description="Kind")
    def kind(self, obj):
        return obj.metadata.get("kind", "")

    @admin.display(description="Under headings")
    def headings(self, obj):
        return " > ".join(h[:40] for h in obj.metadata.get("heading_path", []))

    @admin.display(description="Text")
    def preview(self, obj):
        return format_html("{}", obj.text[:120].replace("\n", " / "))
