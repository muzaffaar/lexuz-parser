from django.core.management.base import BaseCommand, CommandError

from apps.legal_documents.models import LegalDocumentVersion
from apps.search.services import rechunk_version, versions_needing_rechunk


class Command(BaseCommand):
    help = "Rebuild chunks with the current chunker (stale/missing by default). Sections are never modified."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Rechunk every version, not only stale ones")
        parser.add_argument("--version-id", help="Rechunk a single version")
        parser.add_argument("--yes", action="store_true", help="Confirm --all: it deletes every existing embedding")

    def handle(self, all=False, version_id=None, yes=False, **options):
        if all and not yes:
            raise CommandError("--all cascade-deletes ALL embeddings (they must be regenerated). Re-run with --yes to confirm.")
        if version_id:
            qs = LegalDocumentVersion.objects.filter(pk=version_id)
        elif all:
            qs = LegalDocumentVersion.objects.all()
        else:
            qs = versions_needing_rechunk()
        done = chunks = 0
        for version in qs.iterator(chunk_size=200):
            chunks += rechunk_version(version)
            done += 1
        self.stdout.write(self.style.SUCCESS(f"Rechunked {done} version(s) -> {chunks} chunk(s). Regenerate embeddings for them."))
