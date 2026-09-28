from django.db import models

from apps.common.ids import uuid7
from apps.common.models import TimestampedModel


class LegalSource(TimestampedModel):
    code = models.SlugField(max_length=40, unique=True)  # e.g. "lexuz"
    name = models.CharField(max_length=255)
    base_url = models.URLField(max_length=300)
    is_official = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.code


class SnapshotKind(models.TextChoices):
    HTML = "html"
    PDF = "pdf"
    WORD = "word"
    JSON = "json"
    XML = "xml"
    ZIP = "zip"


class SourceSnapshot(models.Model):
    """Pointer to the raw bytes of something fetched from a source (TZ 56).

    The bytes live in object storage; Postgres keeps metadata + key only (TZ 98).
    Content-addressed: one row per distinct (source, sha256). Immutable.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    source = models.ForeignKey(LegalSource, on_delete=models.PROTECT, related_name="snapshots")
    kind = models.CharField(max_length=10, choices=SnapshotKind.choices)
    url = models.CharField(max_length=600)  # first URL this content was seen at
    sha256 = models.CharField(max_length=64)  # of the *original* (decoded) bytes
    object_key = models.CharField(max_length=300)
    content_encoding = models.CharField(max_length=20, blank=True)  # "gzip" when stored compressed
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.BigIntegerField()  # decoded size
    fetched_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "sha256"], name="uniq_snapshot_source_sha256")]
        indexes = [models.Index(fields=["url"])]

    def __str__(self):
        return f"{self.kind}:{self.sha256[:12]}"
