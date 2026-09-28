from django.core.management.base import BaseCommand, CommandError

from apps.parsers.ingestion.archive import ArchiveError, CrawlArchive
from apps.parsers.ingestion.runner import get_or_create_lexuz
from apps.statistics.comparison import compare, latest_snapshot
from apps.statistics.loader import load_source_statistics


class Command(BaseCommand):
    help = "Print lex.uz's own statistics next to what our database holds (optionally importing new snapshots first)."

    def add_arguments(self, parser):
        parser.add_argument("archive", nargs="?", help="Crawler data directory: import its captured statistics first")

    def handle(self, archive=None, **options):
        source = get_or_create_lexuz()
        if archive:
            try:
                reader = CrawlArchive(archive)
            except ArchiveError as exc:
                raise CommandError(str(exc)) from exc
            try:
                for snapshot in load_source_statistics(reader, source):
                    self.stdout.write(f"Imported {snapshot}")
            except ArchiveError as exc:
                raise CommandError(str(exc)) from exc
            finally:
                reader.close()
        snapshot = latest_snapshot(source)
        if snapshot is None:
            raise CommandError("No statistics loaded. Run `python -m lex_crawler stats` first, then pass the data directory.")
        result = compare(snapshot)
        self.stdout.write(f"\nlex.uz statistics of {snapshot.captured_at:%Y-%m-%d %H:%M} UTC vs. our database now")
        if not snapshot.consistent:
            self.stdout.write(self.style.WARNING("WARNING: the page did not add up when read: " + "; ".join(snapshot.problems)))
        for section in result.sections:
            self.stdout.write(f"\n{section.title}")
            self.stdout.write(f"  {'':52s} {'lex.uz':>9s} {'ours':>9s} {'missing':>9s} {'cover':>7s}")
            for row in section.rows:
                fmt = lambda v: "-" if v is None else f"{v:,}"  # noqa: E731
                cover = "-" if row.coverage is None else f"{row.coverage}%"
                self.stdout.write(f"  {row.label[:52]:52s} {fmt(row.site):>9s} {fmt(row.ours):>9s} {fmt(row.missing):>9s} {cover:>7s}")
