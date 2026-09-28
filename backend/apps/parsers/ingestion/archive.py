"""Reader for the lex-crawler archive layout (the hand-off between the Internet-facing collector and
the private zone, TZ 53). This is the ONLY module that knows the crawler's on-disk format:

    <root>/state.sqlite3                 tasks / edges (read-only here)
    <root>/documents/<id>_<task>/document.json
    <root>/resources/<task>/{page.html,file.pdf,text.json,source.json,...}
    <root>/blobs/<aa>/<sha256>.gz        gzip of the original HTTP body

The private zone never connects back to the crawler, and the crawler never sees Postgres.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from apps.common.hashing import sha256_hex

# Bump when ingestion semantics change in a way that should re-apply already-ingested documents
# (a new extracted field, a new relation type...). Part of the fingerprint below.
INGEST_LOGIC_VERSION = 4  # 2: PDF attachments; 3: PDFs from zip assets; 4: numberless-title dates + start-date upgrade

CARD_URLS = {
    "passport": "https://lex.uz/doc-passport/{id}",
    "card1": "https://lex.uz/actinfo/card1/{id}",
    "card2": "https://lex.uz/actinfo/card2/{id}",
    "basrev": "https://lex.uz/actinfo/basrev/{id}",
    "revhis": "https://lex.uz/actinfo/revhis/{id}",
    "correspondents": "https://lex.uz/actinfo/correspondents/{id}",
    "respondents": "https://lex.uz/actinfo/respondents/{id}",
}


log = logging.getLogger(__name__)


class ArchiveError(RuntimeError):
    pass


@dataclass
class ArchivedDocument:
    """A document folder. The JSON is parsed lazily, INSIDE the caller's per-item error handling, so one
    truncated/corrupt file (a collector writing while we ingest) fails one item instead of the whole job."""

    folder: Path
    _record: dict | None = field(default=None, repr=False)

    @property
    def folder_key(self) -> str:
        return self.folder.name

    @property
    def external_id(self) -> str:
        return self.folder.name.rsplit("_", 1)[0]  # "<signed id>_<task id>"

    @property
    def record(self) -> dict:
        if self._record is None:
            self._record = json.loads((self.folder / "document.json").read_text(encoding="utf-8"))
        return self._record


class CrawlArchive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        db_path = self.root / "state.sqlite3"
        if not db_path.exists():
            raise ArchiveError(f"Not a crawler archive (no state.sqlite3): {self.root}")
        # read-only: never write to (or lock) an archive a crawler may still be using
        self._db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._tasks = {r["url"]: dict(r) for r in self._db.execute("SELECT id, url, kind, status, source_hash FROM tasks")}
        self._edges: dict[str, list[tuple[str, str]]] = {}
        for r in self._db.execute("SELECT source, target, relation FROM edges WHERE relation IN ('language', 'edition')"):
            self._edges.setdefault(r["source"], []).append((r["target"], r["relation"]))

    def close(self):
        self._db.close()

    def iter_documents(self, only_ids: set[str] | None = None) -> Iterator[ArchivedDocument]:
        for folder in sorted((self.root / "documents").iterdir()):
            if not folder.is_dir():
                continue
            doc = ArchivedDocument(folder)
            if only_ids is not None and doc.external_id not in only_ids:
                continue
            yield doc

    def iter_source_statistics(self) -> Iterator[dict]:
        """lex.uz's own statistics page as captured by `lex_crawler stats`, oldest first. `latest.json` is only a
        copy of the newest timestamped file, so it is skipped."""
        folder = self.root / "source_stats"
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            if path.name == "latest.json":
                continue
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:  # one damaged capture must not hide the newer ones
                log.warning("Unreadable statistics file %s skipped: %s", path.name, exc)

    def count_documents(self) -> int:
        return sum(1 for f in (self.root / "documents").iterdir() if (f / "document.json").exists())

    def resource_dir(self, url: str, *, include_failed: bool = False) -> Path | None:
        """Folder of a *completed* resource task, or None. `include_failed` also accepts a task whose download
        succeeded but a later local step failed (the file is on disk); callers must verify the bytes themselves."""
        task = self._tasks.get(url)
        if task and (task["status"] == "done" or (include_failed and task["status"] == "failed")):
            path = (self.root / "resources" / str(int(task["id"]))).resolve()
            if self.root in path.parents and path.exists():
                return path
        return None

    def edges(self, url: str) -> list[tuple[str, str]]:
        return self._edges.get(url, [])

    def blob_path(self, relative: str) -> Path:
        # archives written on Windows record 'blobs\ab\<sha>.gz'; a Linux server must read those too
        path = (self.root / relative.replace("\\", "/")).resolve()
        if self.root not in path.parents:
            raise ArchiveError(f"Blob path escapes the archive: {relative}")
        return path

    def fingerprint(self, record: dict) -> str:
        """Cheap "did anything about this document change?" key: the page bytes, every metadata card,
        the primary PDF, the language/edition links, and our own ingest-logic version. Equal fingerprint =>
        re-parsing would give the same result, so a daily run can skip the document without reading it."""
        external_id = str(record.get("document_id") or "")
        parts = [INGEST_LOGIC_VERSION, record.get("source_sha256", ""), record.get("representation", "")]
        for kind, pattern in CARD_URLS.items():
            task = self._tasks.get(pattern.format(id=external_id))
            parts.append((kind, task["status"], task["source_hash"]) if task else (kind, None, None))
        pdf_urls = record.get("primary_pdf_urls") or [f"https://lex.uz/pdffile/{external_id}"]
        for url in pdf_urls:
            task = self._tasks.get(url)
            parts.append(("pdf", url, task["status"], task["source_hash"]) if task else ("pdf", url, None, None))
        for url in record.get("assets") or []:  # e.g. /files/<n>.zip holding the act's PDF
            task = self._tasks.get(url)
            parts.append(("asset", url, task["status"], task["source_hash"]) if task else ("asset", url, None, None))
        parts.append(sorted(self.edges(record.get("url", ""))))
        return sha256_hex(json.dumps(parts, sort_keys=True, default=str))
