from django.contrib import admin

from apps.common.admin import ReadOnlyAdmin, ReadOnlyTabularInline

from .models import ParserError, ParserItem, ParserJob


class ErrorInline(ReadOnlyTabularInline):
    model = ParserError
    fields = ("url", "exception", "retry_count", "occurred_at")
    readonly_fields = fields


@admin.register(ParserJob)
class ParserJobAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "status", "trigger", "total_count", "new_count", "updated_count", "unchanged_count", "skipped_count", "failed_count", "duration")
    list_filter = ("status", "trigger")
    date_hierarchy = "started_at"
    inlines = [ErrorInline]


@admin.register(ParserItem)
class ParserItemAdmin(ReadOnlyAdmin):
    list_display = ("job", "status", "external_id", "url", "detail")
    list_filter = ("status",)
    search_fields = ("external_id", "url")
    raw_id_fields = ("job", "document", "version", "snapshot")


@admin.register(ParserError)
class ParserErrorAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "url", "exception", "retry_count")
    search_fields = ("url", "exception")
    raw_id_fields = ("job", "item", "source")
