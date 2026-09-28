from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


@dataclass
class RawSnapshotDraft:
    """Raw bytes to archive in object storage. Bytes are read lazily: PDFs can be 12 MB+."""

    role: str  # "primary" (what the text came from) | "page"
    kind: str  # html | pdf | word
    url: str
    sha256: str  # of the decoded content
    path: Path
    fetched_at: datetime
    content_encoding: str = ""  # "gzip" when the file on disk is a gzip of the content
    content_type: str = ""
    data: bytes | None = None   # content held in memory (e.g. a PDF unpacked from a zip); `path` is then only provenance

    def read(self) -> bytes:
        return self.data if self.data is not None else self.path.read_bytes()


@dataclass
class CardDraft:
    kind: str
    raw_text: str
    fields: dict = field(default_factory=dict)
    fetched_at: datetime | None = None


@dataclass
class ParsedDocument:
    """Everything the loader needs, already parsed. Independent of the crawler's file layout."""

    external_id: str
    edition_on: date | None
    source_url: str
    fetched_at: datetime
    source_sha256: str

    title: str
    document_number: str = ""
    number_key: str = ""
    adopted_at: date | None = None
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    document_type: str = ""
    document_form: str = ""
    authority: str = ""
    rank_key: tuple[str, str] | None = None        # (card type, card form), normalised
    requisite_key: tuple[str, str] | None = None   # (folded authority, folded form) from the act itself
    status: str = "unknown"
    status_raw: str = ""

    language: str = ""
    script: str = ""
    language_source: str = ""

    representation: str = "html"
    text_status: str = "ok"
    text_source: str = "html"

    sections: list = field(default_factory=list)
    normalized_text: str = ""
    content_hash: str = ""

    classifications: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    cards: list[CardDraft] = field(default_factory=list)
    snapshots: list[RawSnapshotDraft] = field(default_factory=list)
    attachments: list[RawSnapshotDraft] = field(default_factory=list)  # files shown next to the text (PDF)

    valid_from: date | None = None
    valid_from_source: str = "observed"
    version_metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""  # cheap "anything changed?" key, see CrawlArchive.fingerprint

    @property
    def primary_snapshot(self) -> RawSnapshotDraft | None:
        return next((s for s in self.snapshots if s.role == "primary"), None)
