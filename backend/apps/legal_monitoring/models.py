from django.db import models

from apps.common.models import UUIDModel


class UpdateType(models.TextChoices):
    NEW = "new"
    AMENDED = "amended"
    EXPIRED = "expired"
    EFFECTIVE = "effective"


class LegalUpdate(UUIDModel):
    """Change detected by the parser (TZ 118)."""

    document = models.ForeignKey("legal_documents.LegalDocument", on_delete=models.PROTECT, related_name="updates")
    version = models.ForeignKey(
        "legal_documents.LegalDocumentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="updates"
    )
    parser_job = models.ForeignKey(
        "parsers.ParserJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="legal_updates"
    )
    update_type = models.CharField(max_length=10, choices=UpdateType.choices)
    detected_at = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["update_type", "-detected_at"]), models.Index(fields=["document"])]
