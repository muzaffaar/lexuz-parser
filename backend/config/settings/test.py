import tempfile

from .admin_ui import ADMIN_INSTALLED_APPS, ADMIN_MIDDLEWARE, ADMIN_TEMPLATES
from .base import *  # noqa: F401,F403

OBJECT_STORE = {"BACKEND": "filesystem", "ROOT": tempfile.mkdtemp(prefix="yurist-objstore-")}

# the admin is exercised by tests/test_admin.py
INSTALLED_APPS = [*ADMIN_INSTALLED_APPS, *INSTALLED_APPS]  # noqa: F405
MIDDLEWARE = ADMIN_MIDDLEWARE
TEMPLATES = ADMIN_TEMPLATES
STATIC_URL = "static/"
ALLOWED_HOSTS = ["testserver", "localhost"]
