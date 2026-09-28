from django.db import models

from apps.common.models import TimestampedModel


class Organization(TimestampedModel):
    """Minimal tenant record. The full accounts/RBAC/licensing layer is a later milestone;
    it exists here because org-scoped tables (chunks, embeddings) must reference it."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
