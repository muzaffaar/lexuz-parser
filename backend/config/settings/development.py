from .admin_ui import ADMIN_INSTALLED_APPS, ADMIN_MIDDLEWARE, ADMIN_TEMPLATES
from .base import *  # noqa: F401,F403

DEBUG = True

# read-only data browser at /admin/ (development only)
INSTALLED_APPS = [*ADMIN_INSTALLED_APPS, *INSTALLED_APPS]  # noqa: F405
MIDDLEWARE = ADMIN_MIDDLEWARE
TEMPLATES = ADMIN_TEMPLATES
STATIC_URL = "static/"
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
