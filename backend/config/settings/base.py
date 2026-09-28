"""Base settings. Everything environment-specific comes from env vars (TZ 139)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = False
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.postgres",
    "apps.common",
    "apps.organizations",
    "apps.legal_sources",
    "apps.legal_documents",
    "apps.legal_monitoring",
    "apps.parsers",
    "apps.search",
    "apps.embeddings",
    "apps.statistics",
]

ROOT_URLCONF = "config.urls"
USE_TZ = True          # TZ 23: all server times are UTC
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "yurist"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", "postgres"),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "55432"),
        "CONN_MAX_AGE": 0,
    }
}

# Raw legal sources live in S3-compatible storage, never in Postgres (TZ 98).
# "filesystem" is for development and tests; production sets an S3 backend.
OBJECT_STORE = {
    "BACKEND": env("OBJECT_STORE_BACKEND", "filesystem"),  # "filesystem" | "s3"
    "ROOT": env("OBJECT_STORE_ROOT", str(BASE_DIR / ".object-store")),
    # s3 backend (AWS S3 / MinIO / Ceph); credentials come from the environment, never from Git (TZ 139)
    "BUCKET": env("OBJECT_STORE_BUCKET", ""),
    "ENDPOINT_URL": env("OBJECT_STORE_ENDPOINT_URL", ""),
    "REGION": env("OBJECT_STORE_REGION", ""),
    "ACCESS_KEY": env("OBJECT_STORE_ACCESS_KEY", ""),
    "SECRET_KEY": env("OBJECT_STORE_SECRET_KEY", ""),
    "PREFIX": env("OBJECT_STORE_PREFIX", ""),
}

# Ingestion / chunking knobs. Token counts are *estimates* until the embedding
# model's tokenizer is chosen (TZ 65), so they are configuration, not constants.
YURIST = {
    "CHUNK_MAX_TOKENS": int(env("CHUNK_MAX_TOKENS", "450")),
    "CHUNK_MIN_TOKENS": int(env("CHUNK_MIN_TOKENS", "60")),
    "PDF_MIN_CHARS_PER_PAGE": int(env("PDF_MIN_CHARS_PER_PAGE", "200")),
}
