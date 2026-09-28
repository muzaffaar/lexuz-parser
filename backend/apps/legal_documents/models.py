from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.contrib.postgres.indexes import GinIndex, GistIndex, OpClass
from django.db import models
from django.db.models import F, Func, Q, Value

from apps.common.fields import LTreeField
from apps.common.ids import uuid7
from apps.common.models import TimestampedModel, UUIDModel

from .constants import (
    AttachmentRole,
    CardKind,
    DocumentStatus,
    Language,
    RelationType,
    Representation,
    Script,
    SectionType,
    TextSource,
    TextStatus,
    ValidFromSource,
)


class DocumentTypeRank(TimestampedModel):
    """Editable lookup: (type, form) -> legal rank, so lawyers can maintain it without a deploy.

    Ranks are NOT guessed from title prefixes (a bare "501-son" and a ministry registration
    number look alike). Unmatched pairs are left NULL and reported by every ingestion job.
    Higher number = higher legal force.

    Two key families, tried in order:
      card       the legal-analysis card's "document type" / "document form" (exact, but only ~7% of the
                 archive has a card yet)
      requisite  the act's own authority + form, e.g. ("ozbekiston respublikasi prezidentining", "qarori"),
                 folded to the Latin search key so Cyrillic/Latin editions share one row. Always available.
    """

    key_kind = models.CharField(max_length=10, default="card")  # "card" | "requisite"
    type_key = models.CharField(max_length=300)  # normalised card type, or folded authority
    form_key = models.CharField(max_length=200, blank=True)  # normalised card form / folded form word
    rank = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key_kind", "type_key", "form_key"], name="uniq_rank_key")]


class LegalDocument(TimestampedModel):
    """One lex.uz document ID = one language/script variant of an act (TZ 45, 46).

    The same act in Uzbek-Cyrillic, Uzbek-Latin, Russian and English has *different* IDs; they are
    tied together by `group_id` / LANGUAGE_VARIANT relations. Text lives in versions, not here.
    """

    source = models.ForeignKey("legal_sources.LegalSource", on_delete=models.PROTECT, related_name="documents")
    external_id = models.CharField(max_length=32)  # signed, exactly as on the site: "-8488547"
    source_url = models.CharField(max_length=500)

    language = models.CharField(max_length=2, choices=Language.choices, blank=True)
    script = models.CharField(max_length=4, choices=Script.choices, blank=True)
    language_source = models.CharField(max_length=20, blank=True)  # "card" | "detected"

    document_number = models.CharField(max_length=64, blank=True)
    number_key = models.CharField(max_length=64, blank=True)  # script-folded, for exact search across scripts
    title = models.TextField()
    document_type = models.CharField(max_length=200, blank=True)
    document_form = models.CharField(max_length=200, blank=True)
    authority = models.TextField(blank=True)
    legal_rank = models.PositiveSmallIntegerField(null=True, blank=True)

    adopted_at = models.DateField(null=True, blank=True)
    published_at = models.DateField(null=True, blank=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)  # date it lost force

    status = models.CharField(max_length=20, choices=DocumentStatus.choices, default=DocumentStatus.UNKNOWN)
    status_raw = models.CharField(max_length=100, blank=True)
    representation = models.CharField(max_length=10, choices=Representation.choices)
    text_status = models.CharField(max_length=10, choices=TextStatus.choices)

    current_version = models.ForeignKey(
        "LegalDocumentVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    group_id = models.UUIDField(null=True, blank=True, db_index=True)

    last_checked_at = models.DateTimeField(null=True, blank=True)  # of the latest CURRENT-text observation
    last_source_sha256 = models.CharField(max_length=64, blank=True)
    # Cheap "did anything about this document change?" key (page + cards + PDF + language links + ingest
    # logic version). Lets a daily run skip parsing of untouched documents.
    observation_fingerprint = models.CharField(max_length=64, blank=True)
    # First time we saw the act as expired while no loss-of-force date was known: the honest end of the
    # last window until a card supplies the real date.
    expiry_observed_on = models.DateField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # soft delete only (TZ 146)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="uniq_document_source_external"),
            models.CheckConstraint(
                condition=Q(status__in=DocumentStatus.values), name="chk_document_status"
            ),
            models.CheckConstraint(
                condition=Q(text_status__in=TextStatus.values), name="chk_document_text_status"
            ),
        ]
        indexes = [
            models.Index(fields=["number_key"]),
            models.Index(fields=["status"]),
            models.Index(fields=["adopted_at"]),
            models.Index(fields=["language", "text_status"]),
            GinIndex(OpClass("title", name="gin_trgm_ops"), name="legaldoc_title_trgm"),
        ]

    def __str__(self):
        return f"{self.external_id} {self.title[:60]}"


class LegalDocumentVersion(UUIDModel):
    """An immutable text edition (TZ 47). Content columns can never change; only the validity
    window / current flag move as newer editions arrive (enforced by a trigger)."""

    document = models.ForeignKey(LegalDocument, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()  # arrival order, not chronological
    edition_on = models.DateField(null=True, blank=True)  # the site's ?ONDATE= edition, if fetched as such

    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)  # inclusive; NULL = open ended
    valid_from_source = models.CharField(max_length=20, choices=ValidFromSource.choices)
    valid_period = models.GeneratedField(
        expression=Func(F("valid_from"), F("valid_to"), Value("[]"), function="daterange"),
        output_field=DateRangeField(),
        db_persist=True,
    )
    is_current = models.BooleanField(default=False)

    content_hash = models.CharField(max_length=64)  # sha256 of normalized_text (TZ 55)
    raw_snapshot = models.ForeignKey(
        "legal_sources.SourceSnapshot", null=True, blank=True, on_delete=models.PROTECT, related_name="versions"
    )
    normalized_text = models.TextField(blank=True)
    text_source = models.CharField(max_length=15, choices=TextSource.choices)
    parser_version = models.PositiveSmallIntegerField()  # structure-extractor version that built the sections
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "version_number"], name="uniq_version_number"),
            models.UniqueConstraint(
                fields=["document"], condition=Q(is_current=True), name="one_current_version_per_document"
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")), name="chk_version_range"
            ),
            # Two editions of one act can never claim the same day (TZ 77, critical defects 8/9).
            # Deferrable so a window rebuild can shuffle several rows inside one transaction.
            ExclusionConstraint(
                name="no_overlapping_versions",
                expressions=[("document", RangeOperators.EQUAL), ("valid_period", RangeOperators.OVERLAPS)],
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]
        indexes = [
            models.Index(fields=["document", "content_hash"]),
            GistIndex(fields=["valid_period"], name="version_valid_period_gist"),
        ]


class LegalDocumentSection(UUIDModel):
    """Structural node of one version (TZ 48-49). Immutable. Every content block of the source
    becomes exactly one section, so nothing is silently dropped."""

    version = models.ForeignKey(LegalDocumentVersion, on_delete=models.PROTECT, related_name="sections")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    section_type = models.CharField(max_length=15, choices=SectionType.choices)
    number = models.CharField(max_length=32, blank=True)
    title = models.TextField(blank=True)
    text = models.TextField(blank=True)
    order_index = models.PositiveIntegerField()
    path = LTreeField()
    source_anchor = models.CharField(max_length=32, blank=True)  # lex.uz element id -> source_url#anchor
    text_hash = models.CharField(max_length=64)  # lets the citation validator prove a quote is verbatim
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["version", "order_index"], name="uniq_section_order"),
            # "article N of document X" citation lookups (TZ 88) need a unique, indexed address
            models.UniqueConstraint(fields=["version", "path"], name="uniq_section_path"),
        ]
        indexes = [
            models.Index(fields=["version", "parent"]),
            models.Index(fields=["source_anchor"]),
            GistIndex(fields=["path"], name="section_path_gist"),  # subtree queries: path <@ 'ch_i'
        ]


class LegalDocumentRelation(UUIDModel):
    """Directed link between documents (TZ 44). Targets are stored by external id + url so links to
    documents that are not ingested *yet* are preserved and resolved later."""

    source_document = models.ForeignKey(LegalDocument, on_delete=models.CASCADE, related_name="relations_out")
    source_section = models.ForeignKey(
        LegalDocumentSection, null=True, blank=True, on_delete=models.PROTECT, related_name="relations_out"
    )
    target_document = models.ForeignKey(
        LegalDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="relations_in"
    )
    target_url = models.CharField(max_length=600)
    target_external_id = models.CharField(max_length=32, blank=True)
    target_edition_on = models.DateField(null=True, blank=True)
    target_anchor = models.CharField(max_length=64, blank=True)
    relation_type = models.CharField(max_length=20, choices=RelationType.choices)
    anchor_text = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_document", "target_url", "relation_type", "source_section"],
                name="uniq_relation",
                nulls_distinct=False,
            )
        ]
        indexes = [
            models.Index(fields=["target_external_id"]),
            models.Index(fields=["source_document", "relation_type"]),
        ]


class LegalDocumentClassification(UUIDModel):
    """OKOZ / TSZ subject classification (the INDEXES_ON_REF annotation blocks)."""

    document = models.ForeignKey(LegalDocument, on_delete=models.CASCADE, related_name="classifications")
    system = models.CharField(max_length=20)  # "okoz" | "tsz"
    code = models.CharField(max_length=40, blank=True)  # leaf code like 05.05.01.00 (TSZ has none)
    label = models.TextField()  # full "a / b / c" path
    fingerprint = models.CharField(max_length=32)  # md5(system + label), for the unique key

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "system", "fingerprint"], name="uniq_classification")
        ]
        indexes = [models.Index(fields=["system", "code"])]


class LegalDocumentAttachment(UUIDModel):
    """A file belonging to a document, kept as a raw snapshot in object storage (e.g. the PDF shown next to the
    text in the admin). Mutable bookkeeping, unlike versions: files can arrive on a later crawl than the text."""

    document = models.ForeignKey(LegalDocument, on_delete=models.CASCADE, related_name="attachments")
    snapshot = models.ForeignKey("legal_sources.SourceSnapshot", on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=20, choices=AttachmentRole.choices)
    source_url = models.CharField(max_length=600)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "snapshot"], name="uniq_attachment")]


class LegalDocumentCard(UUIDModel):
    """Metadata cards from the source (passport, legal analysis card, ...). Stored as text +
    parsed key/value pairs, so parsing can be redone without re-crawling."""

    document = models.ForeignKey(LegalDocument, on_delete=models.CASCADE, related_name="cards")
    kind = models.CharField(max_length=20, choices=CardKind.choices)
    raw_text = models.TextField()
    fields = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "kind"], name="uniq_card_kind")]
