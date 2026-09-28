from django.contrib import admin

from apps.common.admin import ReadOnlyAdmin

from .models import LegalUpdate


@admin.register(LegalUpdate)
class LegalUpdateAdmin(ReadOnlyAdmin):
    list_display = ("detected_at", "update_type", "document", "parser_job")
    list_filter = ("update_type",)
    search_fields = ("document__external_id", "document__title")
    date_hierarchy = "detected_at"
    list_select_related = ("document",)
    raw_id_fields = ("document", "version", "parser_job")
