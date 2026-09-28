"""An archive crawled on Windows must ingest on a Linux server (and vice versa)."""
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from apps.parsers.ingestion.archive import ArchiveError, CrawlArchive


def make_root(tmp):
    root = Path(tmp)
    (root / "documents").mkdir()
    (root / "blobs" / "ab").mkdir(parents=True)
    (root / "blobs" / "ab" / "abcdef.gz").write_bytes(b"x")
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, url TEXT, kind TEXT, status TEXT, source_hash TEXT);"
        "CREATE TABLE edges(source TEXT, target TEXT, relation TEXT);"
    )
    db.close()
    return root


class BlobPathTests(TestCase):
    def test_backslash_paths_written_by_a_windows_crawler_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = CrawlArchive(make_root(tmp))
            expected = archive.root / "blobs" / "ab" / "abcdef.gz"
            self.assertEqual(archive.blob_path("blobs\\ab\\abcdef.gz"), expected)
            self.assertEqual(archive.blob_path("blobs/ab/abcdef.gz"), expected)
            archive.close()

    def test_escaping_the_archive_is_still_refused_in_either_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = CrawlArchive(make_root(tmp))
            for bad in ("..\\outside.gz", "../outside.gz", "blobs\\..\\..\\outside.gz"):
                with self.assertRaises(ArchiveError):
                    archive.blob_path(bad)
            archive.close()
