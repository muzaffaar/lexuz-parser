from django.core.management.base import BaseCommand, CommandError

from apps.parsers.ingestion.archive import ArchiveError
from apps.parsers.ingestion.runner import run_ingest


class Command(BaseCommand):
    help = "Ingest a lex-crawler archive into PostgreSQL (runs in the private zone; never touches the Internet)."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="Path to the crawler data directory (contains state.sqlite3)")
        parser.add_argument("--limit", type=int, help="Process at most N documents")
        parser.add_argument("--only", nargs="+", help="Only these lex.uz document ids (signed, e.g. -8488547)")

    def handle(self, archive, limit=None, only=None, **options):
        def progress(n, status, url):
            if n % 25 == 0 or status == "failed":
                self.stdout.write(f"  [{n}] {status:9s} {url}")

        try:
            job = run_ingest(archive, limit=limit, only_ids=set(only) if only else None, progress=progress)
        except ArchiveError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Job {job.id} {job.status}: total={job.total_count} new={job.new_count} updated={job.updated_count} "
                f"unchanged={job.unchanged_count} failed={job.failed_count} in {job.duration}"
            )
        )
        if job.error_summary:
            self.stdout.write(self.style.WARNING(job.error_summary))
        if job.failed_count:
            raise CommandError(f"{job.failed_count} document(s) failed; see parsers_parsererror for job {job.id}")
