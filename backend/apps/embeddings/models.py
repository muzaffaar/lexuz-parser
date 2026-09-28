from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from pgvector.django import VectorField

from apps.common.models import TimestampedModel, UUIDModel


class DistanceMetric(models.TextChoices):
    COSINE = "cosine"
    L2 = "l2"
    INNER_PRODUCT = "ip"


class EmbeddingProfile(TimestampedModel):
    """A concrete embedding model + settings (TZ 64). Never hard-coded in code.

    `dimension` is fixed for the life of the profile. Switching models = a *new* profile,
    re-embedding in the background, benchmark, then flipping is_active (TZ 66, 68).
    """

    name = models.CharField(max_length=100, unique=True)
    model_name = models.CharField(max_length=200)
    model_version = models.CharField(max_length=100)
    # pgvector's HNSW index supports at most 2000 dimensions for `vector`.
    dimension = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(2000)])
    distance_metric = models.CharField(max_length=10, choices=DistanceMetric.choices, default=DistanceMetric.COSINE)
    hnsw_m = models.PositiveSmallIntegerField(default=16)
    hnsw_ef_construction = models.PositiveSmallIntegerField(default=64)
    is_active = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # at most one active profile at any time
            models.UniqueConstraint(fields=["is_active"], condition=Q(is_active=True), name="one_active_embedding_profile"),
            models.CheckConstraint(condition=Q(dimension__gte=1, dimension__lte=2000), name="chk_profile_dimension"),
        ]

    def __str__(self):
        return f"{self.name} ({self.dimension}d)"


class ChunkEmbedding(UUIDModel):
    chunk = models.ForeignKey("search.DocumentChunk", on_delete=models.CASCADE, related_name="embeddings")
    embedding_profile = models.ForeignKey(EmbeddingProfile, on_delete=models.PROTECT, related_name="embeddings")
    # Denormalised from the chunk so the RLS policy needs no subquery (that would defeat ANN
    # index use). RLS policy verifies it equals the chunk's organization.
    organization = models.ForeignKey(
        "organizations.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    # Untyped `vector`: dimensions differ per profile. A trigger checks vector_dims() against the
    # profile; each profile gets its own partial HNSW index over embedding::vector(N).
    embedding = VectorField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["chunk", "embedding_profile"], name="uniq_chunk_profile")]
        indexes = [models.Index(fields=["embedding_profile"]), models.Index(fields=["organization"])]
