"""Run one ingestion job over a crawler archive (TZ 54, 57-59)."""
import logging
import time
import traceback
from datetime import timedelta

from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.utils import timezone

from apps.common.storage import ObjectStore, get_object_store
from apps.legal_documents.models import LegalDocument
from apps.legal_documents.services.groups import link_language_groups, resolve_relation_targets
from apps.legal_documents.services.versions import VersionConflict
from apps.legal_sources.models import LegalSource
from apps.parsers.models import ItemStatus, JobStatus, ParserError, ParserItem, ParserJob

from .archive import CrawlArchive
from .builder import build_parsed_document
from .loader import IngestService

log = logging.getLogger(__name__)
RETRY_DELAYS = (0.5, 1.0, 2.0)  # TZ 59: at least 3 retries with exponential backoff
ABANDONED_AFTER = timedelta(hours=12)
# Unique-constraint races between two workers ingesting the same new document / snapshot: the loser simply
# retries and then sees the winner's row.
_RACE_CONSTRAINTS = ("uniq_document_source_external", "uniq_snapshot_source_sha256")


def get_or_create_lexuz() -> LegalSource:
    source, _ = LegalSource.objects.get_or_create(
        code="lexuz", defaults={"name": "Lex.uz National Legal Database", "base_url": "https://lex.uz/", "is_official": True}
    )
    return source


def _reap_abandoned_jobs(source: LegalSource) -> None:
    """A killed process leaves its job 'running' forever; mark stale ones failed so dashboards stay truthful."""
    stale = ParserJob.objects.filter(source=source, status=JobStatus.RUNNING, started_at__lt=timezone.now() - ABANDONED_AFTER)
    stale.update(status=JobStatus.FAILED, finished_at=timezone.now(), error_summary="abandoned: process died before finishing")


def run_ingest(
    archive_root,
    *,
    source: LegalSource | None = None,
    store: ObjectStore | None = None,
    trigger: str = "manual",
    limit: int | None = None,
    only_ids: set[str] | None = None,
    retry_delays=RETRY_DELAYS,
    progress=None,
) -> ParserJob:
    source = source or get_or_create_lexuz()
    store = store or get_object_store()
    archive = CrawlArchive(archive_root)
    _reap_abandoned_jobs(source)
    job = ParserJob.objects.create(
        source=source, trigger=trigger, params={"archive": str(archive.root), "limit": limit, "only_ids": sorted(only_ids or [])}
    )
    service = IngestService(source, store, job)
    started = time.monotonic()
    counts = dict.fromkeys(ItemStatus.values, 0)
    total = 0
    # "Has anything about this document changed since we last ingested it?" (TZ 54: no full daily reload)
    known = dict(LegalDocument.objects.filter(source=source).exclude(observation_fingerprint="").values_list("external_id", "observation_fingerprint"))
    try:
        for archived in archive.iter_documents(only_ids):
            if limit is not None and total >= limit:
                break
            total += 1
            status, item_kwargs = _process_one(service, archive, archived, job, source, retry_delays, known)
            counts[status] += 1
            # A daily run over ~100k documents would otherwise write ~100k "unchanged" rows a day. Unchanged
            # documents are counted on the job; only events (new/updated/skipped/failed) get item rows.
            if status != ItemStatus.UNCHANGED:
                url = item_kwargs.pop("url", archived.folder_key)
                item = ParserItem.objects.create(job=job, url=url, external_id=archived.external_id, status=status, **item_kwargs)
                if status == ItemStatus.FAILED:
                    ParserError.objects.filter(job=job, item__isnull=True, url=url).update(item=item)
            if progress:
                progress(total, status, archived.folder_key)
        with transaction.atomic():
            resolve_relation_targets(source)
            link_language_groups(source)
        _load_statistics(archive, source)
    except Exception:
        job.status = JobStatus.FAILED
        job.error_summary = traceback.format_exc()[-4000:]
        raise
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS if counts[ItemStatus.FAILED] else JobStatus.COMPLETED
        if service.unmapped_types:
            job.error_summary = "Unmapped pairs need a DocumentTypeRank row: " + "; ".join(
                f"{k}:{t!r}/{f!r}" for k, t, f in sorted(service.unmapped_types)
            )
    finally:
        archive.close()
        job.finished_at = timezone.now()
        job.duration = job.finished_at - job.started_at
        job.total_count = total
        job.new_count, job.updated_count = counts[ItemStatus.NEW], counts[ItemStatus.UPDATED]
        job.unchanged_count, job.skipped_count, job.failed_count = counts[ItemStatus.UNCHANGED], counts[ItemStatus.SKIPPED], counts[ItemStatus.FAILED]
        job.save()
    return job


def _load_statistics(archive, source) -> None:
    """Pick up lex.uz's own statistics captured by the crawler. A problem here must never fail a document run."""
    from apps.statistics.loader import load_source_statistics

    try:
        for snapshot in load_source_statistics(archive, source):
            log.info("Loaded lex.uz statistics %s", snapshot)
    except Exception:  # noqa: BLE001 - statistics are informational
        log.exception("Could not load lex.uz statistics")


def _is_race(exc: Exception) -> bool:
    return isinstance(exc, IntegrityError) and any(name in str(exc) for name in _RACE_CONSTRAINTS)


def _process_one(service, archive, archived, job, source, retry_delays, known=None):
    """One document, isolated: any failure is recorded and the job carries on (TZ 58)."""
    attempt = 0
    while True:
        try:
            record = archived.record  # parsed here, inside the per-item guard
            fingerprint = archive.fingerprint(record) if archive is not None else ""
            url = record.get("url", "")
            if known is not None and fingerprint and "ONDATE" not in url and known.get(str(record.get("document_id"))) == fingerprint:
                return ItemStatus.UNCHANGED, {}
            parsed = build_parsed_document(archived, archive)
            parsed.fingerprint = fingerprint
            result = service.ingest(parsed)
            return result.status, {
                "url": url, "document": result.document, "version": result.version, "snapshot": result.snapshot,
                "content_hash": parsed.content_hash, "detail": result.detail or "; ".join(parsed.warnings)[:1000],
            }
        except (OperationalError, InterfaceError) as exc:  # transient DB trouble (deadlock, dropped connection)
            if attempt < len(retry_delays):
                log.warning("Transient DB error on %s (attempt %d): %s", archived.folder_key, attempt + 1, exc)
                time.sleep(retry_delays[attempt])
                attempt += 1
                continue
            return _fail(job, source, archived, exc, attempt)
        except IntegrityError as exc:
            if _is_race(exc) and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                attempt += 1
                continue
            return _fail(job, source, archived, exc, attempt)
        except VersionConflict as exc:
            return _fail(job, source, archived, exc, attempt)
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the job
            return _fail(job, source, archived, exc, attempt)


def _fail(job, source, archived, exc, retries):
    try:
        url = archived.record.get("url", "") or archived.folder_key
    except Exception:  # noqa: BLE001 - the record itself may be the corrupt thing
        url = archived.folder_key
    message = f"{type(exc).__name__}: {exc}"
    ParserError.objects.create(
        job=job, source=source, url=url, exception=message[:4000], traceback=traceback.format_exc()[-6000:], retry_count=retries
    )
    return ItemStatus.FAILED, {"url": url, "detail": message[:1000]}
