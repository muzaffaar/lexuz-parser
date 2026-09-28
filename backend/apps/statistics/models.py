from django.db import models

from apps.common.models import TimestampedModel, UUIDModel


class SourceStatisticsSnapshot(UUIDModel):
    """What lex.uz itself said about its database at one moment (https://lex.uz/uz/statistic), captured by
    `python -m lex_crawler stats`. Kept as it was read so the archive can be measured against the source later."""

    source = models.ForeignKey("legal_sources.LegalSource", on_delete=models.PROTECT, related_name="statistics_snapshots")
    captured_at = models.DateTimeField()
    source_sha256 = models.CharField(max_length=64)
    total_documents = models.PositiveIntegerField()
    header_total = models.PositiveIntegerField(null=True, blank=True)  # "Bugun bazada N ta hujjat"
    consistent = models.BooleanField()  # the page's own arithmetic held when it was read
    problems = models.JSONField(default=list, blank=True)
    payload = models.JSONField()  # {"grand_total": {...}, "categories": [...], "rows": [...]} exactly as parsed
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]
        constraints = [models.UniqueConstraint(fields=["source", "captured_at"], name="uniq_stats_source_captured")]

    def __str__(self):
        return f"lex.uz statistics {self.captured_at:%Y-%m-%d %H:%M} ({self.total_documents:,} documents)"


class DocumentCategoryMap(TimestampedModel):
    """Editable lookup: the legal-analysis card's "document type" -> lex.uz's statistics category.

    lex.uz counts by category (Prezident hujjatlari, Hukumat qarorlari ...). Only the card names a document's
    category, so a document without a card is reported as "not classified" rather than guessed."""

    type_key = models.CharField(max_length=300, unique=True)  # apps.statistics.keys.category_key(card type)
    category = models.CharField(max_length=200)  # exactly as written on https://lex.uz/uz/statistic
    label = models.CharField(max_length=200, blank=True)  # the card's own wording, for humans
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.label or self.type_key} -> {self.category}"
