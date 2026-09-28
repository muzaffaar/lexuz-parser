"""S3-compatible object store (AWS S3, MinIO, Ceph...) for raw source snapshots (TZ 56, 98, 151).

Keys are content addressed, so an existing key already holds exactly these bytes. Uploads use a
conditional write (`If-None-Match: *`) so two workers racing on the same snapshot can never overwrite one
another; production buckets should additionally enable versioning / object lock.
"""
from botocore.exceptions import ClientError

from .storage import validate_key


class S3ObjectStore:
    def __init__(self, bucket: str, *, client=None, endpoint_url=None, region=None, access_key=None, secret_key=None, prefix: str = ""):
        if client is None:
            import boto3

            client = boto3.client(
                "s3", endpoint_url=endpoint_url or None, region_name=region or None,
                aws_access_key_id=access_key or None, aws_secret_access_key=secret_key or None,
            )
        self.client, self.bucket, self.prefix = client, bucket, prefix.strip("/")

    def _key(self, key: str) -> str:
        validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def put(self, key: str, data: bytes, *, content_type: str = "", content_encoding: str = "") -> bool:
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        if content_encoding:
            extra["ContentEncoding"] = content_encoding
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, IfNoneMatch="*", **extra)
            return True
        except ClientError as exc:
            # 412: somebody stored it first. Content-addressed => identical bytes, nothing to do.
            if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "412") or \
                    exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412:
                return False
            raise

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
