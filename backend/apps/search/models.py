from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import F, Func, Q

from apps.common.models import UUIDModel


class SourceType(models.TextChoices):
    OFFICIAL_LAW = "official_law"
    OFFICIAL_GUIDANCE = "official_guidance"
    ORGANIZATION_INTERNAL = "organization_internal"
    DRAFT = "draft"


OFFICIAL_SOURCE_TYPES = [SourceType.OFFICIAL_LAW, SourceType.OFFICIAL_GUIDANCE]


class DocumentChunk(UUIDModel):
    """Retrieval unit (TZ 61-63).

    One physical table serves official law, organization knowledge base and drafts, told apart by
    `source_type`; KnowledgeChunk (TZ 44) is a view of this table filtered by source_type.
    organization_id NULL = shared/official (TZ 63) and is enforced by a CHECK plus RLS.
    """

    organization = models.ForeignKey(
        "organizations.Organization", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    source_type = models.CharField(max_length=25, choices=SourceType.choices)

    document = models.ForeignKey(
        "legal_documents.LegalDocument", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    version = models.ForeignKey(
        "legal_documents.LegalDocumentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="chunks"
    )
    section = models.ForeignKey(
        "legal_documents.LegalDocumentSection", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # KnowledgeDocument arrives with the knowledge_base app; plain column until then.
    knowledge_document_id = models.UUIDField(null=True, blank=True)

    chunk_index = models.PositiveIntegerField()
    text = models.TextField()  # verbatim content (what a citation must match)
    search_text = models.TextField()  # normalised for FTS: folded apostrophes, Uzbek Cyrillic -> Latin
    search_tsv = models.GeneratedField(
        expression=Func(
            F("search_text"),
            function="to_tsvector",
            template="to_tsvector('simple'::regconfig, %(expressions)s)",
        ),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    token_count = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    language = models.CharField(max_length=2, blank=True)
    chunker_version = models.PositiveSmallIntegerField()
    metadata = models.JSONField(default=dict, blank=True)  # heading_path, section_orders, anchors, ...
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # official <=> no organization (TZ 63); internal/draft <=> organization required
            models.CheckConstraint(
                condition=(
                    Q(source_type__in=OFFICIAL_SOURCE_TYPES, organization__isnull=True)
                    | (~Q(source_type__in=OFFICIAL_SOURCE_TYPES) & Q(organization__isnull=False))
                ),
                name="chk_chunk_org_matches_source_type",
            ),
            models.CheckConstraint(
                condition=Q(version__isnull=False) | Q(knowledge_document_id__isnull=False),
                name="chk_chunk_has_owner",
            ),
            models.UniqueConstraint(
                fields=["version", "chunk_index"],
                condition=Q(version__isnull=False),
                name="uniq_chunk_version_index",
            ),
        ]
        indexes = [
            models.Index(fields=["organization"]),
            models.Index(fields=["document"]),
            models.Index(fields=["source_type", "language"]),
            GinIndex(fields=["search_tsv"], name="chunk_search_tsv_gin"),
        ]
