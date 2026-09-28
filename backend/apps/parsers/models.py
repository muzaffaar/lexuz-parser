from django.db import models

from apps.common.models import UUIDModel


class JobStatus(models.TextChoices):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class ItemStatus(models.TextChoices):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"


class ParserJob(UUIDModel):
    """One ingestion run (TZ 57)."""

    source = models.ForeignKey("legal_sources.LegalSource", on_delete=models.PROTECT, related_name="parser_jobs")
    trigger = models.CharField(max_length=20, default="manual")  # manual | scheduled
    status = models.CharField(max_length=25, choices=JobStatus.choices, default=JobStatus.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    total_count = models.PositiveIntegerField(default=0)
    new_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)
    params = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["-started_at"])]


class ParserItem(UUIDModel):
    job = models.ForeignKey(ParserJob, on_delete=models.CASCADE, related_name="items")
    url = models.CharField(max_length=600)
    external_id = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=10, choices=ItemStatus.choices)
    document = models.ForeignKey("legal_documents.LegalDocument", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    version = models.ForeignKey("legal_documents.LegalDocumentVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    snapshot = models.ForeignKey("legal_sources.SourceSnapshot", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    content_hash = models.CharField(max_length=64, blank=True)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["job", "status"]), models.Index(fields=["external_id"])]


class ParserError(UUIDModel):
    """A failure that did not stop the job (TZ 58)."""

    job = models.ForeignKey(ParserJob, on_delete=models.CASCADE, related_name="errors")
    item = models.ForeignKey(ParserItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="errors")
    source = models.ForeignKey("legal_sources.LegalSource", on_delete=models.PROTECT, related_name="+")
    url = models.CharField(max_length=600, blank=True)
    exception = models.TextField()
    traceback = models.TextField(blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["job"])]
