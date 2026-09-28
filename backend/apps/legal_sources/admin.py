from django.contrib import admin

from apps.common.admin import ReadOnlyAdmin

from .models import LegalSource, SourceSnapshot


@admin.register(LegalSource)
class LegalSourceAdmin(ReadOnlyAdmin):
    list_display = ("code", "name", "base_url", "is_official", "is_active")


@admin.register(SourceSnapshot)
class SourceSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("kind", "url", "size_bytes", "fetched_at", "sha256_short")
    list_filter = ("kind",)
    search_fields = ("url", "sha256")

    @admin.display(description="sha256")
    def sha256_short(self, obj):
        return obj.sha256[:12]
