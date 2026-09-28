from django.conf import settings
from django.urls import path

urlpatterns = []  # API layer (api/v1) is a later milestone; this repo slice is the data platform.

if "django.contrib.admin" in settings.INSTALLED_APPS:  # development/test only, see settings/admin_ui.py
    from django.contrib import admin

    admin.site.site_header = "Yurist AI - legal data (read-only)"
    admin.site.site_title = "Yurist AI"
    admin.site.index_title = "Legal database"
    urlpatterns += [path("admin/", admin.site.urls)]
