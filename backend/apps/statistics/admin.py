from django.contrib import admin
from django.http import Http404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.common.admin import ReadOnlyAdmin

from .comparison import compare, latest_snapshot
from .models import DocumentCategoryMap, SourceStatisticsSnapshot


@admin.register(SourceStatisticsSnapshot)
class SourceStatisticsSnapshotAdmin(ReadOnlyAdmin):
    """Snapshots of lex.uz's statistics page, and the page that compares them with our database."""

    list_display = ("captured_at", "total_documents", "consistent", "compare_link")
    list_filter = ("consistent",)
    readonly_fields = ("source", "captured_at", "source_sha256", "total_documents", "header_total", "consistent", "problems", "payload")
    change_list_template = "admin/statistics/snapshot_changelist.html"

    @admin.display(description="Compared with our database")
    def compare_link(self, obj):
        return format_html('<a href="{}">Compare</a>', reverse("admin:statistics_snapshot_compare", args=[obj.pk]))

    def get_urls(self):
        view = self.admin_site.admin_view(self.compare_view)
        custom = [
            path("compare/", view, name="statistics_compare_latest"),
            path("<uuid:snapshot_id>/compare/", view, name="statistics_snapshot_compare"),
        ]
        return custom + super().get_urls()

    def compare_view(self, request, snapshot_id=None):
        if snapshot_id is not None:
            snapshot = SourceStatisticsSnapshot.objects.filter(pk=snapshot_id).first()
        else:
            snapshot = SourceStatisticsSnapshot.objects.order_by("-captured_at").first()
        context = {**self.admin_site.each_context(request), "title": "lex.uz statistics vs. our database", "opts": self.model._meta}
        if snapshot is None:
            if snapshot_id is not None:
                raise Http404("No such statistics snapshot")
            context["comparison"] = None
        else:
            context["comparison"] = compare(snapshot)
        return TemplateResponse(request, "admin/statistics/compare.html", context)


@admin.register(DocumentCategoryMap)
class DocumentCategoryMapAdmin(admin.ModelAdmin):
    """Editable: lawyers map a card's "document type" to the category lex.uz counts it under."""

    list_display = ("label", "type_key", "category")
    search_fields = ("label", "type_key", "category")
