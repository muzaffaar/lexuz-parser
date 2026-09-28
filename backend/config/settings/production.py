from .base import *  # noqa: F401,F403

DEBUG = False  # TZ 161

# Fail fast on unsafe defaults instead of starting a "production" that would violate the spec.
if SECRET_KEY == "insecure-dev-key-change-me" or len(SECRET_KEY) < 32:  # noqa: F405
    raise RuntimeError("DJANGO_SECRET_KEY must be set to a long random value in production (TZ 139)")
_db = DATABASES["default"]  # noqa: F405
if _db["USER"] in ("postgres", "root", "") or _db["PASSWORD"] in ("postgres", ""):
    raise RuntimeError(
        "Production must connect as a least-privilege member of yurist_api/yurist_parser, never as a superuser "
        "with the default password (TZ 140); a superuser also bypasses Row Level Security entirely."
    )
if OBJECT_STORE["BACKEND"] == "filesystem":  # noqa: F405
    raise RuntimeError("Production raw snapshots belong in S3-compatible storage (TZ 98): set OBJECT_STORE_BACKEND=s3")
