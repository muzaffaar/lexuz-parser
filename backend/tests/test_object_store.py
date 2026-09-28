"""Object-store contract, run against BOTH implementations. The S3 side talks real HTTP to a local
moto S3 server through boto3, exactly as it would to MinIO/AWS."""
import tempfile
import unittest
from unittest import TestCase

from django.test import TestCase as DjangoTestCase

from apps.common.hashing import sha256_hex
from apps.common.storage import FilesystemObjectStore, snapshot_key

try:
    import boto3
    from moto.server import ThreadedMotoServer

    HAVE_S3_TEST_DEPS = True
except ImportError:  # pragma: no cover
    HAVE_S3_TEST_DEPS = False


class StoreContract:
    """Mixin: subclasses provide self.store."""

    def test_put_get_exists_roundtrip(self):
        key = snapshot_key(sha256_hex("abc"))
        self.assertFalse(self.store.exists(key))
        self.assertTrue(self.store.put(key, b"payload", content_type="text/html", content_encoding="gzip"))
        self.assertTrue(self.store.exists(key))
        self.assertEqual(self.store.get(key), b"payload")

    def test_second_put_of_the_same_key_never_overwrites(self):
        key = snapshot_key(sha256_hex("k"))
        self.assertTrue(self.store.put(key, b"first"))
        self.assertFalse(self.store.put(key, b"second"))
        self.assertEqual(self.store.get(key), b"first")

    def test_unsafe_keys_are_rejected(self):
        for bad in ("../escape", "/abs", "a/../../b", "C:/x", "", "..\\x"):
            with self.assertRaises(ValueError, msg=bad):
                self.store.put(bad, b"x")

    def test_missing_key(self):
        self.assertFalse(self.store.exists("raw/zz/nothing"))
        with self.assertRaises(Exception):
            self.store.get("raw/zz/nothing")


class FilesystemStoreTests(StoreContract, TestCase):
    def setUp(self):
        self.store = FilesystemObjectStore(tempfile.mkdtemp())


@unittest.skipUnless(HAVE_S3_TEST_DEPS, "boto3/moto not installed")
class S3StoreTests(StoreContract, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        cls.server.start()
        host, port = cls.server.get_host_and_port()
        cls.endpoint = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        super().tearDownClass()

    def setUp(self):
        from apps.common.s3_store import S3ObjectStore

        self.bucket = f"snap-{sha256_hex(str(id(self)))[:12]}"
        boto3.client("s3", endpoint_url=self.endpoint, region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s").create_bucket(Bucket=self.bucket)
        self.store = S3ObjectStore(self.bucket, endpoint_url=self.endpoint, region="us-east-1", access_key="k", secret_key="s", prefix="yurist")

    def test_prefix_is_applied_and_content_metadata_kept(self):
        key = snapshot_key(sha256_hex("meta"))
        self.store.put(key, b"zz", content_type="text/html", content_encoding="gzip")
        head = self.store.client.head_object(Bucket=self.bucket, Key=f"yurist/{key}")
        self.assertEqual((head["ContentType"], head["ContentEncoding"]), ("text/html", "gzip"))


@unittest.skipUnless(HAVE_S3_TEST_DEPS, "boto3/moto not installed")
class IngestWithS3Tests(DjangoTestCase):
    def test_ingestion_stores_snapshots_in_s3(self):
        from apps.common.s3_store import S3ObjectStore
        from apps.legal_sources.models import SourceSnapshot
        from apps.parsers.ingestion.loader import IngestService

        from .factories import make_source, parsed

        server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        server.start()
        try:
            host, port = server.get_host_and_port()
            endpoint = f"http://{host}:{port}"
            boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s").create_bucket(Bucket="raw")
            store = S3ObjectStore("raw", endpoint_url=endpoint, region="us-east-1", access_key="k", secret_key="s")
            result = IngestService(make_source(), store).ingest(parsed())
            snap = SourceSnapshot.objects.get()
            self.assertTrue(store.exists(snap.object_key))
            self.assertEqual(len(store.get(snap.object_key)) > 0, True)
            self.assertEqual(result.version.raw_snapshot_id, snap.id)
        finally:
            server.stop()
