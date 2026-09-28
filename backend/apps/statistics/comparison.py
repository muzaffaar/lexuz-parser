"""Our database measured against lex.uz's own statistics page.

lex.uz counts every language variant as a separate document, exactly as we store one LegalDocument per lex.uz
document id, so the numbers are directly comparable. Historical editions (?ONDATE=) are versions of one document
on both sides and are not counted twice. A dimension we cannot fill from what the archive holds is reported as
"unknown" on our side instead of being guessed, so a low coverage always says whether documents are missing or
merely not classified yet.
"""
from dataclasses import dataclass, field

from django.db.models import Count

from apps.legal_documents.models import LegalDocument, LegalDocumentVersion
from apps.parsers.models import JobStatus, ParserJob

from .keys import category_key
from .models import DocumentCategoryMap, SourceStatisticsSnapshot

LANGUAGES = (
    ("ru", "Russian (Ruscha)", {"language": "ru"}),
    ("uz_cyrl", "Uzbek Cyrillic (O‘zbekcha kirillcha)", {"language": "uz", "script": "cyrl"}),
    ("uz_latn", "Uzbek Latin (O‘zbekcha lotincha)", {"language": "uz", "script": "latn"}),
    ("en", "English (Inglizcha)", {"language": "en"}),
)
STATUSES = (
    ("in_force", "In force (Amalda)", "active"),
    ("new_edition", "New edition pending (Yangi tahrirda)", None),  # lex.uz-only concept; we track editions as versions
    ("lost_force", "Lost force (Kuchini yo‘qotgan)", "expired"),
)
NOT_CLASSIFIED = "Not classified (no legal-analysis card yet)"


@dataclass
class Row:
    label: str
    site: int | None  # None: lex.uz has no such line
    ours: int | None  # None: we cannot say
    note: str = ""

    @property
    def missing(self):
        return None if self.site is None or self.ours is None else self.site - self.ours

    @property
    def coverage(self):
        """Percent of lex.uz's documents we hold; None when either side is unknown or lex.uz has none."""
        if not self.site or self.ours is None:
            return None
        return min(100.0, round(100.0 * self.ours / self.site, 1))

    @property
    def more_than_site(self):
        return self.site is not None and self.ours is not None and self.ours > self.site


@dataclass
class Section:
    key: str
    title: str
    rows: list[Row] = field(default_factory=list)
    explanation: str = ""


@dataclass
class Comparison:
    snapshot: SourceStatisticsSnapshot
    previous: SourceStatisticsSnapshot | None
    sections: list[Section]
    overall: Row  # lex.uz vs. documents ingested into the database
    crawled: Row | None  # lex.uz vs. items in the crawler archive (None: no full ingest run recorded yet)
    job: ParserJob | None  # the run that measured `crawled`

    @property
    def site_change(self):
        return None if self.previous is None else self.snapshot.total_documents - self.previous.total_documents


def latest_snapshot(source):
    return SourceStatisticsSnapshot.objects.filter(source=source).order_by("-captured_at").first()


def compare(snapshot: SourceStatisticsSnapshot) -> Comparison:
    live = LegalDocument.objects.filter(source=snapshot.source, deleted_at__isnull=True)
    grand = snapshot.payload["grand_total"]
    total = live.count()
    previous = (
        SourceStatisticsSnapshot.objects.filter(source=snapshot.source, captured_at__lt=snapshot.captured_at)
        .order_by("-captured_at").first()
    )

    job = _last_full_run(snapshot.source)
    crawled = None
    if job is not None:
        crawled = Row(
            "Crawled (items in the crawler archive)", grand["total"], job.total_count,
            f"counted by the ingest run of {job.started_at:%Y-%m-%d %H:%M}; includes historical editions",
        )
    ingested = Row("Ingested (legal documents in the database)", grand["total"], total)
    versions = LegalDocumentVersion.objects.filter(document__in=live).count()
    steps = Section(
        "progress", "Progress: lex.uz -> crawled -> ingested",
        explanation=(
            "Crawled = what the crawler has saved; ingested = what is now a legal document in PostgreSQL. "
            "A historical edition of an act is stored as a version of that act, so it is crawled but not a separate document."
        ),
    )
    if crawled:
        steps.rows.append(crawled)
    steps.rows.append(ingested)
    steps.rows.append(Row("Document versions stored (current + historical editions)", None, versions, "only ours"))
    if job is not None:
        steps.rows.append(Row(
            "Of the crawled items, ingested unchanged/new/updated", job.total_count,
            job.new_count + job.updated_count + job.unchanged_count,
            f"skipped {job.skipped_count}, failed {job.failed_count} in that run",
        ))
    text = Section(
        "text", "Ingested documents by text captured",
        explanation="'stub' pages have only a header (untranslated language variants); 'needs_ocr' are scanned PDFs with no usable text.",
    )
    for row in live.values("text_status").annotate(n=Count("id")).order_by("-n"):
        text.rows.append(Row(row["text_status"] or "unknown", None, row["n"], "only ours"))

    languages = Section(
        "language", "By language", explanation="lex.uz counts each language variant of an act as its own document; so do we."
    )
    matched = 0
    for key, label, where in LANGUAGES:
        ours = live.filter(**where).count()
        matched += ours
        languages.rows.append(Row(label, grand[key], ours))
    if matched != total:
        languages.rows.append(Row("Language not detected", None, total - matched, "only ours"))

    statuses = Section(
        "status", "By legal status",
        explanation="Our status comes from the legal-analysis card; documents without one are 'unknown', never assumed in force.",
    )
    known = 0
    for key, label, ours_status in STATUSES:
        ours = live.filter(status=ours_status).count() if ours_status else None
        known += ours or 0
        statuses.rows.append(Row(label, grand[key], ours, "tracked as document versions" if ours is None else ""))
    statuses.rows.append(Row("Status not known", None, total - known, "only ours"))

    sections = [steps, languages, statuses, _categories(snapshot, live, total), text]
    return Comparison(snapshot, previous, sections, ingested, crawled, job)


def _last_full_run(source):
    """Latest finished ingest run over the whole archive (a --limit / --only run does not measure it)."""
    for job in ParserJob.objects.filter(source=source, status__in=[JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS]).order_by("-started_at")[:20]:
        if not job.params.get("limit") and not job.params.get("only_ids"):
            return job
    return None


def _categories(snapshot, live, total) -> Section:
    site = {c["category"]: c["total"] for c in snapshot.payload["categories"]}
    mapping = {m.type_key: m.category for m in DocumentCategoryMap.objects.all()}
    ours = dict.fromkeys(site, 0)
    unclassified = 0
    unmapped: dict[str, int] = {}
    for row in live.values("document_type").annotate(n=Count("id")):
        raw = row["document_type"]
        if not raw:
            unclassified += row["n"]
            continue
        category = mapping.get(category_key(raw))
        if category in ours:
            ours[category] += row["n"]
        else:
            unmapped[raw] = unmapped.get(raw, 0) + row["n"]
    section = Section(
        "category", "By document kind",
        explanation=(
            "lex.uz's category is known to us only through the legal-analysis card. Documents without a card are listed "
            "as not classified, so their absence from a row here is not proof that they are missing."
        ),
    )
    section.rows = [Row(name, count, ours[name]) for name, count in site.items()]
    if unclassified:
        section.rows.append(Row(NOT_CLASSIFIED, None, unclassified, "only ours"))
    for raw, count in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        section.rows.append(Row(f"Card type not mapped yet: {raw}", None, count, "add it under Document category maps"))
    return section
