"""Ops settings: production rules + the read-only Django admin, served by gunicorn behind an optional TLS proxy.

The TZ forbids Django for the *product* UI; the admin is an internal data browser, so it lives here and is loaded
only by the `admin` container. Every non-admin process (migrate, ingest, crawler-adjacent jobs) uses
config.settings.production and never installs the admin.
"""
from .admin_ui import ADMIN_INSTALLED_APPS, ADMIN_MIDDLEWARE, ADMIN_TEMPLATES
from .production import *  # noqa: F401,F403
from .production import BASE_DIR, INSTALLED_APPS, env  # noqa: F401

INSTALLED_APPS = [*ADMIN_INSTALLED_APPS, *INSTALLED_APPS]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves the admin's static files without a separate web server
    *ADMIN_MIDDLEWARE,
]
TEMPLATES = ADMIN_TEMPLATES

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Behind Caddy (TLS terminates there): trust its forwarded protocol and only send cookies over HTTPS.
if env("DJANGO_HTTPS", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 60 * 60 * 12
X_FRAME_OPTIONS = "SAMEORIGIN"  # the admin embeds the act's PDF in an iframe from its own origin
