"""Store lex.uz's own statistics (captured by the crawler's `stats` command) so the archive can be measured
against them. Runs in the private zone from files only; nothing here touches the Internet."""
import logging
from datetime import datetime

from django.db import IntegrityError, transaction

from .models import SourceStatisticsSnapshot

log = logging.getLogger(__name__)
_REQUIRED = ("captured_at", "source_sha256", "total_documents", "grand_total", "categories", "rows")


def load_source_statistics(archive, source) -> list[SourceStatisticsSnapshot]:
    """Import every captured statistics file not yet stored. Idempotent; returns the newly stored snapshots.
    A malformed file is logged and skipped so it can never stop a document ingestion."""
    created = []
    for data in archive.iter_source_statistics():
        missing = [k for k in _REQUIRED if k not in data]
        if missing:
            log.warning("Statistics file skipped, missing %s", missing)
            continue
        try:
            captured_at = datetime.fromisoformat(data["captured_at"])
            with transaction.atomic():
                snapshot, was_new = SourceStatisticsSnapshot.objects.get_or_create(
                    source=source,
                    captured_at=captured_at,
                    defaults={
                        "source_sha256": data["source_sha256"],
                        "total_documents": data["total_documents"],
                        "header_total": data.get("header_total"),
                        "consistent": bool(data.get("consistent")),
                        "problems": data.get("problems") or [],
                        "payload": {k: data[k] for k in ("grand_total", "categories", "rows")},
                    },
                )
        except (ValueError, TypeError, IntegrityError) as exc:
            log.warning("Statistics file skipped: %s", exc)
            continue
        if was_new:
            created.append(snapshot)
    return created
