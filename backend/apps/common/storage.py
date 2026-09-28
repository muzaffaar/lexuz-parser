"""Object storage for raw source snapshots (TZ 56, 98).

Postgres keeps only metadata + object key. Keys are content-addressed
(`raw/<sha[:2]>/<sha>`), so uploading the same bytes twice is a no-op and a
snapshot can never be silently replaced by different bytes.
"""
import os
from pathlib import Path
from typing import Protocol

from django.conf import settings


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "", content_encoding: str = "") -> bool:
        """Store data under key. Returns True if newly written, False if it already existed."""

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


def validate_key(key: str) -> None:
    """Keys are content-addressed paths we generate; anything else (traversal, absolute, drive) is a bug."""
    if not key or key.startswith(("/", "\\")) or ".." in Path(key).parts or ":" in key:
        raise ValueError(f"Unsafe object key: {key!r}")


class FilesystemObjectStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError(f"Object key escapes the store root: {key!r}")
        return path

    def put(self, key, data, *, content_type="", content_encoding=""):
        path = self._path(key)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        return True

    def get(self, key):
        return self._path(key).read_bytes()

    def exists(self, key):
        return self._path(key).exists()


def snapshot_key(sha256: str) -> str:
    return f"raw/{sha256[:2]}/{sha256}"


def get_object_store() -> ObjectStore:
    conf = settings.OBJECT_STORE
    if conf["BACKEND"] == "filesystem":
        return FilesystemObjectStore(conf["ROOT"])
    if conf["BACKEND"] == "s3":
        from .s3_store import S3ObjectStore

        return S3ObjectStore(
            conf["BUCKET"], endpoint_url=conf.get("ENDPOINT_URL"), region=conf.get("REGION"),
            access_key=conf.get("ACCESS_KEY"), secret_key=conf.get("SECRET_KEY"), prefix=conf.get("PREFIX", ""),
        )
    raise NotImplementedError(f"Object store backend {conf['BACKEND']!r} is not configured")
